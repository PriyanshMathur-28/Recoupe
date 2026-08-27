<#
  Restores the pre-Stitch frontend checkpoint.
  Run from anywhere; paths are resolved relative to this script.
#>
$ErrorActionPreference = 'Stop'

$checkpoint = $PSScriptRoot
$root       = (Resolve-Path (Join-Path $checkpoint '..\..')).Path
$stamp      = Get-Date -Format 'yyyy-MM-dd-HHmmss'
$backup     = Join-Path $root ".checkpoints\replaced-$stamp"

Write-Host "Repository root : $root"
Write-Host "Restoring from  : $checkpoint"
Write-Host "Current code -> : $backup"
Write-Host ''

New-Item -ItemType Directory -Force -Path $backup | Out-Null

# --- frontend source ---------------------------------------------------------
$frontend = Join-Path $root 'frontend'
$modules  = Join-Path $frontend 'node_modules'
$parked   = Join-Path $root '.node_modules_parked'

if (Test-Path $modules) {
    # Park node_modules so the swap does not have to copy thousands of files.
    if (Test-Path $parked) { Remove-Item -Recurse -Force $parked }
    Move-Item $modules $parked
}

if (Test-Path $frontend) {
    Move-Item $frontend (Join-Path $backup 'frontend')
}
Copy-Item (Join-Path $checkpoint 'frontend') $frontend -Recurse

if (Test-Path $parked) {
    Move-Item $parked (Join-Path $frontend 'node_modules')
}

# --- compiled bundle ---------------------------------------------------------
$built = Join-Path $root 'static\clients'
if (Test-Path $built) {
    Move-Item $built (Join-Path $backup 'static-clients')
}
New-Item -ItemType Directory -Force -Path (Join-Path $root 'static') | Out-Null
Copy-Item (Join-Path $checkpoint 'static-clients') $built -Recurse

Write-Host 'Restored.' -ForegroundColor Green
Write-Host ''
Write-Host 'The previous compiled bundle is back in static\clients, so:'
Write-Host '    python dashboard.py'
Write-Host 'serves the old dashboard right away. No npm build needed.'
Write-Host ''
Write-Host "To undo this restore, the replaced code is in:"
Write-Host "    $backup"
