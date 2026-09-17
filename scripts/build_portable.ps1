param(
    [string]$OutputRoot = (Join-Path $PSScriptRoot "..\portable"),
    [string]$FfmpegPath = "$env:LOCALAPPDATA\Narezchik\tools\ffmpeg",
    [switch]$SkipArchive
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Local virtual environment is missing: $python" }
& $python -c "import faster_whisper, scenedetect, cv2, sentence_transformers, torch, transformers"
if ($LASTEXITCODE -ne 0) { throw "Install video and matching dependencies in .venv before building the portable release." }
& $python -c "import torch; print('Visual analysis device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU fallback')"

$ffmpeg = Join-Path $FfmpegPath "ffmpeg.exe"
$ffprobe = Join-Path $FfmpegPath "ffprobe.exe"
if (-not (Test-Path -LiteralPath $ffmpeg) -or -not (Test-Path -LiteralPath $ffprobe)) {
    throw "FfmpegPath must contain ffmpeg.exe and ffprobe.exe. Received: $FfmpegPath"
}

$portable = [System.IO.Path]::GetFullPath($OutputRoot)
$stageRoot = Join-Path $projectRoot ".scratch\portable-stage"
$workPath = Join-Path $projectRoot ".scratch\pyinstaller"
$env:PYINSTALLER_CONFIG_DIR = Join-Path $projectRoot ".scratch\pyinstaller-cache"
# Build away from the user-owned portable folder. PyInstaller may remove its
# destination directory, so it must never receive $portable as --distpath.
& $python -m PyInstaller --noconfirm --clean --onedir --windowed --name Narezchik `
    --distpath $stageRoot --workpath $workPath --specpath $workPath `
    --paths (Join-Path $projectRoot "src") `
    --collect-all faster_whisper --collect-all ctranslate2 `
    (Join-Path $projectRoot "src\narezchik\main.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$stagedApp = Join-Path $stageRoot "Narezchik"
if (-not (Test-Path -LiteralPath (Join-Path $stagedApp "Narezchik.exe"))) { throw "PyInstaller did not create Narezchik.exe." }

# PyInstaller can accidentally collect ICU DLLs from the machine-wide PATH.
# They shadow the Windows ICU library and make PySide6 QtCore fail to load when
# the user starts the portable EXE from Explorer. The application does not use
# those collected copies; Qt uses the system-provided library on Windows.
foreach ($name in @("icuuc.dll", "icudt78.dll")) {
    $unwanted = Join-Path $stagedApp "_internal\$name"
    if (Test-Path -LiteralPath $unwanted) { Remove-Item -LiteralPath $unwanted -Force }
}

# The application imports Whisper dynamically, so its VAD model is not found
# by PyInstaller unless explicitly collected. Verify every runtime component
# that is needed before replacing the user's currently working application.
$requiredFiles = @(
    "Narezchik.exe",
    "_internal\python312.dll",
    "_internal\faster_whisper\assets\silero_vad_v6.onnx",
    "_internal\ctranslate2\ctranslate2.dll",
    "_internal\torch\lib\cublas64_12.dll",
    "_internal\torch\lib\cudnn64_9.dll"
)
foreach ($relativePath in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $stagedApp $relativePath))) {
        throw "Portable build is incomplete: required file is missing: $relativePath"
    }
}

New-Item -ItemType Directory -Force -Path $portable, (Join-Path $portable "data\projects"), (Join-Path $portable "data\models\whisper"), (Join-Path $portable "data\cache"), (Join-Path $portable "data\logs") | Out-Null
# Only application files are replaced. $portable\data contains user projects
# and downloaded models, so this script never deletes or copies over it.
foreach ($name in @("Narezchik.exe", "_internal", "tools")) {
    $target = Join-Path $portable $name
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
}
Move-Item -LiteralPath (Join-Path $stagedApp "Narezchik.exe") -Destination (Join-Path $portable "Narezchik.exe")
Move-Item -LiteralPath (Join-Path $stagedApp "_internal") -Destination (Join-Path $portable "_internal")

$tools = Join-Path $portable "tools\ffmpeg"
New-Item -ItemType Directory -Force -Path $tools | Out-Null
Copy-Item -LiteralPath $ffmpeg, $ffprobe -Destination $tools -Force
# The archive intentionally excludes user data and downloaded models. They are
# created by the application on first start beside the executable. A CUDA build
# is large, so local development builds may explicitly keep only the ready folder.
if (-not $SkipArchive) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = Join-Path (Split-Path -Parent $portable) "Narezchik-portable.zip"
    try {
        Compress-Archive -Path (Join-Path $portable "Narezchik.exe"), (Join-Path $portable "_internal"), (Join-Path $portable "tools") -DestinationPath $archive -Force -ErrorAction Stop
        $zip = [System.IO.Compression.ZipFile]::OpenRead($archive)
        try {
            if ($zip.Entries.Count -eq 0) { throw "The portable archive is empty." }
        }
        finally {
            $zip.Dispose()
        }
    }
    catch {
        if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
        throw "Portable folder is ready, but the ZIP archive could not be created: $($_.Exception.Message)"
    }
}
Write-Host "Portable application ready: $portable"
