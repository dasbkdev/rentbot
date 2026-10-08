param(
    [ValidateSet('run','check','ids','test')][string]$Mode = 'run',
    [string]$PythonExe = ''
)
# Keep ASCII source for Windows PowerShell 5.1 compatibility.
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    if (-not $PythonExe) {
        $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        if (Test-Path -LiteralPath $bundledPython) { $PythonExe = $bundledPython }
        elseif (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -m venv .venv
        }
        else { throw 'Specify Python 3.10+: .\run.ps1 -PythonExe C:\Path\python.exe' }
    }
    if ($PythonExe) { & $PythonExe -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the Python virtual environment.' }
}
$botPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if ($Mode -eq 'test') {
    & $botPython -m pip install -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $botPython -m pytest -q
} else {
    & $botPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $botPython -m rentbot $Mode
}
exit $LASTEXITCODE
