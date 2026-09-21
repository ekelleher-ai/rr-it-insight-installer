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

## Diagnosing a client machine

The pusher runs invisibly by design (built with `--noconsole` — a client should
never see a black window pop up at logon) and logs to
`C:\ProgramData\RR-IT Insight\pusher.log` instead. That's the first thing to
check if a client reports missing data: confirm the log is growing, and look
for repeated "Could not reach local ActivityWatch API" warnings, which usually
just means ActivityWatch itself isn't running (check for `aw-qt.exe` /
`aw-server.exe` in Task Manager).

## Uninstalling (client cancels the service)

Uninstalling — from Windows Settings > Apps, or by running `unins000.exe`
under the install folder — is a **full removal**, not just this app. It asks
for confirmation, then:

1. Deletes the "RR-IT Insight Pusher" Scheduled Task and kills `aw_pusher.exe`.
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
  staging/            build output lands here (gitignored, created by CI)
.github/workflows/   the Windows-runner build pipeline
```
