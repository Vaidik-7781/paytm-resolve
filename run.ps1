# Windows PowerShell: one-command setup and run
$ErrorActionPreference = "Stop"
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python -m pip install -q -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$port = if ($env:PORT) { [int]$env:PORT } else { 8001 }
Write-Host "Ops console:   http://127.0.0.1:$port/"
Write-Host "Customer app:  http://127.0.0.1:$port/customer"
.\.venv\Scripts\python -m uvicorn resolve.api.app:app --host 127.0.0.1 --port $port
