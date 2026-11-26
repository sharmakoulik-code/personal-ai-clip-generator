<#
.SYNOPSIS
    Loads Groq / Gemini API keys from the clipboard and (re)starts the local app.

.DESCRIPTION
    You copy a key to the clipboard, press Enter, and this script takes it from
    there. The key is never typed into the shell, never echoed and never written
    into the repository. The clipboard is overwritten afterwards.

    After the keys are loaded, stale backend/frontend processes on ports 8000 and
    5173 are stopped and both servers are started again with the keys in their
    environment. If the virtual environment or node_modules are missing, the
    existing start-local.ps1 is used instead (it installs dependencies first).

.PARAMETER GroqOnly
    Ask only for the Groq key. Gemini stays unset.

.PARAMETER Persist
    Also save the loaded keys in your Windows user environment so new terminals
    inherit them. This stores them in the registry; omit the switch to keep them
    memory-only for this session.

.PARAMETER NoRestart
    Only load the keys and print a summary; do not stop or start any server.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\start-local-with-keys.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\start-local-with-keys.ps1 -GroqOnly -NoRestart
#>
[CmdletBinding()]
param(
    [switch]$GroqOnly,
    [switch]$Persist,
    [switch]$NoRestart
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

function Read-KeyFromClipboard {
    param(
        [Parameter(Mandatory)][string]$Name,
        [string]$Prefix = ''
    )
    $prompt = "Copy your $Name API key to the clipboard, then press Enter. Type nothing here. " +
              "Leave the clipboard empty and press Enter to skip."
    $null = Read-Host $prompt
    $value = ''
    try { $value = (Get-Clipboard -Raw) } catch { $value = '' }
    # Overwrite the key as soon as it has been read, in case the prompt is retried.
    try { Set-Clipboard -Value ' ' } catch { }
    if ($null -ne $value) { $value = $value.Trim() }

    if (-not $value) {
        Write-Host "  $Name : skipped (clipboard was empty)." -ForegroundColor DarkGray
        return $null
    }
    if ($value -match '\s') {
        throw "$Name key contains whitespace. Copy only the key itself: no quotes and no line breaks."
    }
    if ($Prefix -and -not $value.StartsWith($Prefix)) {
        Write-Warning "$Name : loaded $($value.Length) characters, but keys normally start with '$Prefix'. Double-check you copied the right value."
        return $value
    }
    Write-Host ("  {0} : loaded {1} characters." -f $Name, $value.Length) -ForegroundColor Green
    return $value
}

Write-Host 'Personal AI Clip Generator - start with cloud AI keys' -ForegroundColor Cyan
Write-Host 'Keys stay in this process only (and in the two server windows it opens).' -ForegroundColor DarkGray
Write-Host 'They are never written into the repository, logs or the database.' -ForegroundColor DarkGray
Write-Host ''

$groq = Read-KeyFromClipboard -Name 'Groq' -Prefix 'gsk_'
if ($GroqOnly) {
    $gemini = $null
    Write-Host '  Gemini : skipped (-GroqOnly).' -ForegroundColor DarkGray
} else {
    $gemini = Read-KeyFromClipboard -Name 'Gemini' -Prefix 'AIza'
}

# Fall back to anything already present in this shell's environment.
if (-not $groq -and $env:GROQ_API_KEY) {
    $groq = $env:GROQ_API_KEY
    Write-Host '  Groq : reusing GROQ_API_KEY already set in this shell.' -ForegroundColor DarkGray
}
if (-not $gemini -and -not $GroqOnly -and $env:GEMINI_API_KEY) {
    $gemini = $env:GEMINI_API_KEY
    Write-Host '  Gemini : reusing GEMINI_API_KEY already set in this shell.' -ForegroundColor DarkGray
}

if (-not $groq -and -not $gemini) {
    Write-Warning 'No keys loaded. Cloud AI needs GROQ_API_KEY or GEMINI_API_KEY.'
    Write-Host 'For local-only mode (no cloud AI) run: powershell -ExecutionPolicy Bypass -File .\start-local.ps1' -ForegroundColor Yellow
    exit 1
}

$env:GROQ_API_KEY = $groq
$env:GEMINI_API_KEY = $gemini

if ($Persist) {
    foreach ($pair in @(@('GROQ_API_KEY', $groq), @('GEMINI_API_KEY', $gemini))) {
        if ($pair[1]) {
            [Environment]::SetEnvironmentVariable($pair[0], $pair[1], 'User')
        }
    }
    Write-Host 'Saved to your Windows user environment; new terminals will inherit them.' -ForegroundColor Yellow
}

if ($NoRestart) {
    Write-Host ''
    Write-Host 'Keys loaded into this PowerShell process only (-NoRestart).' -ForegroundColor Green
    Write-Host 'Start the servers from this same window so they inherit the keys.' -ForegroundColor DarkGray
    exit 0
}

$python = Join-Path $root '.venv\Scripts\python.exe'
$frontendDir = Join-Path $root 'frontend'
$backendDir = Join-Path $root 'backend'
$hasEnvironment = (Test-Path $python) -and (Test-Path (Join-Path $frontendDir 'node_modules'))

if ($hasEnvironment) {
    # Fast restart: stop only the servers bound to our two ports.
    $stopped = $false
    foreach ($port in 8000, 5173) {
        $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($listener in $listeners) {
            $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
            if ($process -and $process.ProcessName -in @('python', 'pythonw', 'node')) {
                Write-Host ("Stopping {0} (PID {1}) on port {2}" -f $process.ProcessName, $process.Id, $port) -ForegroundColor Yellow
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
                $stopped = $true
            }
        }
    }
    if (-not $stopped) { Write-Host 'No running servers found; starting fresh.' -ForegroundColor DarkGray }
    Start-Sleep -Milliseconds 1000

    foreach ($port in 8000, 5173) {
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
            throw "Port $port is still in use. Close the process using it, then run this script again."
        }
    }

    # Quote paths for the child PowerShell processes.
    $backendArg = $backendDir.Replace("'", "''")
    $frontendArg = $frontendDir.Replace("'", "''")
    $pythonArg = $python.Replace("'", "''")

    Start-Process powershell.exe -ArgumentList @(
        '-NoExit', '-Command',
        "Set-Location -LiteralPath '$backendArg'; & '$pythonArg' -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
    )
    Start-Process powershell.exe -ArgumentList @(
        '-NoExit', '-Command',
        "Set-Location -LiteralPath '$frontendArg'; & npm.cmd run dev -- --host 127.0.0.1 --port 5173 --strictPort"
    )

    Write-Host ''
    Write-Host 'Started two server windows with the keys in their environment.' -ForegroundColor Green
} else {
    Write-Host 'Virtual environment or node_modules missing - running start-local.ps1 (installs dependencies first).' -ForegroundColor DarkGray
    & (Join-Path $root 'start-local.ps1')
    Write-Host ''
    Write-Host 'Servers started via start-local.ps1 with the keys in their environment.' -ForegroundColor Green
}

Write-Host 'Open http://127.0.0.1:5173 once both windows report ready.' -ForegroundColor Green
Write-Host 'Close those two windows to unload the keys. Never paste keys into chat, screenshots or Git.' -ForegroundColor DarkGray
