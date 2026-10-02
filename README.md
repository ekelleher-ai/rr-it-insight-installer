# RR-IT Insight — installer

Builds the single master Windows installer for RR-IT Insight: one admin-elevated
`.exe` that installs ActivityWatch, the compiled pusher, a Scheduled Task to keep
it running, and force-installs the `aw-watcher-web` browser extension for both
Chrome and Edge — no manual steps beyond the initial UAC prompt and pasting in
the client's Client ID / Ingest API Key when asked.

## How it's built

`.github/workflows/build.yml` runs on a Windows GitHub Actions runner (this repo
doesn't need Windows locally) on every push to `main`, or manually via the
Actions tab ("Run workflow"). It:

1. Compiles `pusher/aw_pusher.py` into a standalone `aw_pusher.exe` with
   PyInstaller — no Python install needed on client machines.
2. Downloads the latest ActivityWatch Windows installer straight from their
   GitHub releases.
3. Compiles `installer/installer.iss` (Inno Setup) into `RR-IT-Insight-Setup.exe`,
   bundling both of the above.
4. Uploads the finished installer as a workflow artifact — go to the **Actions**
   tab, open the latest run, and download it from there.

## Reliability architecture (v2.0.0+)

A live incident on the "Bowmans x Laptop" pilot machine (tenant "Test Clint")
found the original Scheduled-Task-based Pusher/USB Watcher unreliable for 5
separate reasons:

1. A task set to "run only when user is logged on" dies the moment that
   session ends (logoff, not just a crash).
2. Switching it to "run whether logged on or not" breaks USB write detection
   instead — Session-0 isolation means it no longer has the interactive
   session USB Watcher needs.
3. The logon trigger doesn't always fire.
4. The default AC-power condition silently blocks everything on battery, with
   nothing logged anywhere to say why.
5. Windows auto-disables a task after enough repeated failures (e.g. during
   reboot testing), again with no visible alert.

v2.0.0 replaced both components:

- **Pusher → Windows Service.** Installed via bundled **NSSM** rather than a
  Scheduled Task: starts at boot with nobody logged in, has no power
  condition, and gets NSSM's own restart-on-exit policy. Diagnose it the
  normal Windows-service way — `services.msc`, `sc query "RR-IT Insight
  Pusher"`, or `Get-Service "RR-IT Insight Pusher"` — rather than via Task
  Scheduler.
- **USB Watcher → rebuilt Scheduled Task.** Registered from Task XML (not the
  plain `schtasks /Create /SC ONLOGON` form) with its run-as principal set to
  the built-in **Users group** (`S-1-5-32-545`) instead of one named account
  — works for any user who logs on, no per-user install, no stored password
  — both AC-power conditions explicitly off, and dual logon+boot triggers.
  Diagnose it via `taskschd.msc` under "RR-IT Insight USB Watcher", same as
  before.
- **New Watchdog task.** Runs as SYSTEM every 15 minutes
  (`installer/watchdog.ps1`, registered as "RR-IT Insight Watchdog") and
  re-enables/restarts either the Pusher service or the USB Watcher task if
  something external — an RMM tool, an AV product — disables them. Logs to
  `C:\ProgramData\RR-IT Insight\watchdog.log`.
- Upgrading from a pre-2.0.0 install removes the legacy Scheduled-Task-based
  pusher automatically, on both install and uninstall.

**Still open, not part of this fix:** whether the RMM tool Action1 was the one
disabling the Scheduled Tasks in the first place hasn't been confirmed — that
investigation continues separately from this reliability rework.

This should be tested end-to-end on a real Windows machine (ideally the Test
Clint pilot laptop again) before being relied on for other clients.

## Upload schedule and Zite workflow runs (v2.0.0.14+)

Every upload from a device is one Zite "workflow run", and the plan allows a
fixed number per month across all devices (Business: 50,000). Before
v2.0.0.14 the pusher uploaded roughly every 40 seconds, 24/7 while a PC was
on — about 2,000 runs a day per PC.

The pusher still reads ActivityWatch every 30 seconds and queues everything
locally (nothing is recorded differently or lost), but it only **uploads**:
- once per **upload interval** while there's real activity queued — set per
  client in the RR-IT console ("Upload every", default 30 minutes, 5–240);
- every **4 hours** at most while only idle time is queued (PC locked or
  unattended, left on overnight);
- straight away the very first time after install, so a new device appears
  on the dashboard immediately.

The server returns the client's interval on every upload and the pusher
stores it locally, so changing it in the console reaches each device at its
next upload — no reinstall. `upload_interval_minutes` in `config.json` is
only the starting value. Consecutive queued rows for the same window are
merged into one event before upload (totals unchanged).

Rough cost per PC per month (office hours) when uploading straight to Zite:
~530 runs at 30 minutes, ~330 at 60. From v2.0.0.15 uploads go through the
receiver instead (next section), which makes the Zite cost independent of the
number of PCs.

## Receiver (v2.0.0.15+)

Activity and USB uploads go to the **RR-IT receiver**
(`https://rrit-receiver.ekelleher.workers.dev`, a Cloudflare Worker with a D1
database in the EU, shared with Device Monitor but on its own `/ins/` routes,
tables and secret). The Insight Zite app collects everything queued there in
**one run every 5 minutes**, about 8,600 runs a month whatever the number of
PCs. Because uploads no longer cost Zite runs, the default upload interval via
the receiver is **5 minutes**, so dashboards are at most ~10 minutes behind.
The console's "Upload every" setting still applies.

- **Fallback**: if the receiver can't be reached or answers 404/5xx, the same
  upload goes straight to Zite (the pre-2.0.0.15 path). If Zite hasn't
  collected for 20+ minutes, the receiver tells the PC to *also* post directly
  so dashboards don't go stale; Zite ignores the duplicate when it collects.
- The receiver stores each upload as one row **without the ingest key** (it
  keeps a SHA-256 of it); Zite checks that hash against the client's real key
  before accepting anything. Suspended clients and clients without USB
  Monitoring get the same 403s as from Zite.
- USB events can now take up to ~5 minutes to appear (they used to be
  immediate).
- `"receiver_url": ""` in a device's `config.json` turns the receiver off for
  that PC (straight to Zite, as before).

## Live view (v2.0.0.15+, opt-in per client)

When RR-IT switches Live view on for a client in the console, each PC sends a
tiny status to the receiver about once a minute: the **current app or website
name** and **active/idle** (and since when). Never window titles, URLs or
screen content. The dashboard's Live tab reads it directly from the receiver
with a short-lived pass issued by Zite, so live updates cost no Zite runs.
Staff names never leave Zite; the receiver only holds host names. Live rows
are removed when Live is switched off. Update the client's monitoring notice
before switching it on.

"Refresh now" (Live clients): asks that client's PCs to upload immediately;
they act on it within about a minute. Limited to once every 5 minutes.

## Auto-update (v2.0.0.15+)

From v2.0.0.15, PCs update themselves to the version RR-IT **approves per
client** in the console. Nothing updates until a version is approved, and
never to an older version. How it's kept safe:

1. The pusher only writes the approved version number to
   `update-target.json` in the install folder (Program Files — standard users
   can't write there).
2. The Watchdog task (SYSTEM, every 15 minutes) downloads `manifest.json`,
   `manifest.sig` and the installer for exactly that version from this repo's
   GitHub release (fixed URL).
3. It checks `manifest.sig` against the **release-signing public key built
   into `watchdog.ps1`** (`installer/update-signing-public.pem` is the same
   key), then the installer's size and SHA-256 against the signed manifest.
   Anything that doesn't match is refused.
4. It runs the installer silently with `/RRITUPDATE=1`, which keeps
   `config.json` and leaves ActivityWatch alone.

Releases are signed by the build (`installer/sign_release.py`) with the
private key held in the **`UPDATE_SIGNING_KEY`** repository secret. A tag build
fails if the secret is missing, the key doesn't match the public key, or the
tag isn't exactly `v` + the installer's version. A failing update is retried
at most hourly and abandoned after 3 attempts until a different version is
approved. Progress is in `update-status.json` / `watchdog.log` and is shown in
the console next to each PC's version.

**Watchdog fix in v2.0.0.15**: until this release `watchdog.ps1` contained
non-ASCII dashes, which Windows PowerShell 5.1 misreads in a file without a
BOM, and the script failed to parse, so the watchdog never actually ran on
client PCs. The file is now plain ASCII. Keep it that way.

**Installer change**: the Watchdog task's run-time limit went from 5 to 30
minutes, to allow for the ~100 MB download. A hand-added `extra_watch_drives`
line in `config.json` is now kept across reinstalls/updates.

## Diagnosing a client machine

The pusher runs invisibly by design (built with `--noconsole` — a client should
never see a black window pop up at logon) and logs to
`C:\ProgramData\RR-IT Insight\pusher.log` instead. That's the first thing to
check if a client reports missing data: confirm the log is growing, and look
for repeated "Could not reach local ActivityWatch API" warnings, which usually
just means ActivityWatch itself isn't running (check for `aw-qt.exe` /
`aw-server.exe` in Task Manager).

As of v2.0.0, also check the Pusher's own Windows Service status
(`Get-Service "RR-IT Insight Pusher"`) and `C:\ProgramData\RR-IT
Insight\watchdog.log` for anything the Watchdog task had to fix recently
(e.g. "Pusher service was Disabled — re-enabling") — a pattern of repeated
watchdog interventions on one machine is worth investigating rather than
just letting the watchdog quietly paper over it every 15 minutes.

## Uninstalling (client cancels the service)

Uninstalling — from Windows Settings > Apps, or by running `unins000.exe`
under the install folder — is a **full removal**, not just this app. It asks
for confirmation, then:

1. Stops and removes the "RR-IT Insight Pusher" Windows Service (via NSSM),
   deletes the "RR-IT Insight USB Watcher" and "RR-IT Insight Watchdog"
   Scheduled Tasks, and kills `aw_pusher.exe`/`usb_watcher.exe` if still
   running.
2. If confirmed: kills ActivityWatch's processes, runs **ActivityWatch's own
   uninstaller** silently (found wherever it landed, matching the installer's
   own search logic), deletes its Startup-folder shortcut as a backstop, and
   removes the forced `aw-watcher-web` browser-extension policy for Chrome
   and Edge.
3. Deletes `C:\ProgramData\RR-IT Insight\` (the pusher's log and local state)
   either way.

A client who cancels is left with nothing from RR-IT Insight still running,
autostarting, or logging on their machine.

## Antivirus exclusions

Confirmed live on a test machine: COMODO silently disabled our Scheduled Task
after install with no visible error — the pusher looked "installed" but never
ran again after the first logon. Most RR-IT clients run one of COMODO, McAfee,
or plain Windows Defender, so exclusions need adding for all three:

- **Windows Defender** — handled automatically by the installer (`installer.iss`,
  `AddDefenderExclusions`), via `Add-MpPreference`. Nothing to do by hand.
- **COMODO and McAfee** — no safe, universal silent command across their many
  product editions, so this is a manual step during onboarding (2 minutes in
  each console). Add these as exclusions/exceptions:
  - **Paths:**
    - `C:\Program Files\RR-IT Insight\` (or wherever it was installed, if
      the admin changed the default during setup)
    - ActivityWatch's install folder — usually
      `C:\Users\<user>\AppData\Local\Programs\ActivityWatch\`, but check;
      some machines have it under Program Files instead
    - `C:\ProgramData\RR-IT Insight\`
  - **Processes:** `aw_pusher.exe`, `aw-qt.exe`, `aw-server.exe`,
    `aw-watcher-afk.exe`, `aw-watcher-window.exe`
  - In COMODO specifically, also check **HIPS** and **Containment** (not just
    the antivirus scan exclusions) — a HIPS rule silently blocking the
    Scheduled Task's action, rather than a virus scan quarantining a file,
    is what actually happened on the test machine.
  - Worth automating properly if this keeps coming up — COMODO does have a
    command-line config import (`cfp_config`/`cmdagent`) and McAfee's managed
    products can be scripted via ePO, but both need per-deployment testing
    before it'd be safe to run unattended from this installer; not done yet.

## USB removable-drive monitoring (optional, per-client add-on)

Off by default — only turn this on for a client with a specific requirement to
know when a USB storage device is used to copy files off a monitored PC.
Two things both have to be true for it to actually record anything:

1. **On the device**: the installer wizard's "Enable USB removable-drive
   monitoring on this device" checkbox must be ticked at install time. This
   registers a second Scheduled Task ("RR-IT Insight USB Watcher") running
   `usb_watcher.py`/`usb_watcher.exe`.
2. **On the client tenant**: the client's **USB Monitoring** toggle must be
   switched **On** in the RR-IT console's client list. `ingestUsbEvents`
   checks this server-side on every call, so a device with the checkbox
   ticked still sends nothing if the tenant hasn't been switched on (and
   vice versa — the console toggle alone does nothing without the device
   also being installed with the checkbox ticked).

**What it can and can't tell you** (read `usb_watcher.py`'s header for the
full reasoning, and say this plainly to the client too): it reliably detects
a USB drive being connected/disconnected, and files being written to it —
which covers the actual data-loss scenario most clients care about (someone
copying company files from the PC onto a USB stick). It does **not** detect
files copied FROM a USB drive onto the PC — Windows gives no cheap way to
watch file reads without a kernel driver, well beyond what this script (or
ActivityWatch itself) does. Don't present "no USB events" as "nothing was
copied off this machine" without that caveat.

**Which drives it watches** (v2.0.0.12+): USB flash drives and SD cards
(which Windows reports as "removable"), plus external USB hard drives and
SSDs (which Windows reports as "fixed", like an internal disk — before
v2.0.0.12 these were silently ignored). Internal disks and the Windows
system drive are never watched.

**Scanning schedule** (v2.0.0.13+): flash drives and SD cards are fully
scanned every 15 seconds, as before. External USB hard drives and SSDs are
checked every 15 seconds with a cheap free-space read that doesn't wake an
idle drive: if free space has changed at all, the drive is scanned straight
away, so ordinary copying (which always uses space) is still caught within
about 15 seconds, including a quick copy-then-unplug. On top of that they get
a full scan at least every 2 minutes, which catches the rare writes that
leave free space unchanged (a same-size overwrite, a delete and a copy that
cancel out, very small files). A permanently-connected backup drive is
therefore walked every 2 minutes rather than every 15 seconds; it still
wakes periodically, so it won't sleep for long stretches.

**Drives left plugged in** (v2.0.0.17+): the watcher remembers which drives
were connected (by volume serial number) when it stops — PC shut down, user
logged off, agent updated. When it starts again, the same drive found within
2 minutes is picked up quietly: no new "Device Connected" event and no USB
alert. A different drive at that letter, or a drive whose serial can't be
read, is still reported as connected. A remembered drive that isn't back
within 2 minutes is reported as disconnected (it was removed while the
watcher wasn't running). Files written to a drive while the watcher wasn't
running can't be detected.

**One report per file** (v2.0.0.14+): a newly-written file is reported once
it's been seen unchanged on two consecutive scans, so a large copy still in
progress is no longer reported twice (part-size, then full size). The report
carries the time the file was first seen. Anything still settling when the
drive is removed is reported at that point with its last-seen size, so a
quick copy-then-unplug is still caught.

**Very large drives**: a drive is enumerated up to 100,000 files / 20 seconds
per scan. On a drive bigger than that the file list is incomplete, so — to
avoid falsely reporting files that were already there — such a drive only
reports a file as written when its creation or modified time is at/after the
moment monitoring started. Creation time is what catches a copy: Windows
gives a copied file a new creation time but keeps the original's modified
time. `usb_watcher.log` notes when a drive is too large to enumerate fully.

**USB4 / Thunderbolt NVMe enclosures**: these can report their bus as NVMe
rather than USB, and are *not* auto-detected — an internal system NVMe disk
reports the same bus, and watching an internal disk would log everything the
user does locally. For the rare client with such an enclosure, add its drive
letter to an opt-in `extra_watch_drives` list in that device's `config.json`,
e.g. `"extra_watch_drives": ["G:"]`. The system drive is never watched even
if listed there by mistake.

Where it shows up: the client dashboard's Individual drill-down shows a "USB
removable-drive activity" table for that day, but only for a client with USB
Monitoring enabled — it's simply not present in the UI for anyone else.

GDPR/consent note: since this is more sensitive than app/website tracking,
make sure a client's monitoring notice specifically mentions USB monitoring
before switching it on for them — the standard delivered template doesn't
call this out by default since it's not part of the default build.

## Before shipping this to a client

- **aw-watcher-web extension ID** — confirmed and wired in: `nglaklhklhcoonedhgnpgddginnjdadi`,
  the official "ActivityWatch Web Watcher" listing published by ActivityWatch
  ([Chrome Web Store](https://chromewebstore.google.com/detail/activitywatch-web-watcher/nglaklhklhcoonedhgnpgddginnjdadi)).
  Still worth a one-off real-machine test before relying on it for a client —
  in particular, Edge accepting an install from the Chrome Web Store's update
  URL via policy is common but not guaranteed; if it doesn't take, aw-watcher-web
  may need its own Edge Add-ons listing instead.
- **Per-client keys**: right now the installer asks the admin to paste in the
  Client ID and Ingest API Key during setup (given to them by RR-IT). A future
  "Generate installer" button in the client dashboard (planned, not built yet)
  would stamp these into the installer automatically so nothing needs typing in
  by hand.

## Repo layout

```
pusher/             the ActivityWatch pusher script + its config template
  aw_pusher.py        always installed — app/website activity
  usb_watcher.py       optional add-on — USB removable-drive activity
installer/
  installer.iss      Inno Setup script — the actual installer logic
  watchdog.ps1       re-enables/restarts the Pusher service or USB Watcher
                       task if something external disables them (v2.0.0+),
                       and installs approved, signed updates (v2.0.0.15+).
                       Keep it plain ASCII (PowerShell 5.1).
  sign_release.py    signs each release's manifest for auto-update (CI)
  update-signing-public.pem   public half of the release-signing key
  staging/            build output lands here (gitignored, created by CI —
                       compiled pusher/usb_watcher exes, the bundled AW
                       installer, and NSSM)
.github/workflows/   the Windows-runner build pipeline
```
