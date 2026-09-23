<#
RR-IT Insight — watchdog.

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
either one after the fact with no visible error and no alert — this script
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

# Rotate the log by hand rather than pulling in a module — this script has
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
    # Get-Service doesn't expose start mode (Automatic/Disabled/Manual) —
    # that's WMI/CIM's job. sc.exe config (not Set-Service -StartupType,
    # which isn't available on every PowerShell 5.0 client we support) is
    # the most portable way to flip it back to Automatic.
    $wmiService = Get-CimInstance -ClassName Win32_Service `
        -Filter "Name='$pusherServiceName'" -ErrorAction SilentlyContinue

    if ($wmiService -and $wmiService.StartMode -eq 'Disabled') {
        Write-Log "Pusher service was Disabled — re-enabling (Automatic start)."
        & sc.exe config $pusherServiceName start= auto | Out-Null
    }

    if ($service.Status -ne 'Running') {
        Write-Log "Pusher service was not running (status: $($service.Status)) — starting it."
        Start-Service -Name $pusherServiceName -ErrorAction SilentlyContinue
    }
} else {
    Write-Log "Pusher service '$pusherServiceName' not found — nothing to do (not installed on this machine, or the installer needs re-running)."
}

# --- USB Watcher Scheduled Task -----------------------------------------

$usbTaskName = 'RR-IT Insight USB Watcher'
$usbTask = Get-ScheduledTask -TaskName $usbTaskName -ErrorAction SilentlyContinue

if ($usbTask) {
    if ($usbTask.State -eq 'Disabled') {
        Write-Log "USB Watcher task was Disabled — re-enabling."
        Enable-ScheduledTask -TaskName $usbTaskName -ErrorAction SilentlyContinue | Out-Null
    }

    if ($usbTask.State -ne 'Running') {
        Write-Log "USB Watcher task was not running (state: $($usbTask.State)) — starting it."
        # Safe even if it's actually already running under the hood: the
        # task's MultipleInstancesPolicy is IgnoreNew, so this can't spin
        # up a duplicate, independent copy.
        Start-ScheduledTask -TaskName $usbTaskName -ErrorAction SilentlyContinue
    }
}
# Deliberately no "else" logging here: most clients don't have USB
# monitoring enabled at all, so this task simply won't exist on most
# machines — that's the normal case, not something worth a log line every
# 15 minutes.
