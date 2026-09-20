from __future__ import annotations

import logging
import os

import uvicorn

from smartstitch.runtime import (
    application_support_root,
    configure_bundled_media_tools,
    resource_root,
)


HOST = "127.0.0.1"


def main() -> None:
    root = resource_root()
    configure_bundled_media_tools(root)
    support_root = application_support_root()
    support_root.mkdir(parents=True, exist_ok=True)
    os.chdir(support_root)

    log_directory = support_root / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_directory / "smartstitch.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Import only after PATH and writable runtime directories are prepared.
    from smartstitch.api import create_app

    app = create_app()
    port = int(os.environ.get("SMARTSTITCH_PORT", "0"))
    headless = os.environ.get("SMARTSTITCH_HEADLESS") == "1"
    # Backward compatibility for existing packaged smoke-test scripts.
    headless = headless or os.environ.get("SMARTSTITCH_NO_BROWSER") == "1"
    if headless:
        uvicorn.run(app, host=HOST, port=port or 8766, log_config=None)
        return

    from smartstitch.desktop import run_desktop_application

    run_desktop_application(app, port=port)


if __name__ == "__main__":
    main()
