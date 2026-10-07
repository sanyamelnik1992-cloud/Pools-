#!/usr/bin/env bash
# Забрать изменения с GitHub (например, сделанные с телефона) для проверки: сервер скачивает ветку, показывает
# новые коммиты и что в них изменилось, переносит их в git-копию, затем код копируется на Mac. Боевой бот работает
# на старом коде, пока Claude не проверит изменения и не выложит их: ./deploy.sh "что изменилось".
set -euo pipefail
cd "$(dirname "$0")"
HOST=${SUIBOT_HOST:-$(cat .server 2>/dev/null || true)}
[ -n "$HOST" ] || { echo "нет адреса сервера: файл .server"; exit 1; }
BR=claude/lp-pools-analysis
ssh "$HOST" "bash -s -- $BR" <<'REMOTE'
set -euo pipefail
BR=$1
cd /root/pools-git
git fetch -q origin "$BR"
NEW=$(git rev-list --count "HEAD..origin/$BR")
OURS=$(git rev-list --count "origin/$BR..HEAD")
echo "на GitHub новых коммитов: $NEW; наших, ещё не отправленных на GitHub: $OURS"
[ "$NEW" -gt 0 ] || exit 0
git log --format='  %h %ad %s' --date=format:'%d.%m %H:%M' "HEAD..origin/$BR"
git diff --stat HEAD "origin/$BR" | tail -20
if [ "$OURS" -gt 0 ]; then
  echo "❌ на GitHub и у нас разные новые версии — нужно слить вручную (Claude разберёт)"; exit 2
fi
git merge -q --ff-only "origin/$BR" && echo "✅ изменения с GitHub перенесены в копию на сервере"
REMOTE
rsync -rlpt --delete --exclude '.git/' --include '.env.example' --exclude '.env' --exclude '.env.*' \
  --exclude '.server' --exclude '.claude/' --exclude 'data/private/' --exclude 'data/.cache/' \
  --exclude 'executor/node_modules/' --exclude '__pycache__/' --exclude '*.pyc' --exclude '.DS_Store' \
  "$HOST:/root/pools-git/" ./
echo "код на Mac обновлён — дальше проверка и ./deploy.sh"
