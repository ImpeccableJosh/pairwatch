# PairWatch

A background agent that watches your local code editing activity, detects friction patterns (churn, stalls, revert loops, repeated mistakes), and surfaces short, useful observations — only when it has high confidence the interruption is worth it.

This repo is the **MVP**: a Python CLI that watches a directory for file saves and calls an LLM every 60 seconds with a window of recent edits. The longer-term target is a VS Code extension; see `pairwatch_prd.md`.

## Install

Requires Python 3.10+ and an API key for at least one of:

- [Anthropic / Claude](https://console.anthropic.com/) (default)
- [Google Gemini](https://aistudio.google.com/apikey)
- [Cloudflare Workers AI](https://dash.cloudflare.com/profile/api-tokens) (needs both an API token with "Workers AI" permission and your account ID)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: set PAIRWATCH_PROVIDER and fill in keys for that provider
```

## Run

```bash
python -m pairwatch.main --target-dir /path/to/your/project
```

Flags:

| Flag | Default | Description |
|---|---|---|
| `--target-dir` | (required) | Directory to watch (recursive). |
| `--interval` | `60` | Seconds between agent ticks. Lower for testing. |
| `--window-minutes` | `15` | Sliding event window fed to the agent. |
| `--quiet` | off | Suppress terminal output; interventions still logged to `.pairwatch/interventions.jsonl`. |
| `--provider` | from env | Override `PAIRWATCH_PROVIDER`. One of `anthropic`, `gemini`, `cloudflare`. |

The watcher ignores `.git`, `.pairwatch`, `node_modules`, `__pycache__`, virtualenvs, build dirs, and common binary file types.

## Swapping providers

PairWatch ships with three swappable LLM backends. Pick one via `PAIRWATCH_PROVIDER` in `.env` or per-run with `--provider`.

| Provider | Default model | Env vars required |
|---|---|---|
| `anthropic` | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| `gemini` | `gemini-2.5-flash` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) |
| `cloudflare` | `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` |

Override the model for any provider via `PAIRWATCH_<PROVIDER>_MODEL` (e.g. `PAIRWATCH_GEMINI_MODEL=gemini-2.5-pro`). All three providers return the same decision schema; the policy gating (confidence threshold, cooldowns, rate limit) is identical regardless of backend.

```bash
# one-off run against Gemini
python -m pairwatch.main --target-dir . --provider gemini

# one-off run against Cloudflare's Llama 3.3
python -m pairwatch.main --target-dir . --provider cloudflare
```

## What it does

Every save inside the target directory is appended to `<target>/.pairwatch/events.jsonl` with a timestamp, content hash, unified diff vs. the prior save of that file, and net line change.

Every `--interval` seconds the main loop:
1. Reads the recent event window from the JSONL log.
2. Computes lightweight signals for four patterns (`detector.py`).
3. Sends the events + signals to Claude (`claude-sonnet-4-6`), which returns a structured decision via tool use.
4. Applies the intervention policy (confidence ≥ 0.7, global 15-min rate limit, per-pattern 30-min cooldown, severity routing).
5. Prints a banner to the terminal if the decision survives gating.

All raw decisions (fired or not) are logged to `<target>/.pairwatch/decisions.jsonl` for later review.

## Patterns

| Pattern | Trigger heuristic |
|---|---|
| **churn** | >5 saves of same file in 10 min, |net lines| < 10 |
| **stall** | >8 min of silence after a burst of activity |
| **revert_loop** | A recent diff largely undoes a prior diff on the same file |
| **repeated_mistake** | Same structural issue (e.g. bare `except`, missing null check) appears in multiple recent edits |

Heuristics are inputs to Claude, not verdicts — Claude makes the final call.

## Project layout

```
pairwatch/
├── watcher.py      # watchdog observer + filters
├── log.py          # append-only JSONL + read_recent + diff computation
├── detector.py     # 4 pattern signal computations
├── agent.py        # Claude call (tool-use forced JSON) + policy gating
├── notify.py       # ANSI-colored terminal banners
└── main.py         # CLI entry point + 60s poll loop
```

Each module has an `if __name__ == "__main__"` smoke test:

```bash
python -m pairwatch.watcher --target-dir /tmp/pw-test  # print events to stdout
python -m pairwatch.log                                 # write & read fake events
python -m pairwatch.detector                            # signals on synthetic data
python -m pairwatch.agent                               # one live Claude call (needs API key)
python -m pairwatch.notify                              # render sample banners
```

## State files

| File | Purpose |
|---|---|
| `.pairwatch/events.jsonl` | Append-only save log |
| `.pairwatch/decisions.jsonl` | Every Claude decision (fired or gated) |
| `.pairwatch/interventions.jsonl` | Only decisions that fired |
| `.pairwatch/state.json` | Last-fire timestamps for cooldown enforcement |

All four are inside the watched directory and ignored by the watcher itself. Add `.pairwatch/` to your project's `.gitignore`.

## Limitations of the MVP

- File-save granularity only (no keystrokes, no cursor, no undo). The VS Code extension is the natural next step.
- No cross-session memory beyond cooldown timestamps.
- No feedback loop yet (thumbs up/down) — interventions are logged but not rated.
- No desktop notifications.

See "Stretch" in `pairwatch_prd.md` for the post-MVP roadmap.
