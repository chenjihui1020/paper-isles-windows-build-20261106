$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Version = (Get-Content VERSION -Raw).Trim()
if (-not (Test-Path .build/windows/venv/Scripts/python.exe)) { py -3.13 -m venv .build/windows/venv }
$Python = Join-Path $Root '.build/windows/venv/Scripts/python.exe'
& $Python -m pip install -r requirements-build-windows.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& $Python -m unittest discover -s tests -p 'test_*.py' -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
& $Python -c "from PIL import Image; Image.open('assets/PaperIsles-transparent.png').save('windows/PaperIsles.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if ($LASTEXITCODE -ne 0) { throw 'Icon creation failed' }
& $Python -m PyInstaller --noconfirm --clean --onedir --windowed --disable-windowed-traceback --name PaperIsles --icon (Join-Path $Root 'windows/PaperIsles.ico') --add-data "$(Join-Path $Root 'app/static');static" --collect-data webview --hidden-import webview.platforms.edgechromium --distpath .build/windows/dist --workpath .build/windows/work --specpath .build/windows/spec app/app.py
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed' }
$env:PAPER_ISLES_HOME = Join-Path $env:TEMP ('paper-isles-check-' + [guid]::NewGuid())
$process = Start-Process -FilePath '.build/windows/dist/PaperIsles/PaperIsles.exe' -ArgumentList '--self-check' -PassThru
if (-not $process.WaitForExit(60000)) { $process.Kill(); throw 'Packaged self-check timed out' }
if (Test-Path "$env:PAPER_ISLES_HOME/startup-error.log") { Get-Content "$env:PAPER_ISLES_HOME/startup-error.log" }
if ($process.ExitCode -ne 0 -or -not (Test-Path "$env:PAPER_ISLES_HOME/paper_isles.sqlite3")) { throw 'Packaged self-check failed' }
Remove-Item $env:PAPER_ISLES_HOME -Recurse -Force
Remove-Item Env:PAPER_ISLES_HOME
$compiler = ${env:ISCC_PATH}
if (-not $compiler) { $compiler = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe' }
if (-not (Test-Path $compiler)) { throw 'Inno Setup 6 is required on the build machine.' }
& $compiler "/DAppVersion=$Version" windows/installer.iss
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
Write-Host "Installer: release/windows/PaperIsles-$Version-Windows-x64-Setup.exe"
