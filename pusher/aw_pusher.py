#!/usr/bin/env python3
"""
RR-IT Insight — ActivityWatch pusher.

Runs on each monitored Windows PC alongside ActivityWatch. Polls the local
AW REST API, resolves each window event's productivity-relevant identity
(app name, or website domain when the window is a browser and a matching
web-watcher event exists) and idle state (from the AFK watcher), queues the
result in a local SQLite outbox, and flushes that outbox to the RR-IT
Insight ingestion endpoint in Zite.

Design notes (see RR-IT Insight spec, sections 2 and 7):
  - Local-first: nothing is dropped if the network or Zite is unreachable.
    Events are read from AW and written to a local outbox table on every
    poll; a separate flush step tries to send the outbox and only deletes
    rows Zite has actually accepted.
  - Offline queue + backfill: the outbox is a durable SQLite file, so a
    laptop that's offline for days just accumulates rows and catches up
    once connectivity returns — no separate "backfill mode" needed, since
    AW itself never loses local history and our watermark resumes exactly
    where it left off.
  - Retries use exponential backoff and never crash the loop: a bad
    response is logged and retried next cycle.

Deployment: silent install via RMM, run as a persistent process (Windows
service via NSSM, or a Scheduled Task set to run at logon / on an interval
with "if already running, do nothing"). See README.md alongside this file.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import socket
import sqlite3
import sys
import time
import getpass
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# --------------------------------------------------------------------------
# Single-instance lock — a Windows named mutex so only one copy of this
# pusher is ever actually doing work at once. Confirmed live: the
# installer's Scheduled Task fires an immediate /Run on every install AND
# reinstall, on top of the task's own "run at logon" trigger, with no check
# for an existing copy already running — and since this script loops
# forever, each extra launch just piles up as another permanent,
# fully-independent pusher. That pileup (2, then 3+ aw_pusher.exe
# processes accumulating across install/reinstall/manual-Run cycles) was
# behind a real incident where local activity tracking appeared to freeze
# entirely — most likely several processes contending for the same
# outbox/watermark SQLite file. A reboot cleared it that time; this lock
# stops it from being able to happen at all.
# --------------------------------------------------------------------------

SINGLE_INSTANCE_MUTEX_NAME = "Global\\RRITInsightPusherSingleInstance"
ERROR_ALREADY_EXISTS = 183


def acquire_single_instance_lock() -> bool:
    """Returns True if this process is the only one holding the lock (safe
    to proceed), False if another copy already holds it (this process
    should exit without doing any work). The handle is deliberately never
    closed — it releases automatically when this process exits, which is
    exactly when we want the lock freed. If mutex creation itself fails
    for some reason, fail OPEN (return True) rather than refuse to run the
    pusher at all."""
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        if not handle:
            return True
        return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _app_dir() -> Path:
    """Directory the pusher should treat as "home" for its files.

    A normal `python aw_pusher.py` run uses the script's own folder. A
    PyInstaller --onefile build is different: __file__ resolves to a
    temp extraction folder (_MEIxxxxx), not the real .exe's folder, so
    anything relying on Path(__file__) silently looks in the wrong place
    the moment the exe is run without an explicit config path argument.
    sys.executable is the one that's reliable in a frozen build.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


# ProgramData rather than the exe's own folder (often Program Files) so this
# works whether the pusher runs elevated (during testing) or as a normal,
# non-admin client user at logon — Program Files isn't writable by standard
# users, ProgramData is.
_DATA_DIR = Path(os.environ.get("PROGRAMDATA", str(_app_dir()))) / "RR-IT Insight"

DEFAULT_CONFIG_PATH = _app_dir() / "config.json"
DEFAULT_STATE_DIR = _DATA_DIR / "state"
DEFAULT_LOG_PATH = _DATA_DIR / "pusher.log"

BROWSER_APP_NAMES = {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "chrome", "msedge", "firefox", "brave", "opera",
}

