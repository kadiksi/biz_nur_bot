#!/bin/bash
# Сайт + главный бот как служба macOS (launchd): стартует при входе в систему,
# перезапускается после сбоя, не даёт Mac засыпать от простоя.
#
#   scripts/service.sh install    — установить и запустить
#   scripts/service.sh restart    — перезапустить (после обновления кода или .env)
#   scripts/service.sh status     — работает ли
#   scripts/service.sh logs       — последние строки лога (logs -f — следить)
#   scripts/service.sh uninstall  — остановить и удалить службу
set -euo pipefail

LABEL="com.cornergroup.ops"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/corner-ops"
LOG="$LOG_DIR/run.log"
DOMAIN="gui/$(id -u)"

xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
  local p py l
  p="$(xml_escape "$PROJECT")"; py="$(xml_escape "$PYTHON")"; l="$(xml_escape "$LOG")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <!-- caffeinate: Mac не засыпает от простоя, пока служба работает -->
    <string>/usr/bin/caffeinate</string><string>-i</string><string>-s</string>
    <string>$py</string><string>-m</string><string>app.run</string>
  </array>
  <key>WorkingDirectory</key><string>$p</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$l</string>
  <key>StandardErrorPath</key><string>$l</string>
</dict>
</plist>
EOF
  plutil -lint "$PLIST" >/dev/null
}

is_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

status() {
  if ! is_loaded; then echo "Служба не установлена."; return 1; fi
  local pid
  pid="$(launchctl print "$DOMAIN/$LABEL" | awk '/^\tpid = /{print $3}')"
  if [[ -n "$pid" ]]; then echo "Работает (pid $pid)."; else echo "Установлена, но сейчас не запущена — смотрите: $0 logs"; fi
  if curl -s -m 3 -o /dev/null "http://127.0.0.1:${SITE_PORT:-8000}/healthz"; then
    echo "Сайт отвечает."
  else
    echo "Сайт пока не отвечает."
  fi
  grep -E "Сайт: http" "$LOG" 2>/dev/null | tail -1 | sed -E 's/.*(Сайт: [^ ]+).*/\1/' || true
}

case "${1:-}" in
  install)
    [[ -x "$PYTHON" ]] || { echo "Нет $PYTHON — сначала создайте .venv (см. README)"; exit 1; }
    is_loaded && launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && sleep 2 || true
    if pgrep -f "[Pp]ython -m app\.(run|web|bots\.main_bot)" >/dev/null; then
      echo "Бот или сайт уже запущены вручную в терминале — остановите их (Ctrl+C) и повторите."; exit 1
    fi
    write_plist
    launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl enable "$DOMAIN/$LABEL"
    echo "Служба установлена: $PLIST"
    sleep 6; status || true
    ;;
  restart)
    is_loaded || { echo "Служба не установлена: $0 install"; exit 1; }
    echo "Перезапускаю…"
    # сначала полностью остановить: иначе новый процесс может не успеть занять порт сайта
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    for _ in $(seq 1 30); do
      if ! is_loaded && ! lsof -nP -iTCP:"${SITE_PORT:-8000}" -sTCP:LISTEN >/dev/null 2>&1; then break; fi
      sleep 1
    done
    # launchd иногда ещё не готов принять службу обратно — пробуем несколько раз
    for _ in 1 2 3 4 5; do
      launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && break
      sleep 2
    done
    is_loaded || { echo "Не удалось запустить службу — попробуйте: $0 install"; exit 1; }
    for _ in $(seq 1 30); do
      curl -s -m 2 -o /dev/null "http://127.0.0.1:${SITE_PORT:-8000}/healthz" && break
      sleep 1
    done
    status || true
    ;;
  status) status ;;
  logs)
    if [[ "${2:-}" == "-f" ]]; then tail -n 50 -f "$LOG"; else tail -n 50 "$LOG"; fi
    ;;
  uninstall)
    is_loaded && launchctl bootout "$DOMAIN/$LABEL" || true
    rm -f "$PLIST"
    echo "Служба остановлена и удалена. Лог остался в $LOG"
    ;;
  *)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
