# Starts the Phase 1 application on loopback only. Run from any directory.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$python = Join-Path $root '.venv\Scripts\python.exe'

try {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'Python launcher (py) not found. Install Python 3.11 or newer.' }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'npm not found. Install a supported Node.js version.' }
    foreach ($port in @(8000, 5173)) {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
        try { $listener.Start() } catch { throw "Port $port is already in use. Stop the existing server and retry." } finally { $listener.Stop() }
    }
    if (-not (Test-Path $python)) {
        & py -3 -m venv (Join-Path $root '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Unable to create the Python virtual environment.' }
    }
    & $python -m pip install -r (Join-Path $backend 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
    Push-Location $frontend
    try {
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    } finally { Pop-Location }

    # Escape single quotes because the commands are passed to a new PowerShell process.
    $backendDir = $backend.Replace("'", "''")
    $frontendDir = $frontend.Replace("'", "''")
    $pythonPath = $python.Replace("'", "''")
    Start-Process powershell.exe -ArgumentList @('-NoExit', '-Command', "Set-Location -LiteralPath '$backendDir'; & '$pythonPath' -m uvicorn app.main:app --host 127.0.0.1 --port 8000")
    Start-Process powershell.exe -ArgumentList @('-NoExit', '-Command', "Set-Location -LiteralPath '$frontendDir'; & npm.cmd run dev -- --host 127.0.0.1 --port 5173 --strictPort")
    Write-Host 'Started two server windows. Open http://127.0.0.1:5173 after both servers are ready.'
    Write-Host 'Stop each server with Ctrl+C in its window. Both listen on 127.0.0.1 only.'
} catch {
    Write-Error $_
    exit 1
}