POLL_BATCH_LIMIT = 1000     # AW events fetched per bucket per poll
SEND_BATCH_SIZE = 200       # events per HTTP POST to Zite
MAX_BACKOFF_SECONDS = 300


@dataclass
class Config:
    aw_api_url: str
    zite_ingest_url: str
    api_key: str
    client_id: str
    poll_interval_seconds: int
    hostname: str
    user_name: Optional[str]

    @staticmethod
    def load(path: Path) -> "Config":
        if not path.exists():
            raise SystemExit(
                f"Config file not found: {path}\n"
                f"Copy config.example.json to config.json and fill it in."
            )
        raw = json.loads(path.read_text())
        return Config(
            aw_api_url=raw.get("aw_api_url", "http://localhost:5600").rstrip("/"),
            zite_ingest_url=raw["zite_ingest_url"].rstrip("/"),
            api_key=raw["api_key"],
            client_id=raw["client_id"],
            poll_interval_seconds=int(raw.get("poll_interval_seconds", 30)),
            hostname=raw.get("hostname") or socket.gethostname(),
            user_name=raw.get("user_name") or _default_user_name(),
        )


def _default_user_name() -> Optional[str]:
    try:
        return getpass.getuser()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Local state (watermarks + outbox) — a small SQLite file next to the script
# --------------------------------------------------------------------------

