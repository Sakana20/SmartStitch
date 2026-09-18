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

if [[ -z "${SMARTSTITCH_CONFIG_DIRECTORY:-}" && ! -d /Volumes/home/Smartstitch ]]; then
  alternate_nas_directories=(/Volumes/homes/*/Smartstitch(N/))
  if (( ${#alternate_nas_directories[@]} != 1 )); then
    echo ""
    echo "无法启动 SmartStitch：未连接 NAS。"
    echo "请先在 Finder 中连接 NAS，并确认 /Volumes/home/Smartstitch 已挂载，然后重新启动。"
    echo ""
    read "?按回车键关闭…"
    exit 1
  fi
fi

echo "SmartStitch 将在浏览器地址 http://127.0.0.1:8766 运行"
(sleep 1; open "http://127.0.0.1:8766") &
.venv/bin/smartstitch
