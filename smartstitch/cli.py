from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 SmartStitch 本地服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机可访问")
    parser.add_argument("--port", type=int, default=8766, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发时自动重载")
    parser.add_argument("--bootstrap-admin", action="store_true", help="首次设置管理员后退出")
    parser.add_argument("--allow-short-bootstrap-password", action="store_true", help="仅首次管理员初始化时允许不足 6 位的密码")
    parser.add_argument("--admin-username", help="首次管理员登录名")
    parser.add_argument("--admin-name", help="首次管理员显示名称")
    parser.add_argument(
        "--config-directory",
        help=(
            "共享配置目录；默认自动识别 /Volumes/home/Smartstitch "
            "及 /Volumes/homes/<用户>/Smartstitch"
        ),
    )
    args = parser.parse_args()
    if args.config_directory:
        os.environ["SMARTSTITCH_CONFIG_DIRECTORY"] = args.config_directory
    if args.bootstrap_admin:
        from .accounts import AccountStore
        from .api import resolve_config_directory
        from .runtime import resource_root

        directory = resolve_config_directory(resource_root(), allow_shared_default=True)
        username = args.admin_username or input("管理员用户名: ").strip()
        display_name = args.admin_name or input("管理员显示名称: ").strip()
        prompt = "管理员密码: " if args.allow_short_bootstrap_password else "管理员密码（至少 6 位）: "
        password = getpass.getpass(prompt)
        confirm = getpass.getpass("确认密码: ")
        if password != confirm:
            parser.error("两次密码不一致")
        result = AccountStore(Path(directory)).bootstrap(
            username, display_name, password,
            allow_short_password=args.allow_short_bootstrap_password,
        )
        print(f"管理员 {result['username']} 已创建")
        return
    uvicorn.run(
        "smartstitch.api:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=True,
    )


if __name__ == "__main__":
    main()
