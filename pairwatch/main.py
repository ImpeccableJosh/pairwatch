"""PairWatch CLI entry point. Wires watcher → log → detector → agent → notify."""
from __future__ import annotations

import argparse
import json
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from pairwatch.agent import Agent
from pairwatch.detector import compute_signals
from pairwatch.log import EventLog
from pairwatch.notify import render, render_silent
from pairwatch.providers import available_provider_names
from pairwatch.watcher import SaveEvent, start_watcher


def _drain_events(event_q: "queue.Queue[SaveEvent]", log: EventLog, stop: threading.Event) -> None:
    """Background thread: pull save events off the queue and append to the JSONL log."""
    while not stop.is_set():
        try:
            ev = event_q.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            log.append(ev.to_dict())
        except Exception as exc:
            print(f"[pairwatch] log append failed: {exc}", file=sys.stderr)


def _tick(log: EventLog, agent: Agent, window_minutes: int, target_dir: Path, quiet: bool) -> None:
    events = log.read_recent(window_minutes)
    if not events:
        return
    signals = compute_signals(events, now=datetime.now(timezone.utc))
    try:
        result = agent.decide(events, signals)
    except Exception as exc:
        print(f"[pairwatch] agent call failed: {exc}", file=sys.stderr)
        return

    decisions_path = target_dir / ".pairwatch" / "decisions.jsonl"
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    with decisions_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "fired": result.get("fired"),
            "reason": result.get("reason"),
            "raw": result.get("raw"),
        }) + "\n")

    if not result.get("fired"):
        if result.get("raw") and not quiet:
            render_silent(result.get("raw") or {}, result.get("reason", ""))
        return
    decision = result["raw"]
    if quiet:
        return
    render(decision, target_dir=target_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pairwatch", description="Background pair-programming observer.")
    parser.add_argument("--target-dir", required=True, help="Directory to watch.")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between agent ticks (default 60).")
    parser.add_argument("--window-minutes", type=int, default=15, help="Sliding event window fed to the agent (default 15).")
    parser.add_argument("--quiet", action="store_true", help="Suppress terminal output (interventions still logged).")
    parser.add_argument(
        "--provider",
        choices=available_provider_names() + ["claude", "google", "cf"],
        default=None,
        help="LLM backend to use. Overrides PAIRWATCH_PROVIDER env var. Default: anthropic.",
    )
    args = parser.parse_args(argv)

    target_dir = Path(args.target_dir).resolve()
    if not target_dir.exists():
        print(f"[pairwatch] target dir does not exist: {target_dir}", file=sys.stderr)
        return 2
    if not target_dir.is_dir():
        print(f"[pairwatch] target is not a directory: {target_dir}", file=sys.stderr)
        return 2

    log = EventLog(target_dir)
    agent = Agent(target_dir, provider_name=args.provider)

    event_q: "queue.Queue[SaveEvent]" = queue.Queue()
    stop = threading.Event()

    observer = start_watcher(target_dir, event_q.put)
    drainer = threading.Thread(target=_drain_events, args=(event_q, log, stop), daemon=True)
    drainer.start()

    def _shutdown(*_):
        stop.set()
        observer.stop()
        observer.join(timeout=5)
        print("\n[pairwatch] stopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    mode = "quiet" if args.quiet else "loud"
    print(f"[pairwatch] watching {target_dir}")
    print(f"[pairwatch] tick={args.interval}s  window={args.window_minutes}m  mode={mode}  provider={agent.provider.name}")
    print("[pairwatch] Ctrl-C to stop.")

    next_tick = time.monotonic() + args.interval
    while True:
        time.sleep(0.5)
        if time.monotonic() >= next_tick:
            _tick(log, agent, args.window_minutes, target_dir, args.quiet)
            next_tick = time.monotonic() + args.interval


if __name__ == "__main__":
    sys.exit(main())
