"""Find the game install, the workshop folder, and the user's Zomboid folder.

Everything can be overridden in config.json at the project root:

  {
    "game_dir": "D:/SteamLibrary/steamapps/common/ProjectZomboid",
    "workshop_dir": "D:/SteamLibrary/steamapps/workshop/content/108600",
    "user_dir": "C:/Users/you/Zomboid",
    "game_version": "42.21",
    "mods_dir": "C:/Users/you/Zomboid/mods",
    "port": 18765
  }
"""

import glob
import json
import os
import re

APP_ID = "108600"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# MAPMOVER_DATA moves the layout and backups elsewhere (used for test runs).
DATA_DIR = os.environ.get("MAPMOVER_DATA") or os.path.join(ROOT, "data")


def _steam_root():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return winreg.QueryValueEx(k, "SteamPath")[0]
    except OSError:
        pass
    for guess in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(guess):
            return guess
    return None


def _libraries(steam_root):
    libs = []
    if not steam_root:
        return libs
    for vdf in (os.path.join(steam_root, "steamapps", "libraryfolders.vdf"),
                os.path.join(steam_root, "config", "libraryfolders.vdf")):
        if not os.path.isfile(vdf):
            continue
        with open(vdf, encoding="utf-8", errors="replace") as f:
            text = f.read()
        # Each library block: "path" "X" ... "apps" { "id" "size" ... }
        for block in re.split(r'\n\s*"\d+"\s*\n\s*\{', text)[1:]:
            pm = re.search(r'"path"\s*"([^"]+)"', block)
            if not pm:
                continue
            path = pm.group(1).replace("\\\\", "\\")
            has_pz = re.search(r'"%s"\s*"' % APP_ID, block) is not None
            libs.append((path, has_pz))
        if libs:
            break
    if not libs:
        libs.append((steam_root, True))
    return libs


def _game_version(user_dir):
    """The version the game last ran as, from its own logs."""
    candidates = sorted(glob.glob(os.path.join(user_dir, "Logs", "*DebugLog*.txt")),
                        key=os.path.getmtime, reverse=True)
    candidates += sorted(glob.glob(os.path.join(user_dir, "Logs", "*", "*DebugLog*.txt")),
                         key=os.path.getmtime, reverse=True)
    candidates.append(os.path.join(user_dir, "console.txt"))
    for path in candidates[:20]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                head = f.read(400000)
        except OSError:
            continue
        # "General f:0> version=42.21.0 4a0e9546ec demo=false" (not java.*version=)
        m = re.search(r"[>\s]version=(\d+)\.(\d+)\.\d+ \w+ demo=", head)
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    return None


def load_config():
    cfg = {}
    # MAPMOVER_CONFIG points at another config file (used for test runs).
    path = os.environ.get("MAPMOVER_CONFIG") or os.path.join(ROOT, "config.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)

    if not cfg.get("game_dir") or not cfg.get("workshop_dir"):
        for lib, has_pz in sorted(_libraries(_steam_root()), key=lambda l: not l[1]):
            game = os.path.join(lib, "steamapps", "common", "ProjectZomboid")
            if os.path.isdir(game):
                cfg.setdefault("game_dir", game)
                cfg.setdefault("workshop_dir", os.path.join(lib, "steamapps", "workshop", "content", APP_ID))
                break

    cfg.setdefault("user_dir", os.path.join(os.path.expanduser("~"), "Zomboid"))
    if not cfg.get("game_version"):
        found = _game_version(cfg["user_dir"])
        cfg["game_version"] = found or "42.999"
        cfg["game_version_guessed"] = found is None

    for key in ("game_dir", "workshop_dir", "user_dir"):
        if cfg.get(key):
            cfg[key] = os.path.normpath(cfg[key])
    cfg["mods_dir"] = (os.path.normpath(cfg["mods_dir"]) if cfg.get("mods_dir")
                       else os.path.join(cfg["user_dir"], "mods"))
    return cfg


def version_int(name):
    """Same rule as ZomboidFileSystem.getGameVersionIntFromName."""
    def num(s):
        return int(s) if re.fullmatch(r"-?\d+", s.strip()) else 0

    parts = name.split(".")
    if len(parts) == 1:
        return num(parts[0]) * 1000
    return num(parts[0]) * 1000 + min(num(parts[1]), 999)
