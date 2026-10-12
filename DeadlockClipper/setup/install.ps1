# Deadlock Clipper setup: installs Python (if needed), the app's Python packages,
# ffmpeg, and creates a desktop shortcut. Safe to run again to repair or update.
# Keep this file plain ASCII (Windows PowerShell 5.1 reads it as ANSI).

# 'Continue' so warnings printed by pip/python don't abort setup; real failures are checked explicitly.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$InstallLog = Join-Path $LogDir 'install.log'
Start-Transcript -Path $InstallLog -Append | Out-Null

function Step($msg) { Write-Host ''; Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "    $msg" -ForegroundColor Green }

function Get-PythonVersionOk([string[]]$cmd) {
    try {
        $exe = $cmd[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $null }
        $global:LASTEXITCODE = 1
        $rest = @()
        if ($cmd.Length -gt 1) { $rest = $cmd[1..($cmd.Length - 1)] }
        $out = & $exe @rest -c "import sys; print('%d.%d|%s' % (sys.version_info[0], sys.version_info[1], sys.executable))" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out -match '^3\.(1[0-3])\|(.+)$') { return $Matches[2].Trim() }
    } catch { }
    return $null
}

function Find-Python {
    $candidates = @(
        @('py', '-3.12'), @('py', '-3.11'), @('py', '-3.13'), @('py', '-3.10'),
        @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"),
        @('python')
    )
    foreach ($c in $candidates) {
        $found = Get-PythonVersionOk $c
        if ($found) { return $found }
    }
    return $null
}

function Download($url, $dest) {
    Write-Host "    Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -ErrorAction Stop
}

try {
    Write-Host 'Deadlock Clipper setup' -ForegroundColor Yellow
    Write-Host "App folder: $Root"
    Write-Host "Setup log:  $InstallLog"

    # Files from a downloaded ZIP are marked as "from the internet"; unblock them.
    Get-ChildItem -Path $Root -Recurse -File | Unblock-File -ErrorAction SilentlyContinue

    # ---------------------------------------------------------------- Python
    Step 'Checking for Python 3.10 - 3.13'
    $python = Find-Python
    if (-not $python) {
        Write-Host '    Python not found. Installing Python 3.12 (just for your user, no admin needed)...'
        $installer = Join-Path $env:TEMP 'python-3.12.10-amd64.exe'
        try {
            Download 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' $installer
            $p = Start-Process -ErrorAction Stop -FilePath $installer -Wait -PassThru -ArgumentList @(
                '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_test=0',
                'Include_launcher=1', 'InstallLauncherAllUsers=0')
            Write-Host "    Python installer finished with code $($p.ExitCode)"
        } catch {
            Write-Host "    Direct download failed ($($_.Exception.Message)). Trying winget..."
            winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
        }
        $python = Find-Python
        if (-not $python) { throw 'Python could not be installed. Install Python 3.12 from python.org, then run Install.bat again.' }
    }
    Ok "Using Python: $python"

    # ---------------------------------------------------------------- Packages
    Step 'Setting up the app environment'
    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPy)) {
        & $python -m venv (Join-Path $Root '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the app environment (.venv).' }
    }
    Step 'Installing Python packages (PySide6, OpenCV, numpy, yt-dlp) - this can take a few minutes'
    & $venvPy -m pip install --disable-pip-version-check --upgrade pip
    & $venvPy -m pip install --disable-pip-version-check --upgrade -r (Join-Path $Root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Installing Python packages failed. Check your internet connection and run Install.bat again.' }
    & $venvPy -c "import PySide6, cv2, numpy, yt_dlp; print('packages ok')"
    if ($LASTEXITCODE -ne 0) { throw 'The Python packages did not install correctly.' }
    Ok 'Packages installed'

    # ---------------------------------------------------------------- ffmpeg
    Step 'Checking for ffmpeg'
    $ffDir = Join-Path $Root 'tools\ffmpeg'
    $ffExe = Join-Path $ffDir 'ffmpeg.exe'
    if (-not (Test-Path $ffExe) -or -not (Test-Path (Join-Path $ffDir 'ffplay.exe'))) {
        New-Item -ItemType Directory -Force -Path $ffDir | Out-Null
        $zip = Join-Path $env:TEMP 'deadlock_clipper_ffmpeg.zip'
        $unz = Join-Path $env:TEMP 'deadlock_clipper_ffmpeg'
        $urls = @(
            'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
            'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
        )
        $got = $false
        foreach ($u in $urls) {
            try {
                Download $u $zip
                if (Test-Path $unz) { Remove-Item -Recurse -Force $unz }
                Expand-Archive -Path $zip -DestinationPath $unz -Force -ErrorAction Stop
                $exe = Get-ChildItem -Path $unz -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1
                if ($exe) {
                    Copy-Item -Path (Join-Path $exe.DirectoryName '*.exe') -Destination $ffDir -Force -ErrorAction Stop
                    $got = $true
                    break
                }
            } catch {
                Write-Host "    That download failed: $($_.Exception.Message)" -ForegroundColor DarkYellow
            }
        }
        Remove-Item -Force $zip -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $unz -ErrorAction SilentlyContinue
        if (-not $got) { throw 'Could not download ffmpeg. Check your internet connection and run Install.bat again.' }
    }
    & $ffExe -hide_banner -version | Select-Object -First 1
    Ok 'ffmpeg ready'

    # ---------------------------------------------------------------- Shortcut
    Step 'Creating the desktop shortcut'
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut((Join-Path $desktop 'Deadlock Clipper.lnk'))
    $lnk.TargetPath = Join-Path $Root '.venv\Scripts\pythonw.exe'
    $lnk.Arguments = '"' + (Join-Path $Root 'app\main.py') + '"'
    $lnk.WorkingDirectory = $Root
    $icon = Join-Path $Root 'app\icon.ico'
    if (Test-Path $icon) { $lnk.IconLocation = $icon }
    $lnk.Description = 'Turn Deadlock recordings into Shorts/Reels highlight clips'
    $lnk.Save()
    Ok "Shortcut created on your desktop: Deadlock Clipper"

    Write-Host ''
    Write-Host 'ALL DONE! Double-click "Deadlock Clipper" on your desktop to start.' -ForegroundColor Green
    Stop-Transcript | Out-Null
    exit 0
} catch {
    Write-Host ''
    Write-Host "SETUP FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Please send this file to Claude: $InstallLog" -ForegroundColor Yellow
    Stop-Transcript | Out-Null
    exit 1
}
