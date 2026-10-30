"""Open the dashboard in its own app-style window (no address bar, tabs or bookmarks) when a browser can do that.

Chrome, Edge and Brave all accept --app=URL. If none is installed, or the window can't be started, the
dashboard opens in the default browser as a normal tab. --no-app-window (or EVBUDGET_NO_APP_WINDOW=1) skips
the app window, for troubleshooting.
"""

import ntpath
import os
import posixpath
import shutil
import subprocess
import sys
import webbrowser
from typing import Callable, List, Mapping, Optional, Tuple

WINDOW_SIZE = "1620,1400"
DISABLE_ENV = "EVBUDGET_NO_APP_WINDOW"

Browser = Tuple[str, str]  # (name, path to the program)


def _windows_candidates(env: Mapping[str, str]) -> List[Browser]:
    roots = [env[k] for k in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData") if env.get(k)]
    programs = [
        ("Chrome", r"Google\Chrome\Application\chrome.exe"),
        ("Edge", r"Microsoft\Edge\Application\msedge.exe"),
        ("Brave", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    return [(name, ntpath.join(root, rel)) for name, rel in programs for root in roots]


def _mac_candidates(env: Mapping[str, str]) -> List[Browser]:
    apps = [
        ("Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("Edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ("Brave", "Brave Browser.app/Contents/MacOS/Brave Browser"),
    ]
    homes = ["/Applications", posixpath.join(env.get("HOME", ""), "Applications")]
    return [(name, posixpath.join(home, rel)) for name, rel in apps for home in homes if home]


def find_app_browser(
    platform: str = sys.platform,
    env: Mapping[str, str] = os.environ,
    exists: Callable[[str], bool] = os.path.isfile,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Optional[Browser]:
    """The first installed browser that can run a page as an app window, or None."""
    if platform == "win32":
        candidates = _windows_candidates(env)
    elif platform == "darwin":
        candidates = _mac_candidates(env)
    else:
        names = [("Chrome", "google-chrome"), ("Chrome", "google-chrome-stable"), ("Chromium", "chromium"),
                 ("Chromium", "chromium-browser"), ("Edge", "microsoft-edge"), ("Brave", "brave-browser")]
        candidates = [(name, which(cmd) or "") for name, cmd in names]
    return next(((name, path) for name, path in candidates if path and exists(path)), None)


def app_window_disabled(argv: List[str], env: Mapping[str, str] = os.environ) -> bool:
    return "--no-app-window" in argv or env.get(DISABLE_ENV, "") not in ("", "0")


def launch(url: str, browser: Optional[Browser]) -> None:
    """Open url in an app window using `browser`, or in the default browser if there is none or it fails."""
    if browser is not None:
        try:
            subprocess.Popen(
                [browser[1], f"--app={url}", f"--window-size={WINDOW_SIZE}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            pass
    webbrowser.open(url)
