param(
    [string]$Python = "py"
)

$ErrorActionPreference = "Stop"

& $Python -m pip install --upgrade pyinstaller
& $Python -m PyInstaller --noconfirm --clean --onefile --name VTX-Power-Logger vtx_power_logger.py

Write-Host "Created dist\\VTX-Power-Logger.exe"
