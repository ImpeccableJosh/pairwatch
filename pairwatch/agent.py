"""Claude intervention call + policy gating (rate limit, cooldowns, confidence threshold)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

MODEL = "claude-sonnet-4-6"
GLOBAL_COOLDOWN_MIN = 15
PER_PATTERN_COOLDOWN_MIN = 30
CONFIDENCE_THRESHOLD = 0.7
MAX_EVENTS_IN_PROMPT = 40

SYSTEM_PROMPT = """You are PairWatch, a background pair-programming observer that watches a developer's file saves and decides whether to surface a short, useful observation.

You are interrupting a working developer. The default is silence. Only intervene when the signal is strong and the observation is genuinely actionable.

## Pattern taxonomy
- **churn**: The developer has rewritten the same file repeatedly (>5 saves in 10 min) with little net progress (|net lines| < 10). Suggests they may be stuck in local rewrites of the same logic.
- **stall**: After a burst of activity on a file, no saves for >8 minutes. Could mean they hit a wall, looked something up, or stepped away. Only intervene if the silence pattern is suspicious (e.g. follows a churn burst, file appears incomplete).
- **revert_loop**: A recent save largely undoes a prior recent save. Developer is going back and forth on the same change.
- **repeated_mistake**: The same structural issue (e.g. missing null check, bare except, missing await) appears across multiple recent edits. Surface only when the recurring issue is genuinely worth flagging — not stylistic preferences.

## Intervention policy
- Be conservative. If unsure, return should_intervene: false.
- Confidence must reflect your actual certainty. Below 0.7 will be dropped by the caller.
- Severity:
  - low: not worth interrupting (caller will only log it)
  - medium: a useful nudge the developer would want to see
  - high: something likely costing them serious time / heading toward a bug
- Messages must be ≤ 2 sentences, specific to the file/code in question, and frame the observation as a question or gentle prompt — never a command. Reference concrete details (file names, line counts, what the diffs show).
- Pick at most ONE pattern to surface per call — the most worth-interrupting-for.

You will be called with a recent window of save events and pre-computed pattern signals. Use those signals as anchors but apply your own judgment — they are heuristics, not verdicts.

