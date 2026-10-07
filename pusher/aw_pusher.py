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
  - Batched uploads (v2.0.0.14+): ActivityWatch is read and queued every
    poll, but the queue is only uploaded once per client-configured interval
    while there's real activity, or every 15 min while only idle time is queued.
    See "Upload schedule" below.
  - Receiver first (v2.0.0.15+): uploads go to the RR-IT receiver (a small
    Cloudflare Worker), which Zite collects from in one run every 5 minutes,
    so Zite's workflow-run usage no longer grows with the number of PCs.
    If the receiver can't be reached (or answers with a server error) the
    same upload goes straight to Zite, exactly as before — nothing depends
    on the receiver being up. See "Receiver" below.
  - Live view (v2.0.0.15+, opt-in per client): while enabled for this
    client, a tiny "current app + active/idle" status is sent to the
    receiver about once a minute. App/website name only — never window
    titles, URLs or screen content.
  - Auto-update (v2.0.0.15+): the server says which agent version RR-IT has
    approved for this client; this script only writes that version number
    to update-target.json. The Watchdog task (SYSTEM) does the download,
    checks the release's signature and hash, and installs it.

Deployment: silent install via RMM, run as a persistent process (Windows
service via NSSM, or a Scheduled Task set to run at logon / on an interval
with "if already running, do nothing"). See README.md alongside this file.
"""

from __future__ import annotations

import ctypes
import json
import logging
import logging.handlers
import os
import socket
import sqlite3
import sys
import time
import getpass
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
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
# Written by the installer (v2.0.0.15+); the version this PC is running.
VERSION_FILE = _app_dir() / "version.txt"
# Auto-update hand-off with watchdog.ps1. Both live in the install folder
# (Program Files), which standard users can't write to — only this service
# (LocalSystem) and the SYSTEM watchdog can.
UPDATE_TARGET_FILE = _app_dir() / "update-target.json"
UPDATE_STATUS_FILE = _app_dir() / "update-status.json"
AGENT_VERSION_FALLBACK = "2.0.0.15"
DEFAULT_STATE_DIR = _DATA_DIR / "state"
DEFAULT_LOG_PATH = _DATA_DIR / "pusher.log"

BROWSER_APP_NAMES = {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "chrome", "msedge", "firefox", "brave", "opera",
}

POLL_BATCH_LIMIT = 1000     # AW events fetched per bucket per poll
SEND_BATCH_SIZE = 500       # events per HTTP POST to Zite (the endpoint's own cap)
MAX_BACKOFF_SECONDS = 300

# --------------------------------------------------------------------------
# Upload schedule (v2.0.0.14+)
#
# Every upload is one Zite "workflow run", and the plan allows a fixed number
# per month across ALL devices. Before v2.0.0.14 the pusher uploaded whatever
# it found on every 30s poll, 24/7 while the PC was on — ~2,000 uploads a day
# per PC, usually one event each (confirmed 28 Sept: 26,018 runs from one PC).
#
# Now: ActivityWatch is still read every poll_interval_seconds and everything
# still goes into the local outbox straight away (nothing recorded
# differently, nothing lost), but the outbox is only UPLOADED:
#   - once per upload interval, when there's real (non-idle) activity queued;
#   - otherwise, while only idle time is queued (PC locked/unattended,
#     overnight), at most every IDLE_CATCHUP_SECONDS. This was 4 hours
#     while uploads cost a Zite workflow run each; since 2 Oct 2026 they go
#     to the Cloudflare receiver, so from v2.0.0.19 it's 15 minutes — an
#     idle PC still checks in every quarter-hour, which lets the dashboard
#     tell "idle since 18:26" from "offline since 18:26" (seen live on
#     RoryMack-L023 that evening: locked at 18:26, nothing heard until the
#     next logon two hours later);
#   - immediately on the very first upload after install, so a new device
#     shows up on the dashboard straight away.
# The interval is set per client in the RR-IT console; ingestEvents returns it
# in every response and it's stored locally, so a change reaches each device
# at its next upload. config.json's upload_interval_minutes is only the
# starting value until the server has said otherwise.
# --------------------------------------------------------------------------
DEFAULT_UPLOAD_INTERVAL_MINUTES = 30
MIN_UPLOAD_INTERVAL_MINUTES = 5
MAX_UPLOAD_INTERVAL_MINUTES = 240
IDLE_CATCHUP_SECONDS = 15 * 60

# Consecutive outbox rows for the same window that pick up exactly where the
# previous one ended (the same focused window reported again on the next
# poll, with its new seconds) are merged into one event before upload —
# fewer, longer rows for the server to store and the dashboard to read.
COALESCE_MAX_GAP_SECONDS = 2.0

# --------------------------------------------------------------------------
# Receiver (v2.0.0.15+)
#
# Uploads go to the RR-IT receiver first and Zite collects them from there in
# one run every 5 minutes, whatever the number of PCs (each direct upload to
# Zite is one workflow run). The receiver answers in the same shape as Zite's
# ingestEvents, plus:
#   deliverDirect  — Zite hasn't collected for 20+ min (or ever): ALSO post
#                    this upload straight to Zite so the dashboard doesn't go
#                    stale. Zite ignores the duplicate when it later collects.
#   live           — {"enabled": bool, "intervalSeconds": n} for Live view.
#   update         — {"version": "x.y.z.w"} approved by RR-IT, or null.
# v2.0.0.21+: if the receiver can't be reached, or answers 404/5xx, the upload
# stays in the outbox and the receiver is retried with backoff (no Zite
# fallback any more). 401/403/400/413/422 are real answers
# and handled like Zite's; 429 means "slow down" and is just retried later.
# config.json "receiver_url": "" turns the receiver off for this PC.
# --------------------------------------------------------------------------
DEFAULT_RECEIVER_URL = "https://rrit-receiver.ekelleher.workers.dev"
RECEIVER_TRIES = 2
LIVE_DEFAULT_INTERVAL_SECONDS = 60
LIVE_MIN_INTERVAL_SECONDS = 30
LIVE_MAX_INTERVAL_SECONDS = 600


def read_agent_version() -> str:
    try:
        v = VERSION_FILE.read_text(encoding="utf-8").strip()
        if v and all(part.isdigit() for part in v.split(".")):
            return v
    except OSError:
        pass
    return AGENT_VERSION_FALLBACK


def read_update_status() -> Optional[str]:
    """One-line summary of watchdog.ps1's last update attempt, reported with
    uploads so the console can show it. None if no attempt was ever made."""
    try:
        data = json.loads(UPDATE_STATUS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    parts = [str(data.get("state") or "unknown")]
    if data.get("target"):
        parts.append(str(data["target"]))
    if data.get("at"):
        parts.append("at " + str(data["at"]))
    if data.get("error"):
        parts.append("- " + str(data["error"])[:200])
    return " ".join(parts)[:300]


def write_update_target(version: Optional[str], log: logging.Logger) -> None:
    """Tell watchdog.ps1 which version RR-IT has approved (None = no update)."""
    try:
        if version:
            UPDATE_TARGET_FILE.write_text(
                json.dumps({"version": version, "setAt": _now_iso()}), encoding="utf-8"
            )
        elif UPDATE_TARGET_FILE.exists():
            UPDATE_TARGET_FILE.unlink()
    except OSError as exc:
        log.warning("Could not write %s: %s", UPDATE_TARGET_FILE, exc)


def valid_version(v) -> Optional[str]:
    if not isinstance(v, str):
        return None
    v = v.strip()
    parts = v.split(".")
    if 2 <= len(parts) <= 4 and all(p.isdigit() and len(p) <= 6 for p in parts):
        return v
    return None


def clamp_live_interval(value) -> int:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return LIVE_DEFAULT_INTERVAL_SECONDS
    return max(LIVE_MIN_INTERVAL_SECONDS, min(LIVE_MAX_INTERVAL_SECONDS, n))


def clamp_upload_interval(value) -> int:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_UPLOAD_INTERVAL_MINUTES
    if n <= 0:
        return DEFAULT_UPLOAD_INTERVAL_MINUTES
    return max(MIN_UPLOAD_INTERVAL_MINUTES, min(MAX_UPLOAD_INTERVAL_MINUTES, n))


def upload_due(
    now_epoch: float,
    last_upload_epoch: Optional[float],
    interval_seconds: float,
    queued_rows: int,
    has_active: bool,
) -> bool:
    """Whether the outbox should be uploaded now. Pure, so it can be tested."""
    if queued_rows <= 0:
        return False
    if last_upload_epoch is None:
        return True  # never uploaded from this machine: show up straight away
    elapsed = now_epoch - last_upload_epoch
    if elapsed < 0:
        return True  # clock moved backwards; upload once rather than stall
    if has_active and elapsed >= interval_seconds:
        return True
    return elapsed >= IDLE_CATCHUP_SECONDS


def coalesce_events(events: list[dict]) -> list[dict]:
    """Merge contiguous events for the same window (same app/domain, title,
    URL and idle state, next one starting within COALESCE_MAX_GAP_SECONDS of
    the previous one's end) into one. Order-preserving; never merges events
    that aren't contiguous, so the worst case is simply no merging."""
    out: list[dict] = []
    for e in events:
        if out:
            p = out[-1]
            if (
                p["appOrDomain"] == e["appOrDomain"]
                and p.get("title") == e.get("title")
                and p.get("url") == e.get("url")
                and bool(p.get("idleFlag")) == bool(e.get("idleFlag"))
            ):
                p_start = _parse_ts(p["timestamp"]).timestamp()
                e_start = _parse_ts(e["timestamp"]).timestamp()
                p_end = p_start + float(p["durationSeconds"])
                if abs(e_start - p_end) <= COALESCE_MAX_GAP_SECONDS:
                    # Sum the recorded durations rather than stretching the
                    # event across the small gap, so totals stay exactly what
                    # ActivityWatch recorded.
                    p["durationSeconds"] = float(p["durationSeconds"]) + float(e["durationSeconds"])
                    continue
        out.append(dict(e))
    return out


def find_upload_interval(payload, depth: int = 0) -> Optional[int]:
    """Pull uploadIntervalMinutes out of an ingest response. It's a top-level
    field today (checked against the live endpoint on 29 Sept), but search a
    couple of levels down too so a response wrapper added later doesn't
    silently stop interval changes reaching devices."""
    if depth > 3:
        return None
    if isinstance(payload, dict):
        v = payload.get("uploadIntervalMinutes")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return clamp_upload_interval(v)
        for child in payload.values():
            found = find_upload_interval(child, depth + 1)
            if found is not None:
                return found
    return None


@dataclass
class Config:
    aw_api_url: str
    zite_ingest_url: str
    api_key: str
    client_id: str
    poll_interval_seconds: int
    hostname: str
    user_name: Optional[str]
    # Starting upload interval only — the server's per-client value (sent
    # back on every upload) takes over from the first successful upload.
    upload_interval_minutes: int
    # "" = receiver off (upload straight to Zite, pre-2.0.0.15 behaviour).
    receiver_url: str = DEFAULT_RECEIVER_URL

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
            upload_interval_minutes=clamp_upload_interval(
                raw.get("upload_interval_minutes", DEFAULT_UPLOAD_INTERVAL_MINUTES)
            ),
            receiver_url=str(raw.get("receiver_url", DEFAULT_RECEIVER_URL) or "").rstrip("/"),
        )


