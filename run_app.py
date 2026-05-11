from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen

import uvicorn


APP_NAME = "Paper PPT Agent"
HOST = "127.0.0.1"
PORT = 8000


def _resource_root() -> Path:
    if getattr(sys, "frozen", False):
        bundle_dir = getattr(sys, "_MEIPASS", None)
        if bundle_dir:
            return Path(bundle_dir).resolve()
    return Path(__file__).resolve().parent


def _data_root() -> Path:
    override = os.getenv("PAPER_PPT_AGENT_DATA_DIR")
    if override:
        return Path(override).resolve()
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data).resolve() / "PaperPPTAgent"
    return Path.home() / "AppData" / "Local" / "PaperPPTAgent"


def _wait_and_open_browser(url: str) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urlopen(f"{url}/healthz", timeout=1):
                webbrowser.open(url)
                return
        except Exception:
            time.sleep(0.5)


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def main() -> None:
    resource_root = _resource_root()
    data_root = _data_root()
    data_root.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("PAPER_PPT_AGENT_PROJECT_ROOT", str(resource_root))
    os.environ.setdefault("PAPER_PPT_AGENT_DATA_DIR", str(data_root))

    url = f"http://{HOST}:{PORT}"
    if _port_in_use(HOST, PORT):
        print(f"{APP_NAME} is already running: {url}")
        webbrowser.open(url)
        return

    print(f"Starting {APP_NAME}...")
    print(f"Resource directory: {resource_root}")
    print(f"User data directory: {data_root}")
    print(f"Open in browser: {url}")

    browser_thread = threading.Thread(
        target=_wait_and_open_browser,
        args=(url,),
        daemon=True,
    )
    browser_thread.start()

    config = uvicorn.Config(
        "backend.app:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )
    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    main()
