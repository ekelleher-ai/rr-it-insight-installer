<#
RR-IT Insight - watchdog.

Runs as SYSTEM every 15 minutes via the "RR-IT Insight Watchdog" Scheduled
Task (registered by installer.iss's BuildWatchdogTaskXml). This exists as a
direct backstop for the incident on the "Bowmans x Laptop" pilot machine
(tenant "Test Clint"): the Scheduled-Task-based Pusher/USB Watcher were
found unreliable for five separate reasons (a logged-on-only task dies at
logoff; a "run whether logged on or not" task breaks USB detection instead;
the logon trigger doesn't always fire; the default AC-power condition
silently blocks a task on battery with nothing logged; and Windows
auto-disables a task after enough repeated failures). Moving the Pusher to
a real Windows Service (via NSSM) and rebuilding the USB Watcher task from
XML fixes the root causes, but an RMM tool or AV product can still disable
either one after the fact with no visible error and no alert - this script
notices and fixes that the next time it runs, rather than everyone waiting
for a client to go quiet before anyone notices.

Every check below is idempotent and safe to run repeatedly: re-enabling an
already-enabled service/task, or "starting" an already-running one, is a
harmless no-op.
#>

$ErrorActionPreference = 'SilentlyContinue'

$logDir = Join-Path $env:ProgramData 'RR-IT Insight'
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logPath = Join-Path $logDir 'watchdog.log'

function Write-Log {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $logPath -Value $line
}

# Rotate the log by hand rather than pulling in a module - this script has
# no dependency on aw_pusher.py's Python logging setup, and a watchdog that
# runs every 15 minutes forever needs the same "don't grow forever"
# treatment pusher.log and usb_watcher.log already got. Truncate (keep the
# tail) rather than delete-and-recreate, so a concurrent reader (a support
# tech with the file open) never sees it disappear out from under them.
try {
    $maxBytes = 5MB
    if ((Test-Path $logPath) -and ((Get-Item $logPath).Length -gt $maxBytes)) {
        $tail = Get-Content -Path $logPath -Tail 500
        Set-Content -Path $logPath -Value $tail
    }
} catch { }

# --- Pusher Windows Service --------------------------------------------

$pusherServiceName = 'RR-IT Insight Pusher'
$service = Get-Service -Name $pusherServiceName -ErrorAction SilentlyContinue

if ($service) {
    # Get-Service doesn't expose start mode (Automatic/Disabled/Manual) -
    # that's WMI/CIM's job. sc.exe config (not Set-Service -StartupType,
    # which isn't available on every PowerShell 5.0 client we support) is
    # the most portable way to flip it back to Automatic.
    $wmiService = Get-CimInstance -ClassName Win32_Service `
        -Filter "Name='$pusherServiceName'" -ErrorAction SilentlyContinue

    if ($wmiService -and $wmiService.StartMode -eq 'Disabled') {
        Write-Log "Pusher service was Disabled - re-enabling (Automatic start)."
        & sc.exe config $pusherServiceName start= auto | Out-Null
    }

    if ($service.Status -ne 'Running') {
        Write-Log "Pusher service was not running (status: $($service.Status)) - starting it."
        Start-Service -Name $pusherServiceName -ErrorAction SilentlyContinue
    }
} else {
    Write-Log "Pusher service '$pusherServiceName' not found - nothing to do (not installed on this machine, or the installer needs re-running)."
}

# --- USB Watcher Scheduled Task -----------------------------------------

$usbTaskName = 'RR-IT Insight USB Watcher'
$usbTask = Get-ScheduledTask -TaskName $usbTaskName -ErrorAction SilentlyContinue

if ($usbTask) {
    if ($usbTask.State -eq 'Disabled') {
        Write-Log "USB Watcher task was Disabled - re-enabling."
        Enable-ScheduledTask -TaskName $usbTaskName -ErrorAction SilentlyContinue | Out-Null
    }

    if ($usbTask.State -ne 'Running') {
        Write-Log "USB Watcher task was not running (state: $($usbTask.State)) - starting it."
        # Safe even if it's actually already running under the hood: the
        # task's MultipleInstancesPolicy is IgnoreNew, so this can't spin
        # up a duplicate, independent copy.
        Start-ScheduledTask -TaskName $usbTaskName -ErrorAction SilentlyContinue
    }
}
# Deliberately no "else" logging here: most clients don't have USB
# monitoring enabled at all, so this task simply won't exist on most
# machines - that's the normal case, not something worth a log line every
# 15 minutes.


# --- ActivityWatch Scheduled Task (v2.0.0.18+) --------------------------
# AW itself is the data source; if its task is disabled or not running, the
# Pusher has nothing to send. Only present on machines installed with
# v2.0.0.18+ (older installs rely on AW's own Startup shortcut), so - like
# the USB task above - act only if the task exists, no "else" noise.

$awTaskName = 'RR-IT Insight ActivityWatch'
$awTask = Get-ScheduledTask -TaskName $awTaskName -ErrorAction SilentlyContinue

if ($awTask) {
    if ($awTask.State -eq 'Disabled') {
        Write-Log "ActivityWatch task was Disabled - re-enabling."
        Enable-ScheduledTask -TaskName $awTaskName -ErrorAction SilentlyContinue | Out-Null
    }

    # aw-qt launches the watchers then can exit, so checking the task 'State'
    # isn't enough - check the actual process. If aw-qt isn't running, start
    # the task (MultipleInstancesPolicy IgnoreNew means this can't double up).
    $awProc = Get-Process -Name 'aw-qt' -ErrorAction SilentlyContinue
    if (-not $awProc) {
        Write-Log "aw-qt not running - starting the ActivityWatch task."
        Start-ScheduledTask -TaskName $awTaskName -ErrorAction SilentlyContinue
    }
}

# --- Auto-update (v2.0.0.15+) ---------------------------------------------
#
# RR-IT approves an agent version per client in the RR-IT console. The pusher
# (a LocalSystem service) writes that version to update-target.json in the
# install folder - Program Files, which standard users can't write to. This
# SYSTEM task does the rest, and only ever installs a release that:
#   1. is NEWER than the version installed here (never a downgrade);
#   2. comes from this repo's GitHub releases (fixed URL, not configurable);
#   3. has a manifest.json signed by RR-IT's release key - checked against
#      the public key below, so a tampered or substituted release is refused
#      even if someone got into the GitHub account without the signing key;
#   4. has exactly the size and SHA-256 the signed manifest says.
# The installer is downloaded into the install folder (not user-writable),
# checked, then started detached with the silent "update" switch, which keeps
# config.json and doesn't touch ActivityWatch. Progress and errors go to
# update-status.json (reported to the console with the next upload) and to
# watchdog.log. A failing version is retried at most once an hour and given up
# after 3 attempts until RR-IT approves a different version.

$UpdateRepoBase = 'https://github.com/ekelleher-ai/rr-it-insight-installer/releases/download'
$UpdateInstallerName = 'RR-IT-Insight-Setup.exe'
$UpdateMaxAttempts = 3
$UpdateRetryMinutes = 60
$UpdateInstallTimeoutMinutes = 30
# RR-IT release-signing public key (RSA-3072). The matching private key is the
# UPDATE_SIGNING_KEY secret in the GitHub repo and is never on client PCs.
$UpdatePublicKeyXml = '<RSAKeyValue><Modulus>teioVmzxqB0k5JfBdv97UP9CieAy5Dc7K1NIgjPsDh/CBYSGtciPd8kIRvbVrF/zmjk6Qjr5zOf5WWKfF2LKBPsmktdlou2NGZePOXKKppJUj9gJT7pZTAo9g5Z0ExolyEUfFzMDhpr5gGt/a7545hWsVC2pfYqwSkXW1Q1xSU6nA7dIRhNpmofbhoWFG2NAt0ORd5lzCFXa0WuAQ+OsGNNRdKs2TGJ+HjlFVPIxvVhqdYM16+p/Y/2b2kgpGr5HyTtXkvptZunbbeF4sUkqL6RoACwu6IpNv45K1x6Ax2lBuKxms+x42/DWj0wf76nSeiQHwPAr4RwbchqrklVXzRb44sbZjxOw33oXY5jhptWBS9SuQY1errZhVUkITEBgWqJq9WDSTFA3S2TKJNbdxtZ/WMiHeIG/8T8x2hh8yfcr64IidYe2vr//GlOnJk1oWQB9X5EErjsjlqFa9vUs1bqEkNBBtGZVruk1Dexcq10O1mkRtzghSf2+jLL7NTE1</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>'

function Get-InstallDir {
    return Split-Path -Parent $PSCommandPath
}

function Read-JsonFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return (Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop) } catch { return $null }
}

function Write-UpdateStatus([string]$Path, [hashtable]$Status) {
    $now = [DateTimeOffset]::UtcNow
    $Status['at'] = $now.ToString('yyyy-MM-ddTHH:mm:ssZ')
    # Epoch seconds for the retry/timeout maths: a plain number reads back the
    # same on every PowerShell version (ConvertFrom-Json turns ISO dates into
    # DateTime on some versions and leaves them as strings on others).
    $Status['atEpoch'] = $now.ToUnixTimeSeconds()
    try { ($Status | ConvertTo-Json -Compress) | Set-Content -LiteralPath $Path -Encoding UTF8 } catch { }
}

function Get-VersionOrNull([string]$Text) {
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $v = $null
    if ([version]::TryParse($Text.Trim(), [ref]$v)) { return $v }
    return $null
}

function Test-ReleaseSignature([byte[]]$Data, [byte[]]$Signature) {
    if ($UpdatePublicKeyXml -like '__*') { return $false }   # key not baked in: never install
    # RSA PKCS#1 v1.5 with SHA-256. RSACng first (.NET 4.6+, every supported
    # Windows); the old CSP class as a fallback, forced onto the AES provider
    # (type 24) because the default provider on older builds has no SHA-256.
    try {
        $rsa = New-Object System.Security.Cryptography.RSACng
        try {
            $rsa.FromXmlString($UpdatePublicKeyXml)
            return [bool]$rsa.VerifyData($Data, $Signature,
                [System.Security.Cryptography.HashAlgorithmName]::SHA256,
                [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
        } finally { $rsa.Dispose() }
    } catch { }
    try {
        $rsa1 = [System.Security.Cryptography.RSA]::Create()
        try {
            $rsa1.FromXmlString($UpdatePublicKeyXml)
            return [bool]$rsa1.VerifyData($Data, $Signature,
                [System.Security.Cryptography.HashAlgorithmName]::SHA256,
                [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
        } finally { $rsa1.Dispose() }
    } catch { }
    try {
        $csp = New-Object System.Security.Cryptography.CspParameters 24
        $rsa2 = New-Object System.Security.Cryptography.RSACryptoServiceProvider -ArgumentList $csp
        try {
            $rsa2.PersistKeyInCsp = $false
            $rsa2.FromXmlString($UpdatePublicKeyXml)
            return [bool]$rsa2.VerifyData($Data, 'SHA256', $Signature)
        } finally { $rsa2.Dispose() }
    } catch {
        return $false
    }
}

function Invoke-AgentUpdate {
    $installDir = Get-InstallDir
    $targetPath = Join-Path $installDir 'update-target.json'
    $statusPath = Join-Path $installDir 'update-status.json'
    $versionPath = Join-Path $installDir 'version.txt'

    $target = Read-JsonFile $targetPath
    if (-not $target -or -not $target.version) { return }
    $targetVersion = Get-VersionOrNull ([string]$target.version)
    if (-not $targetVersion) { Write-Log "Update: ignoring invalid target version '$($target.version)'."; return }

    $installedText = $null
    if (Test-Path -LiteralPath $versionPath) { $installedText = ([string](Get-Content -LiteralPath $versionPath -Raw)).Trim() }
    $installed = Get-VersionOrNull $installedText
    $status = Read-JsonFile $statusPath
    $targetText = $targetVersion.ToString()

    if ($installed -and $installed -ge $targetVersion) {
        if (-not $status -or $status.state -ne 'installed' -or $status.target -ne $installed.ToString()) {
            if ($status -and $status.state -eq 'installing' -and $status.target -eq $targetText) {
                Write-Log "Update: version $targetText installed successfully."
            }
            Write-UpdateStatus $statusPath @{ state = 'installed'; target = $installed.ToString() }
        }
        return
    }

    # An install we started earlier: still running, or did it fail?
    $attempts = 0
    if ($status -and $status.target -eq $targetText) {
        $attempts = [int]($status.attempts)
        $ageMin = 9999
        try {
            if ($status.atEpoch) { $ageMin = ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [int64]$status.atEpoch) / 60 }
        } catch { }
        if ($status.state -eq 'installing' -and $ageMin -lt $UpdateInstallTimeoutMinutes) { return }
        if ($status.state -eq 'installing') {
            Write-Log "Update: install of $targetText didn't complete within $UpdateInstallTimeoutMinutes min (still on $installedText)."
            Write-UpdateStatus $statusPath @{ state = 'failed'; target = $targetText; attempts = $attempts; error = 'installer did not complete' }
            return
        }
        if ($attempts -ge $UpdateMaxAttempts) { return }
        if ($status.state -eq 'failed' -and $ageMin -lt $UpdateRetryMinutes) { return }
    }
    $attempts++

    $fail = {
        param([string]$Why)
        Write-Log "Update to $targetText failed: $Why"
        Write-UpdateStatus $statusPath @{ state = 'failed'; target = $targetText; attempts = $attempts; error = $Why }
    }

    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $ProgressPreference = 'SilentlyContinue'
        $updatesDir = Join-Path $installDir 'updates'
        if (-not (Test-Path -LiteralPath $updatesDir)) { New-Item -ItemType Directory -Path $updatesDir -Force | Out-Null }
        Get-ChildItem -LiteralPath $updatesDir -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

        Write-Log "Update: RR-IT approved $targetText (installed: $installedText) - downloading."
        Write-UpdateStatus $statusPath @{ state = 'downloading'; target = $targetText; attempts = $attempts }

        $base = "$UpdateRepoBase/v$targetText"
        $manifestPath = Join-Path $updatesDir 'manifest.json'
        $sigPath = Join-Path $updatesDir 'manifest.sig'
        Invoke-WebRequest -Uri "$base/manifest.json" -OutFile $manifestPath -UseBasicParsing -TimeoutSec 60 -ErrorAction Stop
        Invoke-WebRequest -Uri "$base/manifest.sig" -OutFile $sigPath -UseBasicParsing -TimeoutSec 60 -ErrorAction Stop

        $manifestBytes = [System.IO.File]::ReadAllBytes($manifestPath)
        $sigBytes = [Convert]::FromBase64String(([System.IO.File]::ReadAllText($sigPath)).Trim())
        if (-not (Test-ReleaseSignature $manifestBytes $sigBytes)) { & $fail 'release signature check failed'; return }

        $manifest = [System.Text.Encoding]::UTF8.GetString($manifestBytes) | ConvertFrom-Json
        if ($manifest.product -ne 'rr-it-insight-agent') { & $fail 'manifest is for a different product'; return }
        if ($manifest.version -ne $targetText) { & $fail "manifest is for version $($manifest.version)"; return }
        if ($manifest.file -ne $UpdateInstallerName) { & $fail 'unexpected installer file name'; return }

        $exePath = Join-Path $updatesDir $UpdateInstallerName
        Invoke-WebRequest -Uri "$base/$UpdateInstallerName" -OutFile $exePath -UseBasicParsing -TimeoutSec 1200 -ErrorAction Stop
        $size = (Get-Item -LiteralPath $exePath).Length
        if ($size -ne [int64]$manifest.size) { & $fail "installer size $size does not match signed manifest"; return }
        $hash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne ([string]$manifest.sha256).ToLowerInvariant()) { & $fail 'installer hash does not match signed manifest'; return }

        Write-Log "Update: $targetText verified (signature, size, SHA-256) - starting silent install."
        Write-UpdateStatus $statusPath @{ state = 'installing'; target = $targetText; attempts = $attempts }
        $installLog = Join-Path $logDir 'update-install.log'
        # Detached: the installer stops and re-registers this very task, so
        # this script must not wait for it. The next run confirms the result.
        Start-Process -FilePath $exePath -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/RRITUPDATE=1', "/LOG=`"$installLog`"") -WindowStyle Hidden
    } catch {
        & $fail ($_.Exception.Message)
    }
}

try { Invoke-AgentUpdate } catch { Write-Log "Update check error: $($_.Exception.Message)" }