def _console_session_username() -> Optional[str]:
    """Look up the username of whoever is in the active console session via
    the Windows Terminal Services API. Needed once the pusher runs as a
    Windows Service under LocalSystem (see installer.iss's
    InstallPusherService, v2.0.0+) rather than as the logged-in user's own
    process — in that case getpass.getuser() returns "SYSTEM", not the
    person actually using the machine, silently breaking event
    attribution. Returns None on non-Windows, no active console session
    (e.g. a locked/disconnected machine), or any WTS API failure — callers
    fall back to getpass.getuser() in that case."""
    if os.name != "nt":
        return None
    try:
        WTS_CURRENT_SERVER_HANDLE = 0
        WTS_USER_NAME = 5

        session_id = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
        if session_id in (0xFFFFFFFF, -1):
            return None  # no one is in the console session right now

        wtsapi32 = ctypes.windll.wtsapi32
        buf = ctypes.c_void_p()
        bytes_returned = ctypes.c_ulong()
        ok = wtsapi32.WTSQuerySessionInformationW(
            WTS_CURRENT_SERVER_HANDLE,
            session_id,
            WTS_USER_NAME,
            ctypes.byref(buf),
            ctypes.byref(bytes_returned),
        )
        if not ok or not buf.value:
            return None
        try:
            username = ctypes.wstring_at(buf.value)
        finally:
            wtsapi32.WTSFreeMemory(buf)
        return username or None
    except Exception:
        return None


