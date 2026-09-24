#!/usr/bin/env python3
"""
RR-IT Insight — USB removable-drive watcher.

Optional add-on, NOT part of the standard install. Only enabled for a
client who has a specific requirement to know when someone plugs a USB
storage device into a monitored PC and copies files onto it — this is a
separate, deliberately narrower thing than the always-on ActivityWatch
pusher (aw_pusher.py), and it only runs at all when this device's
config.json has "usb_monitoring_enabled": true (set by the installer only
when its "Enable USB removable-drive monitoring" checkbox was ticked).

The server also independently checks the client's own "USB Monitoring
Enabled" flag on every ingest call (see ingestUsbEvents.ts), so a stray or
misconfigured copy of this watcher can't quietly report USB activity for a
client who never agreed to it — two places have to say yes, not one.

IMPORTANT — what this can and can't detect (be honest about this with
clients): Windows gives no cheap, reliable, elevated-privilege-free way to
watch "a file was read from a USB drive" as it happens. What IS reliable is
polling the removable drive's own filesystem for files that are new or have
changed since we last looked — which is exactly the scenario that actually
matters for a data-loss concern (someone copying company files FROM this
PC ONTO a USB stick to take them elsewhere). This script does that:
  - Detects a removable drive appearing/disappearing (by drive letter).
  - On appearance, takes a baseline snapshot of what's already on it —
    pre-existing files are NOT reported (a drive that already has files on
    it isn't news; only NEW activity is).
  - On each poll after that, re-scans and reports any file that's new or
    has changed since the baseline — this is "written to the device"
    (either freshly copied there, or edited in place on the drive).
  - It does NOT detect files copied OFF the drive onto the PC — reading a
    file from USB leaves no trace on the drive itself, and watching every
    read on the PC's own disk would need a kernel filter driver, which is
    well beyond what a Python script (or even ActivityWatch itself) can
    do. Don't present the absence of events as "nothing was taken from
    this device" — say what it actually means: "no files were written TO
    the USB device," full stop.

Same local-first design as aw_pusher.py: events queue in a durable SQLite
outbox and flush opportunistically, so nothing is lost if Zite or the
network is briefly unreachable.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# --------------------------------------------------------------------------
# Config — shares config.json with aw_pusher.py; see that file's header for
# why _app_dir()/sys.executable matters for a PyInstaller onefile build.
# --------------------------------------------------------------------------

def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


_DATA_DIR = Path(os.environ.get("PROGRAMDATA", str(_app_dir()))) / "RR-IT Insight"

DEFAULT_CONFIG_PATH = _app_dir() / "config.json"
DEFAULT_STATE_DIR = _DATA_DIR / "state"
DEFAULT_LOG_PATH = _DATA_DIR / "usb_watcher.log"

POLL_INTERVAL_SECONDS = 15   # how often to re-check drives and re-scan for new files
SEND_BATCH_SIZE = 200
MAX_BACKOFF_SECONDS = 300
MAX_FILES_PER_DRIVE_SCAN = 20000  # safety cap so a huge drive can't hang a poll cycle

# How long a drive can vanish and reappear before we treat it as a genuinely
# new connect/disconnect. Some setups make a drive blip on and off within a
# second or two without anyone touching it — most commonly a VM host's USB
# arbitrator (e.g. VMware) handing the device back and forth between the
# host and a guest — and without this, each blip looked like a full
# disconnect+reconnect: a duplicate "connected" event, and a duplicate
# "file written" for every file already on it (since reconnecting re-ran
# the baseline scan). Confirmed live on a VMware test machine: a single
# plug-in produced 4 connect events and 2 duplicate file-written events
# for the same file before this fix. A real removal is still reported —
# just after sitting gone for this long, not instantly.
DEBOUNCE_SECONDS = 30

# Windows drive type constant (from GetDriveTypeW) for removable media —
# USB flash drives and SD cards via a reader both report this.
DRIVE_REMOVABLE = 2

# Named mutex used to make sure only one copy of this watcher is ever
# actually doing work at once. Confirmed live: the installer's Scheduled
# Task fires an immediate /Run on every install AND reinstall, on top of
# the task's own "run at logon" trigger, with no check for an existing
# copy already running — and since this script loops forever, each extra
# launch just piles up as another permanent, fully independent watcher.
# Two independent copies both see the same drive and both report their
# own events, which is what produced exact, consistent duplicate pairs
# (2x every connect/disconnect/file-written, at near-identical
# timestamps) — not a flaky USB port, and not something a debounce delay
# can fix, since each copy has no idea the other exists.
SINGLE_INSTANCE_MUTEX_NAME = "Global\\RRITInsightUsbWatcherSingleInstance"
ERROR_ALREADY_EXISTS = 183

# Folders/files Windows itself writes to a removable drive just by looking at
# it (indexing, thumbnails, the recycle bin) — not something a person put
# there, and pure noise in a "what did they copy" report. Matched
# case-insensitively against path components/filenames.
IGNORED_DIR_NAMES = {"system volume information", "$recycle.bin"}
IGNORED_FILE_NAMES = {"desktop.ini", "thumbs.db", "autorun.inf"}


@dataclass
class Config:
    zite_usb_ingest_url: str
    api_key: str
    client_id: str
    usb_monitoring_enabled: bool
    hostname: str
    user_name: Optional[str]

    @staticmethod
    def load(path: Path) -> "Config":
        if not path.exists():
            raise SystemExit(f"Config file not found: {path}")
        raw = json.loads(path.read_text())
        # Derive the USB endpoint from the same base as the activity
        # endpoint rather than requiring a second URL in config.json — one
        # less thing to get wrong when hand-editing a config file.
        base_ingest_url = raw["zite_ingest_url"].rstrip("/")
        usb_url = base_ingest_url.rsplit("/", 1)[0] + "/ingestUsbEvents"
        return Config(
            zite_usb_ingest_url=raw.get("zite_usb_ingest_url", usb_url).rstrip("/"),
            api_key=raw["api_key"],
            client_id=raw["client_id"],
            usb_monitoring_enabled=bool(raw.get("usb_monitoring_enabled", False)),
            hostname=raw.get("hostname") or socket.gethostname(),
            user_name=raw.get("user_name") or _default_user_name(),
        )


def _default_user_name() -> Optional[str]:
    try:
        return getpass.getuser()
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Local state — its own outbox table, in the same SQLite file the pusher
# already keeps in ProgramData (separate table, so neither script's writes
# collide with the other's).
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
            CREATE TABLE IF NOT EXISTS usb_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                timestamp_iso TEXT NOT NULL,
                usb_device_name TEXT,
                usb_serial_number TEXT,
                drive_letter TEXT,
                file_path TEXT,
                file_size_bytes INTEGER,
                queued_at_iso TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def enqueue(self, events: list[dict]) -> None:
        if not events:
            return
        now = _now_iso()
        self.conn.executemany(
            """
            INSERT INTO usb_outbox
                (event_type, timestamp_iso, usb_device_name, usb_serial_number,
                 drive_letter, file_path, file_size_bytes, queued_at_iso)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    e["eventType"],
                    e["timestamp"],
                    e.get("usbDeviceName"),
                    e.get("usbSerialNumber"),
                    e.get("driveLetter"),
                    e.get("filePath"),
                    e.get("fileSizeBytes"),
                    now,
                )
                for e in events
            ],
        )
        self.conn.commit()

    def peek_batch(self, limit: int) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            "SELECT * FROM usb_outbox ORDER BY id ASC LIMIT ?", (limit,)
        )
        return cur.fetchall()

    def delete_ids(self, ids: list[int]) -> None:
        if not ids:
            return
        qmarks = ",".join("?" for _ in ids)
        self.conn.execute(f"DELETE FROM usb_outbox WHERE id IN ({qmarks})", ids)
        self.conn.commit()

    def outbox_size(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM usb_outbox").fetchone()[0]


# --------------------------------------------------------------------------
# Drive detection (Windows API via ctypes — no extra pip dependency)
# --------------------------------------------------------------------------

def list_removable_drives() -> list[str]:
    """Return drive letters (e.g. ['E:\\\\']) currently mounted as removable."""
    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if not (bitmask & (1 << i)):
            continue
        letter = f"{chr(65 + i)}:\\"
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(letter) == DRIVE_REMOVABLE:
                drives.append(letter)
        except Exception:
            continue
    return drives


def get_volume_info(drive_letter: str) -> tuple[Optional[str], Optional[str]]:
    """Return (volume_label, serial_number_hex) for a drive, best-effort."""
    name_buf = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32(0)
    try:
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive_letter),
            name_buf,
            ctypes.sizeof(name_buf),
            ctypes.byref(serial),
            None,
            None,
            None,
            0,
        )
        if not ok:
            return None, None
        label = name_buf.value or None
        serial_hex = f"{serial.value:08X}" if serial.value else None
        return label, serial_hex
    except Exception:
        return None, None


