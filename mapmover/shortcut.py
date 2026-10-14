"""The desktop shortcut: py -3 -m mapmover.shortcut

It targets pythonw.exe so no console window sits behind the app, starts in
the project root, and carries mapmover.ico. A .lnk is a COM object, so this
drives WScript.Shell through PowerShell. Running it again
overwrites the old shortcut, including the one that pointed at run.bat.
"""

import os
import subprocess
import sys

from .desktop import ICON, ROOT, TITLE


def pythonw():
    w = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return w if os.path.isfile(w) else sys.executable


def desktop_dir():
    # OneDrive moves the Desktop on many installs and leaves an empty one behind.
    od = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if od and os.path.isdir(os.path.join(od, "Desktop")):
        return os.path.join(od, "Desktop")
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _q(v):
    return "'" + str(v).replace("'", "''") + "'"


def create(directory=None):
    link = os.path.join(directory or desktop_dir(), TITLE + ".lnk")
    icon = f"$s.IconLocation = {_q(ICON + ',0')}" if os.path.isfile(ICON) else ""
    script = f"""
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut({_q(link)})
$s.TargetPath = {_q(pythonw())}
$s.Arguments = '-m mapmover.desktop'
$s.WorkingDirectory = {_q(ROOT)}
$s.Description = 'Move and join Project Zomboid map mods'
{icon}
$s.Save()
"""
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not os.path.isfile(link):
        raise RuntimeError((r.stderr or r.stdout or "PowerShell refused").strip())
    return link


if __name__ == "__main__":
    print("wrote", create(sys.argv[1] if len(sys.argv) > 1 else None))
