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

## Before shipping this to a client

- **Confirm the aw-watcher-web extension ID.** `installer/installer.iss` has a
  placeholder `ExtensionId` constant — look up aw-watcher-web's real Chrome
  Web Store listing and paste its ID in before building for real use. If Edge
  doesn't accept installs from the Chrome Web Store update URL, it may need
  its own Edge Add-ons listing instead — worth testing once on a spare machine.
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