def acquire_single_instance_lock() -> bool:
    """Best-effort Windows named-mutex lock. Returns True if this process
    is the only one holding it (safe to proceed), False if another copy
    already holds it (this process should exit without doing any work).
    The handle is deliberately never closed — it releases automatically
    when this process exits, which is exactly when we want the lock
    freed. If mutex creation itself fails for some reason, fail OPEN
    (return True) rather than refuse to run the watcher at all."""
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        if not handle:
            return True
        return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True


def scan_drive(drive_letter: str, log: logging.Logger) -> dict[str, tuple[float, int]]:
    """Return {relative_path: (mtime, size)} for every file on the drive,
    capped at MAX_FILES_PER_DRIVE_SCAN so a huge/slow drive can't hang a
    poll cycle indefinitely."""
    snapshot: dict[str, tuple[float, int]] = {}
    count = 0
    try:
        for root, dirs, files in os.walk(drive_letter):
            # Don't even descend into Windows' own housekeeping folders —
            # cheaper than filtering afterwards, and keeps a $Recycle.Bin
            # full of a client's own deleted files out of the report too.
            dirs[:] = [d for d in dirs if d.lower() not in IGNORED_DIR_NAMES]
            for name in files:
                if name.lower() in IGNORED_FILE_NAMES:
                    continue
                if count >= MAX_FILES_PER_DRIVE_SCAN:
                    log.warning(
                        "%s has more than %d files — stopped scanning early, "
                        "some new files may not be reported this cycle",
                        drive_letter, MAX_FILES_PER_DRIVE_SCAN,
                    )
                    return snapshot
                full_path = os.path.join(root, name)
                try:
                    stat = os.stat(full_path)
                except OSError:
                    continue
                rel_path = os.path.relpath(full_path, drive_letter)
                snapshot[rel_path] = (stat.st_mtime, stat.st_size)
                count += 1
    except OSError as exc:
        log.warning("Could not scan %s: %s (drive may have been removed mid-scan)", drive_letter, exc)
    return snapshot


