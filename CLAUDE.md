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

Debugging a bad parse: `--show-parse-errors` prints clang diagnostics. A degraded parse silently produces an under-populated call graph and therefore a falsely clean report, so check it first whenever findings look too good.

## Architecture

A four-stage pipeline, one module per stage, wired together only in `cli.py`:

**`config.py` — policy.** `DEFAULT_EFFECTS` is the built-in table of forbidden leaf symbols grouped into categories (`alloc`, `lock`, `io`, `syscall`, `block`, `terminate`), each with a human message used verbatim in the report ("must not *allocate or free memory*"). `load()` layers a user `rtcheck.toml` over these: `[forbidden.X]` unions symbols into category X (or replaces them with `replace = true`, or creates a new category). `Config.symbol_to_effect` flattens the table to `symbol -> category`, applying `enabled` filtering and the allowlist. Everything downstream matches on **bare function name**, not USR — the config knows nothing about clang.

**`graph.py` — parse.** libclang builds a `CallGraph` of `Node`s keyed by **USR**, so two `static` functions sharing a name stay distinct while one external resolves to a single node project-wide. `_Builder.visit` walks cursors tracking the enclosing function; a `CALL_EXPR` whose `referenced` is a `FUNCTION_DECL` becomes a direct edge, anything else becomes an `indirect_site`. A `DECL_REF_EXPR` to a function marks `address_taken` — but only outside callee position, which is tracked by propagating `in_callee_pos` down the leftmost child spine so it survives clang's `UNEXPOSED_EXPR` wrappers. Getting that wrong makes every ordinary call look like an address-taken function; `test_direct_call_does_not_count_as_address_taken` guards it. `_configure_library()` and `builtin_include_dirs()` exist because the libclang wheel ships the shared library but not clang's resource headers — without the injected `-isystem`, every `#include <stdlib.h>` fails.

**`analyse.py` — search.** BFS from each entry point, deliberately *not* DFS: the shortest path is the most readable one, and `test_shortest_path_is_chosen` locks that in. Traversal rules that matter: an allowlisted function is skipped entirely (its subtree is never walked); a forbidden symbol is a **terminus**, recorded and not walked past; only the first violation per symbol per entry point is reported; a node with no body and no forbidden match is an *opaque leaf*, accumulated into one summary `unresolved` finding rather than reported individually. Indirect calls behave per `cfg.indirect`: `warn` emits a blind-spot finding, `address-taken` fans every indirect site out to all address-taken defined nodes, `ignore` drops them. `Finding.key` deduplicates warnings; `_reconstruct` walks the BFS `prev` map, attributing each step the *call site* line rather than the callee's declaration line.

**`report.py` — output.** `render_text` splits findings into errors (`kind == "effect"`) and warnings (everything else); colour is applied only when both requested and `stdout.isatty()`. `render_json` is the CI-facing form. `cli.py` exits 1 iff any `effect` finding exists — warnings never fail the build.

## Conventions

- The tool's credibility rests on never claiming clean when it cannot see. Any change that could hide a blind spot (swallowing an unresolved call, dropping an indirect site, treating an unparsed file as empty) needs a compensating warning finding.
- The default effect tables are *known* leaves only. rtcheck does not guess at symbols by prefix or heuristic; new coverage means new explicit entries.
- Tests are end-to-end: they write real C to `tmp_path`, parse it with libclang, and assert on findings. Add tests in that style — inline C snippets via the `run()` helper — rather than mocking the graph.
- The README's "What it cannot do" section is a load-bearing contract, not marketing. If a change alters those limits, update it.
