"""Policy gating layer (rate limit, cooldowns, confidence threshold).

The actual LLM call is delegated to a Provider — see pairwatch/providers.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from pairwatch.providers import Provider, make_provider

GLOBAL_COOLDOWN_MIN = 15
PER_PATTERN_COOLDOWN_MIN = 30
CONFIDENCE_THRESHOLD = 0.7


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

    def globally_blocked(self) -> tuple[bool, str]:
        if self._within(self._state.get("last_fire"), GLOBAL_COOLDOWN_MIN):
            return True, f"global cooldown ({GLOBAL_COOLDOWN_MIN}m) active"
        return False, ""

    def allows(self, pattern: str) -> tuple[bool, str]:
        blocked, why = self.globally_blocked()
        if blocked:
            return False, why
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
    def __init__(self, target_dir: Path, provider: Optional[Provider] = None, provider_name: Optional[str] = None):
        load_dotenv()
        self.provider: Provider = provider or make_provider(provider_name)
        self.policy = Policy(target_dir)

    def decide(self, events: list[dict], signals: dict) -> dict:
        """Returns {raw, fired, reason, provider}. `raw` may be None if pre-gated."""
        if not events:
            return {"raw": None, "fired": False, "reason": "no events in window", "provider": self.provider.name}

        # Pre-gate on global cooldown — skips the LLM call entirely when active.
        blocked, why = self.policy.globally_blocked()
        if blocked:
            return {"raw": None, "fired": False, "reason": why, "provider": self.provider.name}

        raw = self.provider.call(events, signals)
        base = {"raw": raw, "provider": self.provider.name}

        if not raw.get("should_intervene"):
            return {**base, "fired": False, "reason": "model declined"}
        confidence = float(raw.get("confidence", 0.0))
        if confidence < CONFIDENCE_THRESHOLD:
            return {**base, "fired": False, "reason": f"confidence {confidence:.2f} < {CONFIDENCE_THRESHOLD}"}
        severity = raw.get("severity", "low")
        if severity in ("low", "none"):
            return {**base, "fired": False, "reason": f"severity {severity} — log only"}
        pattern = raw.get("pattern", "none")
        allowed, gate_reason = self.policy.allows(pattern)
        if not allowed:
            return {**base, "fired": False, "reason": gate_reason}
        self.policy.record_fire(pattern)
        return {**base, "fired": True, "reason": "gated through"}


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
            "diff": "--- a\n+++ b\n@@\n-result = parse(x)\n+result = parse(x) or None\n",
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
