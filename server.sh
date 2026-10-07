#!/usr/bin/env bash
# Управление ботом на сервере (адрес — в .server, не в git):
#   ./server.sh status    отчёт бота и состояние службы      ./server.sh logs [N]   последние N строк журнала
#   ./server.sh restart   перезапуск                          ./server.sh stop       остановка (позиция остаётся в пуле)
#   ./server.sh start     запуск                              ./server.sh check      самопроверка (.env, Telegram, кошелёк)
set -euo pipefail
cd "$(dirname "$0")"
HOST=${SUIBOT_HOST:-$(cat .server 2>/dev/null || true)}
[ -n "$HOST" ] || { echo "нет адреса сервера: файл .server"; exit 1; }
RUN='cd /home/suibot/Pools- && sudo -u suibot env PATH=/opt/node/bin:/usr/bin:/bin python3 bot.py'
case "${1:-status}" in
  status)  ssh "$HOST" "systemctl is-active suibot; $RUN status" ;;
  logs)    ssh "$HOST" "journalctl -u suibot --no-pager -o cat -n ${2:-40}" ;;
  restart|stop|start) ssh "$HOST" "systemctl $1 suibot; sleep 3; systemctl is-active suibot || true" ;;
  check)   ssh "$HOST" "$RUN check" ;;
  *) sed -n '2,5p' "$0"; exit 1 ;;
esac
