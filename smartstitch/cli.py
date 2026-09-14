from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 SmartStitch 本地服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机可访问")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发时自动重载")
    args = parser.parse_args()
    uvicorn.run("smartstitch.api:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
