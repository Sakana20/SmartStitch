from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from smartstitch.runtime import (
    application_support_root,
    configure_bundled_media_tools,
    resource_root,
)


HOST = "127.0.0.1"
PORT = int(os.environ.get("SMARTSTITCH_PORT", "8766"))
URL = f"http://{HOST}:{PORT}"


def _open_browser_when_ready() -> None:
    health_url = f"{URL}/api/v1/system/health"
    for _ in range(120):
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as response:
                if response.status == 200:
                    webbrowser.open(URL)
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)


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
    from smartstitch.api import app

    if os.environ.get("SMARTSTITCH_NO_BROWSER") != "1":
        threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, log_config=None)


if __name__ == "__main__":
    main()