class State:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS watermarks (
                bucket_id TEXT PRIMARY KEY,
                last_synced_iso TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_or_domain TEXT NOT NULL,
                title TEXT,
                url TEXT,
                timestamp_iso TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                idle_flag INTEGER NOT NULL DEFAULT 0,
                queued_at_iso TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_progress (
                bucket_id TEXT PRIMARY KEY,
                last_timestamp_iso TEXT NOT NULL,
                duration_sent REAL NOT NULL
            );
            """
        )
        self.conn.commit()
        # Migration: an outbox table created by an older version of this
        # script won't have the "url" column yet — CREATE TABLE IF NOT
        # EXISTS above is a no-op on an existing table, so add it here.
        try:
            self.conn.execute("ALTER TABLE outbox ADD COLUMN url TEXT")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # already has the column

    def get_watermark(self, bucket_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT last_synced_iso FROM watermarks WHERE bucket_id = ?", (bucket_id,)
        ).fetchone()
        return row[0] if row else None

    def set_watermark(self, bucket_id: str, iso_ts: str) -> None:
        self.conn.execute(
            """
            INSERT INTO watermarks (bucket_id, last_synced_iso) VALUES (?, ?)
            ON CONFLICT(bucket_id) DO UPDATE SET last_synced_iso = excluded.last_synced_iso
            """,
            (bucket_id, iso_ts),
        )
        self.conn.commit()

    def enqueue(self, events: list[dict]) -> None:
        if not events:
            return
        now = _now_iso()
        self.conn.executemany(
            """
            INSERT INTO outbox (app_or_domain, title, url, timestamp_iso, duration_seconds, idle_flag, queued_at_iso)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    e["appOrDomain"],
                    e.get("title"),
                    e.get("url"),
                    e["timestamp"],
                    e["durationSeconds"],
                    int(e.get("idleFlag", False)),
                    now,
                )
                for e in events
            ],
        )
        self.conn.commit()

    def peek_batch(self, limit: int) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            "SELECT * FROM outbox ORDER BY id ASC LIMIT ?", (limit,)
        )
        return cur.fetchall()

    def delete_ids(self, ids: list[int]) -> None:
        if not ids:
            return
        qmarks = ",".join("?" for _ in ids)
        self.conn.execute(f"DELETE FROM outbox WHERE id IN ({qmarks})", ids)
        self.conn.commit()

    def outbox_size(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def get_progress(self, bucket_id: str) -> tuple[Optional[str], float]:
        row = self.conn.execute(
            "SELECT last_timestamp_iso, duration_sent FROM event_progress WHERE bucket_id = ?",
            (bucket_id,),
        ).fetchone()
        return (row[0], row[1]) if row else (None, 0.0)

    def set_progress(self, bucket_id: str, last_timestamp_iso: str, duration_sent: float) -> None:
        self.conn.execute(
            """
            INSERT INTO event_progress (bucket_id, last_timestamp_iso, duration_sent) VALUES (?, ?, ?)
            ON CONFLICT(bucket_id) DO UPDATE SET last_timestamp_iso = excluded.last_timestamp_iso,
                                                  duration_sent = excluded.duration_sent
            """,
            (bucket_id, last_timestamp_iso, duration_sent),
        )
        self.conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# ActivityWatch client
# --------------------------------------------------------------------------

class AWClient:
    def __init__(self, base_url: str, session: requests.Session):
        self.base_url = base_url
        self.session = session

    def list_buckets(self) -> dict:
        r = self.session.get(f"{self.base_url}/api/0/buckets/", timeout=10)
        r.raise_for_status()
        return r.json()

    def get_events(self, bucket_id: str, start_iso: Optional[str], limit: int) -> list[dict]:
        params = {"limit": limit}
        if start_iso:
            params["start"] = start_iso
        r = self.session.get(
            f"{self.base_url}/api/0/buckets/{bucket_id}/events", params=params, timeout=15
        )
        r.raise_for_status()
        # AW returns newest-first; we want oldest-first so watermarks advance monotonically.
        events = r.json()
        events.sort(key=lambda e: e["timestamp"])
        return events


def pick_buckets(buckets: dict, hostname: str) -> tuple[Optional[str], Optional[str], list[str]]:
    """Return (window_bucket_id, afk_bucket_id, [web_bucket_ids])."""
    window_id = afk_id = None
    web_ids: list[str] = []
    for bucket_id, meta in buckets.items():
        btype = meta.get("type", "")
        if btype == "currentwindow":
            window_id = bucket_id
        elif btype == "afkstatus":
            afk_id = bucket_id
        elif btype in ("web.tab.current", "currentwindow.web") or "web" in bucket_id:
            web_ids.append(bucket_id)
    return window_id, afk_id, web_ids


# --------------------------------------------------------------------------
# Event transformation
# --------------------------------------------------------------------------

def _parse_ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def build_afk_intervals(afk_events: list[dict]) -> list[tuple[datetime, datetime]]:
    """Return a list of (start, end) intervals where the user was AFK."""
    intervals = []
    for e in afk_events:
        if e.get("data", {}).get("status") == "afk":
            start = _parse_ts(e["timestamp"])
            end = start.timestamp() + e.get("duration", 0)
            intervals.append((start, datetime.fromtimestamp(end, tz=timezone.utc)))
    return intervals


def overlaps_afk(start: datetime, duration_s: float, afk_intervals: list[tuple[datetime, datetime]]) -> bool:
    if not afk_intervals:
        return False
    end = datetime.fromtimestamp(start.timestamp() + duration_s, tz=timezone.utc)
    mid = datetime.fromtimestamp((start.timestamp() + end.timestamp()) / 2, tz=timezone.utc)
    for a_start, a_end in afk_intervals:
        if a_start <= mid <= a_end:
            return True
    return False


def build_web_domain_lookup(web_events: list[dict]) -> list[tuple[datetime, datetime, str, str, str]]:
    """Return (start, end, domain, title, url) for each web-watcher event."""
    out = []
    for e in web_events:
        data = e.get("data", {})
        url = data.get("url")
        if not url:
            continue
        domain = _extract_domain(url)
        start = _parse_ts(e["timestamp"])
        end = datetime.fromtimestamp(start.timestamp() + e.get("duration", 0), tz=timezone.utc)
        out.append((start, end, domain, data.get("title", ""), url))
    return out


def _extract_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        netloc = urlparse(url).netloc
        return netloc.replace("www.", "") or url
    except Exception:
        return url


def find_domain_for_window(
    start: datetime, duration_s: float, web_lookup: list[tuple[datetime, datetime, str, str, str]]
) -> Optional[tuple[str, str, str]]:
    end = datetime.fromtimestamp(start.timestamp() + duration_s, tz=timezone.utc)
    best = None
    best_overlap = 0.0
    for w_start, w_end, domain, title, url in web_lookup:
        overlap = min(end, w_end).timestamp() - max(start, w_start).timestamp()
        if overlap > best_overlap:
            best_overlap = overlap
            best = (domain, title, url)
    return best


def dedupe_growing_events(
    window_events: list[dict], progress: tuple[Optional[str], float]
) -> tuple[list[dict], tuple[str, float]]:
    """AW keeps the currently-focused window as a single event whose
    `duration` grows on every heartbeat, rather than closing it. Since we
    poll with `start=<watermark>` (inclusive), re-polling while that window
    is still focused returns the SAME event again — same `timestamp`, bigger
    `duration` — and left alone we'd re-enqueue its FULL duration each time,
    massively double- (or, for something left open a long time, like a
    screensaver, many-times-) counting it.

    This rewrites each such repeat into just the NEW seconds since we last
    saw it, using `progress` (the last timestamp we processed for this
    bucket, and how much of ITS duration we've already sent). A genuinely
    new event (different timestamp) resets progress and is sent in full.
    """
    last_ts, sent = progress
    out = []
    for e in window_events:
        ts = e["timestamp"]
        duration = float(e.get("duration", 0))
        if last_ts is not None and ts == last_ts:
            delta = duration - sent
            sent = duration
            if delta <= 0:
                continue  # AW returned it unchanged — nothing new to send
            e = {**e, "duration": delta}
        else:
            sent = duration
        last_ts = ts
        out.append(e)
    return out, (last_ts, sent)


def transform_window_events(
    window_events: list[dict],
    afk_intervals: list[tuple[datetime, datetime]],
    web_lookup: list[tuple[datetime, datetime, str, str, str]],
) -> list[dict]:
    out = []
    for e in window_events:
        data = e.get("data", {})
        app = data.get("app", "unknown")
        title = data.get("title", "")
        start = _parse_ts(e["timestamp"])
        duration_s = float(e.get("duration", 0))
        if duration_s <= 0:
            continue

        app_or_domain = app
        url = None
        if app.lower() in BROWSER_APP_NAMES:
            match = find_domain_for_window(start, duration_s, web_lookup)
            if match:
                app_or_domain, title, url = match[0], match[1] or title, match[2]

        idle = overlaps_afk(start, duration_s, afk_intervals)

        out.append(
            {
                "appOrDomain": app_or_domain,
                "title": title,
                "url": url,
                "timestamp": start.isoformat(),
                "durationSeconds": duration_s,
                "idleFlag": idle,
            }
        )
    return out


# --------------------------------------------------------------------------
# Send loop
# --------------------------------------------------------------------------

def flush_outbox(state: State, cfg: Config, session: requests.Session, log: logging.Logger) -> None:
    backoff = 1
    while True:
        batch = state.peek_batch(SEND_BATCH_SIZE)
        if not batch:
            return

        # Zite's app endpoints expect the actual arguments wrapped under an
        # "inputs" key, not sent flat as the top-level POST body.
        payload = {
            "inputs": {
                "apiKey": cfg.api_key,
                "clientId": cfg.client_id,
                "hostname": cfg.hostname,
                "userName": cfg.user_name,
                "events": [
                    {
                        "appOrDomain": row["app_or_domain"],
                        "title": row["title"],
                        "url": row["url"] if "url" in row.keys() else None,
                        "timestamp": row["timestamp_iso"],
                        "durationSeconds": row["duration_seconds"],
                        "idleFlag": bool(row["idle_flag"]),
                    }
                    for row in batch
                ],
            }
        }

        try:
            r = session.post(cfg.zite_ingest_url, json=payload, timeout=30)
        except requests.RequestException as exc:
            log.warning("Ingest POST failed (network): %s — will retry", exc)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            return  # stop this flush cycle; next poll cycle will retry

        if r.status_code == 200:
            state.delete_ids([row["id"] for row in batch])
            backoff = 1
            log.info("Pushed %d events (outbox now %d)", len(batch), state.outbox_size())
            continue  # keep draining if more remain

        if r.status_code in (401, 403):
            log.error(
                "Ingest rejected the request (%s): %s — check api_key/client_id/tenant status. "
                "Leaving events queued.",
                r.status_code,
                r.text[:300],
            )
            return

        log.warning("Ingest returned %s: %s — will retry", r.status_code, r.text[:300])
        time.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        return


def poll_once(aw: AWClient, state: State, cfg: Config, log: logging.Logger) -> None:
    buckets = aw.list_buckets()
    window_id, afk_id, web_ids = pick_buckets(buckets, cfg.hostname)

    if not window_id:
        log.warning("No currentwindow bucket found — is ActivityWatch's window watcher running?")
        return

    afk_events = aw.get_events(afk_id, state.get_watermark(afk_id), POLL_BATCH_LIMIT) if afk_id else []
    afk_intervals = build_afk_intervals(afk_events)
    if afk_id and afk_events:
        state.set_watermark(afk_id, afk_events[-1]["timestamp"])

    web_lookup: list[tuple[datetime, datetime, str, str]] = []
    for wid in web_ids:
        web_events = aw.get_events(wid, state.get_watermark(wid), POLL_BATCH_LIMIT)
        web_lookup.extend(build_web_domain_lookup(web_events))
        if web_events:
            state.set_watermark(wid, web_events[-1]["timestamp"])

    window_events = aw.get_events(window_id, state.get_watermark(window_id), POLL_BATCH_LIMIT)
    if not window_events:
        return

    progress = state.get_progress(window_id)
    deduped_events, new_progress = dedupe_growing_events(window_events, progress)
    state.set_progress(window_id, *new_progress)

    transformed = transform_window_events(deduped_events, afk_intervals, web_lookup)
    state.enqueue(transformed)
    state.set_watermark(window_id, window_events[-1]["timestamp"])
    log.info("Queued %d events from AW (outbox now %d)", len(transformed), state.outbox_size())


def main() -> None:
    # Built with --noconsole for real installs (see build.yml), so there is
    # no console to print to — sys.stdout/stderr are None, and a plain
    # StreamHandler would crash the first time it tries to write. Log to a
    # file instead; this is also what actually lets us support a client
    # remotely, since nobody's watching a console window on their machine.
    DEFAULT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(DEFAULT_LOG_PATH, encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    log = logging.getLogger("aw_pusher")

    if not acquire_single_instance_lock():
        log.info(
            "Another copy of the pusher is already running on this machine "
            "— exiting immediately rather than running a second, "
            "independent copy."
        )
        return

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    cfg = Config.load(config_path)
    DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = State(DEFAULT_STATE_DIR / "state.sqlite3")
    session = requests.Session()
    aw = AWClient(cfg.aw_api_url, session)

    log.info(
        "RR-IT Insight pusher starting — host=%s client=%s aw=%s -> %s",
        cfg.hostname, cfg.client_id, cfg.aw_api_url, cfg.zite_ingest_url,
    )

    while True:
        try:
            poll_once(aw, state, cfg, log)
        except requests.RequestException as exc:
            log.warning("Could not reach local ActivityWatch API: %s", exc)
        except Exception:
            log.exception("Unexpected error during poll — continuing")

        try:
            flush_outbox(state, cfg, session, log)
        except Exception:
            log.exception("Unexpected error during flush — continuing")

        time.sleep(cfg.poll_interval_seconds)


if __name__ == "__main__":
    main()