# --------------------------------------------------------------------------
# Send loop — same shape as aw_pusher.py's flush_outbox
# --------------------------------------------------------------------------

def flush_outbox(state: State, cfg: Config, session: requests.Session, log: logging.Logger) -> None:
    backoff = 1
    while True:
        batch = state.peek_batch(SEND_BATCH_SIZE)
        if not batch:
            return

        payload = {
            "inputs": {
                "apiKey": cfg.api_key,
                "clientId": cfg.client_id,
                "hostname": cfg.hostname,
                "userName": cfg.user_name,
                "events": [
                    {
                        "eventType": row["event_type"],
                        "timestamp": row["timestamp_iso"],
                        "usbDeviceName": row["usb_device_name"],
                        "usbSerialNumber": row["usb_serial_number"],
                        "driveLetter": row["drive_letter"],
                        "filePath": row["file_path"],
                        "fileSizeBytes": row["file_size_bytes"],
                    }
                    for row in batch
                ],
            }
        }

        try:
            r = session.post(cfg.zite_usb_ingest_url, json=payload, timeout=30)
        except requests.RequestException as exc:
            log.warning("USB ingest POST failed (network): %s — will retry", exc)
            time.sleep(backoff)
            return

        if r.status_code == 200:
            state.delete_ids([row["id"] for row in batch])
            backoff = 1
            log.info("Pushed %d USB events (outbox now %d)", len(batch), state.outbox_size())
            continue

        if r.status_code in (401, 403):
            log.error(
                "USB ingest rejected the request (%s): %s — check api_key/client_id, "
                "and that USB Monitoring is enabled for this client. Leaving events queued.",
                r.status_code, r.text[:300],
            )
            return

        log.warning("USB ingest returned %s: %s — will retry", r.status_code, r.text[:300])
        time.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        return


