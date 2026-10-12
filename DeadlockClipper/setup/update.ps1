# Deadlock Clipper updater: downloads the latest version from GitHub and replaces the app files.
# Keeps your settings, logs, saved scans, Python environment and ffmpeg.
# Keep this file plain ASCII (Windows PowerShell 5.1 reads it as ANSI).

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = 'animator222/Startupideas'
$Branch = 'ccr-bd989d6f-sb6fm5'

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$UpdateLog = Join-Path $LogDir 'update.log'
Start-Transcript -Path $UpdateLog -Append | Out-Null

try {
    Write-Host 'Updating Deadlock Clipper' -ForegroundColor Yellow
    Write-Host "App folder: $Root"
    if (Get-Process -Name pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$Root*" }) {
        throw 'Deadlock Clipper is still open. Close it, then run Update.bat again.'
    }

    $zip = Join-Path $env:TEMP 'deadlock_clipper_update.zip'
    $unz = Join-Path $env:TEMP 'deadlock_clipper_update'
    Write-Host '==> Downloading the latest version' -ForegroundColor Cyan
    Invoke-WebRequest -Uri "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip -UseBasicParsing -ErrorAction Stop
    if (Test-Path $unz) { Remove-Item -Recurse -Force $unz }
    Expand-Archive -Path $zip -DestinationPath $unz -Force -ErrorAction Stop
    $src = Get-ChildItem -Path $unz -Recurse -Directory -Filter 'DeadlockClipper' | Select-Object -First 1
    if (-not $src) { throw 'The download did not contain the app.' }

    Write-Host '==> Replacing app files' -ForegroundColor Cyan
    Remove-Item -Recurse -Force (Join-Path $Root 'app') -ErrorAction Stop
    Copy-Item -Recurse -Force (Join-Path $src.FullName 'app') (Join-Path $Root 'app') -ErrorAction Stop
    Copy-Item -Recurse -Force (Join-Path $src.FullName 'setup\*') (Join-Path $Root 'setup') -ErrorAction Stop
    foreach ($f in @('requirements.txt', 'README.md', 'Install.bat', 'Deadlock Clipper.bat', 'Update.bat')) {
        $from = Join-Path $src.FullName $f
        if (Test-Path $from) { Copy-Item -Force $from (Join-Path $Root $f) }
    }
    Get-ChildItem -Path $Root -Recurse -File -Exclude '*.pyc' | Unblock-File -ErrorAction SilentlyContinue
    Remove-Item -Force $zip -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $unz -ErrorAction SilentlyContinue

    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path $venvPy) {
        Write-Host '==> Checking Python packages' -ForegroundColor Cyan
        & $venvPy -m pip install --disable-pip-version-check --quiet --upgrade -r (Join-Path $Root 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Updating Python packages failed. Run Install.bat to repair.' }
    } else {
        Write-Host 'App environment missing - run Install.bat once to finish setup.' -ForegroundColor Yellow
    }

    $ver = Select-String -Path (Join-Path $Root 'app\clipper\__init__.py') -Pattern 'APP_VERSION = "(.+)"' | Select-Object -First 1
    Write-Host ''
    Write-Host "UPDATED to version $($ver.Matches[0].Groups[1].Value). Start the app from your desktop shortcut." -ForegroundColor Green
    Stop-Transcript | Out-Null
    exit 0
} catch {
    Write-Host ''
    Write-Host "UPDATE FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Please send this file to Claude: $UpdateLog" -ForegroundColor Yellow
    Stop-Transcript | Out-Null
    exit 1
}
