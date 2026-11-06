$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m unittest -v test_project_manager.py test_full_cleanup.py
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
python -m PyInstaller --noconfirm --clean --onefile --windowed --name ProjectRecordManager project_manager.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
Write-Host 'Built dist/ProjectRecordManager.exe'
