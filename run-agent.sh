#!/bin/bash
# Datavera local agent - one run (Mac). Double-click "Datavera Agent" on the Desktop, or:
#   ./run-agent.sh                      find one dataset and submit it
#   ./run-agent.sh --topic "bee colonies"
#   ./run-agent.sh --check              test the browser only; publishes nothing
cd "$(dirname "$0")"
CHECK=0; PAUSE=1; ARGS=()
for a in "$@"; do
  case "$a" in --check) CHECK=1 ;; --no-pause) PAUSE=0 ;; *) ARGS+=("$a") ;; esac
done
for b in /opt/homebrew/bin /usr/local/bin; do [ -d "$b" ] && PATH="$b:$PATH"; done
PY=".venv/bin/python"
STARTED=""
port_open() { nc -z 127.0.0.1 "$1" >/dev/null 2>&1; }
wait_port() {
  for i in $(seq 1 "$2"); do port_open "$1" && return 0; [ $((i % 10)) -eq 1 ] && echo "  waiting for $3..."; sleep 1; done
  echo "$3 did not start within $2 seconds."; return 1
}
finish() {
  [ -n "$STARTED" ] && kill "$STARTED" >/dev/null 2>&1 && pkill -f "@playwright/mcp.*--port 8931" >/dev/null 2>&1
  if [ "$PAUSE" = 1 ]; then echo; read -r -p "Finished. Press Enter to close this window " _; fi
}
trap finish EXIT

[ -x "$PY" ] || { echo "The agent is not installed here yet. Run the setup line from datavera.io/publish/agent first."; exit 1; }
echo "Datavera Agent - $(date '+%A %d %B, %H:%M')"
echo "Keep this window open. Your Mac must stay awake and online until it says Finished."
caffeinate -dimsw $$ &   # keep the Mac awake while this window runs

if ! port_open 11434; then (brew services start ollama >/dev/null 2>&1 || (ollama serve >/dev/null 2>&1 &)); wait_port 11434 30 "the AI model server (Ollama)" || exit 1; fi
if ! port_open 8931; then
  npx -y @playwright/mcp@latest --browser chrome --isolated --port 8931 --host 127.0.0.1 >/dev/null 2>&1 &
  STARTED=$!
  wait_port 8931 120 "the browser helper" || exit 1
fi

if [ "$CHECK" = 1 ]; then
  "$PY" agent.py doctor --browser
  exit $?
fi
"$PY" -u agent.py run "${ARGS[@]}"
CODE=$?
LATEST="$(ls -1d outputs/runs/*/ 2>/dev/null | sort | tail -1)"
if [ -n "$LATEST" ]; then
  echo; echo "------------------------------ Result ------------------------------"
  cat "$LATEST/SUMMARY.md" 2>/dev/null
  if [ -f "$LATEST/draft.json" ]; then
    URL="$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1])).get('edit_url',''))" "$LATEST/draft.json")"
    if [ -n "$URL" ] && [ "$PAUSE" = 1 ]; then
      read -r -p "Type O and press Enter to open your dataset on datavera.io (or just press Enter) " ans
      case "$ans" in [Oo]*) open "$URL" ;; esac
    fi
  elif [ "$CODE" != 0 ]; then
    echo "No dataset this time - that's normal for some subjects. Just run it again later; it will try a different one."
  fi
fi
