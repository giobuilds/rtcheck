"""End-to-end tests. These parse real C and assert on the findings."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from rtcheck import analyse, cli, config, graph, report

EXAMPLES = Path(__file__).parent.parent / "examples" / "audio"


def run(source: str, entry: str, tmp_path: Path, **cfg_kw) -> list:
    src = tmp_path / "unit.c"
    src.write_text(textwrap.dedent(source))
    cfg = config.Config(entrypoints=[entry], **cfg_kw)
    cg = graph.build([str(src)], ["-std=c11"])
    return analyse.analyse(cg, cfg)


def violations(findings) -> list:
    return [f for f in findings if f.kind == "effect"]


def test_direct_allocation_is_caught(tmp_path):
    f = run("""
        #include <stdlib.h>
        void rt(void){ malloc(16); }
    """, "rt", tmp_path)
    v = violations(f)
    assert len(v) == 1
    assert v[0].symbol == "malloc"
    assert v[0].effect == "alloc"


def test_transitive_allocation_is_caught_with_full_path(tmp_path):
    f = run("""
        #include <stdlib.h>
        static void c(void){ malloc(1); }
        static void b(void){ c(); }
        static void a(void){ b(); }
        void rt(void){ a(); }
    """, "rt", tmp_path)
    v = violations(f)
    assert len(v) == 1
    assert [s.name for s in v[0].path] == ["rt", "a", "b", "c", "malloc"]


def test_pure_arithmetic_is_clean(tmp_path):
    f = run("""
        static float g(float x){ return x * 0.5f; }
        void rt(float *o, int n){ for (int i=0;i<n;i++) o[i]=g(o[i]); }
    """, "rt", tmp_path)
    assert violations(f) == []


def test_unreachable_violation_is_not_reported(tmp_path):
    """A malloc elsewhere in the file must not implicate the entry point."""
    f = run("""
        #include <stdlib.h>
        void setup(void){ malloc(32); }
        void rt(void){ }
    """, "rt", tmp_path)
    assert violations(f) == []


def test_shortest_path_is_chosen(tmp_path):
    f = run("""
        #include <stdlib.h>
        static void leaf(void){ malloc(1); }
        static void longer(void){ leaf(); }
        static void wrap(void){ longer(); }
        void rt(void){ leaf(); wrap(); }
    """, "rt", tmp_path)
    v = violations(f)
    assert len(v) == 1
    assert [s.name for s in v[0].path] == ["rt", "leaf", "malloc"]


def test_allowlist_suppresses(tmp_path):
    f = run("""
        #include <stdlib.h>
        static void audited(void){ malloc(1); }
        void rt(void){ audited(); }
    """, "rt", tmp_path, allow=["audited"])
    assert violations(f) == []


def test_indirect_call_warns_by_default(tmp_path):
    f = run("""
        typedef void (*Fn)(void);
        void rt(Fn f){ f(); }
    """, "rt", tmp_path)
    assert any(x.kind == "indirect" for x in f)


def test_address_taken_mode_finds_pointer_target(tmp_path):
    f = run("""
        #include <stdlib.h>
        typedef void (*Fn)(void);
        static void bad(void){ malloc(1); }
        static Fn slot;
        void install(void){ slot = bad; }
        void rt(void){ slot(); }
    """, "rt", tmp_path, indirect="address-taken")
    v = violations(f)
    assert len(v) == 1
    assert v[0].symbol == "malloc"
    assert "bad" in [s.name for s in v[0].path]


def test_direct_call_does_not_count_as_address_taken(tmp_path):
    """Regression: a plain call must not be treated as taking the address."""
    src = tmp_path / "u.c"
    src.write_text("static void helper(void){} void rt(void){ helper(); }")
    cg = graph.build([str(src)], ["-std=c11"])
    assert cg.address_taken_nodes() == []


def test_recursion_is_flagged(tmp_path):
    f = run("""
        void rt(int n){ if (n) rt(n-1); }
    """, "rt", tmp_path)
    assert any(x.kind == "recursion" for x in f)


def test_missing_entrypoint_is_reported(tmp_path):
    f = run("void other(void){}", "nope", tmp_path)
    assert any(x.kind == "unresolved" and "nope" in x.detail for x in f)


def test_effect_categories_are_distinguished(tmp_path):
    f = run("""
        #include <stdio.h>
        #include <pthread.h>
        static pthread_mutex_t m;
        void rt(void){ printf("x"); pthread_mutex_lock(&m); }
    """, "rt", tmp_path)
    effects = {v.effect for v in violations(f)}
    assert effects == {"io", "lock"}


@pytest.mark.skipif(not (EXAMPLES / "mixer.c").exists(), reason="examples missing")
def test_example_mixer_finds_all_three():
    cfg = config.Config(entrypoints=["mix_frame"])
    cg = graph.build([str(EXAMPLES / "mixer.c")], ["-std=c11"])
    v = violations(analyse.analyse(cg, cfg))
    assert {x.effect for x in v} == {"alloc", "io", "lock"}


# --- compile_commands.json flag handling -------------------------------------
#
# These guard a failure mode that is worse than a missed violation: a mangled
# flag list degrades the parse, the call graph comes out empty, and rtcheck
# reports a clean build. Every test here asserts the violation is still found.

def _cc_project(tmp_path: Path, arguments: list[str]) -> Path:
    """A project whose allocating wrapper is only visible via an include path."""
    (tmp_path / "inc").mkdir()
    (tmp_path / "inc" / "rtwrap.h").write_text(
        "#include <stdlib.h>\n"
        "static inline void *rt_alloc(unsigned n){ return malloc(n); }\n"
    )
    (tmp_path / "main.c").write_text(
        "#include <rtwrap.h>\n"
        "void mix_frame(void){ rt_alloc(64); }\n"
    )
    db = tmp_path / "compile_commands.json"
    db.write_text(json.dumps([{
        "directory": str(tmp_path),
        "file": "main.c",
        "arguments": ["cc", *arguments, "-c", "main.c", "-o", "main.o"],
    }]))
    return db


@pytest.mark.parametrize("include_flag", [
    ["-I", "inc"],          # split form: the operand is a separate argv token
    ["-Iinc"],              # joined form, relative to the entry's directory
    ["-I", "./inc"],
    ["-isystem", "inc"],
])
def test_include_flags_survive_the_compile_commands_filter(tmp_path, capsys, include_flag):
    """Split operands must be kept, and relative paths resolved against
    `directory` -- rtcheck does not run clang from there."""
    db = _cc_project(tmp_path, [*include_flag, "-std=c11"])
    assert cli.main(["--compile-commands", str(db), "--entry", "mix_frame"]) == 1
    assert "malloc" in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [
    ["-isystem", "/usr/include", "-DENABLE_REVERB=1"],
    ["-DENABLE_REVERB=1", "-isystem", "/usr/include"],
])
def test_orphaned_path_flag_does_not_swallow_the_next_flag(tmp_path, arguments):
    """Keeping `-isystem` but dropping its operand left it to consume the
    following flag, so flag *order* decided whether the violation was found."""
    (tmp_path / "main.c").write_text(
        "#include <stdlib.h>\n"
        "void mix_frame(void){\n"
        "#ifdef ENABLE_REVERB\n"
        "    malloc(64);\n"
        "#endif\n"
        "}\n"
    )
    db = tmp_path / "compile_commands.json"
    db.write_text(json.dumps([{
        "directory": str(tmp_path), "file": "main.c",
        "arguments": ["cc", *arguments, "-std=c11", "-c", "main.c", "-o", "main.o"],
    }]))
    assert cli.main(["--compile-commands", str(db), "--entry", "mix_frame"]) == 1


def test_relative_paths_are_resolved_against_the_entry_directory():
    args = graph._relevant_args(["-I", "inc", "-Iother", "-DFOO=1"], "/proj/build")
    assert args == ["-I", "/proj/build/inc", "-I/proj/build/other", "-DFOO=1"]


# --- exit codes ---------------------------------------------------------------

def test_unknown_entrypoint_fails_the_build(tmp_path):
    """A renamed or misspelled entry point would otherwise leave CI green
    forever while nothing was being checked."""
    (tmp_path / "u.c").write_text("void other(void){}")
    assert cli.main([str(tmp_path / "u.c"), "--entry", "mix_frame"]) == 1


def test_entrypoint_without_a_body_fails_the_build(tmp_path):
    (tmp_path / "u.c").write_text("void mix_frame(void);")
    assert cli.main([str(tmp_path / "u.c"), "--entry", "mix_frame"]) == 1


def test_unparseable_source_does_not_pass_silently(tmp_path):
    assert cli.main([str(tmp_path / "missing.c"), "--entry", "mix_frame"]) == 1


def test_opaque_leaves_do_not_fail_the_build(tmp_path):
    """Library functions with no source are a warning, not a failure: a real
    project reaches hundreds of them."""
    (tmp_path / "u.c").write_text("void ext(void);\nvoid mix_frame(void){ ext(); }\n")
    assert cli.main([str(tmp_path / "u.c"), "--entry", "mix_frame"]) == 0


def test_opaque_summary_is_a_distinct_kind_from_an_unresolved_entrypoint(tmp_path):
    f = run("void ext(void); void rt(void){ ext(); }", "rt", tmp_path)
    assert [x.kind for x in f] == ["opaque"]


# --- recursion ----------------------------------------------------------------

def test_recursion_in_a_helper_is_flagged(tmp_path):
    """Unbounded stack growth one level down is just as fatal as at the entry."""
    f = run("""
        static void spin(int n){ if (n) spin(n-1); }
        void rt(int d){ spin(d); }
    """, "rt", tmp_path)
    rec = [x for x in f if x.kind == "recursion"]
    assert len(rec) == 1
    assert [s.name for s in rec[0].path] == ["rt", "spin", "spin"]


def test_mutual_recursion_between_helpers_is_flagged(tmp_path):
    f = run("""
        static void descend(int n);
        static void walk(int n){ if (n) descend(n-1); }
        static void descend(int n){ if (n) walk(n-1); }
        void rt(int d){ walk(d); }
    """, "rt", tmp_path)
    rec = [x for x in f if x.kind == "recursion"]
    assert len(rec) == 1
    assert [s.name for s in rec[0].path] == ["rt", "walk", "descend", "walk"]


def test_diamond_is_not_mistaken_for_recursion(tmp_path):
    """A function reached twice by different paths is not a cycle."""
    f = run("""
        static void leaf(void){}
        static void a(void){ leaf(); }
        static void b(void){ leaf(); }
        void rt(void){ a(); b(); }
    """, "rt", tmp_path)
    assert not [x for x in f if x.kind == "recursion"]


def test_allowlisted_function_is_not_searched_for_cycles(tmp_path):
    f = run("""
        static void spin(int n){ if (n) spin(n-1); }
        void rt(int d){ spin(d); }
    """, "rt", tmp_path, allow=["spin"])
    assert not [x for x in f if x.kind == "recursion"]


def test_recursion_reporting_can_be_disabled(tmp_path):
    f = run("""
        static void spin(int n){ if (n) spin(n-1); }
        void rt(int d){ spin(d); }
    """, "rt", tmp_path, flag_recursion=False)
    assert not [x for x in f if x.kind == "recursion"]


# --- per-file compile flags ---------------------------------------------------

def test_per_file_flags_do_not_leak_between_translation_units(tmp_path):
    """A -D belonging to one target must not change how another one parses."""
    for name in ("a", "b"):
        (tmp_path / f"{name}.c").write_text(
            "#include <stdlib.h>\n"
            f"void rt_{name}(void){{\n#ifdef GUARD\n    malloc(1);\n#endif\n}}\n"
        )
    db = tmp_path / "compile_commands.json"
    db.write_text(json.dumps([
        {"directory": str(tmp_path), "file": "a.c",
         "arguments": ["cc", "-DGUARD=1", "-std=c11", "-c", "a.c"]},
        {"directory": str(tmp_path), "file": "b.c",
         "arguments": ["cc", "-std=c11", "-c", "b.c"]},
    ]))
    assert cli.main(["--compile-commands", str(db), "--entry", "rt_a"]) == 1
    assert cli.main(["--compile-commands", str(db), "--entry", "rt_b"]) == 0


def test_compile_commands_accepts_the_command_string_form(tmp_path):
    db = tmp_path / "compile_commands.json"
    db.write_text(json.dumps([{
        "directory": str(tmp_path), "file": "main.c",
        "command": "cc -Iinc -DX=1 -c main.c -o main.o",
    }]))
    sources, per_file = graph.compile_commands_sources(db)
    assert sources == [str(tmp_path / "main.c")]
    assert per_file[sources[0]] == [f"-I{tmp_path / 'inc'}", "-DX=1"]


# --- degraded parses ----------------------------------------------------------

def test_parse_errors_are_reported_without_a_flag(tmp_path):
    """A half-parsed file used to be silent unless --show-parse-errors was on."""
    src = tmp_path / "u.c"
    src.write_text("#include <definitely_not_a_real_header.h>\nvoid rt(void){}\n")
    cg = graph.build([str(src)], ["-std=c11"])
    f = analyse.analyse(cg, config.Config(entrypoints=["rt"]))
    assert any(x.kind == "parse" for x in f)


def test_a_file_that_cannot_be_parsed_at_all_is_a_failed_check(tmp_path):
    cg = graph.build([str(tmp_path / "missing.c")], ["-std=c11"])
    f = analyse.analyse(cg, config.Config(entrypoints=["rt"]))
    assert any(x.kind == "parse_error" for x in f)


def test_deeply_nested_expression_does_not_exhaust_the_stack(tmp_path):
    """The AST walk is iterative; 3000 nested binary operators would overflow
    a recursive one long before clang objected."""
    chain = "+".join(["1"] * 3000)
    f = run(f"#include <stdlib.h>\nvoid rt(void){{ int x = {chain}; (void)x; malloc(1); }}\n",
            "rt", tmp_path)
    assert violations(f)[0].symbol == "malloc"


def test_include_dirs_are_ranked_by_version_number(tmp_path):
    """Sorting these as strings put llvm-15 behind llvm-9."""
    assert graph._version_of("/usr/lib/llvm-15/lib/clang/15/include") > \
           graph._version_of("/usr/lib/llvm-9/lib/clang/9/include")


# --- configuration ------------------------------------------------------------

def _cfg(tmp_path: Path, body: str):
    path = tmp_path / "rtcheck.toml"
    path.write_text(textwrap.dedent(body))
    return config.load(path)


def test_config_extends_a_builtin_category(tmp_path):
    cfg = _cfg(tmp_path, '[forbidden.alloc]\nsymbols = ["my_arena_grow"]\n')
    assert cfg.symbol_to_effect["my_arena_grow"] == "alloc"
    assert cfg.symbol_to_effect["malloc"] == "alloc"


def test_config_replace_drops_the_builtin_symbols(tmp_path):
    cfg = _cfg(tmp_path, '[forbidden.alloc]\nreplace = true\nsymbols = ["only_this"]\n')
    assert cfg.symbol_to_effect["only_this"] == "alloc"
    assert "malloc" not in cfg.symbol_to_effect


def test_config_defines_a_new_category_with_its_own_message(tmp_path):
    cfg = _cfg(tmp_path, '[forbidden.gpu]\nmessage = "touch the GPU"\n'
                         'symbols = ["glBufferData"]\n')
    assert cfg.symbol_to_effect["glBufferData"] == "gpu"
    assert cfg.message_for("gpu") == "touch the GPU"


def test_config_allow_removes_a_builtin_symbol(tmp_path):
    cfg = _cfg(tmp_path, '[allow]\nfunctions = ["malloc"]\n')
    assert "malloc" not in cfg.symbol_to_effect


def test_config_can_enable_a_subset_of_effects(tmp_path):
    cfg = _cfg(tmp_path, '[options]\neffects = ["alloc"]\n')
    assert "malloc" in cfg.symbol_to_effect
    assert "printf" not in cfg.symbol_to_effect


def test_config_options_round_trip(tmp_path):
    cfg = _cfg(tmp_path, '''
        [entrypoints]
        functions = ["mix_frame", "process_block"]
        [options]
        indirect = "ignore"
        flag_recursion = false
        clang_args = ["-DX=1"]
    ''')
    assert cfg.entrypoints == ["mix_frame", "process_block"]
    assert cfg.indirect == "ignore"
    assert cfg.flag_recursion is False
    assert cfg.clang_args == ["-DX=1"]


def test_config_defaults_apply_without_a_file():
    cfg = config.load(None)
    assert cfg.indirect == "warn" and cfg.flag_recursion is True
    assert cfg.symbol_to_effect["pthread_mutex_lock"] == "lock"


def test_config_is_found_beside_the_sources(tmp_path, monkeypatch, capsys):
    """Invoked from elsewhere, rtcheck should still find the project's config."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "rtcheck.toml").write_text('[entrypoints]\nfunctions = ["mix_frame"]\n')
    (proj / "main.c").write_text("#include <stdlib.h>\nvoid mix_frame(void){ malloc(1); }\n")
    monkeypatch.chdir(tmp_path)
    assert cli.main([str(proj)]) == 1          # entry point came from the config
    assert "malloc" in capsys.readouterr().out