def _default_user_name() -> Optional[str]:
    """Prefer the console session's actual logged-in user — this is the
    correct behaviour whether the pusher is running interactively (where
    it and getpass.getuser() agree anyway) or as the LocalSystem Windows
    Service (where getpass.getuser() would wrongly report "SYSTEM"). Falls
    back to getpass.getuser() when the WTS lookup can't resolve anyone
    (e.g. no one's logged on yet, or running on a non-Windows dev box)."""
    console_user = _console_session_username()
    if console_user:
        return console_user
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
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
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

    def outbox_has_active(self) -> bool:
        """True if anything queued is real (non-idle) activity."""
        return self.conn.execute("SELECT 1 FROM outbox WHERE idle_flag = 0 LIMIT 1").fetchone() is not None

    # Small key/value store for the upload schedule — persisted so a service
    # restart (or NSSM restarting a crashed pusher) doesn't trigger an extra
    # upload or forget the server's interval.
    def get_setting(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_last_upload_epoch(self) -> Optional[float]:
        v = self.get_setting("last_upload_epoch")
        try:
            return float(v) if v is not None else None
        except ValueError:
            return None

    def set_last_upload_epoch(self, epoch: float) -> None:
        self.set_setting("last_upload_epoch", repr(epoch))

    def get_upload_interval_minutes(self, fallback: int) -> int:
        v = self.get_setting("upload_interval_minutes")
        return clamp_upload_interval(v) if v is not None else fallback

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
        """Fetch every event from start_iso onward, oldest-first.

        AW's /events endpoint always returns its NEWEST `limit` events
        matching the filter, truncated server-side — so a single call with
        `start=<watermark>` silently drops anything older than the newest
        `limit` events if more than `limit` have queued up since the
        watermark (e.g. after downtime). To backfill correctly we walk
        backward from "now" using `end` as a cursor: each page's oldest
        timestamp becomes the next page's exclusive upper bound, so we
        keep paging until a page comes back smaller than the page size,
        which means we've reached (or passed) start_iso with nothing left
        in between.
        """
        page_size = min(limit, 1000) if limit else 1000
        collected: dict = {}
        end_iso: Optional[str] = None
        while True:
            params = {"limit": page_size}
            if start_iso:
                params["start"] = start_iso
            if end_iso:
                params["end"] = end_iso
            r = self.session.get(
                f"{self.base_url}/api/0/buckets/{bucket_id}/events", params=params, timeout=15
            )
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            for e in page:
                collected[e["id"]] = e
            if len(page) < page_size:
                # This page covered everything between start_iso and end_iso
                # (or now, if end_iso is unset) — nothing older is missing.
                break
            page.sort(key=lambda e: e["timestamp"])
            oldest_ts = page[0]["timestamp"]
            if oldest_ts == end_iso:
                # Not making progress (a full page all sharing one
                # timestamp) — stop rather than loop forever.
                break
            end_iso = oldest_ts
        events = list(collected.values())
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
            sent_before = sent
            delta = duration - sent_before
            sent = duration
            if delta <= 0:
                continue  # AW returned it unchanged — nothing new to send
            # The delta covers the seconds AFTER what we already sent, so it
            # starts `sent_before` seconds into the original event, not at
            # the event's original start — otherwise AFK-overlap and
            # web-domain checks look at the wrong slice of time for any
            # window that's been open a while.
            shifted_start = _parse_ts(ts) + timedelta(seconds=sent_before)
            e = {**e, "timestamp": shifted_start.isoformat(), "duration": delta}
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
        app = data.get("app") or "unknown"
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

_outbox_backoff_seconds = 1
_outbox_next_attempt_at = 0.0

# A batch rejected with 400/413/422 is retried (with the normal backoff) for
# this long after its FIRST rejection before it's dropped. A 400 can mean a
# genuinely malformed batch OR a temporary server-side bug, and the two look
# identical from here, so a single rejection must never be treated as final.
MAX_BAD_BATCH_RETRY_SECONDS = 24 * 3600
_outbox_rejected_head_id = None      # id of the first row of the batch currently being rejected
_outbox_rejected_since = 0.0         # time.monotonic() of that batch's first rejection

class _Reply:
    """What came back from an upload attempt: an HTTP status (None = network
    failure everywhere) plus which endpoint answered."""
    def __init__(self, status: Optional[int], text: str = "", data=None, via: str = "", error: str = ""):
        self.status = status
        self.text = text
        self.data = data
        self.via = via
        self.error = error


def _post_json(session: requests.Session, url: str, payload: dict, via: str) -> _Reply:
    try:
        r = session.post(url, json=payload, timeout=30)
    except requests.RequestException as exc:
        return _Reply(None, via=via, error=str(exc))
    try:
        data = r.json()
    except ValueError:
        data = None
    return _Reply(r.status_code, r.text, data, via)


def deliver(
    session: requests.Session,
    receiver_url: str,
    receiver_path: str,
    zite_url: str,
    payload: dict,
    log: logging.Logger,
) -> _Reply:
    """Send one upload to the receiver (if configured), else to Zite.
    v2.0.0.21+: no Zite fallback when the receiver fails; the caller keeps the
    batch in the outbox and retries. Mirrored in usb_watcher.py."""
    if receiver_url:
        reply = _Reply(None)
        for attempt in range(RECEIVER_TRIES):
            reply = _post_json(session, receiver_url + receiver_path, payload, "receiver")
            if reply.status is not None and reply.status < 500 and reply.status != 404:
                break
        if reply.status is not None and reply.status < 500 and reply.status != 404:
            if reply.status == 200 and isinstance(reply.data, dict) and reply.data.get("deliverDirect"):
                # Zite hasn't collected from the receiver for a while: also
                # deliver directly so the dashboard stays current. Zite
                # ignores the copy it later collects (its duplicate guard).
                direct = _post_json(session, zite_url, payload, "zite")
                if direct.status != 200:
                    log.warning(
                        "Receiver asked for direct delivery too, but Zite answered %s %s — "
                        "the receiver copy will still reach Zite when collection resumes.",
                        direct.status, (direct.text or direct.error)[:200],
                    )
            return reply
        # v2.0.0.21: never fall back to Zite when the receiver refuses or
        # can't be reached. Every direct upload costs a Zite workflow run, and
        # a receiver blip used to send a whole office's PCs to Zite at once
        # (RoryMack-L04/L05, 6 Oct). The upload stays in the local outbox and
        # flush_outbox retries the receiver with exponential backoff (up to
        # MAX_BACKOFF_SECONDS), so nothing is lost. Zite is only used when no
        # receiver is configured at all (very old config.json).
        log.warning(
            "Receiver unavailable (%s) — keeping this upload in the outbox to retry the receiver.",
            reply.status if reply.status is not None else reply.error[:200],
        )
        return reply
    return _post_json(session, zite_url, payload, "zite")


def apply_server_reply(state: "State", cfg: Config, data, log: logging.Logger) -> None:
    """Adopt the settings the server sends back with every successful upload:
    upload interval, Live view on/off, and the approved agent version."""
    if not isinstance(data, dict):
        return
    server_interval = find_upload_interval(data)
    if server_interval is not None:
        current = state.get_upload_interval_minutes(cfg.upload_interval_minutes)
        if server_interval != current or state.get_setting("upload_interval_minutes") is None:
            state.set_setting("upload_interval_minutes", str(server_interval))
            if server_interval != current:
                log.info("Upload interval is now %d minutes (set by RR-IT)", server_interval)

    if "live" in data:
        live = data.get("live") if isinstance(data.get("live"), dict) else {}
        enabled = "1" if live.get("enabled") else "0"
        interval = str(clamp_live_interval(live.get("intervalSeconds", LIVE_DEFAULT_INTERVAL_SECONDS)))
        if state.get_setting("live_enabled") != enabled:
            log.info("Live view is now %s for this client", "ON" if enabled == "1" else "off")
        state.set_setting("live_enabled", enabled)
        state.set_setting("live_interval_seconds", interval)

    if "update" in data:
        upd = data.get("update") if isinstance(data.get("update"), dict) else None
        target = valid_version(upd.get("version")) if upd else None
        previous = state.get_setting("update_target") or ""
        if (target or "") != previous:
            state.set_setting("update_target", target or "")
            write_update_target(target, log)
            if target:
                log.info("RR-IT approved agent version %s for this client (running %s)", target, read_agent_version())
        elif target and not UPDATE_TARGET_FILE.exists():
            write_update_target(target, log)


def flush_outbox(state: State, cfg: Config, session: requests.Session, log: logging.Logger) -> bool:
    """Send the outbox. Returns True only if it was fully drained (every
    queued event accepted), which is what counts as "uploaded" for the
    schedule in maybe_upload()."""
    global _outbox_backoff_seconds, _outbox_next_attempt_at, _outbox_rejected_head_id, _outbox_rejected_since

    now = time.monotonic()
    if now < _outbox_next_attempt_at:
        return False  # still inside the backoff window from a previous failure

    while True:
        batch = state.peek_batch(SEND_BATCH_SIZE)
        if not batch:
            _outbox_backoff_seconds = 1  # caught up — reset for the next failure
            return True

        # Zite's app endpoints expect the actual arguments wrapped under an
        # "inputs" key, not sent flat as the top-level POST body.
        payload = {
            "inputs": {
                "apiKey": cfg.api_key,
                "clientId": cfg.client_id,
                "hostname": cfg.hostname,
                "userName": cfg.user_name,
                "agentVersion": read_agent_version(),
                "updateStatus": read_update_status(),
                "events": coalesce_events([
                    {
                        "appOrDomain": row["app_or_domain"],
                        "title": row["title"],
                        "url": row["url"] if "url" in row.keys() else None,
                        "timestamp": row["timestamp_iso"],
                        "durationSeconds": row["duration_seconds"],
                        "idleFlag": bool(row["idle_flag"]),
                    }
                    for row in batch
                ]),
            }
        }

        r = deliver(session, cfg.receiver_url, "/ins/events", cfg.zite_ingest_url, payload, log)
        if r.status is None:
            log.warning(
                "Ingest POST failed (network): %s — will retry in %ss",
                r.error[:300], _outbox_backoff_seconds,
            )
            _outbox_next_attempt_at = time.monotonic() + _outbox_backoff_seconds
            _outbox_backoff_seconds = min(_outbox_backoff_seconds * 2, MAX_BACKOFF_SECONDS)
            return False

        if r.status == 200:
            state.delete_ids([row["id"] for row in batch])
            _outbox_rejected_head_id = None
            log.info(
                "Pushed %d queued rows as %d events via %s (outbox now %d)",
                len(batch), len(payload["inputs"]["events"]), r.via, state.outbox_size(),
            )
            apply_server_reply(state, cfg, r.data, log)
            continue

        if r.status == 429:
            log.warning("Receiver asked us to slow down (429) — retrying in %ss", _outbox_backoff_seconds)
            _outbox_next_attempt_at = time.monotonic() + _outbox_backoff_seconds
            _outbox_backoff_seconds = min(_outbox_backoff_seconds * 2, MAX_BACKOFF_SECONDS)
            return False

        if r.status in (401, 403):
            log.error(
                "Ingest rejected the request (%s): %s — check api_key/client_id. Leaving events queued.",
                r.status, r.text[:300],
            )
            _outbox_next_attempt_at = time.monotonic() + MAX_BACKOFF_SECONDS
            return False

        if r.status in (400, 413, 422):
            # 404 is deliberately NOT in this list: it means the ingest URL
            # itself is wrong or the endpoint is temporarily unpublished — a
            # config/server problem, not a bad batch. It falls through to the
            # plain retry-with-backoff path below.
            head_id = batch[0]["id"]
            if head_id != _outbox_rejected_head_id:
                _outbox_rejected_head_id = head_id
                _outbox_rejected_since = time.monotonic()
            rejected_for = time.monotonic() - _outbox_rejected_since
            if rejected_for < MAX_BAD_BATCH_RETRY_SECONDS:
                log.warning(
                    "Ingest rejected a batch of %d events (%s): %s — retrying in %ss "
                    "(rejected for %dm so far; dropped only after %dh of continuous "
                    "rejection, in case this is a temporary server-side fault). "
                    "First event timestamp: %s",
                    len(batch), r.status, r.text[:300], _outbox_backoff_seconds,
                    rejected_for // 60, MAX_BAD_BATCH_RETRY_SECONDS // 3600,
                    batch[0]["timestamp_iso"],
                )
                _outbox_next_attempt_at = time.monotonic() + _outbox_backoff_seconds
                _outbox_backoff_seconds = min(_outbox_backoff_seconds * 2, MAX_BACKOFF_SECONDS)
                return False
            log.error(
                "Ingest has rejected the same batch of %d events (%s) continuously for "
                "%dh: %s — dropping it so later events aren't blocked forever. "
                "First event timestamp: %s",
                len(batch), r.status, MAX_BAD_BATCH_RETRY_SECONDS // 3600,
                r.text[:300], batch[0]["timestamp_iso"],
            )
            state.delete_ids([row["id"] for row in batch])
            _outbox_rejected_head_id = None
            continue

        log.warning(
            "Ingest (%s) returned %s: %s — will retry in %ss",
            r.via, r.status, r.text[:300], _outbox_backoff_seconds,
        )
        _outbox_next_attempt_at = time.monotonic() + _outbox_backoff_seconds
        _outbox_backoff_seconds = min(_outbox_backoff_seconds * 2, MAX_BACKOFF_SECONDS)
        return False


def maybe_upload(
    state: State, cfg: Config, session: requests.Session, log: logging.Logger, force: bool = False
) -> bool:
    """Upload the outbox if the schedule says it's time (see upload_due and
    the "Upload schedule" notes at the top), or straight away when force is
    set (a "Refresh now" from the dashboard). Returns True if an upload fully
    drained the outbox. Recording is unaffected — poll_once() keeps queueing
    every poll; this only decides when to deliver."""
    interval_minutes = state.get_upload_interval_minutes(cfg.upload_interval_minutes)
    queued = state.outbox_size()
    if force and queued > 0:
        log.info("Uploading now (Refresh requested from the dashboard)")
    elif not upload_due(
        now_epoch=time.time(),
        last_upload_epoch=state.get_last_upload_epoch(),
        interval_seconds=interval_minutes * 60,
        queued_rows=queued,
        has_active=state.outbox_has_active(),
    ):
        return False
    drained = flush_outbox(state, cfg, session, log)
    if drained:
        state.set_last_upload_epoch(time.time())
    return drained


# --------------------------------------------------------------------------
# Live view (v2.0.0.15+, opt-in per client)
# --------------------------------------------------------------------------

# What this PC is doing right now, as far as the last poll could tell:
# app/website name only (never the window title or URL), active/idle, since when.
_current = {"app": None, "idle": False, "since": None}


def note_current_activity(transformed: list[dict], afk_events: list[dict]) -> None:
    if transformed:
        last = transformed[-1]
        app = last.get("appOrDomain")
        if app != _current["app"]:
            _current["app"] = app
            _current["since"] = last.get("timestamp")
    if afk_events:
        status = (afk_events[-1].get("data") or {}).get("status")
        if status in ("afk", "not-afk"):
            idle = status == "afk"
            if idle != _current["idle"]:
                _current["idle"] = idle
                _current["since"] = afk_events[-1].get("timestamp") or _current["since"]


def live_due(now_epoch: float, last_epoch: Optional[float], interval_seconds: int) -> bool:
    return last_epoch is None or now_epoch - last_epoch >= interval_seconds or now_epoch < last_epoch


_last_live_epoch: Optional[float] = None


def maybe_send_live(state: State, cfg: Config, session: requests.Session, log: logging.Logger) -> bool:
    """Send the Live view status if Live is on for this client and it's due.
    Returns True if the receiver asked for an immediate upload ("Refresh now").
    Best-effort: a failure here never affects uploads."""
    global _last_live_epoch
    if not cfg.receiver_url or state.get_setting("live_enabled") != "1" or not _current["app"]:
        return False
    interval = clamp_live_interval(state.get_setting("live_interval_seconds") or LIVE_DEFAULT_INTERVAL_SECONDS)
    now = time.time()
    if not live_due(now, _last_live_epoch, interval):
        return False
    _last_live_epoch = now
    payload = {
        "inputs": {
            "apiKey": cfg.api_key,
            "clientId": cfg.client_id,
            "hostname": cfg.hostname,
            "app": _current["app"],
            "state": "idle" if _current["idle"] else "active",
            "since": _current["since"],
        }
    }
    try:
        r = session.post(cfg.receiver_url + "/ins/live", json=payload, timeout=10)
        data = r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError) as exc:
        log.debug("Live status not sent: %s", exc)
        return False
    if not isinstance(data, dict):
        return False
    live = data.get("live")
    if isinstance(live, dict) and not live.get("enabled"):
        state.set_setting("live_enabled", "0")
        log.info("Live view is now off for this client")
    return bool(data.get("uploadNow"))


