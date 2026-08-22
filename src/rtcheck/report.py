"""Turn findings into something a human can act on."""

from __future__ import annotations

import json
import sys

from .analyse import Finding
from .config import Config

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, YELLOW, GREEN, CYAN = "\033[31m", "\033[33m", "\033[32m", "\033[36m"


class Palette:
    def __init__(self, enabled: bool) -> None:
        self.on = enabled

    def __call__(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.on else text


def _location(step) -> str:
    if not step.file:
        return ""
    short = step.file.split("/")[-1]
    return f"{short}:{step.line}"


def render_text(findings: list[Finding], cfg: Config, colour: bool = True) -> str:
    c = Palette(colour and sys.stdout.isatty())
    errors = [f for f in findings if f.kind == "effect"]
    warnings = [f for f in findings if f.kind != "effect"]
    lines: list[str] = []

    for finding in errors:
        head = f"{finding.path[0].name} must not {cfg.message_for(finding.effect or '')}"
        lines.append(c(BOLD, c(RED, "violation: ")) + c(BOLD, head))
        for i, step in enumerate(finding.path):
            arrow = "  " if i == 0 else "  " + c(DIM, "\u2192 ")
            loc = _location(step)
            suffix = c(DIM, f"  {loc}") if loc else ""
            marker = c(RED, step.name) if i == len(finding.path) - 1 else step.name
            lines.append(f"{arrow}{marker}{suffix}")
        lines.append("")

    for finding in warnings:
        label = {"indirect": "blind spot", "recursion": "unbounded", "unresolved": "unknown"}
        lines.append(c(YELLOW, f"{label.get(finding.kind, finding.kind)}: ") + finding.detail)
        if finding.path:
            trail = " \u2192 ".join(s.name for s in finding.path)
            lines.append(c(DIM, f"  via {trail}"))
        lines.append("")

    if errors:
        lines.append(c(RED, f"{len(errors)} violation(s)") +
                     (f", {len(warnings)} warning(s)" if warnings else ""))
    elif warnings:
        lines.append(c(GREEN, "no violations") + f", {len(warnings)} warning(s)")
    else:
        lines.append(c(GREEN, "no violations, no warnings"))

    return "\n".join(lines)


def render_json(findings: list[Finding], cfg: Config) -> str:
    payload = [
        {
            "kind": f.kind,
            "entrypoint": f.entrypoint,
            "effect": f.effect,
            "symbol": f.symbol,
            "detail": f.detail,
            "path": [{"function": s.name, "file": s.file, "line": s.line} for s in f.path],
        }
        for f in findings
    ]
    return json.dumps({"findings": payload,
                       "violations": sum(1 for f in findings if f.kind == "effect")}, indent=2)
