param([Parameter(ValueFromRemainingArguments=$true)][string[]]$HrArguments)
$ErrorActionPreference = 'Stop'
$hrPython = Join-Path $PSScriptRoot '../.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $hrPython)) {
    $hrPython = (Get-Command python -ErrorAction Stop).Source
}
$env:PYTHONDONTWRITEBYTECODE = '1'
& $hrPython -X utf8 @HrArguments
exit $LASTEXITCODE
