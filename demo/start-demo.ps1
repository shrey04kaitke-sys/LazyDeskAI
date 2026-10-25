# Starts the LazyDesk backend on 127.0.0.1:8000 against the DEMO database (never backend\lazydesk.db).
# Stop the normal backend first: the frontend is hard-wired to port 8000. Press Ctrl+C to stop.
param([string]$Base = "", [int]$Port = 8000)
$ErrorActionPreference = "Stop"
$cli = @("-I", (Join-Path $PSScriptRoot "demo_workspace.py"), "info")
if ($Base) { $cli += @("--base", $Base) }
$paths = (& python @cli) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if (-not (Test-Path -LiteralPath $paths.workspace)) { throw "Demo workspace not found. Run .\demo\setup-demo.ps1 first." }
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use (probably your normal backend). Stop it first so the demo database is used."
}
$env:LAZYDESK_DB = $paths.database
Write-Host "Demo database : $($paths.database)"
Write-Host "Scan this path: $($paths.workspace)"
Write-Host "Frontend      : cd frontend; npm run dev  ->  http://127.0.0.1:5173"
Write-Host "Nothing is approved or applied automatically; do not click Apply unless you want the demo files moved."
Push-Location (Join-Path $PSScriptRoot "..\backend")
try { & python -m uvicorn app.main:app --host 127.0.0.1 --port $Port }
finally { Pop-Location; Remove-Item Env:LAZYDESK_DB -ErrorAction SilentlyContinue }
