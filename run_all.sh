#!/usr/bin/env bash
# Полный перезапуск: сбор → анализ → HTML-отчёт.  Нужна переменная KRYSTAL_CLOUD_KEY.
set -euo pipefail
cd "$(dirname "$0")"
python3 collect.py "$@"
python3 analyze.py
python3 build_report.py
echo "Готово: report/index.html, data/processed/pools.csv, data/processed/backtests.csv"
