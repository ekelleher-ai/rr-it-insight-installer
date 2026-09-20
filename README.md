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
installer/
  installer.iss      Inno Setup script — the actual installer logic
  staging/            build output lands here (gitignored, created by CI)
.github/workflows/   the Windows-runner build pipeline
```
