#!/usr/bin/env bash
# Выкладка бота на сервер: проверки на Mac → копирование кода → npm ci (если менялись пакеты) → проверки на
# сервере → версия в git на сервере (/root/pools-git, без .env и состояния) → перезапуск службы suibot.
# Не трогает на сервере: .env (ключ), data/private (состояние и журнал), node_modules.
# Адрес сервера — в файле .server (не в git), например: suibot (имя из ~/.ssh/config).
#   ./deploy.sh "что изменилось"              выложить, записать версию и перезапустить
#   ./deploy.sh --no-restart "что изменилось" только выложить (бот работает на старом коде до перезапуска)
set -euo pipefail
cd "$(dirname "$0")"
HOST=${SUIBOT_HOST:-$(cat .server 2>/dev/null || true)}
[ -n "$HOST" ] || { echo "нет адреса сервера: создайте файл .server (например: suibot)"; exit 1; }
DIR=/home/suibot/Pools-
RESTART=1; MSG=""
for a in "$@"; do case "$a" in --no-restart) RESTART=0 ;; *) MSG="$a" ;; esac; done
echo "▸ GitHub: нет ли новых правок (например, с телефона)"
NEW=$(ssh "$HOST" 'cd /root/pools-git 2>/dev/null && git fetch -q origin claude/lp-pools-analysis && git rev-list --count HEAD..origin/claude/lp-pools-analysis || echo 0')
if [ "${NEW:-0}" != 0 ]; then
  echo "❌ на GitHub $NEW новых коммитов — сначала ./sync.sh и проверка, иначе выкладка затрёт эти правки"; exit 1
fi
echo "▸ проверки на Mac"
python3 tests/test_suibot.py | tail -1
python3 tests/test_live.py 2>/dev/null | tail -1
echo "▸ копирование кода на $HOST"
rsync -az --delete \
  --include '.env.example' --exclude '.env' --exclude '.env.*' --exclude '.server' --exclude '.claude/' \
  --exclude 'data/private/' --exclude 'data/.cache/' --exclude 'executor/node_modules/' \
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.DS_Store' \
  ./ "$HOST:$DIR/"
echo "▸ сервер: пакеты, проверки, перезапуск"
ssh "$HOST" "bash -s -- $(printf '%q ' "$DIR" "$RESTART" "$MSG")" <<'REMOTE'   # %q — описание с пробелами целиком
set -euo pipefail
DIR=$1 RESTART=$2 MSG=$3
chown -R suibot:suibot "$DIR"
cd "$DIR/executor"
H=$(sha256sum package-lock.json | cut -d' ' -f1)
if [ ! -d node_modules ] || [ "$(cat node_modules/.lock-hash 2>/dev/null)" != "$H" ]; then
  sudo -u suibot env PATH=/opt/node/bin:/usr/bin:/bin npm ci --no-fund --no-audit --silent
  echo "$H" | sudo -u suibot tee node_modules/.lock-hash >/dev/null
fi
cd "$DIR"
sudo -u suibot python3 tests/test_suibot.py | tail -1
sudo -u suibot env PATH=/opt/node/bin:/usr/bin:/bin python3 tests/test_live.py 2>/dev/null | tail -1
if [ -d /root/pools-git/.git ]; then                   # версия кода — в git (секреты и состояние не попадают)
  rsync -rlpt --delete --exclude '.git/' --include '.env.example' --exclude '.env' --exclude '.env.*' \
    --exclude 'data/private/' --exclude 'data/.cache/' --exclude 'executor/node_modules/' \
    --exclude '__pycache__/' --exclude '*.pyc' "$DIR/" /root/pools-git/
  cd /root/pools-git
  git add -A
  if ! git diff --cached --quiet; then
    git commit -q -m "${MSG:-Обновление бота}" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
    echo "версия: $(git log --oneline -1)"
  fi
  if git push -q origin HEAD:claude/lp-pools-analysis 2>/dev/null; then   # токен — в /root/.git-credentials-pools
    git rev-parse HEAD > "$DIR/data/private/bot/github_seen_sha"            # сторож не пишет о своей же отправке
    chown suibot:suibot "$DIR/data/private/bot/github_seen_sha"
    echo "GitHub: отправлено"
  else
    echo "⚠️ GitHub: отправить не удалось (версия сохранена на сервере, отправится со следующей выкладкой)"
  fi
fi
if [ "$RESTART" = 1 ] && systemctl is-enabled --quiet suibot 2>/dev/null; then
  systemctl restart suibot
  sleep 8
  echo "служба suibot: $(systemctl is-active suibot)"
fi
REMOTE
echo "✅ готово"
