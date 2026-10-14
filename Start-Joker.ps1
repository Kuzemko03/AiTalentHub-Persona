param([switch]$WithoutTelegram)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python.' }
}
if ($WithoutTelegram) {
    & $taskPython -X utf8 -m joker serve
} else {
    & $taskPython -X utf8 -m joker serve --telegram
}
