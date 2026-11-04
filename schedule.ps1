param([ValidateSet('Install','Remove','Status')][string]$Action='Status',[string]$At='20:00')
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$root=[IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$taskName='LocalPaperReader_7ec94129'
$marker=Get-Content -LiteralPath (Join-Path $root 'config\project.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($marker.app_id -ne $taskName) { throw 'Project identity mismatch' }
$current=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($current -and ($current.Description -ne $taskName -or $current.Actions.Execute -ne (Join-Path $root 'runtime\pythonw.exe'))) { throw 'Task belongs to another installation' }
if ($Action -eq 'Status') { if ($current) { 'installed' } else { 'not installed' }; exit }
if ($Action -eq 'Remove') {
 if ($current) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
 'removed'; exit
}
if ($At -notmatch '^(?:[01]\d|2[0-3]):[0-5]\d$') { throw 'Invalid time' }
$user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$actionObject=New-ScheduledTaskAction -Execute (Join-Path $root 'runtime\pythonw.exe') -Argument ('-B "'+(Join-Path $root 'tasks.py')+'" --worker') -WorkingDirectory $root
$daily=New-ScheduledTaskTrigger -Daily -At ([DateTime]::ParseExact($At,'HH:mm',$null))
$logon=New-ScheduledTaskTrigger -AtLogOn -User $user
$logon.Delay='PT2M'
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
Register-ScheduledTask -TaskName $taskName -Description $taskName -Action $actionObject -Trigger @($daily,$logon) -Principal $principal -Settings $settings -Force | Out-Null
'installed'
