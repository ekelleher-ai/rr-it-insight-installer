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
  - Detects a removable drive appearing/disappearing (by drive letter) —
    flash drives/SD cards, and (from v2.0.0.12) external USB hard drives
    and SSDs, which Windows reports as "fixed" disks rather than removable.
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
from datetime import datetime, timezone, timedelta
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
# Safety limits so a huge drive can't hang a poll cycle. A scan stops at
# whichever comes first: the file cap or the wall-clock budget. The cap is
# generous enough that the vast majority of real drives enumerate fully
# (giving a COMPLETE baseline — see the connect handler and find_written_files),
# and the time budget is the real backstop for a pathologically large one.
MAX_FILES_PER_DRIVE_SCAN = 100000
SCAN_TIME_BUDGET_SECONDS = 20

# When a drive's baseline scan was truncated (too many files to enumerate
# fully — see above), the per-file "is this new" comparison can't be trusted
# on its own: a pre-existing file beyond the cap that a later scan happens to
# reach would look new purely because it wasn't in the (incomplete) baseline.
# For a truncated-baseline drive ONLY, a file is therefore reported as
# written only when its CREATION time (or its modified time) is at/after the
# moment monitoring started, minus this margin to tolerate filesystem/clock
# skew (FAT/exFAT store timestamps in local time).
#
# Creation time is the one that matters: when Windows copies a file onto a
# drive, the copy gets a NEW creation time (the moment of the copy) but KEEPS
# the source file's old modified time. Checking modified time alone would
# therefore ignore a genuinely copied file — caught in review before release.
# Modified time is still checked too, so an existing file edited in place is
# reported. A pre-existing file has both timestamps old, so it's never
# mis-reported. Not used for a complete baseline, where every pre-existing
# file is already known by name.
TRUNCATED_BASELINE_TIME_MARGIN_SECONDS = 4 * 3600

# External USB hard drives/SSDs (DRIVE_FIXED volumes, plus anything in
# extra_watch_drives) can be large, and walking every file on one every 15
# seconds keeps a spinning drive permanently busy. For those drives only:
#   - every cycle, a cheap free-space read (doesn't wake an idle drive); if
#     free space has changed at all, scan straight away — ordinary copying
#     always changes free space, so a quick copy-then-unplug is still caught
#     within one cycle, same as before;
#   - regardless, a full scan at least every FIXED_DRIVE_FULL_SCAN_SECONDS,
#     which catches the rare writes that leave free space unchanged (a
#     same-size overwrite, a delete and a copy that cancel out within one
#     cycle, very small files stored inside the NTFS file table).
# Flash drives and SD cards (DRIVE_REMOVABLE) are small and don't spin, so
# they keep a full scan every cycle, unchanged.
FIXED_DRIVE_FULL_SCAN_SECONDS = 120

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

# Windows drive type constants (from GetDriveTypeW). USB flash drives and SD
# cards via a reader report DRIVE_REMOVABLE. Most external USB hard drives
# and SSDs report DRIVE_FIXED instead — the same type as an internal disk —
# so those are only watched when the storage bus underneath them is USB
# (see _is_usb_bus_drive). Confirmed live on Edmond's PC (25 Sept): an
# external USB hard drive was plugged in and written to with no event at
# all, while a flash drive's connect/disconnect was reported correctly.
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3

