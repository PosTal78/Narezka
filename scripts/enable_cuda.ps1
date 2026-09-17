$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Local virtual environment is missing: $python" }
$pipTemp = Join-Path $projectRoot ".scratch\pip-temp"
New-Item -ItemType Directory -Force -Path $pipTemp | Out-Null
$env:TEMP = $pipTemp
$env:TMP = $pipTemp

# PyPI installs the CPU wheel on Windows. Use the official PyTorch CUDA index
# explicitly so local BLIP and sentence-transformers inference can use NVIDIA.
& $python -m pip install --no-cache-dir --upgrade "torch==2.11.0" --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) { throw "CUDA-enabled PyTorch installation failed." }
& $python -c "import torch; assert torch.cuda.is_available(), 'CUDA is still unavailable'; print(torch.cuda.get_device_name(0))"
if ($LASTEXITCODE -ne 0) { throw "PyTorch was installed, but CUDA is unavailable." }
