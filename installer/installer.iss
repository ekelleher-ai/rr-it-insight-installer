; RR-IT Insight — master Windows installer
; ------------------------------------------------------------------
; Built with Inno Setup (compiled by the GitHub Actions workflow in
; .github/workflows/build.yml, which runs on a Windows runner).
;
; What this does, in order, when an admin runs the resulting .exe and
; clicks through the UAC prompt:
;   1. Asks for this client's Client ID + Ingest API Key (given to them
;      by RR-IT) on a custom wizard page.
;   2. Silently installs ActivityWatch (bundled AW installer, run with
;      /VERYSILENT since AW itself is also built with Inno Setup).
;   3. Copies the compiled pusher (aw_pusher.exe, built by PyInstaller —
;      no Python runtime needed on the client machine) into Program Files,
;      writing config.json with the Client ID / API key from step 1.
;   4. Installs the pusher as a genuine Windows Service (via bundled NSSM)
;      so it starts at boot, runs whether or not anyone is logged on, has
;      no AC-power condition, and restarts itself on any exit — see the
;      "Reliability architecture (v2.0.0+)" section in README.md for the
;      incident (a Scheduled-Task-based pusher/USB watcher found unreliable
;      for 5 separate reasons) that this replaced.
;   4a. Registers a Watchdog Scheduled Task (SYSTEM, every 15 minutes) that
;      re-enables/restarts the Pusher service or USB Watcher task if
;      something external (an RMM tool, an AV product) disables them.
;   4b. Adds Windows Defender exclusions for our own folders/processes
;      (silent, best-effort — no-op if Defender isn't the active AV).
;      COMODO and McAfee need the same done manually — see README.md.
;   4c. OPTIONAL, off by default: if the wizard's "USB removable-drive
;      monitoring" checkbox is ticked, also registers a Scheduled Task
;      for usb_watcher.exe, built from Task XML with the run-as principal
;      set to the built-in Users group (so it works for any interactively
;      logged-on user, no per-user install) and no AC-power condition —
;      see that script's own header and README.md's "USB removable-drive
;      monitoring" section for what it does and its limits. Only tick this
;      for a client who specifically asked for it; it also still requires
;      this client to be enabled for USB Monitoring in the RR-IT console
;      before anything is recorded.
;   5. Force-installs the aw-watcher-web browser extension for BOTH Chrome
;      and Edge via the ExtensionInstallForcelist registry policy, so it
;      works whichever browser(s) this client's staff actually use —
;      unconditionally installing into both is simpler and harmless if a
;      browser isn't present (the policy just sits unused).
;
; Extension ID confirmed against the official Chrome Web Store listing
; ("ActivityWatch Web Watcher", published by ActivityWatch, linked to
; github.com/ActivityWatch/aw-watcher-web):
; https://chromewebstore.google.com/detail/activitywatch-web-watcher/nglaklhklhcoonedhgnpgddginnjdadi
; Untested on Edge — if Edge's Chrome-Web-Store-via-policy install doesn't
; work in practice, it may need its own Edge Add-ons listing instead. Worth
; a one-off test on a spare machine before relying on it for a real client.
;
; Uninstalling (via "Uninstall a program" in Windows Settings, or by running
; unins000.exe under the install folder) is a FULL removal, not just this
; app: it asks to confirm, then stops and removes the pusher, ActivityWatch
; itself (via AW's own uninstaller), AW's Startup-folder shortcut, the forced
; browser-extension policy, and C:\ProgramData\RR-IT Insight\ — a client who
; cancels the service is left with nothing still running or logging.

#define MyAppName "RR-IT Insight Agent"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "Rapid Response IT"
#define ExtensionId "nglaklhklhcoonedhgnpgddginnjdadi"
#define ExtensionUpdateUrl "https://clients2.google.com/service/update2/crx"

[Setup]
AppId={{B6D6E2B1-4E9B-4C2B-9B3E-RRITINSIGHT01}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\RR-IT Insight
DefaultGroupName=RR-IT Insight
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputBaseFilename=RR-IT-Insight-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Bundled ActivityWatch installer — staged into installer\staging\ by the
; CI workflow before compiling this script (see build.yml).
Source: "staging\activitywatch-setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
; Pusher — PyInstaller onefile build, staged by CI.
Source: "staging\aw_pusher.exe"; DestDir: "{app}"; Flags: ignoreversion
; USB watcher — optional add-on, always shipped in the installer but only
; ever run when the wizard's checkbox was ticked (see WriteConfigFile and
; CurStepChanged below). Shipping it unconditionally is simpler than a
; second CI build variant; it just sits unused for clients who don't need it.
Source: "staging\usb_watcher.exe"; DestDir: "{app}"; Flags: ignoreversion
; NSSM ("the Non-Sucking Service Manager") — wraps aw_pusher.exe as a real
; Windows Service. Downloaded and staged by the CI workflow (see
; build.yml); not committed to the repo.
Source: "staging\nssm.exe"; DestDir: "{app}"; Flags: ignoreversion
; Watchdog script — a real source file in this repo (not a build output),
; so it's referenced directly rather than via staging\.
Source: "watchdog.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\pusher\config.example.json"; DestDir: "{app}"; DestName: "config.json.template"; Flags: ignoreversion

[Code]
const
  // Kept as a named constant even though its string is the same as the
  // pre-2.0.0 pusher's Scheduled Task name below — this is the OLD task
  // that upgrades must remove, not the new Service.
  LegacyPusherTaskName = 'RR-IT Insight Pusher';
  PusherServiceName = 'RR-IT Insight Pusher';
  UsbWatcherTaskName = 'RR-IT Insight USB Watcher';
  WatchdogTaskName = 'RR-IT Insight Watchdog';
  // Well-known SIDs — used so the USB Watcher task runs for ANY
  // interactively logged-on user (not one named account) and the Watchdog
  // task runs as SYSTEM. Only reachable via a Task XML definition — the
  // plain `schtasks /Create /RU ...` command-line form has no way to name
  // a group as the run-as principal, only a single named account.
  SidUsersGroup = 'S-1-5-32-545';
  SidLocalSystem = 'S-1-5-18';

var
  ConfigPage: TInputQueryWizardPage;
  UsbPage: TInputOptionWizardPage;

procedure InitializeWizard;
begin
  ConfigPage := CreateInputQueryPage(wpSelectDir,
    'RR-IT Insight Configuration',
    'Enter this client''s connection details',
    'You should have been given these by RR-IT. If not, contact support before continuing.');
  ConfigPage.Add('Client ID:', False);
  ConfigPage.Add('Ingest API Key:', True);

  // Optional add-on, off by default — only for a client with a specific
  // requirement to know about files copied onto a USB drive. See
  // usb_watcher.py's own header for exactly what this can and can't see.
  UsbPage := CreateInputOptionPage(ConfigPage.ID,
    'USB Removable-Drive Monitoring (optional)',
    'Only enable this if RR-IT told you this client specifically needs it',
    'When enabled, this device will log when a USB storage device is plugged in, ' +
    'unplugged, and which files are written to it. Leave this unticked for a standard install — ' +
    'most clients don''t need it, and it also has to be switched on for this client in the RR-IT console ' +
    'before anything is actually recorded.',
    False, False);
  UsbPage.Add('Enable USB removable-drive monitoring on this device');
  UsbPage.Values[0] := False;
end;

function UsbMonitoringEnabled(): Boolean;
begin
  Result := UsbPage.Values[0];
end;

function GetClientId(Param: string): string;
begin
  Result := ConfigPage.Values[0];
end;

function GetApiKey(Param: string): string;
begin
  Result := ConfigPage.Values[1];
end;

procedure WriteConfigFile;
var
  ConfigPath: string;
  Lines: TArrayOfString;
  UsbEnabledStr: string;
begin
  if UsbMonitoringEnabled() then
    UsbEnabledStr := 'true'
  else
    UsbEnabledStr := 'false';

  ConfigPath := ExpandConstant('{app}\config.json');
  SetArrayLength(Lines, 8);
  Lines[0] := '{';
  Lines[1] := '  "aw_api_url": "http://localhost:5600",';
  Lines[2] := '  "zite_ingest_url": "https://2wgpdcmeym.zite.so/api/ingestEvents",';
  Lines[3] := '  "api_key": "' + ConfigPage.Values[1] + '",';
  Lines[4] := '  "client_id": "' + ConfigPage.Values[0] + '",';
  Lines[5] := '  "poll_interval_seconds": 30,';
  Lines[6] := '  "usb_monitoring_enabled": ' + UsbEnabledStr;
  Lines[7] := '}';
  SaveStringsToFile(ConfigPath, Lines, False);
end;

// ActivityWatch's own installer is silent (/VERYSILENT), so nothing launches
// it afterwards and nothing guarantees it starts at the next logon either —
// confirmed by testing: the pusher started before AW did and had to retry
// for several minutes until AW was opened by hand. This finds wherever AW
// actually landed (its own installer can go to either location depending on
// version) and both starts it right away and registers it to autostart.
function FindActivityWatchExe(): string;
var
  Candidate: string;
begin
  Result := '';
  Candidate := ExpandConstant('{localappdata}\Programs\ActivityWatch\aw-qt.exe');
  if FileExists(Candidate) then
  begin
    Result := Candidate;
    Exit;
  end;
  Candidate := ExpandConstant('{pf}\ActivityWatch\aw-qt.exe');
  if FileExists(Candidate) then
  begin
    Result := Candidate;
    Exit;
  end;
  Candidate := ExpandConstant('{autopf}\ActivityWatch\aw-qt.exe');
  if FileExists(Candidate) then
    Result := Candidate;
end;

// Same search as FindActivityWatchExe, but returns AW's own install folder
// (the parent of aw-qt.exe) rather than the exe itself — that's where AW's
// own Inno-Setup-generated uninstaller (unins000.exe) lives.
function FindActivityWatchDir(): string;
var
  AwExe: string;
begin
  AwExe := FindActivityWatchExe();
  if AwExe <> '' then
    Result := ExtractFileDir(AwExe)
  else
    Result := '';
end;

// Windows Defender lets exclusions be added silently from the command line
// (Add-MpPreference), unlike COMODO and McAfee, whose exclusion mechanism
// varies by product edition and generally has no safe, universal silent
// command — see README.md's "Antivirus exclusions" section for the manual
// steps needed on those instead. Failures here (e.g. Defender isn't the
// active AV, or its module isn't present) are harmless and ignored — this
// is a best-effort belt-and-braces step, not something the install depends
// on. Learned the hard way tonight: COMODO silently disabled our Scheduled
// Task after install with no visible error, so this exists to stop the same
// class of problem recurring wherever it CAN be prevented automatically.
procedure AddDefenderExclusions(AppDir, AwDir, ProgramDataDir: string);
var
  ResultCode: Integer;
  PsCommand: string;
begin
  PsCommand :=
    '-NoProfile -ExecutionPolicy Bypass -Command "' +
    'try { ' +
    'Add-MpPreference -ExclusionPath @(''' + AppDir + ''', ''' + AwDir + ''', ''' + ProgramDataDir + ''') -ErrorAction SilentlyContinue; ' +
    'Add-MpPreference -ExclusionProcess @(''aw_pusher.exe'', ''aw-qt.exe'', ''aw-server.exe'', ''aw-watcher-afk.exe'', ''aw-watcher-window.exe'', ''usb_watcher.exe'') -ErrorAction SilentlyContinue ' +
    '} catch { }"';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), PsCommand,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// ---------------------------------------------------------------------
// v2.0.0 reliability rework
// ---------------------------------------------------------------------
// Root cause (live incident on the "Bowmans x Laptop" pilot machine,
// tenant "Test Clint"): the pre-2.0.0 Scheduled-Task-based pusher/USB
// watcher were unreliable for 5 separate reasons — a "run only when
// logged on" task dies the moment that session ends; switching to "run
// whether logged on or not" breaks USB write detection instead (it needs
// the interactive session); the logon trigger doesn't always fire; the
// default AC-power condition silently blocks everything on battery with
// nothing logged; and Windows auto-disables a task after enough repeated
// failures (e.g. during reboot testing). Fix: the Pusher becomes a real
// Windows Service (via bundled NSSM — starts at boot with nobody logged
// in, no power condition, its own restart-on-exit policy); the USB
// Watcher becomes a Scheduled Task built from Task XML with the built-in
// Users group as its run-as principal (works for any user who logs on,
// no per-user install, both power conditions explicitly off, dual
// logon+boot triggers); and a new Watchdog task (SYSTEM, every 15
// minutes) re-enables/restarts either one if something external (an RMM
// tool, an AV product) disables them. See README.md's "Reliability
// architecture (v2.0.0+)" section and the project's phase-1 progress doc.

function NssmExePath(): string;
begin
  Result := ExpandConstant('{app}\nssm.exe');
end;

// Appends one line to a growable TArrayOfString/Count pair rather than a
// pre-sized array with manually-computed indices — the array-building
// functions below were flagged, in an earlier pass at this same fix, as
// the most likely compile-time failure point precisely because manual
// SetArrayLength+index bookkeeping is easy to get subtly wrong across a
// long XML document. Growing by one element per call sidesteps that
// entirely: there's no length to precompute and no index to miscount.
procedure AddXmlLine(var Lines: TArrayOfString; var Count: Integer; const S: string);
begin
  SetArrayLength(Lines, Count + 1);
  Lines[Count] := S;
  Count := Count + 1;
end;

// Removes the pre-2.0.0 Scheduled-Task-based pusher, if present, before
// installing the Service — an upgrade from 1.x would otherwise end up
// with both the old task AND the new service trying to run
// aw_pusher.exe at the same time.
procedure RemoveLegacyPusherTask();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /F /TN "' + LegacyPusherTaskName + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM aw_pusher.exe',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// Installs (or re-installs, on top of an existing 2.0.0+ install) the
// Pusher as a genuine Windows Service using the bundled NSSM, which wraps
// an ordinary console/GUI exe as a proper service: starts at boot with
// nobody logged in (no Session-0/logon-trigger dependence at all), has no
// AC-power condition, and gets NSSM's own restart-on-exit policy instead
// of relying on a Scheduled Task's logon trigger or Windows' own
// (unconfigurable, silent) task-auto-disable-after-failures behaviour.
procedure InstallPusherService();
var
  ResultCode: Integer;
  Nssm, AppExe, AppDir, ConfigPath, ProgramDataDir: string;
begin
  Nssm := NssmExePath();
  AppExe := ExpandConstant('{app}\aw_pusher.exe');
  ConfigPath := ExpandConstant('{app}\config.json');
  AppDir := ExpandConstant('{app}');
  ProgramDataDir := ExpandConstant('{commonappdata}\RR-IT Insight');
  if not DirExists(ProgramDataDir) then
    CreateDir(ProgramDataDir);

  // Remove any existing service registration first (harmless no-op on a
  // fresh install) so `nssm install` below doesn't fail on a name that's
  // already registered — e.g. re-running the installer, or upgrading a
  // machine that already has 2.0.0+.
  Exec(Nssm, 'stop "' + PusherServiceName + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(Nssm, 'remove "' + PusherServiceName + '" confirm', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  Exec(Nssm, 'install "' + PusherServiceName + '" "' + AppExe + '" "\"' + ConfigPath + '\""',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(Nssm, 'set "' + PusherServiceName + '" AppDirectory "' + AppDir + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(Nssm, 'set "' + PusherServiceName + '" DisplayName "RR-IT Insight Pusher"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(Nssm, 'set "' + PusherServiceName + '" Description "Sends app/website activity to the RR-IT Insight dashboard."',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(Nssm, 'set "' + PusherServiceName + '" Start SERVICE_AUTO_START',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // Restart unconditionally on ANY exit (not just crashes) after a short
  // delay — the equivalent of the old task's "restart if it stops",
  // without a logon trigger or power condition standing in the way.
  Exec(Nssm, 'set "' + PusherServiceName + '" AppExit Default Restart',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(Nssm, 'set "' + PusherServiceName + '" AppRestartDelay 5000',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // Capture the wrapped exe's own stdout/stderr in case something goes
  // wrong before its own file logging is even set up (e.g. a bad
  // config.json at startup).
  Exec(Nssm, 'set "' + PusherServiceName + '" AppStdout "' + ProgramDataDir + '\service-stdout.log"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(Nssm, 'set "' + PusherServiceName + '" AppStderr "' + ProgramDataDir + '\service-stderr.log"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  Exec(Nssm, 'start "' + PusherServiceName + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure RemovePusherService();
var
  ResultCode: Integer;
begin
  Exec(NssmExePath(), 'stop "' + PusherServiceName + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(NssmExePath(), 'remove "' + PusherServiceName + '" confirm', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure RemoveTaskIfExists(const TaskName: string);
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /F /TN "' + TaskName + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// Writes Lines to a temp XML file and registers it as a Scheduled Task via
// `schtasks /Create /XML`. This is the only way to reach fields the
// simple `/SC ONLOGON /RU ...` command-line form can't: in particular,
// running as the well-known Users GROUP (rather than one named account),
// and explicitly turning off the AC-power conditions that silently
// blocked the pre-2.0.0 USB Watcher task on battery with nothing logged
// anywhere.
//
// The XML is written via SaveStringsToFile (plain-text) rather than a
// Unicode-specific save function: every value that goes into it (task
// names, SIDs, file paths under {app}, which is always ASCII for this
// installer) is plain ASCII, so there's no encoding mismatch between the
// declared `encoding="UTF-8"` in the XML header and the actual bytes on
// disk — the two known encoding pitfalls with `schtasks /Create /XML`
// (BOM-less UTF-8 vs UTF-16, and non-ASCII bytes) simply don't apply here.
procedure RegisterTaskFromXml(const TaskName: string; const Lines: TArrayOfString);
var
  XmlPath: string;
  LogPath: string;
  BatPath: string;
  ProgramDataDir: string;
  PowershellPath: string;
  Q: string;
  PsCommand: string;
  BatLines: TArrayOfString;
  ResultCode: Integer;
begin
  ProgramDataDir := ExpandConstant('{commonappdata}\RR-IT Insight');
  if not DirExists(ProgramDataDir) then
    CreateDir(ProgramDataDir);

  XmlPath := ProgramDataDir + '\' + TaskName + '.xml';
  LogPath := ProgramDataDir + '\task-registration.log';
  BatPath := ExpandConstant('{tmp}\') + TaskName + '_register.bat';
  PowershellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  Q := '''';

  SaveStringsToUTF8File(XmlPath, Lines, False);

  PsCommand := '$ErrorActionPreference=' + Q + 'Stop' + Q + '; try { ' +
    'Register-ScheduledTask -TaskName ' + Q + TaskName + Q +
    ' -Xml (Get-Content -LiteralPath ' + Q + XmlPath + Q + ' -Raw) -Force | Out-Null; ' +
    'Write-Output ' + Q + 'Registered OK' + Q + '; ' +
    '} catch { Write-Output $_.Exception.Message }';

  SetArrayLength(BatLines, 4);
  BatLines[0] := '@echo off';
  BatLines[1] := 'echo ---- %date% %time% : ' + TaskName + ' ---- >> "' + LogPath + '"';
  BatLines[2] := '"' + PowershellPath + '" -NoProfile -ExecutionPolicy Bypass -Command "' + PsCommand + '" >> "' + LogPath + '" 2>&1';
  BatLines[3] := 'echo exit code: %errorlevel% >> "' + LogPath + '"';
  SaveStringsToFile(BatPath, BatLines, False);

  Exec(BatPath, '', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  DeleteFile(BatPath);
end;

// USB Watcher: Users-group principal (S-1-5-32-545) so it runs for
// whichever user is actually logged on (no per-user install, no stored
// password); both logon and boot triggers (the logon trigger alone
// doesn't always fire — see the incident notes above); both AC-power
// conditions explicitly off; ExecutionTimeLimit PT0S (unlimited) since
// this is a long-running poll loop, not a short task that should be
// killed after Task Scheduler's default 72-hour ceiling.
function BuildUsbWatcherTaskXml(): TArrayOfString;
var
  Lines: TArrayOfString;
  Count: Integer;
  AppExe, ConfigPath: string;
begin
  Count := 0;
  AppExe := ExpandConstant('{app}\usb_watcher.exe');
  ConfigPath := ExpandConstant('{app}\config.json');

  AddXmlLine(Lines, Count, '<?xml version="1.0" encoding="UTF-8"?>');
  AddXmlLine(Lines, Count, '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">');
  AddXmlLine(Lines, Count, '  <RegistrationInfo>');
  AddXmlLine(Lines, Count, '    <Description>RR-IT Insight USB removable-drive watcher. Runs for any interactively logged-on user (Users group), started at both logon and boot, with no AC-power condition.</Description>');
  AddXmlLine(Lines, Count, '  </RegistrationInfo>');
  AddXmlLine(Lines, Count, '  <Triggers>');
  AddXmlLine(Lines, Count, '    <LogonTrigger>');
  AddXmlLine(Lines, Count, '      <Enabled>true</Enabled>');
  AddXmlLine(Lines, Count, '    </LogonTrigger>');
  AddXmlLine(Lines, Count, '    <BootTrigger>');
  AddXmlLine(Lines, Count, '      <Enabled>true</Enabled>');
  AddXmlLine(Lines, Count, '    </BootTrigger>');
  AddXmlLine(Lines, Count, '  </Triggers>');
  AddXmlLine(Lines, Count, '  <Principals>');
  AddXmlLine(Lines, Count, '    <Principal id="Author">');
  AddXmlLine(Lines, Count, '      <GroupId>' + SidUsersGroup + '</GroupId>');
  AddXmlLine(Lines, Count, '      <LogonType>Group</LogonType>');
  AddXmlLine(Lines, Count, '      <RunLevel>LeastPrivilege</RunLevel>');
  AddXmlLine(Lines, Count, '    </Principal>');
  AddXmlLine(Lines, Count, '  </Principals>');
  AddXmlLine(Lines, Count, '  <Settings>');
  AddXmlLine(Lines, Count, '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>');
  AddXmlLine(Lines, Count, '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>');
  AddXmlLine(Lines, Count, '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>');
  AddXmlLine(Lines, Count, '    <AllowHardTerminate>true</AllowHardTerminate>');
  AddXmlLine(Lines, Count, '    <StartWhenAvailable>true</StartWhenAvailable>');
  AddXmlLine(Lines, Count, '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>');
  AddXmlLine(Lines, Count, '    <AllowStartOnDemand>true</AllowStartOnDemand>');
  AddXmlLine(Lines, Count, '    <Enabled>true</Enabled>');
  AddXmlLine(Lines, Count, '    <Hidden>false</Hidden>');
  AddXmlLine(Lines, Count, '    <RunOnlyIfIdle>false</RunOnlyIfIdle>');
  AddXmlLine(Lines, Count, '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>');
  AddXmlLine(Lines, Count, '    <Priority>7</Priority>');
  AddXmlLine(Lines, Count, '  </Settings>');
  AddXmlLine(Lines, Count, '  <Actions Context="Author">');
  AddXmlLine(Lines, Count, '    <Exec>');
  AddXmlLine(Lines, Count, '      <Command>"' + AppExe + '"</Command>');
  AddXmlLine(Lines, Count, '      <Arguments>"' + ConfigPath + '"</Arguments>');
  AddXmlLine(Lines, Count, '    </Exec>');
  AddXmlLine(Lines, Count, '  </Actions>');
  AddXmlLine(Lines, Count, '</Task>');

  Result := Lines;
end;

// Watchdog: SYSTEM principal, fires every 15 minutes starting immediately
// after registration (StartBoundary is a fixed past date purely so the
// Repetition interval has a base to count from — combined with the
// explicit /Run right after registering it, in CurStepChanged, this task
// doesn't sit idle for up to 15 minutes before its first check).
// ExecutionTimeLimit PT5M (rather than PT0S/unlimited, unlike the USB
// Watcher above) because this one really is a short, in-and-out check —
// bounding it stops a hung run from blocking every future occurrence.
function BuildWatchdogTaskXml(): TArrayOfString;
var
  Lines: TArrayOfString;
  Count: Integer;
  ScriptPath: string;
begin
  Count := 0;
  ScriptPath := ExpandConstant('{app}\watchdog.ps1');

  AddXmlLine(Lines, Count, '<?xml version="1.0" encoding="UTF-8"?>');
  AddXmlLine(Lines, Count, '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">');
  AddXmlLine(Lines, Count, '  <RegistrationInfo>');
  AddXmlLine(Lines, Count, '    <Description>RR-IT Insight watchdog. Runs as SYSTEM every 15 minutes and re-enables/restarts the Pusher service and USB Watcher task if something external has disabled them.</Description>');
  AddXmlLine(Lines, Count, '  </RegistrationInfo>');
  AddXmlLine(Lines, Count, '  <Triggers>');
  AddXmlLine(Lines, Count, '    <TimeTrigger>');
  AddXmlLine(Lines, Count, '      <StartBoundary>2024-01-01T00:00:00</StartBoundary>');
  AddXmlLine(Lines, Count, '      <Enabled>true</Enabled>');
  AddXmlLine(Lines, Count, '      <Repetition>');
  AddXmlLine(Lines, Count, '        <Interval>PT15M</Interval>');
  AddXmlLine(Lines, Count, '        <StopAtDurationEnd>false</StopAtDurationEnd>');
  AddXmlLine(Lines, Count, '      </Repetition>');
  AddXmlLine(Lines, Count, '    </TimeTrigger>');
  AddXmlLine(Lines, Count, '  </Triggers>');
  AddXmlLine(Lines, Count, '  <Principals>');
  AddXmlLine(Lines, Count, '    <Principal id="Author">');
  AddXmlLine(Lines, Count, '      <UserId>' + SidLocalSystem + '</UserId>');
  AddXmlLine(Lines, Count, '      <RunLevel>HighestAvailable</RunLevel>');
  AddXmlLine(Lines, Count, '    </Principal>');
  AddXmlLine(Lines, Count, '  </Principals>');
  AddXmlLine(Lines, Count, '  <Settings>');
  AddXmlLine(Lines, Count, '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>');
  AddXmlLine(Lines, Count, '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>');
  AddXmlLine(Lines, Count, '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>');
  AddXmlLine(Lines, Count, '    <AllowHardTerminate>true</AllowHardTerminate>');
  AddXmlLine(Lines, Count, '    <StartWhenAvailable>true</StartWhenAvailable>');
  AddXmlLine(Lines, Count, '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>');
  AddXmlLine(Lines, Count, '    <AllowStartOnDemand>true</AllowStartOnDemand>');
  AddXmlLine(Lines, Count, '    <Enabled>true</Enabled>');
  AddXmlLine(Lines, Count, '    <Hidden>false</Hidden>');
  AddXmlLine(Lines, Count, '    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>');
  AddXmlLine(Lines, Count, '    <Priority>7</Priority>');
  AddXmlLine(Lines, Count, '  </Settings>');
  AddXmlLine(Lines, Count, '  <Actions Context="Author">');
  AddXmlLine(Lines, Count, '    <Exec>');
  AddXmlLine(Lines, Count, '      <Command>powershell.exe</Command>');
  AddXmlLine(Lines, Count, '      <Arguments>-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath + '"</Arguments>');
  AddXmlLine(Lines, Count, '    </Exec>');
  AddXmlLine(Lines, Count, '  </Actions>');
  AddXmlLine(Lines, Count, '</Task>');

  Result := Lines;
end;
// Called automatically by Inno Setup immediately before it starts copying
// files — this is its documented hook for stopping a running process that
// would otherwise hold a lock on a file about to be overwritten.
//
// Needed because InstallPusherService's own "nssm stop"/"nssm remove"
// calls don't run until CurStepChanged's ssPostInstall branch below,
// which fires AFTER the [Files] copy step — too late to release the lock
// on nssm.exe. Confirmed live on Test Clint re-running the installer over
// an existing v2.0.0+ install: the [Files] copy step failed with
// "DeleteFile failed; code 5. Access is denied" on nssm.exe, because
// NSSM's Windows Service host process IS nssm.exe itself (not a separate
// wrapper binary) — while the Pusher service is Running, nssm.exe is a
// live, locked executable, exactly like any other running .exe Windows
// won't let you overwrite. Stopping the service here, before extraction
// begins, releases that lock. InstallPusherService's later stop/remove
// call in CurStepChanged still runs as normal afterwards — by then it's
// a harmless no-op (or, on first install, there's no service yet to stop).
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Nssm: string;
begin
  Result := '';
  Nssm := NssmExePath();
  if FileExists(Nssm) then
    Exec(Nssm, 'stop "' + PusherServiceName + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
  else
    // nssm.exe isn't staged into {app} yet on a genuinely fresh install
    // (nothing to stop). Also try via sc.exe in case the service is still
    // registered under the Service Control Manager even though {app}\
    // nssm.exe itself is somehow already gone — belt and braces, and a
    // harmless no-op if the service doesn't exist either way.
    Exec(ExpandConstant('{sys}\sc.exe'), 'stop "' + PusherServiceName + '"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  // usb_watcher.exe is also copied by the [Files] section below and is
  // just as capable of being a currently-running, locked process (as the
  // USB Watcher Scheduled Task) on a reinstall — same class of bug as
  // nssm.exe above, so the same belt-and-braces treatment: end the task's
  // running instance (harmless no-op if the task doesn't exist or isn't
  // currently running) and taskkill the exe directly as a second attempt,
  // in case something started it outside the task (shouldn't happen, but
  // costs nothing to cover).
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/End /TN "' + UsbWatcherTaskName + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM usb_watcher.exe',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;


procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  AwExePath: string;
  AwDir: string;
begin
  if CurStep = ssPostInstall then
  begin
    WriteConfigFile;

    // Step 2: silent ActivityWatch install
    Exec(ExpandConstant('{tmp}\activitywatch-setup.exe'),
      '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOICONS',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    // Step 2b: launch ActivityWatch now, so it's up immediately rather than
    // waiting for the next logon. NOTE: we deliberately do NOT also
    // register our own autostart entry for it — ActivityWatch's own
    // installer already drops a Startup-folder shortcut
    // (shell:startup\ActivityWatch.lnk) for that. Confirmed by testing:
    // adding a second, our-own Run-key entry on top of that caused two
    // full copies of AW (aw-qt, aw-server, both watchers) to launch at
    // every logon.
    AwExePath := FindActivityWatchExe();
    if AwExePath <> '' then
      Exec(AwExePath, '', '', SW_HIDE, ewNoWait, ResultCode);

    // Step 3: Windows Defender exclusions — automatic wherever Defender is
    // the active AV. See AddDefenderExclusions above for why this can't be
    // done the same way for COMODO/McAfee.
    AwDir := FindActivityWatchDir();
    AddDefenderExclusions(ExpandConstant('{app}'), AwDir, ExpandConstant('{commonappdata}\RR-IT Insight'));

    // Step 4: Pusher as a Windows Service (v2.0.0+) — see the "v2.0.0
    // reliability rework" section above for why this replaced the old
    // Scheduled Task. Remove any pre-2.0.0 leftover first.
    RemoveLegacyPusherTask();
    InstallPusherService();

    // Step 4a: Watchdog — always registered, regardless of whether USB
    // monitoring is enabled for this client, since it also watches the
    // (always-installed) Pusher service.
    RegisterTaskFromXml(WatchdogTaskName, BuildWatchdogTaskXml());
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/Run /TN "' + WatchdogTaskName + '"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    // Step 4b: USB watcher — only registered when the wizard checkbox was
    // ticked. usb_watcher.exe is always copied to {app} above, but a
    // client who didn't ask for this gets no Scheduled Task for it at
    // all, so nothing of it ever runs on their machine. If unticked,
    // clean up any task left behind by a previous install where it WAS
    // ticked (e.g. an admin re-running the installer to turn it off).
    if UsbMonitoringEnabled() then
    begin
      RegisterTaskFromXml(UsbWatcherTaskName, BuildUsbWatcherTaskXml());
      Exec(ExpandConstant('{sys}\schtasks.exe'), '/Run /TN "' + UsbWatcherTaskName + '"',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end
    else
    begin
      RemoveTaskIfExists(UsbWatcherTaskName);
    end;

    // Step 5: force-install aw-watcher-web for Chrome AND Edge.
    RegWriteStringValue(HKLM, 'SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist', '1',
      '{#ExtensionId};{#ExtensionUpdateUrl}');
    RegWriteStringValue(HKLM, 'SOFTWARE\Policies\Microsoft\Edge\ExtensionInstallForcelist', '1',
      '{#ExtensionId};{#ExtensionUpdateUrl}');
  end;
end;

// Full removal, run on uninstall: stops and removes ActivityWatch itself
// (not just our own Scheduled Task/pusher), its Startup-folder shortcut,
// and our ProgramData folder. Windows' generated uninstaller only used to
// remove the Scheduled Task, leaving AW running and logging locally on a
// client who cancelled the service — this closes that gap.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  AwDir: string;
  AwUninstaller: string;
  RemoveAw: Integer;
  StartupShortcut: string;
  ProgramDataDir: string;
begin
  if CurUninstallStep = usUninstall then
  begin
    // Ask once, up front, since removing ActivityWatch is a bigger action
    // than a normal "uninstall this app" click and worth confirming.
    RemoveAw := MsgBox(
      'This will also stop and remove ActivityWatch, the tracking agent ' +
      'RR-IT Insight installed on this computer, along with its logged ' +
      'activity data. Continue?',
      mbConfirmation, MB_YESNO);

    // Step 1: stop everything before touching files — the pusher first
    // (it's what's actively sending data), then AW's own processes so its
    // uninstaller isn't fighting a running instance.
    //
    // v2.0.0+: the pusher is a Windows Service, not a Scheduled Task —
    // stop/remove it via NSSM. The plain schtasks /Delete against the old
    // task name is kept too (harmless no-op on a 2.0.0+-only install)
    // in case this is upgrading straight from a pre-2.0.0 install that
    // somehow still has the legacy task registered.
    RemovePusherService();
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /F /TN "' + LegacyPusherTaskName + '"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM aw_pusher.exe',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    // USB watcher and Watchdog — deleting a Scheduled Task/killing a
    // process that was never created/running (most installs have no USB
    // Watcher) is a harmless no-op here.
    RemoveTaskIfExists(UsbWatcherTaskName);
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM usb_watcher.exe',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    RemoveTaskIfExists(WatchdogTaskName);

    if RemoveAw = IDYES then
    begin
      Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM aw-qt.exe /T',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM aw-server.exe /T',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM aw-watcher-afk.exe /T',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM aw-watcher-window.exe /T',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

      // Step 2: run ActivityWatch's own uninstaller silently, wherever it
      // landed (same search as the installer uses to find aw-qt.exe).
      AwDir := FindActivityWatchDir();
      if AwDir <> '' then
      begin
        AwUninstaller := AwDir + '\unins000.exe';
        if FileExists(AwUninstaller) then
          Exec(AwUninstaller, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART',
            '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;

      // Step 3: belt-and-braces — delete the Startup-folder shortcut AW's
      // installer drops, in case its own uninstaller didn't (or AW wasn't
      // found above, e.g. a client removed it by hand already).
      StartupShortcut := ExpandConstant('{userstartup}\ActivityWatch.lnk');
      if FileExists(StartupShortcut) then
        DeleteFile(StartupShortcut);

      // Step 4: remove the forced browser-extension policy — no reason to
      // keep force-installing aw-watcher-web once AW itself is gone.
      RegDeleteValue(HKLM, 'SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist', '1');
      RegDeleteValue(HKLM, 'SOFTWARE\Policies\Microsoft\Edge\ExtensionInstallForcelist', '1');
    end;

    // Step 5: clean up our own data directory (pusher.log, outbox/state)
    // regardless of the ActivityWatch answer above — this is ours either way.
    ProgramDataDir := ExpandConstant('{commonappdata}\RR-IT Insight');
    if DirExists(ProgramDataDir) then
      DelTree(ProgramDataDir, True, True, True);
  end;
end;