# IOCTL_STORAGE_QUERY_PROPERTY / STORAGE_DEVICE_DESCRIPTOR (winioctl.h),
# used to ask a DRIVE_FIXED volume which bus it's attached through.
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
STORAGE_DEVICE_PROPERTY = 0       # PropertyId: StorageDeviceProperty
PROPERTY_STANDARD_QUERY = 0       # QueryType: PropertyStandardQuery
BUS_TYPE_OFFSET = 28              # byte offset of BusType in STORAGE_DEVICE_DESCRIPTOR
BUS_TYPE_USB = 7                  # STORAGE_BUS_TYPE BusTypeUsb (UASP enclosures report this too)
OPEN_EXISTING = 3
FILE_SHARE_READ_WRITE = 0x1 | 0x2

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
    # Optional, opt-in list of extra drive letters to watch that auto-detection
    # can't safely catch (a USB4/Thunderbolt NVMe enclosure reporting an NVMe
    # bus). Empty for almost every device. See list_removable_drives().
    extra_watch_drives: list[str]

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
            extra_watch_drives=(
                raw.get("extra_watch_drives")
                if isinstance(raw.get("extra_watch_drives"), list)
                else []
            ),
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

    def delete_older_than(self, cutoff_iso: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM usb_outbox WHERE queued_at_iso < ?", (cutoff_iso,)
        )
        self.conn.commit()
        return cur.rowcount


# --------------------------------------------------------------------------
# Drive detection (Windows API via ctypes — no extra pip dependency)
# --------------------------------------------------------------------------

_IOCTL_KERNEL32 = None


def _ioctl_kernel32():
    """A private kernel32 handle with explicit 64-bit-safe signatures for the
    three calls _is_usb_bus_drive needs. Kept separate from ctypes.windll so
    setting argtypes/restype here can't change how any other call in this
    file behaves, and built once rather than on every poll."""
    global _IOCTL_KERNEL32
    if _IOCTL_KERNEL32 is None:
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k.CreateFileW.restype = wintypes.HANDLE
        k.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        k.DeviceIoControl.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        _IOCTL_KERNEL32 = k
    return _IOCTL_KERNEL32


def _is_usb_bus_drive(drive_letter: str) -> bool:
    """True if the volume at drive_letter (e.g. 'E:\\\\') sits on a USB bus.

    Opens the volume with zero desired access — enough for
    IOCTL_STORAGE_QUERY_PROPERTY and allowed for a standard (non-admin)
    user, which matters because this watcher runs as the logged-on user.
    Fails closed (False) on any error, so an internal disk can never be
    mistaken for a USB one; the worst case is the pre-fix behaviour of an
    external drive going unwatched."""
    from ctypes import wintypes

    kernel32 = _ioctl_kernel32()
    volume_path = "\\\\.\\" + drive_letter.rstrip("\\")   # E:\ -> \\.\E:
    handle = kernel32.CreateFileW(volume_path, 0, FILE_SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        return False
    try:
        # STORAGE_PROPERTY_QUERY: PropertyId, QueryType, AdditionalParameters[1] (padded to 12 bytes)
        query = (ctypes.c_uint32 * 3)(STORAGE_DEVICE_PROPERTY, PROPERTY_STANDARD_QUERY, 0)
        out_buf = ctypes.create_string_buffer(1024)
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            handle, IOCTL_STORAGE_QUERY_PROPERTY,
            ctypes.byref(query), ctypes.sizeof(query),
            out_buf, ctypes.sizeof(out_buf),
            ctypes.byref(returned), None,
        )
        if not ok or returned.value < BUS_TYPE_OFFSET + 4:
            return False
        bus_type = int.from_bytes(out_buf.raw[BUS_TYPE_OFFSET:BUS_TYPE_OFFSET + 4], "little")
        return bus_type == BUS_TYPE_USB
    finally:
        kernel32.CloseHandle(handle)


def _system_drive_letter() -> str:
    """e.g. 'C:\\\\' — never treated as a USB drive, even if Windows itself
    boots from USB (Windows To Go), since everything on it would look like a
    file 'written to a USB device'."""
    return (os.environ.get("SystemDrive") or "C:").rstrip("\\").upper() + "\\"


def _normalize_drive_letter(value: str) -> Optional[str]:
    """Turn 'g', 'G', 'G:', 'G:\\' into the canonical 'G:\\' form, or None if
    it isn't a single drive letter."""
    if not value:
        return None
    c = value.strip().rstrip("\\").rstrip(":").upper()
    if len(c) == 1 and "A" <= c <= "Z":
        return c + ":\\"
    return None


