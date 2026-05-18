"""Append-only JSONL event log with diff computation against the last save of each file."""
from __future__ import annotations

import difflib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DIFF_MAX_BYTES = 2048


class EventLog:
    def __init__(self, target_dir: Path):
        self.target_dir = Path(target_dir).resolve()
        self.log_dir = self.target_dir / ".pairwatch"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / "events.jsonl"
        self._last_text: dict[str, str] = {}

    def _diff(self, path: str, new_text: str) -> tuple[str, int]:
        prev = self._last_text.get(path, "")
        prev_lines = prev.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(prev_lines, new_lines, lineterm=""))
        diff_text = "\n".join(diff_lines)
        if len(diff_text.encode("utf-8")) > DIFF_MAX_BYTES:
            diff_text = diff_text.encode("utf-8")[:DIFF_MAX_BYTES].decode("utf-8", errors="ignore") + "\n…[truncated]"
        net = len(new_lines) - len(prev_lines)
        return diff_text, net

    def append(self, event: dict) -> dict:
        """Accepts a dict with keys path, timestamp, content_hash, raw_text. Returns enriched record."""
        path = event["path"]
        raw_text = event.get("raw_text", "")
        diff_text, lines_changed = self._diff(path, raw_text)
        record = {
            "timestamp": event["timestamp"],
            "path": path,
            "content_hash": event["content_hash"],
            "diff": diff_text,
            "lines_changed": lines_changed,
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        self._last_text[path] = raw_text
        return record

    def read_recent(self, minutes: int) -> list[dict]:
        if not self.log_path.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        out: list[dict] = []
        with self.log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = datetime.fromisoformat(rec["timestamp"])
                    if ts >= cutoff:
                        out.append(rec)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        return out


if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="pw-log-"))
    log = EventLog(tmp)
    now = datetime.now(timezone.utc)
    for i, body in enumerate(["x = 1\n", "x = 1\ny = 2\n", "x = 1\ny = 2\nz = 3\n"]):
        log.append({
            "path": str(tmp / "foo.py"),
            "timestamp": (now + timedelta(seconds=i)).isoformat(),
            "content_hash": f"hash{i}",
            "raw_text": body,
        })
    recent = log.read_recent(60)
    print(f"wrote {len(recent)} events to {log.log_path}")
    for r in recent:
        print(f"  {r['timestamp']}  net={r['lines_changed']:+d}  diff_bytes={len(r['diff'])}")
