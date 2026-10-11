#!/usr/bin/env bash
# Chạy Vast Monitor trên máy local: tạo venv lần đầu, cài thư viện, mở trình duyệt.
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8765}"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
fi
./.venv/bin/pip install -q -r requirements.txt
( sleep 1.5; (open "http://127.0.0.1:$PORT" 2>/dev/null || xdg-open "http://127.0.0.1:$PORT" 2>/dev/null) ) &
exec ./.venv/bin/uvicorn app:app --host 127.0.0.1 --port "$PORT"
