#!/bin/bash
# Datavera local agent - Mac installer (also updates an existing install).  NOT YET TESTED ON A REAL MAC.
# Run it by pasting this ONE line into Terminal:
#   curl -fsSL https://raw.githubusercontent.com/kiplabat/datavera-agent/main/install-mac.sh | bash
# Installs Homebrew, Python 3.12, Node.js, Ollama and Google Chrome if missing, downloads the agent into
# ~/DataveraAgent, downloads the AI model, connects your Datavera account and puts a "Datavera Agent"
# icon on your Desktop. Your key, settings and history are kept when you run it again.
set -euo pipefail

REPO="https://github.com/kiplabat/datavera-agent/archive/refs/heads/main.tar.gz"
MODEL="qwen3.5:4b"
DEST="${DATAVERA_AGENT_DIR:-$HOME/DataveraAgent}"
TOTAL=8
step() { printf "\n\033[36m[%s/%s] %s\033[0m\n" "$1" "$TOTAL" "$2"; }
ok() { printf "    \033[32mOK\033[0m  %s\n" "$1"; }
note() { printf "    \033[33m%s\033[0m\n" "$1"; }
fail() { printf "\n\033[31mSetup stopped: %s\033[0m\nNothing is broken. Fix the problem and paste the setup line again.\n" "$1"; exit 1; }

echo "======================================================"
echo "  Datavera local agent - setup"
echo "  Folder: $DEST"
echo "======================================================"

step 1 "Checking this Mac"
[ "$(uname)" = "Darwin" ] || fail "This installer is for macOS."
RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
FREE_GB=$(df -g "$HOME" | awk 'NR==2 {print $4}')
ok "$RAM_GB GB memory, $FREE_GB GB free disk space"
[ "$RAM_GB" -ge 8 ] || note "Less than 8 GB memory: the AI model may be very slow or fail."
[ "$FREE_GB" -ge 10 ] || fail "At least 10 GB of free disk space is needed (the AI model alone is about 3.5 GB)."

step 2 "Homebrew (the Mac app installer)"
if ! command -v brew >/dev/null 2>&1; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
fi
if ! command -v brew >/dev/null 2>&1; then
  note "Installing Homebrew. It will ask for your Mac password (the one you log in with); typing it shows nothing."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/tty
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
fi
command -v brew >/dev/null 2>&1 || fail "Homebrew did not install."
ok "Homebrew $(brew --version | head -1)"

step 3 "Python 3.12, Node.js and Ollama"
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
brew list node >/dev/null 2>&1 || brew install node
brew list ollama >/dev/null 2>&1 || brew install ollama
PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
ok "$($PYTHON --version), Node.js $(node --version), $(ollama --version 2>/dev/null | tail -1)"

step 4 "Google Chrome (the agent's browser) and the Ollama service"
if [ ! -d "/Applications/Google Chrome.app" ]; then brew install --cask google-chrome; fi
ok "Chrome is installed"
curl -s -m 3 http://127.0.0.1:11434/api/tags >/dev/null || brew services start ollama >/dev/null
for i in $(seq 1 30); do curl -s -m 2 http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
curl -s -m 3 http://127.0.0.1:11434/api/tags >/dev/null || fail "Ollama did not start. Restart the Mac and run the setup line again."
ok "Ollama is running"

step 5 "Downloading the Datavera agent"
TMP="$(mktemp -d)"
curl -fsSL "$REPO" | tar -xz -C "$TMP"
SRC="$(find "$TMP" -maxdepth 1 -type d -name 'datavera-agent*' | head -1)"
mkdir -p "$DEST"
BACKUP="$DEST/backup_$(date +%Y%m%d_%H%M%S)"
(cd "$SRC" && find . -type f) | while read -r rel; do
  rel="${rel#./}"
  if [ -f "$DEST/$rel" ]; then mkdir -p "$BACKUP/$(dirname "$rel")"; cp -p "$DEST/$rel" "$BACKUP/$rel"; fi
  mkdir -p "$DEST/$(dirname "$rel")"; cp -p "$SRC/$rel" "$DEST/$rel"
done
rm -rf "$TMP"
chmod +x "$DEST/run-agent.sh"
ok "Agent files are in $DEST (your key, settings and history were not touched)"

step 6 "Installing the Python packages (2-5 minutes)"
[ -x "$DEST/.venv/bin/python" ] || "$PYTHON" -m venv "$DEST/.venv"
"$DEST/.venv/bin/python" -m pip install --disable-pip-version-check -q --upgrade pip
"$DEST/.venv/bin/python" -m pip install --disable-pip-version-check -q -r "$DEST/requirements.txt" || fail "Installing Python packages failed. Check the internet connection."
ok "Python packages installed"

step 7 "Downloading the AI model $MODEL (about 3.5 GB - this is the slow part)"
ollama pull "$MODEL" || fail "The model download failed. Run the setup line again; it continues where it stopped."
note "Fetching the browser helper..."
npx -y @playwright/mcp@latest --help >/dev/null
ok "Model and browser helper ready"

step 8 "Connecting your Datavera account"
cd "$DEST"
if grep -q '^DATAVERA_MCP_TOKEN=.' .env 2>/dev/null && [ -f agent_config.json ]; then
  .venv/bin/python setup_account.py --check
  ok "Already connected. (To change account or key: cd ~/DataveraAgent && .venv/bin/python setup_account.py)"
else
  .venv/bin/python setup_account.py </dev/tty || fail "Account setup did not finish. Run the setup line again when you have your API key."
fi

cat > "$HOME/Desktop/Datavera Agent.command" <<EOF
#!/bin/bash
"$DEST/run-agent.sh"
EOF
chmod +x "$HOME/Desktop/Datavera Agent.command"
ok 'Added a "Datavera Agent" icon to your Desktop'

echo ""
echo "Last check: opening a web page with the browser helper (no AI, nothing published)..."
"$DEST/run-agent.sh" --check --no-pause </dev/tty || true
echo ""
echo "======================================================"
echo "  All done!  Double-click 'Datavera Agent' on your Desktop."
echo "  Each run finds one dataset and submits it (10-20 minutes)."
echo "======================================================"
