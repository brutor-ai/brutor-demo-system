"""JSON file store for applications.

One file, `applications.json`, holds every application plus the generator counters.
Writes are atomic (write to a temporary file in the same directory, then `os.replace`)
and every read-modify-write runs under a re-entrant lock so the generator thread and
the request handlers never interleave.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("applications_mcp.store")

STORE_VERSION = 1
FILE_NAME = "applications.json"


def resolve_data_dir(preferred: str | None = None) -> Path:
    """Pick the data directory.

    `DATA_DIR` (default `/data`) is used when it exists or can be created and is
    writable. Otherwise fall back to `./data` relative to the working directory, which
    is what happens when the server runs on a developer machine without a `/data`
    volume.
    """
    candidates = [preferred or os.environ.get("DATA_DIR") or "/data", "./data"]
    for candidate in candidates:
        path = Path(candidate)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return path.resolve()
        except OSError as exc:
            log.warning("data dir %s not writable (%s), trying next", path, exc)
    raise RuntimeError("no writable data directory found (tried DATA_DIR and ./data)")


def _empty_state() -> dict[str, Any]:
    return {
        "version": STORE_VERSION,
        "generator": {"seq": 0, "batches": 0, "seed": None},
        "applications": {},
    }


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / FILE_NAME
        self._lock = threading.RLock()
        self._mtime_ns: int | None = None
        self._state: dict[str, Any] = self._load()

    # ---- persistence -------------------------------------------------------------------

    def _stat_mtime_ns(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def _load(self) -> dict[str, Any]:
        self._mtime_ns = self._stat_mtime_ns()
        if self._mtime_ns is None:
            return _empty_state()
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError) as exc:
            log.error("could not read %s (%s); starting with an empty store", self.path, exc)
            return _empty_state()
        base = _empty_state()
        base["generator"].update(state.get("generator") or {})
        base["applications"] = dict(state.get("applications") or {})
        return base

    def _write(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self._mtime_ns = self._stat_mtime_ns()

    def _refresh(self) -> None:
        """Reload if another process (the `generate` CLI) rewrote the file."""
        if self._stat_mtime_ns() != self._mtime_ns:
            self._state = self._load()

    # ---- access ------------------------------------------------------------------------

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def mutate(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        """Run `fn(state)` under the lock and persist afterwards."""
        with self._lock:
            self._refresh()
            result = fn(self._state)
            self._write()
            return result

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            return json.loads(json.dumps(self._state))

    def get(self, application_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._refresh()
            record = self._state["applications"].get(application_id)
            return json.loads(json.dumps(record)) if record is not None else None

    def all_applications(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh()
            return [json.loads(json.dumps(r)) for r in self._state["applications"].values()]

    def count(self) -> int:
        with self._lock:
            self._refresh()
            return len(self._state["applications"])

    def generator_state(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            return dict(self._state["generator"])
