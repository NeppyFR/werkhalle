"""Per-run SQLite file: `<data_dir>/run-<RunId>/werkhalle.db`, WAL mode, writes batched on a dedicated thread.

The event loop never touches the disk for a write: `submit` only puts the statement on a queue. A single writer thread
commits batches, so a burst of trace events costs one transaction. Reads go through one lock-guarded connection and see what
has been committed, which lags `submit` by at most `commit_interval_s`. Call `flush` when a read must see earlier writes.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_FILENAME = "werkhalle.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, t REAL, type TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes(
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    label TEXT,
    t REAL,
    qty REAL,
    unit TEXT,
    attrs TEXT NOT NULL DEFAULT '{}',
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_nodes_type_t ON nodes(type, t);
CREATE TABLE IF NOT EXISTS edges(
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    qty REAL NOT NULL,
    unit TEXT,
    PRIMARY KEY(src, dst)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS memberships(
    group_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    qty REAL NOT NULL,
    unit TEXT,
    PRIMARY KEY(group_id, member_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_memberships_member ON memberships(member_id);
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lots(id TEXT PRIMARY KEY, order_id TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_lots_order ON lots(order_id);
CREATE TABLE IF NOT EXISTS alarms(id TEXT PRIMARY KEY, machine TEXT, t REAL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t REAL,
    wall_ts REAL,
    level TEXT,
    source TEXT,
    text TEXT
);
"""


class _Flush:
    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done = threading.Event()


_STOP = object()


def _safe_run_id(run_id: str) -> str:
    # The RunId comes from the wire, and it becomes a directory name.
    return re.sub(r"[^A-Za-z0-9_.-]", "_", run_id) or "unbenannt"


def run_db_path(data_dir: str | Path, run_id: str) -> Path:
    """The database file of one run: `<data_dir>/run-<RunId>/werkhalle.db`."""
    return Path(data_dir) / f"run-{_safe_run_id(run_id)}" / DB_FILENAME


def remove_older_runs(data_dir: str | Path, keep_run_id: str) -> list[Path]:
    """Delete every `run-*` directory except the given run, best effort. Returns what it removed.

    A directory another process still holds open stays behind on Windows; that is not an error.
    """
    base = Path(data_dir)
    keep = f"run-{_safe_run_id(keep_run_id)}"
    removed = []
    if not base.is_dir():
        return removed
    for entry in base.iterdir():
        if entry.is_dir() and entry.name.startswith("run-") and entry.name != keep:
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed.append(entry)
    return removed


def open_run_db(data_dir: str | Path, run_id: str, *, remove_older: bool = True) -> "RunDb":
    """Open or create the database of a run, and remove the files of older runs unless told otherwise.

    After a run change, close the old `RunDb` first: Windows will not delete a file that is still open.
    """
    db = RunDb(run_db_path(data_dir, run_id))
    if remove_older:
        remove_older_runs(data_dir, run_id)
    return db


