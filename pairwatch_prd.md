# PairWatch — PRD

## One-liner
A background agent that watches your local code editing activity, detects patterns of friction (repeated rewrites, long stalls, frequent reverts), and surfaces short, useful observations **only when** it has high confidence the interruption is worth it.

## MVP Scope (build this first, in ~2 hours)
A Python CLI tool that:
1. Watches a target directory for file save events.
2. Logs each save with file path, timestamp, full content hash, and diff vs. the previous save.
3. Every 60 seconds, pulls a sliding window of recent events and computes lightweight pattern signals.
4. Sends signals + raw event window to the Claude API. Claude returns a structured JSON decision: should it intervene, what pattern, confidence, severity, message.
5. If intervention fires, prints a formatted message to the terminal.
6. Persists an append-only JSONL event log across sessions.

## Architecture

### Components
- **`watcher.py`** — `watchdog` observer on a target dir; emits events on save.
- **`log.py`** — Append-only JSONL: `{timestamp, file, content_hash, diff, lines_changed}`. Also exposes `read_recent(minutes)`.
- **`detector.py`** — Computes the lightweight pattern signals (see below) from the recent event window.
- **`agent.py`** — Calls Claude with the API contract below. Forces JSON output.
- **`notify.py`** — Renders interventions to terminal (color-coded by severity).
- **`main.py`** — Wires everything together. Polls every 60s. Reads `--target-dir` flag and `.env` for `ANTHROPIC_API_KEY`.

### Pattern Taxonomy (start with these 4)
1. **Churn** — Same file edited > 5 times in 10 minutes with net line delta < 10 (rewriting the same logic).
2. **Stall** — No saves for > 8 minutes after a period of active editing on the same file.
3. **Revert Loop** — Current save's diff mostly undoes a recent prior save's diff.
4. **Repeated Mistake** — Same structural issue (e.g., missing null check) appears in multiple recent edits.

### Intervention Policy
- **Rate limit:** Max 1 intervention per 15 minutes globally.
- **Confidence threshold:** Claude must return `confidence >= 0.7` to fire.
- **Severity routing:** `low` → silent log only; `medium` → terminal print; `high` → terminal print + (later) desktop notification.
- **Per-pattern cooldown:** Same pattern can't fire twice within 30 minutes.
- **Quiet mode:** Toggleable via `--quiet` flag.

### Success Criteria
- **Precision:** ≥ 60% of fired interventions marked "useful" by the user (thumbs up/down logged).
- **Annoyance ceiling:** ≤ 3 interventions per hour of active coding.
- **Coverage (qualitative):** User reports at least 1 in 3 "stuck moments" in a session were caught.

## API Contract — Claude Intervention Call

**Input:**
- System prompt: defines the 4 patterns, the intervention policy, and forces the output schema.
- User message: serialized recent event window + computed pattern signals from `detector.py`.

**Output (forced JSON):**
```json
{
  "should_intervene": true,
  "pattern": "churn",
  "confidence": 0.82,
  "severity": "medium",
  "message": "You've rewritten the parsing block in utils.py four times in the last 10 minutes — might be worth stepping back and sketching the data flow before continuing.",
  "evidence": "5 saves of utils.py in 10min, net +3 lines"
}
```

If `should_intervene` is false, all other fields can be empty. The caller in `agent.py` enforces the rate-limit, confidence-threshold, and cooldown rules BEFORE displaying anything.

## Stretch (post-MVP, for the rest of the quarter)
- VS Code extension for keystroke / cursor / undo granularity (much richer signal than file saves).
- Cross-session memory ("you always forget null handling around DB calls").
- User feedback loop (thumbs up/down logged, used to tune thresholds).
- Desktop notifications + focus/quiet mode toggles.
- Web dashboard for reviewing detected patterns over time.

## Build order for Claude Code
1. Scaffold project. Deps: `anthropic`, `watchdog`, `python-dotenv`.
2. `watcher.py` — print save events to stdout. Test it standalone.
3. `log.py` — JSONL append + `read_recent(minutes)`. Test standalone.
4. `detector.py` — implement the 4 signal computations. Test on a fake event log.
5. `agent.py` — Claude call with the contract above. Force JSON. Test with a hand-crafted event window.
6. `notify.py` — colored terminal output.
7. `main.py` — wire it together, 60-second polling loop, CLI flags.
8. `.env.example` + a `README.md` with run instructions.

## Out of scope for the 2-hour MVP
- VS Code extension
- Desktop notifications
- Multi-session memory
- Web UI
- User feedback loop (just log to JSONL for now; wire UI later)
