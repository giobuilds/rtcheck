"""Search the call graph for paths from a real-time entry point to a forbidden call."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .config import Config
from .graph import CallGraph, Node


@dataclass
class Step:
    name: str
    file: str | None
    line: int


@dataclass
class Finding:
    kind: str  # "effect" | "indirect" | "recursion" | "unresolved"
               # | "parse_error" | "parse" | "opaque"
    entrypoint: str
    path: list[Step] = field(default_factory=list)
    effect: str | None = None
    symbol: str | None = None
    detail: str = ""

    @property
    def key(self) -> tuple:
        return (self.kind, self.entrypoint, self.symbol, tuple(s.name for s in self.path))


def _successors(node: Node, graph: CallGraph, cfg: Config,
                indirect_targets: list[Node]) -> list[tuple[str, tuple[str, int]]]:
    out = list(node.calls.items())
    if cfg.indirect == "address-taken" and node.indirect_sites:
        site = node.indirect_sites[0]
        for target in indirect_targets:
            out.append((target.usr, site))
    return out


def analyse(graph: CallGraph, cfg: Config) -> list[Finding]:
    forbidden = cfg.symbol_to_effect
    allow = set(cfg.allow)
    indirect_targets = graph.address_taken_nodes() if cfg.indirect == "address-taken" else []
    findings: list[Finding] = []
    seen_keys: set[tuple] = set()
    opaque: set[str] = set()
    entry_label = ", ".join(cfg.entrypoints)

    # A file that would not parse at all contributes nothing to the graph, so
    # anything it defined is invisible. That is a failed check, not a warning.
    for src in graph.failed_files:
        findings.append(Finding(
            kind="parse_error", entrypoint=entry_label,
            detail=f"could not parse {src} at all; nothing defined in it was checked",
        ))

    for entry_name in cfg.entrypoints:
        entries = graph.resolve(entry_name)
        if not entries:
            findings.append(Finding(
                kind="unresolved", entrypoint=entry_name,
                detail=f"no function named '{entry_name}' found in the parsed sources",
            ))
            continue

        for entry in entries:
            if not entry.defined:
                findings.append(Finding(
                    kind="unresolved", entrypoint=entry_name,
                    detail=f"'{entry_name}' is declared but no body was found; nothing to analyse",
                ))
                continue

            # BFS gives the shortest -- i.e. most readable -- path to each problem.
            prev: dict[str, tuple[str, tuple[str, int]]] = {}
            order: dict[str, int] = {entry.usr: 0}
            queue = deque([entry.usr])
            hit_symbols: set[str] = set()

            while queue:
                usr = queue.popleft()
                node = graph.nodes[usr]

                if node.name in allow:
                    continue

                effect = forbidden.get(node.name)
                if effect is not None and usr != entry.usr:
                    if node.name not in hit_symbols:
                        hit_symbols.add(node.name)
                        findings.append(Finding(
                            kind="effect", entrypoint=entry_name, effect=effect,
                            symbol=node.name,
                            path=_reconstruct(graph, prev, entry.usr, usr),
                        ))
                    continue  # a forbidden leaf is a terminus; don't walk past it

                if node.indirect_sites and cfg.indirect == "warn":
                    for site in node.indirect_sites[:1]:
                        f = Finding(
                            kind="indirect", entrypoint=entry_name,
                            path=_reconstruct(graph, prev, entry.usr, usr),
                            detail=f"call through a function pointer at {site[0]}:{site[1]} "
                                   f"-- analysis cannot see past this",
                        )
                        if f.key not in seen_keys:
                            seen_keys.add(f.key)
                            findings.append(f)

                if not node.defined and node.name not in forbidden:
                    # Opaque leaf: a library function we have no source for. These
                    # are collected and summarised rather than reported one by one,
                    # because a real project reaches hundreds of them.
                    opaque.add(node.name)
                    continue

                for callee_usr, site in _successors(node, graph, cfg, indirect_targets):
                    if callee_usr not in order:
                        order[callee_usr] = order[usr] + 1
                        prev[callee_usr] = (usr, site)
                        queue.append(callee_usr)

            if cfg.flag_recursion:
                for chain, start in _find_cycles(graph, entry, allow, forbidden):
                    f = Finding(
                        kind="recursion", entrypoint=entry_name,
                        path=_steps_for_chain(graph, chain + [start]),
                        detail=f"this path forms a cycle back to "
                               f"'{graph.nodes[start].name}'; recursion depth is not "
                               f"statically bounded",
                    )
                    if f.key not in seen_keys:
                        seen_keys.add(f.key)
                        findings.append(f)

    # Diagnostics used to be visible only under --show-parse-errors, which meant
    # a half-parsed translation unit could report a clean bill of health with
    # nothing on screen to suggest the graph was incomplete.
    degraded = len(graph.diagnostics) - len(graph.failed_files)
    if degraded > 0:
        findings.append(Finding(
            kind="parse", entrypoint=entry_label,
            detail=f"{degraded} parse error(s) in the sources; the call graph may be "
                   f"incomplete, so a clean result here is not conclusive. Re-run with "
                   f"--show-parse-errors to see them.",
        ))

    if opaque:
        names = sorted(opaque)
        shown = ", ".join(names[:8]) + (f", +{len(names) - 8} more" if len(names) > 8 else "")
        findings.append(Finding(
            kind="opaque", entrypoint=", ".join(cfg.entrypoints),
            detail=f"{len(names)} reachable function(s) have no body in the parsed sources, "
                   f"so rtcheck cannot see inside them: {shown}",
        ))

    return findings


def _find_cycles(graph: CallGraph, entry: Node,
                 allow: set[str], forbidden: dict[str, str]) -> list[tuple[list[str], str]]:
    """Find back edges anywhere in the subgraph reachable from `entry`.

    Recursion is unbounded stack growth wherever it sits, not only when the
    cycle happens to pass back through the entry point. Iterative rather than
    recursive so a deep call graph cannot exhaust the interpreter stack.

    Only *real* call edges are followed, never the address-taken fan-out. That
    fan-out is a guess -- any indirect call might reach any function whose
    address is taken -- and a cycle that exists only inside the guess is not
    evidence of recursion. On a large single translation unit the difference is
    thousands of invented findings. Over-approximating is right for asking what
    a call could reach; it is not a basis for asserting a function recurses.

    Returns (path from the entry to the closing call, function the cycle
    returns to) for each distinct cycle.
    """
    GREY, BLACK = 1, 2

    def walkable(usr: str) -> bool:
        name = graph.nodes[usr].name
        return name not in allow and name not in forbidden

    def successors(usr: str):
        return iter(list(graph.nodes[usr].calls))

    colour: dict[str, int] = {entry.usr: GREY}
    on_stack: list[str] = [entry.usr]
    stack: list[tuple[str, object]] = [(entry.usr, successors(entry.usr))]
    cycles: list[tuple[list[str], str]] = []
    seen: set[tuple[str, ...]] = set()

    while stack:
        usr, it = stack[-1]
        nxt = next(it, None)
        if nxt is None:
            colour[usr] = BLACK
            on_stack.pop()
            stack.pop()
            continue
        if not walkable(nxt) or colour.get(nxt) == BLACK:
            continue
        if colour.get(nxt) == GREY:
            # Back edge: everything from nxt to the top of the stack is a cycle.
            members = tuple(sorted(set(on_stack[on_stack.index(nxt):])))
            if members not in seen:
                seen.add(members)
                cycles.append((list(on_stack), nxt))
            continue
        colour[nxt] = GREY
        on_stack.append(nxt)
        stack.append((nxt, successors(nxt)))

    return cycles


def _steps_for_chain(graph: CallGraph, chain: list[str]) -> list[Step]:
    """Turn a chain of USRs into steps, each carrying its outgoing call site."""
    steps: list[Step] = []
    for i, usr in enumerate(chain):
        node = graph.nodes[usr]
        if i + 1 < len(chain):
            site = node.calls.get(chain[i + 1], (node.file, node.line))
            steps.append(Step(node.name, site[0], site[1]))
        else:
            steps.append(Step(node.name, node.file, node.line))
    return steps


def _reconstruct(graph: CallGraph, prev: dict, start: str, end: str) -> list[Step]:
    chain: list[str] = [end]
    while chain[-1] != start:
        parent = prev.get(chain[-1])
        if parent is None:
            break
        chain.append(parent[0])
    chain.reverse()

    steps: list[Step] = []
    for i, usr in enumerate(chain):
        node = graph.nodes[usr]
        if i + 1 < len(chain):
            site = prev.get(chain[i + 1], (None, (node.file, node.line)))[1]
            steps.append(Step(node.name, site[0], site[1]))
        else:
            steps.append(Step(node.name, node.file, node.line))
    return steps