def main() -> None:
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
    log = logging.getLogger("usb_watcher")

    if not acquire_single_instance_lock():
        log.info(
            "Another copy of the USB watcher is already running on this "
            "machine — exiting immediately rather than running a second, "
            "independent copy (which would double up every event)."
        )
        return

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    cfg = Config.load(config_path)

    if not cfg.usb_monitoring_enabled:
        # Belt-and-braces: even if this exe somehow got run on a machine
        # where the installer's USB checkbox wasn't ticked, do nothing
        # rather than silently start watching. The Scheduled Task that
        # launches this is itself only created when the checkbox was
        # ticked, so reaching this line at all should be rare.
        log.info("USB monitoring is not enabled in config.json — exiting without watching anything.")
        return

    DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = State(DEFAULT_STATE_DIR / "state.sqlite3")
    session = requests.Session()

    log.info(
        "RR-IT Insight USB watcher starting — host=%s client=%s -> %s",
        cfg.hostname, cfg.client_id, cfg.zite_usb_ingest_url,
    )

    # drive_letter -> {"name": ..., "serial": ..., "files": {rel_path: (mtime, size)}}
    tracked: dict[str, dict] = {}
    # drive_letter -> {"info": <same shape as a tracked entry>, "disconnected_at": epoch_seconds}
    # Drives that vanished but might just be blipping — see DEBOUNCE_SECONDS.
    pending_disconnect: dict[str, dict] = {}

    while True:
        try:
            now_ts = time.time()
            current_drives = set(list_removable_drives())
            known_drives = set(tracked.keys())

            # Newly connected drives.
            for drive in current_drives - known_drives:
                if drive in pending_disconnect:
                    # Reappeared before the debounce window elapsed — same
                    # session, not a new device. Resume with the baseline we
                    # already had rather than re-scanning from scratch, so
                    # files already on it don't get reported as "written"
                    # again, and no duplicate connect event is sent either.
                    tracked[drive] = pending_disconnect.pop(drive)["info"]
                    log.info(
                        "%s reappeared within %ds — treating as still connected, not a new device",
                        drive, DEBOUNCE_SECONDS,
                    )
                    continue
                label, serial = get_volume_info(drive)
                device_name = label or drive.rstrip("\\")
                log.info("USB device connected: %s (serial %s)", device_name, serial or "unknown")
                baseline = scan_drive(drive, log)
                tracked[drive] = {"name": device_name, "serial": serial, "files": baseline}
                state.enqueue([{
                    "eventType": "connected",
                    "timestamp": _now_iso(),
                    "usbDeviceName": device_name,
                    "usbSerialNumber": serial,
                    "driveLetter": drive,
                }])

            # Drives that disappeared — start the debounce clock rather than
            # reporting a disconnect immediately.
            for drive in known_drives - current_drives:
                pending_disconnect[drive] = {"info": tracked.pop(drive), "disconnected_at": now_ts}

            # Drives that have been gone longer than the debounce window are
            # real disconnects.
            for drive in list(pending_disconnect.keys()):
                entry = pending_disconnect[drive]
                if now_ts - entry["disconnected_at"] >= DEBOUNCE_SECONDS:
                    info = pending_disconnect.pop(drive)["info"]
                    log.info("USB device disconnected: %s", info["name"])
                    state.enqueue([{
                        "eventType": "disconnected",
                        "timestamp": _now_iso(),
                        "usbDeviceName": info["name"],
                        "usbSerialNumber": info["serial"],
                        "driveLetter": drive,
                    }])

            # Still-connected drives: re-scan and report new/changed files.
            # (Checked against tracked's current keys, not the known_drives
            # snapshot from the top of the loop, so a drive just resumed from
            # pending_disconnect above is included in this cycle too.)
            for drive in current_drives & set(tracked.keys()):
                info = tracked[drive]
                current_files = scan_drive(drive, log)
                new_events = []
                for rel_path, (mtime, size) in current_files.items():
                    baseline_entry = info["files"].get(rel_path)
                    if baseline_entry is None or baseline_entry[0] != mtime or baseline_entry[1] != size:
                        new_events.append({
                            "eventType": "file_written",
                            "timestamp": _now_iso(),
                            "usbDeviceName": info["name"],
                            "usbSerialNumber": info["serial"],
                            "driveLetter": drive,
                            "filePath": rel_path,
                            "fileSizeBytes": size,
                        })
                if new_events:
                    log.info("%d new/changed file(s) written to %s", len(new_events), info["name"])
                    state.enqueue(new_events)
                # Merge rather than replace: on a drive over MAX_FILES_PER_DRIVE_SCAN,
                # scan_drive() only returns a partial snapshot, and os.walk's traversal
                # order can shift slightly between polls (files added/removed elsewhere
                # on the drive). Overwriting the baseline with just this cycle's partial
                # scan would forget any file that isn't rescanned this time, causing it
                # to be reported as "new" all over again the next time it IS rescanned.
                # Merging keeps every previously-known file's last-seen state unless
                # this cycle's scan actually saw (and possibly updated) it.
                merged_baseline = dict(info["files"])
                merged_baseline.update(current_files)
                info["files"] = merged_baseline

        except Exception:
            log.exception("Unexpected error during USB scan — continuing")

        try:
            flush_outbox(state, cfg, session, log)
        except Exception:
            log.exception("Unexpected error during flush — continuing")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