def list_removable_drives(extra_watch_drives: Optional[list[str]] = None) -> list[str]:
    """Return drive letters (e.g. ['E:\\\\']) of USB storage to watch:
    anything Windows reports as DRIVE_REMOVABLE (flash drives, SD cards),
    plus DRIVE_FIXED volumes on a USB bus (external hard drives/SSDs).

    `extra_watch_drives` is an explicit, opt-in list of drive letters to also
    watch when present, for the narrow case a normal USB HDD/SSD can't cover:
    a Thunderbolt/USB4 NVMe enclosure that reports its bus as NVMe rather than
    USB. Those are deliberately NOT auto-detected — an internal system/data
    NVMe disk reports the exact same bus type, and wrongly watching an
    internal disk would log everything the user does locally. So this stays a
    named, RR-IT-set choice per device, never a guess. The system drive is
    still never watched, even if it's listed here by mistake."""
    system_drive = _system_drive_letter()
    extra = set()
    for v in (extra_watch_drives or []):
        norm = _normalize_drive_letter(v)
        if norm:
            extra.add(norm)

    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if not (bitmask & (1 << i)):
            continue
        letter = f"{chr(65 + i)}:\\"
        if letter == system_drive:
            continue  # never the OS drive, whatever the bus or the allowlist says
        try:
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(letter)
            if drive_type == DRIVE_REMOVABLE:
                drives.append(letter)
            elif drive_type == DRIVE_FIXED and _is_usb_bus_drive(letter):
                drives.append(letter)
            elif letter in extra and drive_type in (DRIVE_REMOVABLE, DRIVE_FIXED):
                # Explicitly allow-listed (e.g. a USB4/Thunderbolt NVMe
                # enclosure). Still require it to be a real disk volume, not a
                # network/CD/RAM drive.
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


def get_free_bytes(drive_letter: str) -> Optional[int]:
    """Free bytes available on the drive, via GetDiskFreeSpaceExW — or None
    if it can't be read. This is a cheap filesystem-metadata call (normally
    served from cache) that does NOT force a spun-down drive back awake the
    way walking its whole tree does. For external hard drives/SSDs the poll
    loop uses a change in this value as the signal to scan immediately,
    between the less frequent scheduled full scans (see
    FIXED_DRIVE_FULL_SCAN_SECONDS)."""
    try:
        free = ctypes.c_ulonglong(0)
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(drive_letter),
            ctypes.byref(free),   # lpFreeBytesAvailableToCaller
            None,                 # lpTotalNumberOfBytes
            None,                 # lpTotalNumberOfFreeBytes
        )
        if not ok:
            return None
        return free.value
    except Exception:
        return None


def _creation_time(st: os.stat_result) -> float:
    """File creation time. On Windows, Python 3.11 reports creation time as
    st_ctime; Python 3.12+ adds st_birthtime for it (and st_ctime starts to
    mean metadata-change time). Prefer st_birthtime where it exists."""
    return getattr(st, "st_birthtime", st.st_ctime)


