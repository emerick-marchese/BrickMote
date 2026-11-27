# BrickMote: SYSTEM scheduled tasks (run by the installer, as administrator).
#   Update  : looks for a signed new version at boot; signed-in users may start
#             it (mandatory updates without UAC), only admins/SYSTEM may change it.
#   Desktop : keeps the lock screen helper running (like AnyDesk's service).
param([string]$App, [int]$Update = 0, [int]$Desktop = 0)
$ErrorActionPreference = 'Stop'
$exe = Join-Path $App 'BrickMote.exe'
$folder = '\BrickMote\'
$principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest

foreach ($name in 'Update', 'Desktop') {
    Stop-ScheduledTask -TaskPath $folder -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskPath $folder -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
}

if ($Update) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = 'PT20S'
    # laptops: also on battery (the default task settings would skip it)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskPath $folder -TaskName 'Update' -Principal $principal -Settings $settings -Trigger $trigger `
        -Action (New-ScheduledTaskAction -Execute $exe -Argument '--update-service') -Force | Out-Null
    $service = New-Object -ComObject Schedule.Service
    $service.Connect()
    $tasks = $service.GetFolder($folder.TrimEnd('\'))
    $tasks.SetSecurityDescriptor('D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;GR;;;AU)', 0)
    $tasks.GetTask('Update').SetSecurityDescriptor('D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;AU)', 0)
}

if ($Desktop) {
    # runs for ever, restarted if it ever stops
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskPath $folder -TaskName 'Desktop' -Principal $principal -Settings $settings `
        -Trigger (New-ScheduledTaskTrigger -AtStartup) `
        -Action (New-ScheduledTaskAction -Execute $exe -Argument '--desktop-service') -Force | Out-Null
    Start-ScheduledTask -TaskPath $folder -TaskName 'Desktop'
}
