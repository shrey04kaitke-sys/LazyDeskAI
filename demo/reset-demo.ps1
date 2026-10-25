# Restores the demo to its original state (recreates every synthetic file, clears the demo database).
# Use -Remove to delete the demo workspace, database and marker entirely.
# Only the marked LazyDeskDemo folder is ever touched; any other folder is refused.
param([string]$Base = "", [switch]$Remove)
$ErrorActionPreference = "Stop"
$command = if ($Remove) { "remove" } else { "reset" }
$cli = @("-I", (Join-Path $PSScriptRoot "demo_workspace.py"), $command)
if ($Base) { $cli += @("--base", $Base) }
& python @cli
exit $LASTEXITCODE
