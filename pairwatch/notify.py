"""Terminal renderer for interventions. ANSI color, banner-style."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
GREEN = "\033[32m"

SEVERITY_COLOR = {"medium": YELLOW, "high": RED, "low": DIM}


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if _supports_color() else text


def render(decision: dict, target_dir: Path | None = None) -> None:
    """Print a banner. Also append to .pairwatch/interventions.jsonl if target_dir given."""
    severity = decision.get("severity", "medium")
    color = SEVERITY_COLOR.get(severity, YELLOW)
    pattern = decision.get("pattern", "?")
    message = decision.get("message", "")
    evidence = decision.get("evidence", "")
    confidence = decision.get("confidence", 0)
    bar = "─" * 60
    print()
    print(_c(bar, color))
    print(_c(f"⚑ PairWatch · {pattern.upper()} · {severity}  ({confidence:.0%})", color + BOLD))
    print()
    print(f"  {message}")
    if evidence:
        print()
        print(_c(f"  evidence: {evidence}", DIM))
    print(_c(bar, color))
    print()

    if target_dir is not None:
        log_dir = Path(target_dir).resolve() / ".pairwatch"
        log_dir.mkdir(parents=True, exist_ok=True)
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), **decision}
        with (log_dir / "interventions.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def render_silent(decision: dict, reason: str) -> None:
    """For --quiet mode or for decisions dropped by policy. Single dim line, no banner."""
    pattern = decision.get("pattern", "?") if decision else "?"
    print(_c(f"· pairwatch: skipped {pattern} ({reason})", DIM))


if __name__ == "__main__":
    for sev in ("medium", "high"):
        render({
            "pattern": "churn",
            "confidence": 0.84,
            "severity": sev,
            "message": "You've rewritten the parsing block in utils.py 6 times in 9 minutes — might be worth sketching the data flow before continuing.",
            "evidence": "6 saves of utils.py, net +0 lines",
        })
    render_silent({"pattern": "stall"}, "global cooldown (15m) active")
