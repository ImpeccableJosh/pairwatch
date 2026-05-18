"""End-to-end scenario tests for PairWatch.

Exercises the detector + agent on synthetic event windows that look like each of
the four patterns, plus filter logic and cooldown gating. One Claude call per
pattern scenario (5 total) + one extra for the cooldown test.

Run with: .venv/bin/python -m tests.e2e_scenarios
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pairwatch.agent import Agent
from pairwatch.detector import compute_signals
from pairwatch.watcher import _is_ignored

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


def section(title: str) -> None:
    print(f"\n{YELLOW}═══ {title} ═══{RESET}")


def ok(msg: str) -> None:
    print(f"{GREEN}✓{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"{RED}✗{RESET} {msg}")


def info(msg: str) -> None:
    print(f"{DIM}  {msg}{RESET}")


def event(path: str, ts: datetime, diff: str, lines_changed: int = 0) -> dict:
    return {
        "timestamp": ts.isoformat(),
        "path": path,
        "content_hash": f"h-{ts.isoformat()}",
        "diff": diff,
        "lines_changed": lines_changed,
    }


def churn_events(base: datetime) -> list[dict]:
    """6 substantively different rewrites of parse() in ~9 minutes."""
    p = "/tmp/proj/parse.py"
    diffs = [
        "--- a\n+++ b\n@@\n+def parse(s):\n+    return s.split(',')",
        "--- a\n+++ b\n@@\n-    return s.split(',')\n+    parts = s.split(',')\n+    return [p.strip() for p in parts]",
        "--- a\n+++ b\n@@\n-    parts = s.split(',')\n-    return [p.strip() for p in parts]\n+    return [p.strip() for p in s.split(',') if p]",
        "--- a\n+++ b\n@@\n-    return [p.strip() for p in s.split(',') if p]\n+    result = []\n+    for p in s.split(','):\n+        if p.strip():\n+            result.append(p.strip())\n+    return result",
        "--- a\n+++ b\n@@\n-    result = []\n-    for p in s.split(','):\n-        if p.strip():\n-            result.append(p.strip())\n-    return result\n+    return list(filter(None, (p.strip() for p in s.split(','))))",
        "--- a\n+++ b\n@@\n-    return list(filter(None, (p.strip() for p in s.split(','))))\n+    return [p.strip() for p in s.split(',') if p.strip()]",
    ]
    return [event(p, base - timedelta(minutes=9 - i * 1.5), d, lines_changed=(1 if i % 2 else 0)) for i, d in enumerate(diffs)]


def stall_events(base: datetime) -> list[dict]:
    """5 saves in a tight burst 12-14 min ago, then total silence."""
    p = "/tmp/proj/parser.py"
    diffs = [
        "--- a\n+++ b\n@@\n+def parse_header(raw):\n+    pass",
        "--- a\n+++ b\n@@\n-    pass\n+    fields = raw.split(';')",
        "--- a\n+++ b\n@@\n+    result = {}",
        "--- a\n+++ b\n@@\n+    for f in fields:\n+        k, v = f.split('=')",
        "--- a\n+++ b\n@@\n+        result[k.strip()] = v.strip()\n+    return result\n+    # TODO handle malformed",
    ]
    return [event(p, base - timedelta(minutes=14 - i * 0.4), d, 2) for i, d in enumerate(diffs)]


def revert_events(base: datetime) -> list[dict]:
    """A → B → back to A on an auth check."""
    p = "/tmp/proj/auth.py"
    return [
        event(p, base - timedelta(minutes=4),
              "--- a\n+++ b\n@@\n+def check_token(t):\n+    return t.startswith('Bearer')", 2),
        event(p, base - timedelta(minutes=2),
              "--- a\n+++ b\n@@\n-    return t.startswith('Bearer')\n+    return t.startswith('Bearer ') and len(t) > 7", 0),
        event(p, base - timedelta(seconds=30),
              "--- a\n+++ b\n@@\n-    return t.startswith('Bearer ') and len(t) > 7\n+    return t.startswith('Bearer')", 0),
    ]


def repeated_mistake_events(base: datetime) -> list[dict]:
    """3 files added back-to-back, each with the same bare try/except pattern."""
    events_ = []
    for i, name in enumerate(["loader", "parser", "saver"]):
        events_.append(event(
            f"/tmp/proj/{name}.py",
            base - timedelta(minutes=8 - i * 2),
            f"--- a\n+++ b\n@@\n+def {name}(x):\n+    try:\n+        return _do_{name}(x)\n+    except:\n+        return None",
            lines_changed=4,
        ))
    return events_


def normal_events(base: datetime) -> list[dict]:
    """Calm coding: 3 small additive saves, nothing suspicious."""
    diffs = [
        "--- a\n+++ b\n@@\n+import json",
        "--- a\n+++ b\n@@\n+from datetime import datetime, timezone",
        "--- a\n+++ b\n@@\n+from pathlib import Path",
    ]
    return [event("/tmp/proj/imports.py", base - timedelta(minutes=4 - i), d, 1) for i, d in enumerate(diffs)]


# ---------------------------------------------------------------------------
# Offline tests (no API calls)
# ---------------------------------------------------------------------------

def test_filters() -> None:
    section("Filter logic (offline)")
    root = Path("/tmp/proj")
    cases = [
        (root / "node_modules" / "x.js", True),
        (root / ".git" / "HEAD", True),
        (root / "build" / "out.txt", True),
        (root / ".pairwatch" / "events.jsonl", True),
        (root / "__pycache__" / "x.pyc", True),
        (root / "src" / "main.py", False),
        (root / "img.png", True),
        (root / "deps.lock", True),
        (root / "README.md", False),
    ]
    for path, expected in cases:
        got = _is_ignored(path, root)
        label = "ignore" if expected else "keep"
        if got == expected:
            ok(f"{label}: {path.relative_to(root)}")
        else:
            fail(f"{path.relative_to(root)}: expected {expected}, got {got}")


def test_detector_signals(base: datetime) -> None:
    section("Detector signals (offline)")
    sigs = compute_signals(churn_events(base), now=base)
    if sigs["churn"]:
        ok(f"churn fired: {sigs['churn'][0]['save_count']} saves, net={sigs['churn'][0]['net_lines_changed']}")
    else:
        fail("churn expected to fire")

    sigs = compute_signals(stall_events(base), now=base)
    if sigs["stall"]:
        ok(f"stall fired: silence={sigs['stall'][0]['silence_minutes']}min")
    else:
        fail(f"stall expected to fire, got {sigs['stall']}")

    sigs = compute_signals(revert_events(base), now=base)
    if sigs["revert_loop"]:
        ok(f"revert_loop fired: similarity={sigs['revert_loop'][0]['similarity']}")
    else:
        fail("revert_loop expected to fire")

    sigs = compute_signals(repeated_mistake_events(base), now=base)
    bare_except = [p for p in sigs["repeated_mistake"] if p["pattern"] == "bare_except"]
    if bare_except:
        ok(f"repeated_mistake fired: bare_except × {bare_except[0]['occurrences']}")
    else:
        fail(f"repeated_mistake expected (bare_except), got {sigs['repeated_mistake']}")


# ---------------------------------------------------------------------------
# Live tests (one Claude call each)
# ---------------------------------------------------------------------------

def run_live(name: str, events: list[dict], expect_fire: bool, expect_pattern: str | None = None) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="pw-test-"))
    try:
        agent = Agent(tmp)
        signals = compute_signals(events)
        result = agent.decide(events, signals)
        raw = result.get("raw") or {}
        fired = bool(result.get("fired"))
        pat = raw.get("pattern", "none")
        conf = raw.get("confidence", 0)
        info(f"fired={fired}  pattern={pat}  conf={conf}  reason={result.get('reason')}")
        if raw.get("message"):
            info(f"msg: {raw['message'][:160]}")
        ok_flag = True
        if expect_fire and not fired:
            fail(f"{name}: expected fire, got decline")
            ok_flag = False
        elif not expect_fire and fired:
            fail(f"{name}: expected decline, got fire ({pat})")
            ok_flag = False
        elif expect_fire and expect_pattern and pat != expect_pattern:
            fail(f"{name}: fired with pattern={pat}, expected {expect_pattern}")
            ok_flag = False
        if ok_flag:
            ok(f"{name}: {'fired ' + pat if fired else 'declined'} as expected")
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_live_scenarios(base: datetime) -> None:
    section("Live Claude calls — one per scenario")
    run_live("normal coding", normal_events(base), expect_fire=False)
    run_live("churn", churn_events(base), expect_fire=True, expect_pattern="churn")
    run_live("stall", stall_events(base), expect_fire=True, expect_pattern="stall")
    run_live("revert_loop", revert_events(base), expect_fire=True, expect_pattern="revert_loop")
    run_live("repeated_mistake", repeated_mistake_events(base), expect_fire=True, expect_pattern="repeated_mistake")


def test_cooldown(base: datetime) -> None:
    section("Cooldown gating (2 Claude calls)")
    tmp = Path(tempfile.mkdtemp(prefix="pw-cooldown-"))
    try:
        agent = Agent(tmp)
        evs = churn_events(base)
        sigs = compute_signals(evs)
        r1 = agent.decide(evs, sigs)
        if not r1.get("fired"):
            fail(f"first call did not fire — {r1.get('reason')}")
            return
        ok(f"first call fired (pattern={r1['raw']['pattern']})")
        r2 = agent.decide(evs, sigs)
        reason = r2.get("reason") or ""
        if not r2.get("fired") and "cooldown" in reason:
            ok(f"second call gated: {reason}")
        else:
            fail(f"second call not properly gated: fired={r2.get('fired')} reason={reason}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    base = datetime.now(timezone.utc)
    test_filters()
    test_detector_signals(base)
    test_live_scenarios(base)
    test_cooldown(base)
    print(f"\n{YELLOW}═══ done ═══{RESET}\n")


if __name__ == "__main__":
    main()
