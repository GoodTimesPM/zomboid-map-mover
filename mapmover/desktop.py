"""The desktop app: a window around the local server.

py -3 -m mapmover.desktop, or the desktop shortcut (pythonw, no console).
The server runs on a thread inside this process, so closing the window stops
everything. Opening the shortcut a second time brings the open window to the
front instead of starting another one.

pywebview shows the page in WebView2 (part of Windows 11). Without pywebview
or WebView2 the page opens in the browser instead and the server keeps
running until you close this process.

With no console, prints and errors go to data/mapmover.log.
"""

import os
import sys
import threading
import time
import traceback
import webbrowser

TITLE = "Zomboid Map Mover"
APP_ID = "GoodTimesPM.ZomboidMapMover"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON = os.path.join(ROOT, "mapmover.ico")


def _log_to_file():
    """pythonw has no stdout; send prints and tracebacks to a log file."""
    from .paths import DATA_DIR
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "mapmover.log")
    try:
        if os.path.getsize(path) > 2_000_000:
            os.replace(path, path + ".1")
    except OSError:
        pass
    f = open(path, "a", encoding="utf-8", buffering=1)
    f.write(f"\n==== started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    sys.stdout = sys.stderr = f


def _set_app_id():
    # Its own taskbar button and icon, not pythonw's.
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def _find_window(title, ours):
    """A visible, unowned top-level window with this title, in this process
    (ours=True) or another one. Windows 11 also makes an invisible copy of the
    title for taskbar thumbnails, which the visible check skips."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    me = os.getpid()
    found = []

    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if (pid.value == me) != ours or not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value == title:
            found.append(hwnd)
            return False
        return True

    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def _raise_open_window():
    try:
        import ctypes
        hwnd = _find_window(TITLE, ours=False)
        if not hwnd:
            return False
        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _set_icon():
    """pywebview can't set the frame icon on Windows, so send WM_SETICON once
    the window exists. A plain icon is better than not opening, so failures
    are ignored."""
    if not os.path.isfile(ICON):
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        for _ in range(60):
            hwnd = _find_window(TITLE, ours=True)
            if hwnd:
                for which, size in ((0, 16), (1, 32)):  # ICON_SMALL, ICON_BIG
                    h = user32.LoadImageW(None, ICON, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
                    if h:
                        user32.SendMessageW(hwnd, 0x80, which, h)  # WM_SETICON
                return
            time.sleep(0.25)
    except Exception:
        pass


def _hold():
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def main():
    windowed = sys.stdout is None or os.path.basename(sys.executable).lower() == "pythonw.exe"
    if windowed:
        _log_to_file()
    _set_app_id()
    if _raise_open_window():
        print("Already open, brought that window to the front.")
        return 0

    from . import server
    url, ours = server.start()
    if url is None:
        print(ours)
        return 1
    print(f"Map mover at {url}" + ("" if ours else " (already running in another process)"))

    try:
        import webview
    except ImportError:
        print("pywebview isn't installed (py -3 -m pip install pywebview), opening the browser instead.")
        webbrowser.open(url)
        return _hold() if ours else 0

    win = webview.create_window(TITLE, url, width=1500, height=950, min_size=(1000, 650),
                                background_color="#12151c", text_select=True)

    def closing():
        # Closing mid-Apply would stop it halfway. Ask first.
        if ours and server.job_running():
            return win.create_confirmation_dialog(
                "Apply is still running",
                "Closing now stops the Apply halfway. Your mod list stays as it was and the next "
                "Apply starts over. Close anyway?")
        return True

    win.events.closing += closing
    threading.Thread(target=_set_icon, daemon=True).start()
    try:
        # Keep the page's saved view and unsaved-change backup between runs.
        from .paths import DATA_DIR
        webview.start(private_mode=False, storage_path=os.path.join(DATA_DIR, "webview"))
    except Exception:
        print("Couldn't open a window, opening the browser instead:\n" + traceback.format_exc())
        webbrowser.open(url)
        return _hold() if ours else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
