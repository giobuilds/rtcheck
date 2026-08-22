"""Build a call graph from C source using libclang.

Identity is by USR (clang's Unified Symbol Resolution string), so a `static`
function in two different files stays two different nodes, and the same
external function referenced from twenty translation units stays one node.
"""

from __future__ import annotations

import glob
import os
import re
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


def _libclang_major() -> int | None:
    """Major version of the libclang actually loaded, if it will tell us."""
    try:
        raw = ci.conf.lib.clang_getClangVersion()
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        match = re.search(r"version\s+(\d+)", text)
        return int(match.group(1)) if match else None
    except Exception:
        return None


def _version_of(path: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", path))


def builtin_include_dirs() -> list[str]:
    """Find compiler builtin headers (stddef.h and friends).

    The libclang wheel ships the library but not clang's resource headers, so
    without this every `#include <stdlib.h>` fails and the parse is degraded.

    Candidates are ranked by version *number*: sorting the paths as strings put
    llvm-15 ahead of llvm-9, and headers from a clang far from the loaded
    libclang's own version are a source of spurious parse errors.
    """
    clang_dirs: list[str] = []
    gcc_dirs: list[str] = []
    for pattern, bucket in (
        ("/usr/lib/llvm-*/lib/clang/*/include", clang_dirs),
        ("/usr/lib/clang/*/include", clang_dirs),
        ("/usr/lib/gcc/*/*/include", gcc_dirs),
        ("/usr/local/lib/gcc/*/*/include", gcc_dirs),
    ):
        for path in glob.glob(pattern):
            if os.path.exists(os.path.join(path, "stddef.h")):
                bucket.append(path)

    major = _libclang_major()
    if major is not None:
        exact = [d for d in clang_dirs if major in _version_of(d)]
        if exact:
            return [max(exact, key=_version_of)]
    for bucket in (clang_dirs, gcc_dirs):
        if bucket:
            return [max(bucket, key=_version_of)]
    return []


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
    failed_files: list[str] = field(default_factory=list)
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


class _Builder:
    def __init__(self, graph: CallGraph) -> None:
        self.graph = graph

    def visit(self, root: ci.Cursor, current: Node | None, main_file: str,
              in_callee_pos: bool = False) -> None:
        """Pre-order walk over the AST.

        Explicitly stacked rather than recursive: a deeply nested expression is
        enough to exhaust the interpreter stack, and a crashed parse is
        indistinguishable to the caller from a clean one.
        """
        stack: list[tuple[ci.Cursor, Node | None, bool]] = [(root, current, in_callee_pos)]
        while stack:
            cursor, current, in_callee_pos = stack.pop()
            self._visit_one(cursor, current, main_file, in_callee_pos, stack)

    def _visit_one(self, cursor: ci.Cursor, current: Node | None, main_file: str,
                   in_callee_pos: bool, stack: list) -> None:
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

        # Pushed in reverse so the children pop in source order.
        for i in range(len(children) - 1, -1, -1):
            # Callee position propagates down the leftmost spine so that it
            # survives the UNEXPOSED_EXPR wrappers clang inserts.
            child_callee_pos = (i == callee_child_index) or (
                in_callee_pos and i == 0 and kind != ci.CursorKind.CALL_EXPR
            )
            stack.append((children[i], current, child_callee_pos))


def build(sources: list[str], clang_args: list[str],
          per_file: dict[str, list[str]] | None = None) -> CallGraph:
    """Parse each source file and merge the results into one graph.

    `per_file` supplies the flags that belong to one translation unit only.
    They are applied to that file alone: flattening a compile database into one
    shared list lets a `-D` from one target silently change how another parses.
    """
    _configure_library()
    graph = CallGraph()
    index = ci.Index.create()

    args = list(clang_args)
    for inc in builtin_include_dirs():
        args.extend(["-isystem", inc])

    builder = _Builder(graph)
    for src in sources:
        try:
            tu = index.parse(src, args=args + list((per_file or {}).get(src, [])))
        except ci.TranslationUnitLoadError as exc:
            graph.diagnostics.append(f"{src}: could not parse ({exc})")
            graph.failed_files.append(src)
            continue

        for diag in tu.diagnostics:
            if diag.severity >= ci.Diagnostic.Error:
                graph.diagnostics.append(f"{diag.location}: {diag.spelling}")

        graph.parsed_files += 1
        builder.visit(tu.cursor, None, src)

    return graph


# Options that take a path. Each appears both split ("-I inc") and joined
# ("-Iinc"); both forms have to survive filtering, and the path has to be made
# absolute, because rtcheck does not run clang from the entry's `directory`.
PATH_OPTS = (
    "-idirafter", "--sysroot", "-isysroot", "-isystem", "-imacros",
    "-include", "-iquote", "-I", "-F",
)
# Options that take a separate operand that is not a path.
VALUE_OPTS = ("-D", "-U")
# Anything in here consumes the following token, so dropping the operand while
# keeping the flag would leave it to swallow whatever came next.
TWO_TOKEN_OPTS = frozenset(PATH_OPTS) | frozenset(VALUE_OPTS)
# Joined flags worth keeping that carry no separate operand.
KEEP_PREFIXES = ("-D", "-U", "-std=", "-nostdinc")


def _absolutise(value: str, directory: str) -> str:
    if not value or os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(directory, value))


def _relevant_args(raw: list[str], directory: str) -> list[str]:
    """Keep the flags that change how clang parses, in a form that survives.

    Two-token options keep their operand, and every path is resolved against
    `directory` so the result no longer depends on the working directory.
    """
    joined = sorted(PATH_OPTS, key=len, reverse=True)
    out: list[str] = []
    i = 0
    while i < len(raw):
        token = raw[i]
        if token in PATH_OPTS or token in VALUE_OPTS:
            if i + 1 < len(raw):
                operand = raw[i + 1]
                out.extend([token, _absolutise(operand, directory)
                            if token in PATH_OPTS else operand])
                i += 2
            else:
                i += 1  # trailing flag with nothing to consume: drop it
            continue
        opt = next((o for o in joined if token.startswith(o) and len(token) > len(o)), None)
        if opt is not None:
            value = token[len(opt):]
            sep, value = ("=", value[1:]) if value.startswith("=") else ("", value)
            out.append(f"{opt}{sep}{_absolutise(value, directory)}")
        elif token.startswith(KEEP_PREFIXES):
            out.append(token)
        i += 1
    return out


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
        per_file[abs_src] = _relevant_args(args, directory)

    return sources, per_file
