"""Pattern signal computations over a recent window of save events.

Returns lightweight, pre-chewed numbers and candidate snippets. Does NOT decide
whether to intervene — that is Claude's job in agent.py.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Iterable


def _ts(rec: dict) -> datetime:
    return datetime.fromisoformat(rec["timestamp"])


def _by_path(events: Iterable[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        grouped[ev["path"]].append(ev)
    for v in grouped.values():
        v.sort(key=_ts)
    return grouped


def churn(events: list[dict], now: datetime, window_minutes: int = 10) -> list[dict]:
    """Files with >5 saves in the window and small net line delta."""
    cutoff = now.timestamp() - window_minutes * 60
    out = []
    for path, evs in _by_path(events).items():
        recent = [e for e in evs if _ts(e).timestamp() >= cutoff]
        if len(recent) <= 5:
            continue
        net = sum(e.get("lines_changed", 0) for e in recent)
        if abs(net) < 10:
            out.append({
                "path": path,
                "save_count": len(recent),
                "net_lines_changed": net,
                "window_minutes": window_minutes,
            })
    return out


def stall(events: list[dict], now: datetime, threshold_minutes: int = 8) -> list[dict]:
    """Files that had recent activity but have gone silent for > threshold."""
    out = []
    for path, evs in _by_path(events).items():
        if not evs:
            continue
        last_ts = _ts(evs[-1]).timestamp()
        silence_sec = now.timestamp() - last_ts
        active_window = 30 * 60
        recent_active = [e for e in evs if now.timestamp() - _ts(e).timestamp() <= active_window]
        if len(recent_active) >= 2 and silence_sec > threshold_minutes * 60:
            out.append({
                "path": path,
                "silence_minutes": round(silence_sec / 60, 1),
                "saves_in_last_30min": len(recent_active),
            })
    return out


def _normalize_diff(diff: str, sign: str) -> str:
    """Pull just the +/- payload lines (no headers) from a unified diff."""
    lines = []
    for line in diff.splitlines():
        if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
            continue
        if line.startswith(sign) and not line.startswith(sign * 2):
            lines.append(line[1:].strip())
    return "\n".join(lines)


def revert_loop(events: list[dict], similarity_threshold: float = 0.7) -> list[dict]:
    """A save's additions look like a recent prior save's deletions (and vice versa)."""
    out = []
    for path, evs in _by_path(events).items():
        if len(evs) < 2:
            continue
        latest = evs[-1]
        latest_added = _normalize_diff(latest.get("diff", ""), "+")
        latest_removed = _normalize_diff(latest.get("diff", ""), "-")
        if not latest_added and not latest_removed:
            continue
        for prior in evs[-6:-1]:
            prior_added = _normalize_diff(prior.get("diff", ""), "+")
            prior_removed = _normalize_diff(prior.get("diff", ""), "-")
            sim_a = difflib.SequenceMatcher(None, latest_added, prior_removed).ratio() if latest_added and prior_removed else 0.0
            sim_b = difflib.SequenceMatcher(None, latest_removed, prior_added).ratio() if latest_removed and prior_added else 0.0
            score = max(sim_a, sim_b)
            if score >= similarity_threshold:
                out.append({
                    "path": path,
                    "similarity": round(score, 2),
                    "current_save_ts": latest["timestamp"],
                    "prior_save_ts": prior["timestamp"],
                })
                break
    return out


MISTAKE_PATTERNS = [
    ("missing_null_check", re.compile(r"\bif\s+\w+\s+is\s+None\b")),
    ("bare_try", re.compile(r"^\s*try:\s*$", re.MULTILINE)),
    ("bare_except", re.compile(r"^\s*except\s*:", re.MULTILINE)),
    ("print_debug", re.compile(r"^\s*print\(.*\)\s*$", re.MULTILINE)),
    ("todo_comment", re.compile(r"#\s*TODO", re.IGNORECASE)),
    ("await_missing", re.compile(r"\b(\w+\.(get|post|fetch|query)\([^)]*\))(?!\s*await)")),
]


def repeated_mistake(events: list[dict]) -> list[dict]:
    """Count recurrences of cheap structural patterns across recent + lines."""
    counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = defaultdict(list)
    for ev in events:
        added = _normalize_diff(ev.get("diff", ""), "+")
        if not added:
            continue
        for name, pat in MISTAKE_PATTERNS:
            for match in pat.finditer(added):
                counts[name] += 1
                if len(samples[name]) < 3:
                    samples[name].append(match.group(0).strip()[:120])
    out = []
    for name, n in counts.items():
        if n >= 2:
            out.append({"pattern": name, "occurrences": n, "samples": samples[name]})
    return out


def compute_signals(events: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {
        "event_count": len(events),
        "churn": churn(events, now),
        "stall": stall(events, now),
        "revert_loop": revert_loop(events),
        "repeated_mistake": repeated_mistake(events),
    }


if __name__ == "__main__":
    import json
    from datetime import timedelta

    base = datetime.now(timezone.utc)
    # Synthetic: 6 churny saves on utils.py within 10 min, small net delta
    fake = []
    for i in range(6):
        fake.append({
            "timestamp": (base - timedelta(minutes=10 - i)).isoformat(),
            "path": "/tmp/proj/utils.py",
            "content_hash": f"h{i}",
            "diff": f"--- a\n+++ b\n@@\n-old line {i}\n+new line {i}\n",
            "lines_changed": 0 if i % 2 else 1,
        })
    # And a stall on parser.py
    fake.append({
        "timestamp": (base - timedelta(minutes=12)).isoformat(),
        "path": "/tmp/proj/parser.py",
        "content_hash": "p0",
        "diff": "+def parse():\n+    pass\n",
        "lines_changed": 2,
    })
    fake.append({
        "timestamp": (base - timedelta(minutes=11)).isoformat(),
        "path": "/tmp/proj/parser.py",
        "content_hash": "p1",
        "diff": "+def parse():\n+    return None\n",
        "lines_changed": 1,
    })
    sigs = compute_signals(fake, now=base)
    print(json.dumps(sigs, indent=2))