class RunDb:
    """The per-run database. Writers call `submit` from any thread; readers call `query` or `read_transaction`."""

    def __init__(self, path: str | Path, *, commit_interval_s: float = 0.1, max_batch: int = 2000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._commit_interval = commit_interval_s
        self._max_batch = max_batch
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._closed = False
        self.write_errors = 0

        bootstrap = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        try:
            bootstrap.execute("PRAGMA journal_mode=WAL")
            bootstrap.executescript(_SCHEMA)
        finally:
            bootstrap.close()

        self._read_lock = threading.RLock()
        self._reader = sqlite3.connect(self.path, timeout=30.0, isolation_level=None, check_same_thread=False)
        self._reader.row_factory = sqlite3.Row
        self._writer = threading.Thread(target=self._write_loop, name="werkhalle-db", daemon=True)
        self._writer.start()

    # --- writing ---

    def submit(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Queue one statement. Never blocks and never touches the disk."""
        if self._closed:
            raise RuntimeError("database is closed")
        self._queue.put((sql, tuple(params)))

    def flush(self, timeout: float = 30.0) -> bool:
        """Block until everything submitted so far is committed. Do not call it on the event loop in production paths."""
        marker = _Flush()
        self._queue.put(marker)
        return marker.done.wait(timeout)

    def close(self) -> None:
        """Commit what is queued, stop the writer and close both connections. A second call does nothing."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(_STOP)
        self._writer.join(timeout=30.0)
        with self._read_lock:
            self._reader.close()

    def _write_loop(self) -> None:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            while True:
                first = self._queue.get()
                if first is _STOP:
                    return
                if isinstance(first, _Flush):
                    first.done.set()
                    continue
                stop = self._write_batch(conn, first)
                if stop:
                    return
        finally:
            conn.close()

    def _write_batch(self, conn: sqlite3.Connection, first: tuple) -> bool:
        """Commit `first` and whatever arrives within the commit interval. Returns True when asked to stop."""
        pending = [first]
        markers: list[_Flush] = []
        stop = False
        deadline = time.monotonic() + self._commit_interval
        while len(pending) < self._max_batch:
            remaining = deadline - time.monotonic()
            try:
                item = self._queue.get(timeout=remaining) if remaining > 0 else self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:
                stop = True
                break
            if isinstance(item, _Flush):
                markers.append(item)
                break
            pending.append(item)
        self._commit(conn, pending)
        for marker in markers:
            marker.done.set()
        return stop

    def _commit(self, conn: sqlite3.Connection, statements: list[tuple]) -> None:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for sql, params in statements:
                conn.execute(sql, params)
            conn.execute("COMMIT")
        except sqlite3.Error:
            logger.exception("batch of %d statements failed, retrying one by one", len(statements))
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            self._commit_each(conn, statements)

    def _commit_each(self, conn: sqlite3.Connection, statements: list[tuple]) -> None:
        # One bad statement must not take a whole batch of genealogy with it.
        for sql, params in statements:
            try:
                conn.execute(sql, params)
            except sqlite3.Error:
                self.write_errors += 1
                logger.exception("statement failed: %s", sql)

    # --- reading ---

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """Run one read. Thread-safe. It blocks briefly on SQLite, so prefer `asyncio.to_thread` for big ones."""
        with self._read_lock:
            return self._reader.execute(sql, tuple(params)).fetchall()

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """A connection with one consistent snapshot, for reads that take several queries."""
        with self._read_lock:
            self._reader.execute("BEGIN")
            try:
                yield self._reader
            finally:
                self._reader.execute("COMMIT")

    # --- mirrors for the runtime: the order book and alarms live in memory, these keep the history ---

    def set_meta(self, key: str, value: Any) -> None:
        """Store a small JSON-serialisable value under a key, for counters that must survive an MES restart."""
        self.submit("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, json.dumps(value, ensure_ascii=False)))

    def get_meta(self, key: str, default: Any = None) -> Any:
        """Read a value stored with `set_meta`."""
        rows = self.query("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def save_order(self, order: Mapping[str, Any]) -> None:
        """Insert or replace an order. `order["id"]` is the key; the rest is stored as JSON."""
        self.submit("INSERT OR REPLACE INTO orders(id, data) VALUES(?, ?)",
                    (order["id"], json.dumps(order, ensure_ascii=False)))

    def load_orders(self) -> list[dict]:
        """All stored orders, by id."""
        return [json.loads(r["data"]) for r in self.query("SELECT data FROM orders ORDER BY id")]

    def save_lot(self, lot: Mapping[str, Any]) -> None:
        """Insert or replace a lot. Needs `lot["id"]`; `lot.get("order_id")` is indexed."""
        self.submit("INSERT OR REPLACE INTO lots(id, order_id, data) VALUES(?, ?, ?)",
                    (lot["id"], lot.get("order_id"), json.dumps(lot, ensure_ascii=False)))

    def load_lots(self, order_id: str | None = None) -> list[dict]:
        """All stored lots, or those of one order."""
        if order_id is None:
            rows = self.query("SELECT data FROM lots ORDER BY id")
        else:
            rows = self.query("SELECT data FROM lots WHERE order_id = ? ORDER BY id", (order_id,))
        return [json.loads(r["data"]) for r in rows]

    def save_alarm(self, row: Mapping[str, Any]) -> None:
        """Insert or replace an alarm row as produced by `AlarmTracker`. Pass it as the tracker's `on_change`."""
        self.submit("INSERT OR REPLACE INTO alarms(id, machine, t, data) VALUES(?, ?, ?, ?)",
                    (row["id"], row.get("machine"), row.get("t"), json.dumps(row, ensure_ascii=False)))

    def load_alarms(self, limit: int = 200) -> list[dict]:
        """Stored alarm rows, newest first."""
        rows = self.query("SELECT data FROM alarms ORDER BY t DESC, id DESC LIMIT ?", (limit,))
        return [json.loads(r["data"]) for r in rows]

    def append_log(self, entry: Mapping[str, Any]) -> None:
        """Append a log entry `{t, wall_ts, level, source, text}`."""
        self.submit("INSERT INTO log(t, wall_ts, level, source, text) VALUES(?, ?, ?, ?, ?)",
                    (entry.get("t"), entry.get("wall_ts"), entry.get("level"), entry.get("source"), entry.get("text")))

    def load_log(self, limit: int = 200) -> list[dict]:
        """Stored log entries, newest first."""
        rows = self.query("SELECT t, wall_ts, level, source, text FROM log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def chunked(items: Iterable[str], size: int = 400) -> Iterator[list[str]]:
    """Split ids into pieces small enough for one SQL `IN (...)`."""
    chunk: list[str] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


__all__ = ["DB_FILENAME", "RunDb", "chunked", "open_run_db", "remove_older_runs", "run_db_path"]
