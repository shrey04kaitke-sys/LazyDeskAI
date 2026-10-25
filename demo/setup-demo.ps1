# Creates the synthetic demo workspace under %LOCALAPPDATA%\LazyDeskDemo (never inside this repo or your personal folders).
# Safe to re-run: it rebuilds only that marked demo folder.
param([string]$Base = "")
$ErrorActionPreference = "Stop"
$cli = @("-I", (Join-Path $PSScriptRoot "demo_workspace.py"), "setup")
if ($Base) { $cli += @("--base", $Base) }
& python @cli
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host ""
Write-Host "Next: .\demo\start-demo.ps1   (backend on the demo database)"
Write-Host "      cd frontend; npm run dev  (then open http://127.0.0.1:5173 and scan the workspace path above)"