def scan_drive(drive_letter: str, log: logging.Logger) -> tuple[dict[str, tuple[float, int, float]], bool]:
    """Return ({relative_path: (mtime, size, creation_time)}, truncated).

    `truncated` is True when the scan stopped early — at MAX_FILES_PER_DRIVE_SCAN
    or SCAN_TIME_BUDGET_SECONDS — so the caller knows the snapshot is
    incomplete and the per-file "is this new" comparison can't be trusted on
    its own for this drive (see the mtime guard in the poll loop)."""
    snapshot: dict[str, tuple[float, int, float]] = {}
    count = 0
    truncated = False
    deadline = time.monotonic() + SCAN_TIME_BUDGET_SECONDS
    try:
        for root, dirs, files in os.walk(drive_letter):
            # Don't even descend into Windows' own housekeeping folders —
            # cheaper than filtering afterwards, and keeps a $Recycle.Bin
            # full of a client's own deleted files out of the report too.
            dirs[:] = [d for d in dirs if d.lower() not in IGNORED_DIR_NAMES]
            for name in files:
                if name.lower() in IGNORED_FILE_NAMES:
                    continue
                if count >= MAX_FILES_PER_DRIVE_SCAN or time.monotonic() > deadline:
                    log.warning(
                        "%s is too large to enumerate fully (stopped after %d files / %ds) "
                        "— on this drive a file is only reported if its creation or "
                        "modified time is after monitoring started.",
                        drive_letter, count, SCAN_TIME_BUDGET_SECONDS,
                    )
                    return snapshot, True
                full_path = os.path.join(root, name)
                try:
                    stat = os.stat(full_path)
                except OSError:
                    continue
                rel_path = os.path.relpath(full_path, drive_letter)
                snapshot[rel_path] = (stat.st_mtime, stat.st_size, _creation_time(stat))
                count += 1
    except OSError as exc:
        log.warning("Could not scan %s: %s (drive may have been removed mid-scan)", drive_letter, exc)
    return snapshot, truncated


def _is_fixed_drive(drive_letter: str) -> bool:
    """True for a DRIVE_FIXED volume (an external USB hard drive/SSD, or an
    allow-listed one), which gets the hybrid scan schedule. False for a
    flash drive/SD card, or if the type can't be read — both of which keep
    the full scan every cycle, the safe default."""
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(drive_letter) == DRIVE_FIXED
    except Exception:
        return False


def should_scan_drive(info: dict, cur_free: Optional[int], now_mono: float) -> bool:
    """Whether to walk this drive's files this cycle.

    Flash drives/SD cards: always. External hard drives/SSDs: when free space
    has changed at all since the last look (ordinary copying always changes
    it, so this catches a copy within one cycle), when free space can't be
    read either now or last time (can't tell, so scan — the old behaviour),
    or when FIXED_DRIVE_FULL_SCAN_SECONDS have passed since the last full
    scan (catches the rare writes that leave free space unchanged)."""
    if not info.get("fixed"):
        return True
    prev_free = info.get("free_bytes")
    if prev_free is None or cur_free is None:
        return True
    if cur_free != prev_free:
        return True
    return now_mono - info.get("last_full_scan", 0.0) >= FIXED_DRIVE_FULL_SCAN_SECONDS


def find_written_files(info: dict, current_files: dict[str, tuple[float, int, float]]) -> list[str]:
    """Relative paths in current_files that count as written since the
    baseline: new, or changed size/modified time.

    On a truncated-baseline drive (too big to enumerate fully), "not in the
    baseline" can just mean the file was beyond the scan limit at connect, so
    such a file is only reported when its creation time or modified time is
    at/after monitoring started (minus TRUNCATED_BASELINE_TIME_MARGIN_SECONDS).
    Creation time is what catches a copy — Windows gives a copied file a new
    creation time but keeps the source's old modified time."""
    baseline = info["files"]
    check_times = not info.get("baseline_complete", True)
    cutoff = info.get("watch_started_at", 0.0) - TRUNCATED_BASELINE_TIME_MARGIN_SECONDS
    written = []
    for rel_path, (mtime, size, created) in current_files.items():
        entry = baseline.get(rel_path)
        if entry is not None and entry[0] == mtime and entry[1] == size:
            continue
        if check_times and max(mtime, created) < cutoff:
            continue
        written.append(rel_path)
    return written


# --------------------------------------------------------------------------
# Send loop — same shape as aw_pusher.py's flush_outbox
# --------------------------------------------------------------------------

MAX_PRE_CONSENT_QUEUE_AGE_HOURS = 24

_usb_outbox_backoff_seconds = 1
_usb_outbox_next_attempt_at = 0.0

