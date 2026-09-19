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
;   5. Force-installs the aw-watcher-web browser extension for BOTH Chrome
;      and Edge via the ExtensionInstallForcelist registry policy, so it
;      works whichever browser(s) this client's staff actually use —
;      unconditionally installing into both is simpler and harmless if a
;      browser isn't present (the policy just sits unused).
;
; NOTE: EXTENSION_ID and EXTENSION_UPDATE_URL below are placeholders —
; confirm aw-watcher-web's real Chrome Web Store listing/ID before
; shipping this to a client, and update both constants. See README.md.

#define MyAppName "RR-IT Insight Agent"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Rapid Response IT"
#define ExtensionId "REPLACE_WITH_CWS_EXTENSION_ID"
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
  Lines[2] := '  "zite_ingest_url": "https://rr-it-insight.zite.so/api/ingestEvents",';
  Lines[3] := '  "api_key": "' + ConfigPage.Values[1] + '",';
  Lines[4] := '  "client_id": "' + ConfigPage.Values[0] + '",';
  Lines[5] := '  "poll_interval_seconds": 30';
  Lines[6] := '}';
  SaveStringsToFile(ConfigPath, Lines, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    WriteConfigFile;

    // Step 2: silent ActivityWatch install
    Exec(ExpandConstant('{tmp}\activitywatch-setup.exe'),
      '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOICONS',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

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

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /F /TN "RR-IT Insight Pusher"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
