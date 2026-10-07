#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Starts the YouTube Transcript Service (development) (backend + frontend) locally.
.DESCRIPTION
    Launches the FastAPI backend and Vite dev server in separate terminal windows.
    Press Ctrl+C in either window to stop.
#>

$ErrorActionPreference = "Stop"
$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  YouTube Transcript Service (development)" -ForegroundColor Cyan
Write-Host "  Starting backend + frontend..." -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check for required environment variables
if (-not $env:YOUTUBE_API_KEY) {
    Write-Host "[WARN] YOUTUBE_API_KEY is not set." -ForegroundColor Yellow
    Write-Host "       /api/health will report "unhealthy" until it is set (in .env or the shell)." -ForegroundColor Yellow
    Write-Host "       Set it with: `$env:YOUTUBE_API_KEY = `"your_key`"" -ForegroundColor Yellow
    Write-Host ""
}

# Resolve python command
$pyCmd = "python"
if (Test-Path (Join-Path $rootDir ".venv\Scripts\python.exe")) {
    $pyCmd = Join-Path $rootDir ".venv\Scripts\python.exe"
} elseif (Test-Path (Join-Path $rootDir "venv\Scripts\python.exe")) {
    $pyCmd = Join-Path $rootDir "venv\Scripts\python.exe"
} else {
    try {
        $testOut = & python --version 2>&1
        if ($LASTEXITCODE -ne 0 -or "$testOut" -like "*Microsoft Store*") {
            $pyCmd = "py"
        }
    } catch {
        $pyCmd = "py"
    }
}

# Start backend
Write-Host "[1/2] Starting FastAPI backend on http://localhost:8000 (using $pyCmd) ..." -ForegroundColor Green
$backendJob = Start-Process -NoNewWindow -FilePath $pyCmd -ArgumentList "-m", "uvicorn", "webapp.main:app", "--host", "127.0.0.1", "--port", "8000", "--reload" -PassThru

Start-Sleep -Seconds 3

# Test backend health
try {
    $health = Invoke-RestMethod -Uri "http://localhost:8000/api/health" -TimeoutSec 5
    Write-Host "       Backend health: $($health.data.status)" -ForegroundColor Green
} catch {
    Write-Host "       Backend starting... (retrying)" -ForegroundColor Yellow
}

# Start frontend
Write-Host "[2/2] Starting Vite dev server on http://localhost:5173 ..." -ForegroundColor Green
$frontendDir = Join-Path $rootDir "frontend"
$frontendJob = Start-Process -NoNewWindow -FilePath "cmd.exe" -ArgumentList "/c", "npm", "run", "dev" -WorkingDirectory $frontendDir -PassThru

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Frontend : http://localhost:5173" -ForegroundColor Cyan
Write-Host "  Backend  : http://localhost:8000" -ForegroundColor Cyan
Write-Host "  API docs : http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Press Ctrl+C to stop all services." -ForegroundColor Gray

# Monitor processes
try {
    while ($true) {
        if ($backendJob.HasExited) {
            Write-Host "`n[WARN] Backend process exited unexpectedly." -ForegroundColor Red
            break
        }
        Start-Sleep -Seconds 1
    }
} finally {
    Write-Host "`nShutting down..." -ForegroundColor Yellow
    if (-not $backendJob.HasExited) {
        Stop-Process -Id $backendJob.Id -Force -ErrorAction SilentlyContinue
    }
    if ($frontendJob -and -not $frontendJob.HasExited) {
        Stop-Process -Id $frontendJob.Id -Force -ErrorAction SilentlyContinue
    }
}
