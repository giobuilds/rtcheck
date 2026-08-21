"""rtcheck command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analyse, config, graph, report


def _collect_sources(paths: list[str]) -> list[str]:
    out: list[str] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for ext in ("*.c", "*.h"):
                out.extend(str(q) for q in sorted(p.rglob(ext)))
        else:
            out.append(str(p))
    return [s for s in out if s.endswith(".c")] or out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rtcheck",
        description="Find paths from a real-time function to operations that may block.",
    )
    ap.add_argument("paths", nargs="*", help="C source files or directories")
    ap.add_argument("-e", "--entry", action="append", default=[],
                    help="function that must stay real-time safe (repeatable)")
    ap.add_argument("-c", "--config", type=Path, help="path to rtcheck.toml")
    ap.add_argument("--compile-commands", type=Path,
                    help="compile_commands.json to take sources and flags from")
    ap.add_argument("-I", dest="includes", action="append", default=[],
                    help="include directory passed through to clang")
    ap.add_argument("-D", dest="defines", action="append", default=[])
    ap.add_argument("--std", default="c11", help="C standard (default: c11)")
    ap.add_argument("--indirect", choices=["warn", "address-taken", "ignore"],
                    help="how to treat calls through function pointers")
    ap.add_argument("--json", action="store_true", help="emit machine-readable output")
    ap.add_argument("--no-colour", action="store_true")
    ap.add_argument("--show-parse-errors", action="store_true",
                    help="print clang diagnostics from the parse")
    args = ap.parse_args(argv)

    cfg_path = args.config
    if cfg_path is None and Path("rtcheck.toml").exists():
        cfg_path = Path("rtcheck.toml")
    cfg = config.load(cfg_path)

    if args.entry:
        cfg.entrypoints = args.entry
    if args.indirect:
        cfg.indirect = args.indirect
    if not cfg.entrypoints:
        ap.error("no entry point given: use --entry NAME or set one in rtcheck.toml")

    clang_args = [f"-std={args.std}"] + cfg.clang_args
    clang_args += [f"-I{i}" for i in args.includes]
    clang_args += [f"-D{d}" for d in args.defines]

    if args.compile_commands:
        sources, per_file = graph.compile_commands_sources(args.compile_commands)
        merged: list[str] = []
        for src in sources:
            merged.extend(per_file.get(src, []))
        clang_args += [a for a in dict.fromkeys(merged) if a.startswith(("-I", "-D", "-std", "-isystem"))]
    else:
        sources = _collect_sources(args.paths)

    if not sources:
        ap.error("no source files found")

    cg = graph.build(sources, clang_args)

    if args.show_parse_errors and cg.diagnostics:
        for d in cg.diagnostics[:40]:
            print(f"clang: {d}", file=sys.stderr)

    findings = analyse.analyse(cg, cfg)

    if args.json:
        print(report.render_json(findings, cfg))
    else:
        print(report.render_text(findings, cfg, colour=not args.no_colour))

    return 1 if any(f.kind == "effect" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
