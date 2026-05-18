"""Swappable LLM providers for PairWatch.

All providers return the same decision shape (see DECISION_SCHEMA). Selected at
runtime via the PAIRWATCH_PROVIDER env var or --provider CLI flag.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Protocol

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

When you do not intervene, set pattern to "none", severity to "none", confidence to your certainty in that decision, and leave message + evidence as empty strings."""

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_intervene": {"type": "boolean"},
        "pattern": {
            "type": "string",
            "enum": ["churn", "stall", "revert_loop", "repeated_mistake", "none"],
        },
        "confidence": {"type": "number"},
        "severity": {"type": "string", "enum": ["low", "medium", "high", "none"]},
        "message": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": ["should_intervene", "pattern", "confidence", "severity", "message", "evidence"],
}


def _truncate(events: list[dict], limit: int = MAX_EVENTS_IN_PROMPT) -> list[dict]:
    return events if len(events) <= limit else events[-limit:]


def serialize_user_message(events: list[dict], signals: dict) -> str:
    compact = []
    for ev in _truncate(events):
        compact.append({
            "ts": ev["timestamp"],
            "path": ev["path"],
            "lines_changed": ev.get("lines_changed", 0),
            "diff_excerpt": (ev.get("diff", "") or "")[:600],
        })
    return json.dumps({
        "now": datetime.now(timezone.utc).isoformat(),
        "signals": signals,
        "events": compact,
    }, indent=2)


class Provider(Protocol):
    name: str

    def call(self, events: list[dict], signals: dict) -> dict:
        """Return a dict matching DECISION_SCHEMA."""


# ---------------------------------------------------------------------------
# Anthropic (Claude)
# ---------------------------------------------------------------------------

class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        from anthropic import Anthropic  # local import keeps Gemini-only/CF-only installs lean

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self.client = Anthropic(api_key=api_key)
        self.model = os.environ.get("PAIRWATCH_ANTHROPIC_MODEL", "claude-sonnet-4-6")

    def call(self, events: list[dict], signals: dict) -> dict:
        user_text = serialize_user_message(events, signals)
        tool = {
            "name": "record_decision",
            "description": "Record whether to intervene and the intervention details.",
            "input_schema": DECISION_SCHEMA,
        }
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_decision"},
            messages=[{"role": "user", "content": user_text}],
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_decision":
                return dict(block.input)
        raise RuntimeError(f"Anthropic did not return a tool call: {response.content!r}")


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------

class GeminiProvider:
    name = "gemini"

    def __init__(self) -> None:
        from google import genai  # noqa: F401 — imported here so providers.py works without google-genai installed when not in use

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")
        from google.genai import Client  # type: ignore[import-not-found]
        self.client = Client(api_key=api_key)
        self.model = os.environ.get("PAIRWATCH_GEMINI_MODEL", "gemini-2.5-flash")

    def call(self, events: list[dict], signals: dict) -> dict:
        from google.genai import types  # type: ignore[import-not-found]

        user_text = serialize_user_message(events, signals)
        response = self.client.models.generate_content(
            model=self.model,
            contents=user_text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=DECISION_SCHEMA,
                temperature=0.3,
            ),
        )
        text = response.text
        if not text:
            raise RuntimeError(f"Gemini returned empty response: {response!r}")
        return json.loads(text)


# ---------------------------------------------------------------------------
# Cloudflare Workers AI
# ---------------------------------------------------------------------------

class CloudflareProvider:
    name = "cloudflare"

    def __init__(self) -> None:
        import httpx  # comes along with anthropic/google-genai; explicit dep in requirements.txt

        api_token = os.environ.get("CLOUDFLARE_API_TOKEN")
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        if not api_token or not account_id:
            raise RuntimeError("CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID must both be set")
        self.api_token = api_token
        self.account_id = account_id
        self.model = os.environ.get(
            "PAIRWATCH_CLOUDFLARE_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
        )
        self.client = httpx.Client(timeout=60.0)

    def _endpoint(self) -> str:
        return (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self.account_id}/ai/run/{self.model}"
        )

    def call(self, events: list[dict], signals: dict) -> dict:
        user_text = serialize_user_message(events, signals)
        payload = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": DECISION_SCHEMA,
            },
            "max_tokens": 1024,
            "temperature": 0.3,
        }
        resp = self.client.post(
            self._endpoint(),
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success", True):
            raise RuntimeError(f"Cloudflare AI error: {body.get('errors')}")
        result = body.get("result", {})
        raw = result.get("response")
        if raw is None:
            raise RuntimeError(f"Cloudflare AI returned no response field: {body!r}")
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, dict):
            return raw
        raise RuntimeError(f"Unexpected Cloudflare response shape: {type(raw).__name__}: {raw!r}")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "claude": AnthropicProvider,
    "gemini": GeminiProvider,
    "google": GeminiProvider,
    "cloudflare": CloudflareProvider,
    "cf": CloudflareProvider,
}


def available_provider_names() -> list[str]:
    return ["anthropic", "gemini", "cloudflare"]


def make_provider(name: str | None = None) -> Provider:
    name = (name or os.environ.get("PAIRWATCH_PROVIDER") or "anthropic").lower()
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown provider {name!r}. Use one of: {', '.join(available_provider_names())}"
        )
    return cls()  # type: ignore[abstract]
