"""One-shot launcher: FastAPI receptionist server + ngrok tunnel.

Starts `server.py` and `ngrok http <port>` as child processes, waits for
ngrok to establish its tunnel, prints the public URL you paste into the
Twilio "A call comes in" webhook, and cleanly shuts both children down
when you Ctrl+C.

Usage:
    .venv\\Scripts\\python.exe run.py

Or via the shortcut on Windows:
    run

Requires ngrok on PATH (https://ngrok.com/download). On the first run,
you'll also need to authenticate ngrok once with `ngrok config add-authtoken`.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).parent
PORT = int(os.getenv("PORT", "7860"))
NGROK_API = "http://127.0.0.1:4040/api/tunnels"
NGROK_STARTUP_TIMEOUT = 20  # seconds we wait for the public URL to appear

# On Windows 10+ this ANSI escape enables VT processing in the parent console,
# which lets our colored prefixes render instead of showing as literal text.
if sys.platform == "win32":
    os.system("")

CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"


processes: list[subprocess.Popen] = []
shutting_down = False


def _find_ngrok() -> str | None:
    """Locate the ngrok binary. Checks PATH first, then a few common Windows
    install locations so users who installed via winget/choco/scoop still work
    without editing PATH.
    """
    on_path = shutil.which("ngrok")
    if on_path:
        return on_path

    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get("ProgramFiles", "")) / "ngrok" / "ngrok.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "ngrok" / "ngrok.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft" / "WinGet" / "Links" / "ngrok.exe",
        ]
        for c in candidates:
            if c.is_file():
                return str(c)

    return None


def _spawn(name: str, argv: list[str], **kwargs) -> subprocess.Popen:
    """Start a child process, tagging it so cleanup() knows about it."""
    # New process group on Windows lets us send CTRL_BREAK_EVENT for a graceful
    # shutdown separate from the Ctrl+C the parent already received.
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)
    proc = subprocess.Popen(argv, cwd=ROOT, **kwargs)
    proc._label = name  # type: ignore[attr-defined]
    processes.append(proc)
    return proc


def _terminate(proc: subprocess.Popen, timeout: float = 4.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        # Best-effort cleanup — swallow anything that stops us from killing
        # remaining processes on shutdown.
        try:
            proc.kill()
        except Exception:
            pass


def cleanup() -> None:
    global shutting_down
    shutting_down = True
    # Stop ngrok first so it can close its tunnel gracefully.
    for proc in reversed(processes):
        _terminate(proc)


atexit.register(cleanup)


def _fetch_public_url() -> str | None:
    """Poll ngrok's local API until it reports an https tunnel or we time out."""
    deadline = time.time() + NGROK_STARTUP_TIMEOUT
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urlopen(NGROK_API, timeout=1) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for tunnel in data.get("tunnels", []):
                url = tunnel.get("public_url", "")
                if url.startswith("https://"):
                    return url
        except (URLError, ConnectionError, TimeoutError, OSError) as e:
            last_error = e
        time.sleep(0.5)
    if last_error:
        print(f"{DIM}(ngrok API poll: {last_error}){RESET}")
    return None


def _print_banner(public_url: str | None) -> None:
    print()
    print(f"{BOLD}{GREEN}Receptionist is up.{RESET}")
    if public_url:
        print(f"  Public URL:      {BOLD}{public_url}{RESET}")
        print(f"  Twilio webhook:  {BOLD}{public_url}/voice{RESET}")
        print(f"  Admin console:   {public_url}/admin/")
    else:
        print(f"  {YELLOW}Could not read the ngrok public URL after "
              f"{NGROK_STARTUP_TIMEOUT}s.{RESET}")
        print(f"  Open {BOLD}http://127.0.0.1:4040{RESET} to inspect it manually.")
    print(f"  Local server:    http://127.0.0.1:{PORT}")
    print(f"  Local admin:     http://127.0.0.1:{PORT}/admin/")
    print(f"  ngrok dashboard: http://127.0.0.1:4040")
    print()
    print(f"{DIM}Press Ctrl+C to stop both processes.{RESET}")
    print()


def main() -> int:
    ngrok = _find_ngrok()
    if not ngrok:
        print(f"{RED}error:{RESET} could not find `ngrok` on PATH.")
        print("Install it from https://ngrok.com/download, then run:")
        print("    ngrok config add-authtoken <your-token>")
        return 1

    print(f"{CYAN}[server]{RESET} starting FastAPI on port {PORT}...")
    _spawn("server", [sys.executable, "server.py"])

    print(f"{CYAN}[ngrok]{RESET}  starting tunnel -> :{PORT}...")
    # Redirect ngrok's chatty logs into oblivion; the local dashboard on
    # 127.0.0.1:4040 already exposes everything we'd want to see.
    _spawn(
        "ngrok",
        [ngrok, "http", str(PORT), "--log", "stdout"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    public_url = _fetch_public_url()
    _print_banner(public_url)

    # Supervise both children. If either dies unexpectedly we bring the other
    # one down so the user isn't left with half a system running.
    try:
        while True:
            for proc in processes:
                if proc.poll() is None:
                    continue
                if shutting_down:
                    return 0
                label = getattr(proc, "_label", "child")
                print(
                    f"\n{RED}[{label}] exited unexpectedly "
                    f"(code {proc.returncode}). Shutting down the rest.{RESET}"
                )
                return proc.returncode or 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Stopping...{RESET}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
