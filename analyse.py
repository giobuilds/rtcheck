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
                    if callee_usr == entry.usr and cfg.flag_recursion:
                        f = Finding(
                            kind="recursion", entrypoint=entry_name,
                            path=_reconstruct(graph, prev, entry.usr, usr),
                            detail="this path returns to the entry point; recursion depth "
                                   "is not statically bounded",
                        )
                        if f.key not in seen_keys:
                            seen_keys.add(f.key)
                            findings.append(f)
                    if callee_usr not in order:
                        order[callee_usr] = order[usr] + 1
                        prev[callee_usr] = (usr, site)
                        queue.append(callee_usr)

    if opaque:
        names = sorted(opaque)
        shown = ", ".join(names[:8]) + (f", +{len(names) - 8} more" if len(names) > 8 else "")
        findings.append(Finding(
            kind="unresolved", entrypoint=", ".join(cfg.entrypoints),
            detail=f"{len(names)} reachable function(s) have no body in the parsed sources, "
                   f"so rtcheck cannot see inside them: {shown}",
        ))

    return findings


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
