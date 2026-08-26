#!/usr/bin/env bash
# 一次性环境准备：建虚拟环境 + 安装依赖 + 保存账号密码
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "创建虚拟环境 .venv ..."
  python3 -m venv .venv
fi

echo "安装依赖 ..."
./.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

echo
echo "保存 EJU 账号密码（macOS 会存进钥匙串）..."
./.venv/bin/python eju_getter.py setup "$@"

echo
echo "完成。以后抓成绩只需要："
echo "  ./eju.sh fetch"
