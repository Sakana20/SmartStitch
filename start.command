#!/bin/zsh
set -e

cd "${0:A:h}"

if [[ ! -x .venv/bin/python ]]; then
  echo "首次运行：正在创建 Python 虚拟环境…"
  python3 -m venv .venv
fi

if ! .venv/bin/python -c 'import fastapi, pydantic, uvicorn, yaml, smartstitch' 2>/dev/null; then
  echo "首次运行：正在安装 SmartStitch 依赖…"
  .venv/bin/python -m pip install -e .
fi

echo "SmartStitch 将在浏览器地址 http://127.0.0.1:8765 运行"
(sleep 1; open "http://127.0.0.1:8765") &
.venv/bin/smartstitch