# See aw_pusher.py's MAX_BAD_BATCH_RETRY_SECONDS: a 400/413/422 is retried
# for up to 24h after the batch's first rejection before it's dropped, since
# a temporary server-side fault returns the same 400 as a malformed batch.
MAX_BAD_BATCH_RETRY_SECONDS = 24 * 3600
_usb_outbox_rejected_head_id = None
_usb_outbox_rejected_since = 0.0

def flush_outbox(state: State, cfg: Config, session: requests.Session, log: logging.Logger) -> None:
    global _usb_outbox_backoff_seconds, _usb_outbox_next_attempt_at, _usb_outbox_rejected_head_id, _usb_outbox_rejected_since

    now = time.monotonic()
    if now < _usb_outbox_next_attempt_at:
        return

    while True:
        batch = state.peek_batch(SEND_BATCH_SIZE)
        if not batch:
            _usb_outbox_backoff_seconds = 1
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
            log.warning(
                "USB ingest POST failed (network): %s — will retry in %ss",
                exc, _usb_outbox_backoff_seconds,
            )
            _usb_outbox_next_attempt_at = time.monotonic() + _usb_outbox_backoff_seconds
            _usb_outbox_backoff_seconds = min(_usb_outbox_backoff_seconds * 2, MAX_BACKOFF_SECONDS)
            return

        if r.status_code == 200:
            state.delete_ids([row["id"] for row in batch])
            _usb_outbox_rejected_head_id = None
            log.info("Pushed %d USB events (outbox now %d)", len(batch), state.outbox_size())
            continue

        if r.status_code in (401, 403):
            log.error(
                "USB ingest rejected the request (%s): %s — check api_key/client_id, "
                "and that USB Monitoring is enabled for this client.",
                r.status_code, r.text[:300],
            )
            # Don't retain rejected USB activity indefinitely: if this
            # client's USB monitoring is simply off (or the key was
            # revoked), queuing forever means a flood of pre-consent
            # activity uploads the moment it's switched on later. Drop
            # anything that's been sitting here more than
            # MAX_PRE_CONSENT_QUEUE_AGE_HOURS instead.
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=MAX_PRE_CONSENT_QUEUE_AGE_HOURS)
            ).isoformat()
            dropped = state.delete_older_than(cutoff)
            if dropped:
                log.warning(
                    "Discarded %d queued USB events older than %dh while ingest is "
                    "rejected — not retaining USB activity indefinitely without "
                    "confirmed consent/config.",
                    dropped, MAX_PRE_CONSENT_QUEUE_AGE_HOURS,
                )
            _usb_outbox_next_attempt_at = time.monotonic() + MAX_BACKOFF_SECONDS
            return

        if r.status_code in (400, 413, 422):
            # 404 deliberately excluded — see aw_pusher.py's flush_outbox.
            head_id = batch[0]["id"]
            if head_id != _usb_outbox_rejected_head_id:
                _usb_outbox_rejected_head_id = head_id
                _usb_outbox_rejected_since = time.monotonic()
            rejected_for = time.monotonic() - _usb_outbox_rejected_since
            if rejected_for < MAX_BAD_BATCH_RETRY_SECONDS:
                log.warning(
                    "USB ingest rejected a batch of %d events (%s): %s — retrying in %ss "
                    "(rejected for %dm so far; dropped only after %dh of continuous "
                    "rejection, in case this is a temporary server-side fault).",
                    len(batch), r.status_code, r.text[:300], _usb_outbox_backoff_seconds,
                    rejected_for // 60, MAX_BAD_BATCH_RETRY_SECONDS // 3600,
                )
                _usb_outbox_next_attempt_at = time.monotonic() + _usb_outbox_backoff_seconds
                _usb_outbox_backoff_seconds = min(_usb_outbox_backoff_seconds * 2, MAX_BACKOFF_SECONDS)
                return
            log.error(
                "USB ingest has rejected the same batch of %d events (%s) continuously "
                "for %dh: %s — dropping it so later events aren't blocked forever.",
                len(batch), r.status_code, MAX_BAD_BATCH_RETRY_SECONDS // 3600, r.text[:300],
            )
            state.delete_ids([row["id"] for row in batch])
            _usb_outbox_rejected_head_id = None
            continue

        log.warning(
            "USB ingest returned %s: %s — will retry in %ss",
            r.status_code, r.text[:300], _usb_outbox_backoff_seconds,
        )
        _usb_outbox_next_attempt_at = time.monotonic() + _usb_outbox_backoff_seconds
        _usb_outbox_backoff_seconds = min(_usb_outbox_backoff_seconds * 2, MAX_BACKOFF_SECONDS)
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

    try:
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
    except (Exception, SystemExit):
        # SystemExit too: Config.load() raises it for a missing config.json.
        log.exception("Fatal error during startup — exiting")
        return

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
            current_drives = set(list_removable_drives(cfg.extra_watch_drives))
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
                baseline, truncated = scan_drive(drive, log)
                tracked[drive] = {
                    "name": device_name,
                    "serial": serial,
                    "files": baseline,
                    # A complete baseline means every pre-existing file is
                    # known, so a later "not in baseline" file is genuinely
                    # new. A truncated one can't promise that — see the mtime
                    # guard in the re-scan step below.
                    "baseline_complete": not truncated,
                    # When monitoring of THIS connection began — the cutoff the
                    # timestamp guard uses for a truncated-baseline drive.
                    "watch_started_at": now_ts,
                    # External HDD/SSD (or allow-listed) → hybrid schedule;
                    # flash drive/SD card → full scan every cycle.
                    "fixed": _is_fixed_drive(drive),
                    # Last known free space, and when the last full scan ran
                    # (the connect scan above counts as one).
                    "free_bytes": get_free_bytes(drive),
                    "last_full_scan": time.monotonic(),
                }
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

                # Decide whether to walk this drive's files this cycle — see
                # should_scan_drive() and FIXED_DRIVE_FULL_SCAN_SECONDS.
                cur_free = get_free_bytes(drive)
                now_mono = time.monotonic()
                if not should_scan_drive(info, cur_free, now_mono):
                    continue

                current_files, truncated = scan_drive(drive, log)
                info["last_full_scan"] = now_mono
                # A drive that started with a complete baseline but is now too
                # big to fully enumerate has become unreliable for exact
                # per-file diffing — treat it as truncated from here on.
                if truncated:
                    info["baseline_complete"] = False
                new_events = []
                for rel_path in find_written_files(info, current_files):
                    new_events.append({
                        "eventType": "file_written",
                        "timestamp": _now_iso(),
                        "usbDeviceName": info["name"],
                        "usbSerialNumber": info["serial"],
                        "driveLetter": drive,
                        "filePath": rel_path,
                        "fileSizeBytes": current_files[rel_path][1],
                    })
                if new_events:
                    log.info("%d new/changed file(s) written to %s", len(new_events), info["name"])
                    state.enqueue(new_events)
                # Merge rather than replace: on a drive too big to enumerate in
                # one pass, scan_drive() only returns a partial snapshot, and
                # os.walk's traversal order can shift slightly between polls
                # (files added/removed elsewhere on the drive). Overwriting the
                # baseline with just this cycle's partial scan would forget any
                # file that isn't rescanned this time, causing it to be reported
                # as "new" all over again the next time it IS rescanned. Merging
                # keeps every previously-known file's last-seen state unless this
                # cycle's scan actually saw (and possibly updated) it.
                merged_baseline = dict(info["files"])
                merged_baseline.update(current_files)
                info["files"] = merged_baseline
                # Record free space AFTER the scan, so the next cycle compares
                # against the state this scan saw. Keep the old value if it
                # couldn't be read this time.
                if cur_free is not None:
                    info["free_bytes"] = cur_free

        except Exception:
            log.exception("Unexpected error during USB scan — continuing")

        try:
            flush_outbox(state, cfg, session, log)
        except Exception:
            log.exception("Unexpected error during flush — continuing")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
