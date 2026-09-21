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
;   4. Registers a Scheduled Task so the pusher starts at logon and keeps
;      running, restarting itself if it stops.
;   4b. Adds Windows Defender exclusions for our own folders/processes
;      (silent, best-effort — no-op if Defender isn't the active AV).
;      COMODO and McAfee need the same done manually — see README.md.
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
#define MyAppVersion "1.0.0"
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
Source: "..\pusher\config.example.json"; DestDir: "{app}"; DestName: "config.json.template"; Flags: ignoreversion

[Code]
var
  ConfigPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  ConfigPage := CreateInputQueryPage(wpSelectDir,
    'RR-IT Insight Configuration',
    'Enter this client''s connection details',
    'You should have been given these by RR-IT. If not, contact support before continuing.');
  ConfigPage.Add('Client ID:', False);
  ConfigPage.Add('Ingest API Key:', True);
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
begin
  ConfigPath := ExpandConstant('{app}\config.json');
  SetArrayLength(Lines, 7);
  Lines[0] := '{';
  Lines[1] := '  "aw_api_url": "http://localhost:5600",';
  Lines[2] := '  "zite_ingest_url": "https://2wgpdcmeym.zite.so/api/ingestEvents",';
  Lines[3] := '  "api_key": "' + ConfigPage.Values[1] + '",';
  Lines[4] := '  "client_id": "' + ConfigPage.Values[0] + '",';
  Lines[5] := '  "poll_interval_seconds": 30';
  Lines[6] := '}';
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
    'Add-MpPreference -ExclusionProcess @(''aw_pusher.exe'', ''aw-qt.exe'', ''aw-server.exe'', ''aw-watcher-afk.exe'', ''aw-watcher-window.exe'') -ErrorAction SilentlyContinue ' +
    '} catch { }"';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), PsCommand,
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

    // Step 4: Scheduled Task — runs at logon, restarts if it stops.
    Exec(ExpandConstant('{sys}\schtasks.exe'),
      '/Create /F /SC ONLOGON /RL HIGHEST /TN "RR-IT Insight Pusher" ' +
      '/TR "\"' + ExpandConstant('{app}') + '\aw_pusher.exe\" \"' + ExpandConstant('{app}') + '\config.json\""',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    // Also start it immediately for this session, without waiting for next logon.
    Exec(ExpandConstant('{sys}\schtasks.exe'),
      '/Run /TN "RR-IT Insight Pusher"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

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
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /F /TN "RR-IT Insight Pusher"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM aw_pusher.exe',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

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
