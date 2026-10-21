param(
    [ValidateSet('Status', 'Install', 'Remove')][string]$Action = 'Status',
    [switch]$Minecraft,
    [switch]$Voice,
    [switch]$WithoutTelegram
)
$ErrorActionPreference = 'Stop'
$taskName = 'Joker-Persona'
if ($Action -eq 'Status') {
    Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Select-Object TaskName, State
    exit
}
if ($Action -eq 'Remove') {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    exit
}
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Сначала создай .venv и установи проект.' }
$taskScript = Join-Path $PSScriptRoot 'tools\supervise.py'
$taskArguments = '-X utf8 "' + $taskScript + '"'
if (-not $WithoutTelegram) { $taskArguments += ' --telegram' }
if ($Minecraft) { $taskArguments += ' --minecraft' }
if ($Voice) { $taskArguments += ' --voice' }
# Registering does not launch the app. The task runs on the next interactive logon.
$taskAction = New-ScheduledTaskAction -Execute $taskPython -Argument $taskArguments -WorkingDirectory $PSScriptRoot
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Force | Out-Null
Write-Output 'Автозапуск Джокера зарегистрирован для следующего входа в Windows.'
