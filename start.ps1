# BD Lead Generation - one-click start for Windows (needs Docker Desktop running).
# Double-click start.bat, or run:  powershell -ExecutionPolicy Bypass -File start.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Random-UrlSafe([int]$bytes) {
    $b = New-Object byte[] $bytes
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    return [Convert]::ToBase64String($b).Replace('+', '-').Replace('/', '_')
}

Write-Host "== BD Lead Generation ==" -ForegroundColor Green

# 1. Docker
try { docker info *> $null } catch { }
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker is not running. Install Docker Desktop (https://www.docker.com/products/docker-desktop/), start it, then run this again." -ForegroundColor Yellow
    Read-Host "Press Enter to close"; exit 1
}

# 2. .env with fresh secrets (only on the first run - never overwrite, the key decrypts your saved API keys)
if (-not (Test-Path ".env")) {
    $envText = Get-Content ".env.example" -Raw
    $envText = $envText -replace "(?m)^APP_SECRET_KEY=.*$", ("APP_SECRET_KEY=" + (Random-UrlSafe 32))
    $envText = $envText -replace "(?m)^SESSION_SECRET=.*$", ("SESSION_SECRET=" + (Random-UrlSafe 32))
    $envText = $envText -replace "(?m)^POSTGRES_PASSWORD=.*$", ("POSTGRES_PASSWORD=" + (Random-UrlSafe 18))
    Set-Content -Path ".env" -Value $envText -NoNewline
    Write-Host "Created .env with new secrets. Keep this file private and back it up." -ForegroundColor Cyan
}

# 3. Build and start
Write-Host "Starting (the first time takes 5-10 minutes: it downloads Python, Postgres and Chromium)..."
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Read-Host "docker compose failed - see the messages above. Press Enter"; exit 1 }

# 4. Wait until the panel answers
Write-Host -NoNewline "Waiting for the admin panel"
$ok = $false
for ($i = 0; $i -lt 150; $i++) {
    try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 http://localhost:8000/health).StatusCode -eq 200) { $ok = $true; break } } catch { }
    Write-Host -NoNewline "."; Start-Sleep -Seconds 2
}
Write-Host ""
if (-not $ok) { Write-Host "The panel did not start. Run:  docker compose logs web" -ForegroundColor Yellow; Read-Host "Press Enter"; exit 1 }

# 5. First admin user (once)
if (-not (Test-Path ".admin-created")) {
    $email = Read-Host "Admin email for the panel"
    Write-Host "Choose a password (10+ characters). It is typed invisibly."
    docker compose exec web python -m scripts.create_admin $email
    if ($LASTEXITCODE -eq 0) { New-Item -ItemType File ".admin-created" | Out-Null }
}

# 6. Open it
Write-Host "Ready: http://localhost:8000  (only this computer can open it)" -ForegroundColor Green
Start-Process "http://localhost:8000"
Write-Host "Stop later with stop.bat. Your data and API keys stay on this computer."
Read-Host "Press Enter to close this window (the server keeps running)"
