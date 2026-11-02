"""A native "choose a folder" window, opened on the computer that runs EVBudget.

A web page can't learn the full path of a folder, so the server opens the operating system's own chooser.
It runs in a separate process and the page polls for the answer, because the server handles one request at a
time and must stay responsive while the window is open.
"""

import subprocess
import sys
from typing import Any, Dict, List, Optional

# Windows and Linux: Python's own chooser (tkinter ships with the python.org installers)
TK_SCRIPT = """
import sys
try:
    import tkinter
    from tkinter import filedialog
except Exception:
    sys.exit(3)
root = tkinter.Tk()
root.withdraw()
try:
    root.attributes('-topmost', True)
except Exception:
    pass
print(filedialog.askdirectory(initialdir=sys.argv[1] or None, title='Choose the import folder', mustexist=True) or '')
"""

MAC_SCRIPT = 'POSIX path of (choose folder with prompt "Choose the import folder")'


class FolderPicker:
    def __init__(self) -> None:
        self._proc: Optional["subprocess.Popen[str]"] = None
        self._mac = False

    def command(self, initial_dir: str) -> List[str]:
        if sys.platform == "darwin":
            return ["osascript", "-e", MAC_SCRIPT]
        return [sys.executable, "-c", TK_SCRIPT, initial_dir or ""]

    def start(self, initial_dir: str = "") -> None:
        """Open the chooser. If one is already open, leave it be."""
        if self._proc is not None and self._proc.poll() is None:
            return
        self._mac = sys.platform == "darwin"
        try:
            self._proc = subprocess.Popen(
                self.command(initial_dir), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            self._proc = None
            raise

    def status(self) -> Dict[str, Any]:
        """idle | waiting | done (with path) | cancelled | unavailable | error"""
        if self._proc is None:
            return {"state": "idle"}
        if self._proc.poll() is None:
            return {"state": "waiting"}
        out, err = self._proc.communicate()
        code, self._proc = self._proc.returncode, None
        out = (out or "").strip()
        if code == 3:
            return {"state": "unavailable"}
        if self._mac and code != 0:
            return {"state": "cancelled"} if "cancel" in (err or "").lower() else {"state": "error", "message": (err or "").strip()[:200]}
        if code != 0:
            return {"state": "error", "message": (err or "").strip()[:200]}
        if not out:
            return {"state": "cancelled"}
        return {"state": "done", "path": out.rstrip("/\\") if len(out) > 3 else out}
