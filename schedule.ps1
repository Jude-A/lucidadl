<#
  Schedule lucidadl to run "in the background" on Windows.

  Registers a Windows Scheduled Task that runs a batch file (`lucida albums` or
  `lucida tracks`) every day at a given time.

  IMPORTANT: the task runs only "when the user is logged on" (Interactive logon),
  because the browser needs a visible desktop to pass Cloudflare the first time the
  cached cookie expires. Stay logged in at the scheduled time.

  Examples:
    .\schedule.ps1                                  # albums, every day at 09:00
    .\schedule.ps1 -Mode tracks -Time 21:30
    .\schedule.ps1 -Mode albums -Time 08:00 -TaskName LucidaMorning
    .\schedule.ps1 -WorkingDir "C:\Users\me\music-lists"   # folder that holds inputs\
  Remove it:  Unregister-ScheduledTask -TaskName LucidaDL -Confirm:$false
#>
param(
  [ValidateSet("albums", "tracks")] [string]$Mode = "albums",
  [string]$Time = "09:00",
  [string]$TaskName = "LucidaDL",
  [string]$WorkingDir = (Get-Location).Path
)

# The batch command (`tracks`/`albums`) reads .\inputs\<mode>.txt relative to the working
# directory, so point the task at a folder that contains your inputs\ folder.
$lucida = (Get-Command lucida -ErrorAction SilentlyContinue).Source
if (-not $lucida) {
  Write-Error "The 'lucida' command was not found on PATH. Install it first (pip install . / pipx install .)."
  exit 1
}

$action    = New-ScheduledTaskAction -Execute $lucida -Argument $Mode -WorkingDirectory $WorkingDir
$trigger   = New-ScheduledTaskTrigger -Daily -At $Time
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Task '$TaskName' created: '$Mode' every day at $Time (while you are logged on)." -ForegroundColor Green
Write-Host "Test it now:  Start-ScheduledTask -TaskName $TaskName"
