import os
import signal
import sys
import ctypes
from pathlib import Path

import nonebot

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        return False


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / ".env.bot.secret", override=True)

# Import registers the matcher and exposes the ASGI app for runtime.
from bot_private_qq.app import app  # noqa: F401


def _disable_console_interrupts():
    # Prevent accidental Ctrl+C / Ctrl+Break from stopping bot process.
    allow_ctrl_c = str(os.getenv("ALLOW_CTRL_C", "")).strip().lower() in {"1", "true", "yes", "on"}
    if allow_ctrl_c:
        return
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        pass
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        try:
            signal.signal(sigbreak, signal.SIG_IGN)
        except Exception:
            pass
    if os.name == "nt":
        try:
            # Ignore console Ctrl events at process level (Ctrl+C / Ctrl+Break / close events).
            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)
        except Exception:
            pass


if __name__ == "__main__":
    _disable_console_interrupts()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    nonebot.run(host=host, port=port)
