# BD Lead Generation - run without Docker (local development, SQLite).
# First time:  python -m venv .venv; .\.venv\Scripts\pip install -r requirements-dev.txt
# Then:        powershell -ExecutionPolicy Bypass -File run-local.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".env")) { Write-Host "Missing .env - copy .env.example and fill APP_SECRET_KEY / SESSION_SECRET." -ForegroundColor Yellow; exit 1 }
Get-Content ".env" | Where-Object { $_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$' } | ForEach-Object {
    Set-Item -Path "env:$($Matches[1])" -Value $Matches[2]
}
if (-not $env:DATABASE_URL) { $env:DATABASE_URL = "sqlite:///./leadgen.db" }

$py = ".\.venv\Scripts\python.exe"
& $py -m app.bootstrap
if ($LASTEXITCODE -ne 0) { exit 1 }

if (-not (Test-Path ".admin-created")) {
    $email = Read-Host "Admin email for the panel"
    & $py -m scripts.create_admin $email
    if ($LASTEXITCODE -eq 0) { New-Item -ItemType File ".admin-created" | Out-Null }
}

$worker = Start-Process -FilePath $py -ArgumentList "-m", "app.worker" -NoNewWindow -PassThru
try {
    Write-Host "Panel: http://localhost:8000  (Ctrl+C to stop)" -ForegroundColor Green
    & $py -m uvicorn app.main:app --host 127.0.0.1 --port 8000
} finally {
    if (-not $worker.HasExited) { Stop-Process -Id $worker.Id }
}
