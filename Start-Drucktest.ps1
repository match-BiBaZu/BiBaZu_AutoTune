param([Parameter(ValueFromRemainingArguments=$true)][string[]]$AppArguments)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    throw 'Zuerst Setup.ps1 ausführen.'
}
& .\.venv\Scripts\python.exe -m bibazu_autotune.pressure_test_app @AppArguments
exit $LASTEXITCODE
