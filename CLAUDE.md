# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Layout

```
src/rtcheck/{__init__,cli,config,graph,analyse,report}.py
tests/test_rtcheck.py
examples/audio/{mixer.c,plugins.c}
```

`pyproject.toml` drives this: `packages.find where = ["src"]`, entry point `rtcheck = "rtcheck.cli:main"`, and for pytest `pythonpath = ["src"]` with `testpaths = ["tests"]`. The modules use package-relative imports, the tests import `from rtcheck import ...`, and `test_rtcheck.py` resolves the example C via `Path(__file__).parent.parent / "examples" / "audio"` — so the three directories are load-bearing, not cosmetic.

## Commands

```sh
pip install -e ".[dev]"     # needs libclang>=16 (wheel bundles libclang.so) and pytest
pytest                      # full suite
pytest tests/test_rtcheck.py::test_shortest_path_is_chosen   # single test
rtcheck examples/audio --entry mix_frame                     # manual smoke run
python -m rtcheck.cli examples/audio --entry mix_frame       # same, without installing the script
```

Config discovery: `cli._find_config` looks for `rtcheck.toml` upwards from the CWD and then from
the source paths, so the tool works from wherever it is invoked.

Debugging a bad parse: `--show-parse-errors` prints the individual clang diagnostics; their *count*
is always reported as a `parse` warning. A degraded parse silently produces an under-populated call graph and therefore a falsely clean report, so check it first whenever findings look too good.

## Architecture

A four-stage pipeline, one module per stage, wired together only in `cli.py`:

**`config.py` — policy.** `DEFAULT_EFFECTS` is the built-in table of forbidden leaf symbols grouped into categories (`alloc`, `lock`, `io`, `syscall`, `block`, `terminate`), each with a human message used verbatim in the report ("must not *allocate or free memory*"). `load()` layers a user `rtcheck.toml` over these: `[forbidden.X]` unions symbols into category X (or replaces them with `replace = true`, or creates a new category). `Config.symbol_to_effect` flattens the table to `symbol -> category`, applying `enabled` filtering and the allowlist. Everything downstream matches on **bare function name**, not USR — the config knows nothing about clang.

**`graph.py` — parse.** The AST walk in `_Builder.visit` is iteratively stacked, not recursive: a
few thousand nested binary operators is enough to exhaust the interpreter stack, and a crashed
parse looks the same to the caller as a clean one. libclang builds a `CallGraph` of `Node`s keyed by **USR**, so two `static` functions sharing a name stay distinct while one external resolves to a single node project-wide. `_Builder.visit` walks cursors tracking the enclosing function; a `CALL_EXPR` whose `referenced` is a `FUNCTION_DECL` becomes a direct edge, anything else becomes an `indirect_site`. A `DECL_REF_EXPR` to a function marks `address_taken` — but only outside callee position, which is tracked by propagating `in_callee_pos` down the leftmost child spine so it survives clang's `UNEXPOSED_EXPR` wrappers. Getting that wrong makes every ordinary call look like an address-taken function; `test_direct_call_does_not_count_as_address_taken` guards it. `compile_commands_sources` filters build flags through `_relevant_args`, which keeps two-token
options (`-I inc`, `-isystem path`) intact and rewrites every path to absolute against the entry's
`directory`. Both halves matter: dropping an operand leaves the flag to swallow the *next* flag, and
clang is never run from `directory`, so a relative `-I` would resolve against the wrong place. Either
way the parse degrades into a falsely clean report. `graph.build` then applies each file's flags to
that file alone — flattening a compile database into one shared argument list lets a `-D` from one
target silently change how another parses. `_configure_library()` and `builtin_include_dirs()` exist because the libclang wheel ships the shared library but not clang's resource headers — without the injected `-isystem`, every `#include <stdlib.h>` fails.

**`analyse.py` — search.** BFS from each entry point, deliberately *not* DFS: the shortest path is the most readable one, and `test_shortest_path_is_chosen` locks that in. Traversal rules that matter: an allowlisted function is skipped entirely (its subtree is never walked); a forbidden symbol is a **terminus**, recorded and not walked past; only the first violation per symbol per entry point is reported; a node with no body and no forbidden match is an *opaque leaf*, accumulated into one summary `opaque` finding rather than reported individually. Recursion is found by `_find_cycles`, an iterative DFS for back edges anywhere in the reachable
subgraph — not just cycles through the entry point, since unbounded stack growth in a helper is
equally fatal. It deliberately follows **real call edges only**, never the address-taken fan-out:
that fan-out is a guess, and a cycle existing only inside the guess is not evidence of recursion.
Wiring it back through `_successors` produced 6,014 phantom findings on a 96k-line file. Indirect calls behave per `cfg.indirect`: `warn` emits a blind-spot finding, `address-taken` fans every indirect site out to all address-taken defined nodes, `ignore` drops them. `Finding.key` deduplicates warnings; `_reconstruct` walks the BFS `prev` map, attributing each step the *call site* line rather than the callee's declaration line.

**`report.py` — output.** `render_text` splits findings into errors (`kind == "effect"`) and warnings (everything else); colour is applied only when both requested and `stdout.isatty()`. `render_json` is the CI-facing form. `cli.py` exits 1 for any `effect`, `unresolved`, or `parse_error` finding. The two fatal kinds are deliberate: an
`unresolved` entry point means the check did not happen, and exiting 0 there would leave a renamed
function green in CI forever. `parse_error` (a file that would not parse at all) is
fatal for the same reason. `opaque` and `parse` are the deliberately *non*-fatal siblings —
respectively reachable library functions with no source, which a real project hits by the hundred,
and a count of clang diagnostics warning that the graph may be incomplete. Keep those distinctions
if you touch any of them: the fatal/non-fatal split is the whole reason the exit code means
anything.

## Conventions

- The tool's credibility rests on never claiming clean when it cannot see. Any change that could hide a blind spot (swallowing an unresolved call, dropping an indirect site, treating an unparsed file as empty) needs a compensating warning finding.
- The default effect tables are *known* leaves only. rtcheck does not guess at symbols by prefix or heuristic; new coverage means new explicit entries.
- Tests are end-to-end: they write real C to `tmp_path`, parse it with libclang, and assert on findings. Add tests in that style — inline C snippets via the `run()` helper — rather than mocking the graph.
- The README's "What it cannot do" section is a load-bearing contract, not marketing. If a change alters those limits, update it.
