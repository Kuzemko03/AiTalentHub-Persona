param([switch]$WithoutTelegram, [switch]$Minecraft, [switch]$Voice)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python.' }
}
$jokerArgs = @('-X', 'utf8', '-m', 'joker', 'serve')
if (-not $WithoutTelegram) { $jokerArgs += '--telegram' }
if ($Minecraft) { $jokerArgs += '--minecraft' }
if ($Voice) { $jokerArgs += '--voice' }
& $taskPython @jokerArgs