def poll_once(aw: AWClient, state: State, cfg: Config, log: logging.Logger) -> None:
    buckets = aw.list_buckets()
    window_id, afk_id, web_ids = pick_buckets(buckets, cfg.hostname)

    if not window_id:
        log.warning("No currentwindow bucket found — is ActivityWatch's window watcher running?")
        return

    afk_events = aw.get_events(afk_id, state.get_watermark(afk_id), POLL_BATCH_LIMIT) if afk_id else []
    note_current_activity([], afk_events)
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
    note_current_activity(transformed, afk_events)
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
    handlers: list[logging.Handler] = [
        logging.handlers.TimedRotatingFileHandler(
        DEFAULT_LOG_PATH, when="midnight", backupCount=14, encoding="utf-8"
        )
    ]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    log = logging.getLogger("aw_pusher")

    try:
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
    except (Exception, SystemExit):
        # SystemExit too: Config.load() raises it (not an Exception
        # subclass) for a missing config.json, and that's exactly the
        # startup failure most worth getting into pusher.log.
        log.exception("Fatal error during startup — exiting")
        return

    log.info(
        "RR-IT Insight pusher %s starting — host=%s client=%s aw=%s -> receiver %s, fallback %s "
        "(upload every %d min when active, idle catch-up every %d min)",
        read_agent_version(), cfg.hostname, cfg.client_id, cfg.aw_api_url, cfg.receiver_url or "(off)",
        cfg.zite_ingest_url,
        state.get_upload_interval_minutes(cfg.upload_interval_minutes), IDLE_CATCHUP_SECONDS // 60,
    )

    while True:
        try:
            poll_once(aw, state, cfg, log)
        except requests.RequestException as exc:
            log.warning("Could not reach local ActivityWatch API: %s", exc)
        except Exception:
            log.exception("Unexpected error during poll — continuing")

        upload_now = False
        try:
            upload_now = maybe_send_live(state, cfg, session, log)
        except Exception:
            log.exception("Unexpected error sending live status — continuing")

        try:
            maybe_upload(state, cfg, session, log, force=upload_now)
        except Exception:
            log.exception("Unexpected error during upload — continuing")

        time.sleep(cfg.poll_interval_seconds)


if __name__ == "__main__":
    main()
