"""End-to-end tests. These parse real C and assert on the findings."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from rtcheck import analyse, config, graph

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
