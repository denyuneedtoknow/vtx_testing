param(
    [string]$Python = "py",
    [string]$DriverDll = ""
)

$ErrorActionPreference = "Stop"

# Run this script on a Windows development machine. The resulting dist\VTX-Testing
# directory is the only project directory that must be copied to the test laptop.
$releaseDir = Join-Path $PSScriptRoot "dist\VTX-Testing"
$workDir = Join-Path $PSScriptRoot "build"
$specDir = Join-Path $PSScriptRoot "build-spec"

Remove-Item -Recurse -Force $releaseDir, $workDir, $specDir -ErrorAction SilentlyContinue

& $Python -m pip install --upgrade pyinstaller -r requirements.txt

& $Python -m PyInstaller --noconfirm --clean --onedir --name VTX-Power-Logger `
    --distpath $releaseDir --workpath $workDir --specpath $specDir `
    vtx_power_logger.py

& $Python -m PyInstaller --noconfirm --clean --onedir --name VTX-MSP-Probe `
    --hidden-import serial `
    --distpath $releaseDir --workpath $workDir --specpath $specDir `
    vtx_msp_probe.py

Copy-Item -Recurse -Force (Join-Path $PSScriptRoot "configs") (Join-Path $releaseDir "configs")
Copy-Item -Force (Join-Path $PSScriptRoot "README.md") (Join-Path $releaseDir "README.md")

if ($DriverDll) {
    Copy-Item -Force $DriverDll (Join-Path $releaseDir "VTX-Power-Logger\rsnrpz_64.dll")
} elseif (Test-Path (Join-Path $PSScriptRoot "rsnrpz_64.dll")) {
    Copy-Item -Force (Join-Path $PSScriptRoot "rsnrpz_64.dll") (Join-Path $releaseDir "VTX-Power-Logger\rsnrpz_64.dll")
} else {
    Write-Warning "rsnrpz_64.dll was not bundled. Install the R&S NRP Toolkit on the test laptop, or rebuild with -DriverDll <path>."
}

$archivePath = Join-Path $PSScriptRoot "dist\VTX-Testing.zip"
Remove-Item -Force $archivePath -ErrorAction SilentlyContinue
Compress-Archive -Path $releaseDir -DestinationPath $archivePath

Write-Host "Release created: $releaseDir"
Write-Host "Transfer archive created: $archivePath"