# --- indirect modes -----------------------------------------------------------

def test_indirect_ignore_mode_reports_nothing(tmp_path):
    f = run("""
        typedef void (*Fn)(void);
        void rt(Fn f){ f(); }
    """, "rt", tmp_path, indirect="ignore")
    assert f == []


# --- reporting ----------------------------------------------------------------

def test_render_json_carries_counts_and_the_full_path(tmp_path):
    f = run("""
        #include <stdlib.h>
        static void helper(void){ malloc(1); }
        void rt(void){ helper(); }
    """, "rt", tmp_path)
    payload = json.loads(report.render_json(f, config.Config()))
    assert payload["violations"] == 1
    assert payload["unresolved"] == 0
    assert [s["function"] for s in payload["findings"][0]["path"]] == ["rt", "helper", "malloc"]


def test_render_text_uses_the_configured_effect_message(tmp_path):
    f = run("#include <stdlib.h>\nvoid rt(void){ malloc(1); }", "rt", tmp_path)
    out = report.render_text(f, config.Config(), colour=False)
    assert "must not allocate or free memory" in out
    assert "1 violation(s)" in out


def test_render_text_never_calls_a_failed_check_clean(tmp_path):
    f = run("void other(void){}", "rt", tmp_path)
    out = report.render_text(f, config.Config(entrypoints=["rt"]), colour=False)
    assert "no violations" not in out
    assert "could not be completed" in out


def test_module_entry_point_is_wired_up():
    from rtcheck import __main__
    assert __main__.main is cli.main
