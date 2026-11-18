# Datavera local agent - one run. Double-click the "Datavera Agent" desktop icon, or:
#   powershell -ExecutionPolicy Bypass -File run-agent.ps1                       (find one dataset and submit it)
#   powershell -ExecutionPolicy Bypass -File run-agent.ps1 --topic "bee colonies" (choose the subject)
#   powershell -ExecutionPolicy Bypass -File run-agent.ps1 -Check                (test the browser only; publishes nothing)
param([switch]$Check, [switch]$NoPause, [Parameter(ValueFromRemainingArguments = $true)][string[]]$AgentArgs)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$Host.UI.RawUI.WindowTitle = 'Datavera Agent'
$Py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$Started = $null

function Test-Port([int]$Port) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $wait = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $wait.AsyncWaitHandle.WaitOne(700)) { return $false }
        $client.EndConnect($wait)
        return $true
    } catch { return $false } finally { $client.Close() }
}

function Wait-Port([int]$Port, [int]$Seconds, [string]$What) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-Port $Port) { return }
        if ($i % 10 -eq 0) { Write-Host "  waiting for $What..." -ForegroundColor DarkGray }
        Start-Sleep -Seconds 1
    }
    throw "$What did not start within $Seconds seconds."
}

try {
    if (-not (Test-Path $Py)) { throw "The agent is not installed here yet. Run the setup line from datavera.io/publish/agent first." }
    Write-Host "Datavera Agent - $(Get-Date -Format 'dddd d MMMM, HH:mm')" -ForegroundColor Cyan
    Write-Host "Keep this window open. Your computer must stay awake and online until it says Finished." -ForegroundColor Cyan

    # The AI model server (Ollama) usually starts with Windows; start it if not.
    if (-not (Test-Port 11434)) {
        $ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
        if (-not $ollama) { $ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe' }
        Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
        Wait-Port 11434 30 'the AI model server (Ollama)'
    }

    # The browser helper (Playwright MCP) drives Microsoft Edge in a separate, private window.
    if (-not (Test-Port 8931)) {
        $Started = Start-Process -FilePath 'cmd.exe' -WindowStyle Minimized -PassThru -ArgumentList @(
            '/c', 'npx.cmd', '-y', '@playwright/mcp@latest', '--browser', 'msedge', '--isolated',
            '--port', '8931', '--host', '127.0.0.1')
        Wait-Port 8931 120 'the browser helper'
    }

    if ($Check) {
        & $Py agent.py doctor --browser
    } else {
        & $Py -u agent.py run @AgentArgs
    }
    $code = $LASTEXITCODE

    $latest = Get-ChildItem (Join-Path $PSScriptRoot 'outputs\runs') -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name | Select-Object -Last 1
    if (-not $Check -and $latest) {
        Write-Host ""
        Write-Host "------------------------------ Result ------------------------------" -ForegroundColor Cyan
        Get-Content (Join-Path $latest.FullName 'SUMMARY.md') -Encoding UTF8 -ErrorAction SilentlyContinue | Write-Host
        $draft = Join-Path $latest.FullName 'draft.json'
        if (Test-Path $draft) {
            $url = (Get-Content $draft -Raw -Encoding UTF8 | ConvertFrom-Json).edit_url
            if ($url -and -not $NoPause) {
                $open = Read-Host "Type O and press Enter to open your dataset on datavera.io (or just press Enter)"
                if ($open -match '^[Oo]') { Start-Process $url }
            }
        } elseif ($code -ne 0) {
            Write-Host "No dataset this time - that's normal for some subjects. Just run it again later; it will try a different one." -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host "Stopped: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    if ($Started) { Start-Process -FilePath 'taskkill.exe' -ArgumentList '/PID', $Started.Id, '/T', '/F' -WindowStyle Hidden -Wait }
    if (-not $NoPause) { Write-Host ""; Read-Host "Finished. Press Enter to close this window" | Out-Null }
}