You MUST respond by calling the record_decision tool exactly once."""

DECISION_TOOL = {
    "name": "record_decision",
    "description": "Record whether to intervene and the intervention details.",
    "input_schema": {
        "type": "object",
        "properties": {
            "should_intervene": {"type": "boolean"},
            "pattern": {
                "type": "string",
                "enum": ["churn", "stall", "revert_loop", "repeated_mistake", "none"],
            },
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "severity": {"type": "string", "enum": ["low", "medium", "high", "none"]},
            "message": {"type": "string"},
            "evidence": {"type": "string"},
        },
        "required": ["should_intervene", "pattern", "confidence", "severity", "message", "evidence"],
    },
}


def _truncate_events(events: list[dict], limit: int = MAX_EVENTS_IN_PROMPT) -> list[dict]:
    if len(events) <= limit:
        return events
    return events[-limit:]


def _serialize_for_prompt(events: list[dict], signals: dict) -> str:
    compact_events = []
    for ev in _truncate_events(events):
        compact_events.append({
            "ts": ev["timestamp"],
            "path": ev["path"],
            "lines_changed": ev.get("lines_changed", 0),
            "diff_excerpt": (ev.get("diff", "") or "")[:600],
        })
    return json.dumps({
        "now": datetime.now(timezone.utc).isoformat(),
        "signals": signals,
        "events": compact_events,
    }, indent=2)


class Policy:
    """Persists last-fire timestamps so cooldowns survive across process restarts."""

    def __init__(self, target_dir: Path):
        self.state_path = Path(target_dir).resolve() / ".pairwatch" / "state.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def _load(self) -> dict:
        if not self.state_path.exists():
            return {"last_fire": None, "last_fire_by_pattern": {}}
        try:
            return json.loads(self.state_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"last_fire": None, "last_fire_by_pattern": {}}

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self._state, indent=2))

    def _within(self, iso_ts: Optional[str], minutes: int) -> bool:
        if not iso_ts:
            return False
        try:
            then = datetime.fromisoformat(iso_ts)
        except ValueError:
            return False
        return datetime.now(timezone.utc) - then < timedelta(minutes=minutes)

    def allows(self, pattern: str) -> tuple[bool, str]:
        if self._within(self._state.get("last_fire"), GLOBAL_COOLDOWN_MIN):
            return False, f"global cooldown ({GLOBAL_COOLDOWN_MIN}m) active"
        last_for_pattern = self._state.get("last_fire_by_pattern", {}).get(pattern)
        if self._within(last_for_pattern, PER_PATTERN_COOLDOWN_MIN):
            return False, f"per-pattern cooldown ({PER_PATTERN_COOLDOWN_MIN}m) for {pattern}"
        return True, "ok"

    def record_fire(self, pattern: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._state["last_fire"] = now
        self._state.setdefault("last_fire_by_pattern", {})[pattern] = now
        self._save()


class Agent:
    def __init__(self, target_dir: Path, client: Optional[Anthropic] = None):
        load_dotenv()
        if client is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set (put it in .env)")
            client = Anthropic(api_key=api_key)
        self.client = client
        self.policy = Policy(target_dir)

    def _call_claude(self, events: list[dict], signals: dict) -> dict:
        user_text = _serialize_for_prompt(events, signals)
        response = self.client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[DECISION_TOOL],
            tool_choice={"type": "tool", "name": "record_decision"},
            messages=[{"role": "user", "content": user_text}],
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_decision":
                return dict(block.input)
        raise RuntimeError(f"Claude did not return a record_decision tool call: {response.content!r}")

    def decide(self, events: list[dict], signals: dict) -> dict:
        """Returns a decision dict augmented with gating metadata.

        Keys: raw (Claude's raw output), fired (bool), reason (why it was/wasn't fired).
        """
        if not events:
            return {"raw": None, "fired": False, "reason": "no events in window"}
        raw = self._call_claude(events, signals)
        if not raw.get("should_intervene"):
            return {"raw": raw, "fired": False, "reason": "model declined"}
        confidence = float(raw.get("confidence", 0.0))
        if confidence < CONFIDENCE_THRESHOLD:
            return {"raw": raw, "fired": False, "reason": f"confidence {confidence:.2f} < {CONFIDENCE_THRESHOLD}"}
        severity = raw.get("severity", "low")
        if severity == "low":
            return {"raw": raw, "fired": False, "reason": "severity low — log only"}
        pattern = raw.get("pattern", "none")
        allowed, why = self.policy.allows(pattern)
        if not allowed:
            return {"raw": raw, "fired": False, "reason": why}
        self.policy.record_fire(pattern)
        return {"raw": raw, "fired": True, "reason": "gated through"}


if __name__ == "__main__":
    import sys
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="pw-agent-"))
    base = datetime.now(timezone.utc)
    fake_events = []
    for i in range(6):
        fake_events.append({
            "timestamp": (base - timedelta(minutes=9 - i)).isoformat(),
            "path": "/tmp/proj/utils.py",
            "content_hash": f"h{i}",
            "diff": f"--- a\n+++ b\n@@\n-result = parse(x)\n+result = parse(x) or None\n",
            "lines_changed": 0,
        })
    fake_signals = {
        "event_count": len(fake_events),
        "churn": [{"path": "/tmp/proj/utils.py", "save_count": 6, "net_lines_changed": 0, "window_minutes": 10}],
        "stall": [],
        "revert_loop": [],
        "repeated_mistake": [],
    }
    try:
        agent = Agent(tmp)
    except RuntimeError as e:
        print(f"skipping live call: {e}")
        sys.exit(0)
    result = agent.decide(fake_events, fake_signals)
    print(json.dumps(result, indent=2))
