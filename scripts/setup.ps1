$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "[setup] Project root: $Root"

if (-not (Test-Path ".venv")) {
    Write-Host "[setup] Creating virtual environment (.venv)..."
    python -m venv .venv
}

$Activate = Join-Path $Root ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $Activate)) {
    throw "[setup] Could not find venv activation script at $Activate"
}

. $Activate

Write-Host "[setup] Installing dependencies..."
python -m pip install --upgrade pip
pip install -r requirements.txt

Write-Host "[setup] Installing Playwright Chromium..."
playwright install chromium

if ((-not (Test-Path ".env")) -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
    Write-Host "[setup] Created .env from .env.example (fill your API keys)."
}

Write-Host "[setup] Done. Activate venv and run PersonaMeet."
