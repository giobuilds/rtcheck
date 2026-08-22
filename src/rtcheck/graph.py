"""Build a call graph from C source using libclang.

Identity is by USR (clang's Unified Symbol Resolution string), so a `static`
function in two different files stays two different nodes, and the same
external function referenced from twenty translation units stays one node.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from pathlib import Path

import clang.cindex as ci


def _configure_library() -> None:
    """Point the bindings at a libclang.so, preferring the bundled wheel."""
    if getattr(ci.Config, "loaded", False):
        return
    try:
        import clang.native  # type: ignore

        candidate = Path(clang.native.__file__).parent / "libclang.so"
        if candidate.exists():
            ci.Config.set_library_file(str(candidate))
            return
    except ImportError:
        pass
    for pattern in ("/usr/lib/llvm-*/lib/libclang.so*", "/usr/lib/*/libclang.so*"):
        found = sorted(glob.glob(pattern))
        if found:
            ci.Config.set_library_file(found[-1])
            return


def builtin_include_dirs() -> list[str]:
    """Find compiler builtin headers (stddef.h and friends).

    The libclang wheel ships the library but not clang's resource headers, so
    without this every `#include <stdlib.h>` fails and the parse is degraded.
    """
    dirs: list[str] = []
    for pattern in (
        "/usr/lib/llvm-*/lib/clang/*/include",
        "/usr/lib/clang/*/include",
        "/usr/lib/gcc/*/*/include",
        "/usr/local/lib/gcc/*/*/include",
    ):
        for path in sorted(glob.glob(pattern)):
            if os.path.exists(os.path.join(path, "stddef.h")):
                dirs.append(path)
    return dirs[:1]


@dataclass
class Node:
    usr: str
    name: str
    file: str | None = None
    line: int = 0
    defined: bool = False
    address_taken: bool = False
    calls: dict[str, tuple[str, int]] = field(default_factory=dict)  # callee usr -> call site
    indirect_sites: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class CallGraph:
    nodes: dict[str, Node] = field(default_factory=dict)
    by_name: dict[str, list[str]] = field(default_factory=dict)  # name -> [usr]
    diagnostics: list[str] = field(default_factory=list)
    parsed_files: int = 0

    def node(self, usr: str, name: str) -> Node:
        n = self.nodes.get(usr)
        if n is None:
            n = Node(usr=usr, name=name)
            self.nodes[usr] = n
            self.by_name.setdefault(name, []).append(usr)
        return n

    def resolve(self, name: str) -> list[Node]:
        """Find nodes by function name, preferring ones we have a body for."""
        usrs = self.by_name.get(name, [])
        defined = [self.nodes[u] for u in usrs if self.nodes[u].defined]
        return defined or [self.nodes[u] for u in usrs]

    def address_taken_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.address_taken and n.defined]


def _enclosing_function(cursor: ci.Cursor) -> ci.Cursor | None:
    cur = cursor.semantic_parent
    while cur is not None:
        if cur.kind == ci.CursorKind.FUNCTION_DECL:
            return cur
        cur = cur.semantic_parent
    return None


class _Builder:
    def __init__(self, graph: CallGraph) -> None:
        self.graph = graph

    def visit(self, cursor: ci.Cursor, current: Node | None, main_file: str,
              in_callee_pos: bool = False) -> None:
        kind = cursor.kind
        children = list(cursor.get_children())
        callee_child_index = -1

        if kind == ci.CursorKind.FUNCTION_DECL:
            usr = cursor.get_usr()
            if usr:
                node = self.graph.node(usr, cursor.spelling)
                if cursor.is_definition() and not node.defined:
                    node.defined = True
                    loc = cursor.location
                    node.file = loc.file.name if loc.file else None
                    node.line = loc.line
                current = node

        elif kind == ci.CursorKind.CALL_EXPR:
            loc = cursor.location
            site = (loc.file.name if loc.file else main_file, loc.line)
            callee = cursor.referenced
            direct = callee is not None and callee.kind == ci.CursorKind.FUNCTION_DECL
            if current is not None:
                if direct:
                    usr = callee.get_usr()
                    if usr:
                        self.graph.node(usr, callee.spelling)
                        current.calls.setdefault(usr, site)
                else:
                    # Anything we cannot resolve to a function declaration is a
                    # call through a pointer: a field, a parameter, a variable.
                    current.indirect_sites.append(site)
            # The callee sub-expression is child 0; references inside it are the
            # call itself, not the function's address being taken.
            if direct and children:
                callee_child_index = 0

        elif kind == ci.CursorKind.DECL_REF_EXPR and not in_callee_pos:
            ref = cursor.referenced
            if ref is not None and ref.kind == ci.CursorKind.FUNCTION_DECL:
                usr = ref.get_usr()
                if usr:
                    self.graph.node(usr, ref.spelling).address_taken = True

        for i, child in enumerate(children):
            # Callee position propagates down the leftmost spine so that it
            # survives the UNEXPOSED_EXPR wrappers clang inserts.
            child_callee_pos = (i == callee_child_index) or (
                in_callee_pos and i == 0 and kind != ci.CursorKind.CALL_EXPR
            )
            self.visit(child, current, main_file, child_callee_pos)


def _mark_call_targets(cursor: ci.Cursor, call_target_usrs: set[str]) -> None:
    if cursor.kind == ci.CursorKind.CALL_EXPR:
        ref = cursor.referenced
        if ref is not None and ref.kind == ci.CursorKind.FUNCTION_DECL:
            usr = ref.get_usr()
            if usr:
                call_target_usrs.add(usr)
    for child in cursor.get_children():
        _mark_call_targets(child, call_target_usrs)


def build(sources: list[str], clang_args: list[str]) -> CallGraph:
    """Parse each source file and merge the results into one graph."""
    _configure_library()
    graph = CallGraph()
    index = ci.Index.create()

    args = list(clang_args)
    for inc in builtin_include_dirs():
        args.extend(["-isystem", inc])

    builder = _Builder(graph)
    for src in sources:
        try:
            tu = index.parse(src, args=args)
        except ci.TranslationUnitLoadError as exc:
            graph.diagnostics.append(f"{src}: could not parse ({exc})")
            continue

        for diag in tu.diagnostics:
            if diag.severity >= ci.Diagnostic.Error:
                graph.diagnostics.append(f"{diag.location}: {diag.spelling}")

        graph.parsed_files += 1
        builder.visit(tu.cursor, None, src)

    return graph


def compile_commands_sources(path: Path) -> tuple[list[str], dict[str, list[str]]]:
    """Read a compile_commands.json into (sources, per-source clang args)."""
    import json
    import shlex

    entries = json.loads(Path(path).read_text())
    sources: list[str] = []
    per_file: dict[str, list[str]] = {}
    skip_next = {"-o", "-c", "-MF", "-MT", "-MQ"}

    for entry in entries:
        directory = entry.get("directory", ".")
        filename = entry["file"]
        abs_src = filename if os.path.isabs(filename) else os.path.join(directory, filename)

        raw = entry.get("arguments") or shlex.split(entry.get("command", ""))
        args: list[str] = []
        skip = True  # drop argv[0], the compiler itself
        for token in raw:
            if skip:
                skip = False
                continue
            if token in skip_next:
                skip = True
                continue
            if token == filename or token == abs_src:
                continue
            args.append(token)

        sources.append(abs_src)
        per_file[abs_src] = args

    return sources, per_file
