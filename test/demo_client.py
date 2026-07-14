#!/usr/bin/env python3
"""

Updated (2025.12.16)

Simple scheduler client demo:
  - Send HTTP requests to the scheduler
  - Test /v1/chat/completions and /v1/completions
  - Show the full application-layer payload (HTTP headers + JSON body)
  - Two modes: python --with-ui and the default CLI mode
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _ensure_project_root_on_syspath() -> None:
    """
    Fallback: allow running demo_client.py directly from the test/ directory,
    while still importing packages such as client/ and UI/ from the project root.
    """
    root = Path(__file__).resolve().parents[1]  # test/ -> project root
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def run_cli() -> None:
    """
    CLI mode: directly reuse main() from client.py.
    """
    _ensure_project_root_on_syspath()

    # If your client module is client/client.py, this import is recommended
    # - If client/__init__.py already does from .client import *, direct import client also works
    try:
        from client.client import run_repl as client_entry  # client/ directory + client.py
    except Exception:
        # Fallback: if client.py is placed at the repository root as a non-package module
        from client import run_repl as client_entry  # type: ignore

    print("[demo_client] entering REPL... (if you do not see a prompt, press Enter once)", flush=True)
    client_entry()


def run_ui(host: str, port: int, scheduler_url: str) -> None:
    """
    UI mode: start the UI FastAPI app and show the access URL.
    """
    _ensure_project_root_on_syspath()

    # UI factory function; app.py under your UI directory must provide create_client_ui_app
    from UI.client_ui.app import create_client_ui_app  # type: ignore

    import uvicorn

    app = create_client_ui_app(default_scheduler_url=scheduler_url)

    ui_url = f"http://{host}:{port}/ui/client"
    print("\n" + "=" * 72)
    print("[Client UI] started")
    print(f"[Client UI] Access URL: {ui_url}")
    print("=" * 72 + "\n")

    uvicorn.run(app, host=host, port=port, reload=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CacheRoute Client demo: CLI by default; add --with-ui to start browser UI."
    )
    parser.add_argument(
        "--with-ui",
        action="store_true",
        help="Start the browser UI (FastAPI + Tailwind) instead of CLI mode",
    )
    parser.add_argument("--ui-host", default="127.0.0.1", help="UI listen address")
    parser.add_argument("--ui-port", type=int, default=7071, help="UI listen port")
    parser.add_argument(
        "--scheduler-url",
        default="http://127.0.0.1:7001/v1/chat/completions",
        help="Default Scheduler URL filled by the UI",
    )

    args = parser.parse_args()

    if args.with_ui:
        run_ui(args.ui_host, args.ui_port, args.scheduler_url)
    else:
        run_cli()


if __name__ == "__main__":
    main()
