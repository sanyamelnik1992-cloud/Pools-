#!/usr/bin/env bash
# Сторож GitHub (служба suibot-gitwatch, раз в 15 мин, от пользователя suibot): если ветка на GitHub изменилась
# (например, правки с телефона), пишет в Telegram. Сам ничего не выкладывает: изменения сначала проверяет Claude
# (./sync.sh → проверка → ./deploy.sh), боевой бот до этого работает на прежнем коде.
set -euo pipefail
cd "$(dirname "$0")"
REPO=https://github.com/sanyamelnik1992-cloud/Pools-.git
BR=claude/lp-pools-analysis
SEEN=data/private/bot/github_seen_sha
SHA=$(git ls-remote "$REPO" "refs/heads/$BR" | cut -f1)
[ -n "$SHA" ] || exit 0
if [ ! -f "$SEEN" ]; then echo "$SHA" > "$SEEN"; exit 0; fi       # первый запуск — только запомнить
[ "$SHA" = "$(cat "$SEEN")" ] && exit 0
echo "$SHA" > "$SEEN"
python3 - "$SHA" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, ".")
from suibot import env, notify
env.load(Path(".env"))
notify.send(f"🔔 <b>На GitHub новые изменения</b>\nветка claude/lp-pools-analysis, версия {sys.argv[1][:7]}\n"
            "Напишите Claude «проверь гит» — он проверит изменения и выложит их на сервер.\n"
            "🧠 <i>почему: боевой бот работает с реальными деньгами, поэтому чужой код сам не ставится — "
            "до проверки работает прежняя версия</i>", html=True)
PY
