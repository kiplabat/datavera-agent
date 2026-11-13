# Datavera local agent - Windows installer (also updates an existing install).
# Run it by pasting this ONE line into PowerShell:
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/kiplabat/datavera-agent/main/install-windows.ps1 | iex"
# It installs Python 3.12, Node.js and Ollama if missing (using winget), downloads the agent into
# %USERPROFILE%\DataveraAgent, downloads the AI model, connects your Datavera account, and puts a
# "Datavera Agent" icon on your desktop. Your key, settings and history are kept when you run it again.

function Install-DataveraAgent {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $Repo = 'https://github.com/kiplabat/datavera-agent/archive/refs/heads/main.zip'
    $Model = 'qwen3.5:4b'
    $Dest = if ($env:DATAVERA_AGENT_DIR) { $env:DATAVERA_AGENT_DIR } else { Join-Path $env:USERPROFILE 'DataveraAgent' }
    $Total = 8

    function Step($n, $text) { Write-Host ""; Write-Host "[$n/$Total] $text" -ForegroundColor Cyan }
    function Ok($text) { Write-Host "    OK  $text" -ForegroundColor Green }
    function Note($text) { Write-Host "    $text" -ForegroundColor Yellow }
    function Refresh-Path {
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    }
    function Winget-Install($id, $name, $extra) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw "winget is missing. Open the Microsoft Store, install 'App Installer', then run this again."
        }
        Note "Installing $name (this can take a few minutes; click Yes if Windows asks for permission)..."
        $wargs = @('install', '--id', $id, '-e', '--silent', '--accept-package-agreements', '--accept-source-agreements') + $extra
        & winget @wargs | Out-Host
        Refresh-Path
    }

    Write-Host "======================================================" -ForegroundColor Cyan
    Write-Host "  Datavera local agent - setup" -ForegroundColor Cyan
    Write-Host "  Folder: $Dest" -ForegroundColor Cyan
    Write-Host "======================================================" -ForegroundColor Cyan

    # 1. Computer check
    Step 1 'Checking this computer'
    $ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
    $drive = Get-PSDrive -Name ($env:USERPROFILE.Substring(0, 1))
    $freeGB = [math]::Round($drive.Free / 1GB)
    Ok "$ramGB GB memory, $freeGB GB free disk space"
    if ($ramGB -lt 8) { Note "Less than 8 GB memory: the AI model may be very slow or fail." }
    if ($freeGB -lt 10) { throw "At least 10 GB of free disk space is needed (the AI model alone is about 3.5 GB)." }

    # 2. Python 3.12
    Step 2 'Python 3.12'
    $Python = $null
    try { $Python = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null) } catch { }
    if (-not $Python) {
        $guess = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path $guess) { $Python = $guess }
    }
    if (-not $Python) {
        Winget-Install 'Python.Python.3.12' 'Python 3.12' @('--scope', 'user')
        $guess = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path $guess) { $Python = $guess } else { $Python = (& py -3.12 -c "import sys; print(sys.executable)") }
    }
    Ok "Python: $Python"

    # 3. Node.js (runs the browser helper)
    Step 3 'Node.js (for the web browser helper)'
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Winget-Install 'OpenJS.NodeJS.LTS' 'Node.js' @() }
    if (-not (Get-Command npx.cmd -ErrorAction SilentlyContinue)) { throw "Node.js did not install correctly. Close this window, open a new PowerShell and run the setup line again." }
    Ok ("Node.js " + (& node --version))

    # 4. Ollama (runs the AI model on this computer)
    Step 4 'Ollama (runs the AI model on this computer)'
    $Ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
    if (-not $Ollama) { $guess = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'; if (Test-Path $guess) { $Ollama = $guess } }
    if (-not $Ollama) {
        Winget-Install 'Ollama.Ollama' 'Ollama' @()
        $Ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
        if (-not $Ollama) { $Ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe' }
    }
    $up = $false
    try { Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 | Out-Null; $up = $true } catch { }
    if (-not $up) {
        Start-Process -FilePath $Ollama -ArgumentList 'serve' -WindowStyle Hidden
        for ($i = 0; $i -lt 30 -and -not $up; $i++) {
            Start-Sleep -Seconds 1
            try { Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null; $up = $true } catch { }
        }
    }
    if (-not $up) { throw "Ollama did not start. Restart the computer and run the setup line again." }
    Ok 'Ollama is running'

    # 5. The agent itself
    Step 5 'Downloading the Datavera agent'
    $tmp = Join-Path $env:TEMP ('datavera-agent-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    Invoke-WebRequest -Uri $Repo -OutFile (Join-Path $tmp 'agent.zip') -UseBasicParsing
    Expand-Archive -Path (Join-Path $tmp 'agent.zip') -DestinationPath $tmp -Force
    $src = Get-ChildItem $tmp -Directory | Where-Object { $_.Name -like 'datavera-agent*' } | Select-Object -First 1
    if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Path $Dest | Out-Null }
    $backup = Join-Path $Dest ('backup_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
    Get-ChildItem $src.FullName -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($src.FullName.Length + 1)
        $target = Join-Path $Dest $rel
        if (Test-Path $target) {
            $b = Join-Path $backup $rel
            New-Item -ItemType Directory -Path (Split-Path $b) -Force | Out-Null
            Copy-Item $target $b -Force
        }
        New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
        Copy-Item $_.FullName $target -Force
    }
    Remove-Item $tmp -Recurse -Force
    Ok "Agent files are in $Dest (your key, settings and history were not touched)"

    # 6. Python packages
    Step 6 'Installing the Python packages (2-5 minutes)'
    $VenvPy = Join-Path $Dest '.venv\Scripts\python.exe'
    if (-not (Test-Path $VenvPy)) { & $Python -m venv (Join-Path $Dest '.venv') }
    & $VenvPy -m pip install --disable-pip-version-check -q --upgrade pip
    & $VenvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Dest 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw "Installing Python packages failed. Check your internet connection and run the setup line again." }
    Ok 'Python packages installed'

    # 7. AI model + browser helper
    Step 7 "Downloading the AI model $Model (about 3.5 GB - this is the slow part)"
    & $Ollama pull $Model
    if ($LASTEXITCODE -ne 0) { throw "The model download failed. Run the setup line again; it continues where it stopped." }
    Note 'Fetching the browser helper...'
    & npx.cmd -y '@playwright/mcp@latest' --help | Out-Null
    Ok 'Model and browser helper ready'

    # 8. Datavera account
    Step 8 'Connecting your Datavera account'
    Set-Location $Dest
    $hasKey = (Test-Path (Join-Path $Dest '.env')) -and (Select-String -Path (Join-Path $Dest '.env') -Pattern '^DATAVERA_MCP_TOKEN=.+' -Quiet)
    $hasConfig = Test-Path (Join-Path $Dest 'agent_config.json')
    if ($hasKey -and $hasConfig) {
        & $VenvPy setup_account.py --check
        Ok 'Already connected. (To change account or key: open the folder and run setup_account.py again.)'
    } else {
        & $VenvPy setup_account.py
        if ($LASTEXITCODE -ne 0) { throw "Account setup did not finish. Run the setup line again when you have your API key." }
    }

    # Desktop icon
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut((Join-Path $desktop 'Datavera Agent.lnk'))
    $lnk.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Dest\run-agent.ps1`""
    $lnk.WorkingDirectory = $Dest
    $lnk.Description = 'Find a public dataset and publish it to Datavera'
    $lnk.Save()
    Ok 'Added a "Datavera Agent" icon to your desktop'

    Write-Host ""
    Write-Host "Last check: opening a web page with the browser helper (no AI, nothing published)..." -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dest 'run-agent.ps1') -Check -NoPause
    Write-Host ""
    Write-Host "======================================================" -ForegroundColor Green
    Write-Host "  All done!  Double-click 'Datavera Agent' on your desktop." -ForegroundColor Green
    Write-Host "  Each run finds one dataset and submits it (10-20 minutes)." -ForegroundColor Green
    Write-Host "======================================================" -ForegroundColor Green
}

try {
    Install-DataveraAgent
} catch {
    Write-Host ""
    Write-Host "Setup stopped: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Nothing is broken. Fix the problem above and paste the setup line again - it continues safely." -ForegroundColor Yellow
}
