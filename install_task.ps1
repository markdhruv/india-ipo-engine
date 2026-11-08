<#
.SYNOPSIS
    Register (or remove) the IPO engine as a Windows Scheduled Task.

.DESCRIPTION
    Indian IPO books update through the day and the UPI mandate cutoff on the
    closing day is around 5 PM IST, so the schedule below polls through market
    hours and deliberately puts a run just before that cutoff.

    The task runs only while you are logged on, which is also its main
    limitation: a sleeping machine fires no alerts. See the README.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_task.ps1
    powershell -ExecutionPolicy Bypass -File install_task.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$TaskName = "IPO Oversubscription Engine",
    [int]$IntervalMinutes = 30,
    [string]$StartTime = "09:30",
    [string]$EndTime = "17:30",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$python = Join-Path $root ".venv\Scripts\pythonw.exe"
$script = Join-Path $root "run.py"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Yellow
    } else {
        Write-Host "No scheduled task named '$TaskName'." -ForegroundColor Yellow
    }
    return
}

if (-not (Test-Path $python)) {
    throw "Virtual environment missing at $python. Run setup first:`n  python -m venv .venv`n  .venv\Scripts\pip install -r requirements.txt"
}
if (-not (Test-Path $script)) { throw "Cannot find run.py at $script" }

# pythonw.exe keeps no console window open on each run.
$action = New-ScheduledTaskAction -Execute $python `
                                  -Argument "`"$script`" once" `
                                  -WorkingDirectory $root

$duration = (Get-Date $EndTime) - (Get-Date $StartTime)
if ($duration.TotalMinutes -le 0) { throw "EndTime must be after StartTime." }

$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $StartTime `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration $duration).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Alerts on oversubscribed Indian IPOs with an estimated listing gain above the configured bar." | Out-Null

Write-Host "Registered '$TaskName'." -ForegroundColor Green
Write-Host "  Runs every $IntervalMinutes min, $StartTime to $EndTime daily."
Write-Host "  Command: $python `"$script`" once"
Write-Host ""
Write-Host "Verify now with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove with:      powershell -ExecutionPolicy Bypass -File install_task.ps1 -Remove"
Write-Host ""
Write-Warning "Alerts only fire while this PC is awake and you are logged on. The closing-day UPI cutoff is ~5 PM IST."
