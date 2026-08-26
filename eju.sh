#!/usr/bin/env bash
# 便捷入口：自动用 .venv 里的 python 运行 eju_getter.py
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "还没准备好环境，先运行 ./setup.sh" >&2
  exit 1
fi
exec ./.venv/bin/python eju_getter.py "$@"
