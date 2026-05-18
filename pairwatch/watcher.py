"""File-save watcher. Emits debounced, filtered save events for a target directory."""
from __future__ import annotations

import hashlib
import os
import queue
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

IGNORED_DIR_NAMES = {
    ".git", ".pairwatch", ".venv", "venv", "env",
    ".idea", ".vscode", "node_modules", "__pycache__",
    "dist", "build", ".next", ".turbo", ".cache", "target",
}

IGNORED_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dylib", ".dll",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".zip", ".tar", ".gz", ".tgz", ".jar",
    ".lock", ".log", ".sqlite", ".db",
}


@dataclass
class SaveEvent:
    path: str
    timestamp: str
    content_hash: str
    raw_text: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "timestamp": self.timestamp,
            "content_hash": self.content_hash,
            "raw_text": self.raw_text,
        }


def _is_ignored(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts
    if any(part in IGNORED_DIR_NAMES for part in rel_parts):
        return True
    if path.suffix.lower() in IGNORED_SUFFIXES:
        return True
    return False


def _read_text_safely(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > 1_000_000:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None


class SaveWatcher(FileSystemEventHandler):
    def __init__(self, root: Path, sink: Callable[[SaveEvent], None]):
        self.root = root.resolve()
        self.sink = sink
        self._last_hash: dict[str, str] = {}

    def _handle(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path).resolve()
        if _is_ignored(path, self.root):
            return
        if not path.is_file():
            return
        text = _read_text_safely(path)
        if text is None:
            return
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = str(path)
        if self._last_hash.get(key) == digest:
            return
        self._last_hash[key] = digest
        self.sink(SaveEvent(
            path=str(path),
            timestamp=datetime.now(timezone.utc).isoformat(),
            content_hash=digest,
            raw_text=text,
        ))

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event)


def start_watcher(root: Path, sink: Callable[[SaveEvent], None]) -> Observer:
    handler = SaveWatcher(root, sink)
    observer = Observer()
    observer.schedule(handler, str(root), recursive=True)
    observer.start()
    return observer


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Print save events for a directory.")
    parser.add_argument("--target-dir", required=True)
    args = parser.parse_args()

    target = Path(args.target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)

    q: queue.Queue[SaveEvent] = queue.Queue()
    observer = start_watcher(target, q.put)
    print(f"watching {target} — edit any file to see events (Ctrl-C to stop)")
    try:
        while True:
            try:
                ev = q.get(timeout=1.0)
                print(f"[{ev.timestamp}] {ev.path}  ({len(ev.raw_text)} bytes, sha={ev.content_hash[:8]})")
            except queue.Empty:
                continue
    except KeyboardInterrupt:
        observer.stop()
        observer.join()
        print("stopped.")
