"""Load the world in a hidden dedicated server to find crashes before the
player does.

The server reads the same mod list and maps as default.txt and loads a new
world the same way the game does, including the script check and the map
stack. It runs from its own cache folder, so the real saves, settings and
Zomboid folder stay untouched.

The game can't read mods through junctions: its file lookups resolve the
link and then miss. So the cache folder holds a real mirror of each mod.
It sits on the workshop's drive, where every workshop file is a hard link
(no extra space, and nothing ever writes into one: a changed file is
unlinked and linked again). Local mods on other drives are copied. Later
runs only touch files whose size or time changed.

The server opens its network port only after the world loads, and -ip binds
it to 127.0.0.1, so Windows shows no firewall prompt.

Run: python -m mapmover.testload
"""
import os
import re
import shutil
import subprocess
import sys
import time

from .loadorder import stack
from .paths import load_config
from .scan import read_modlist, scan_mods

NAME = "mapmover_test"
PORT = 18793
TIMEOUT = 30 * 60
STARTED = "*** SERVER STARTED ***"
STOPPED = "Server Terminated."
# Lines worth showing when the load fails: script errors and crashes.
LOGLINE = re.compile(r"(LOG|WARN|ERROR|DEBUG) *:")
BAD = re.compile(r"Exception thrown|IOException|could not proceed|Fluid not found|Duplicate Prop")


def cache_root(cfg):
    """config.json "testload_dir", or a folder at the top of the workshop's drive."""
    if cfg.get("testload_dir"):
        return os.path.normpath(cfg["testload_dir"])
    drive = os.path.splitdrive(cfg["workshop_dir"])[0]
    return os.path.join(drive + os.sep, "ZomboidMapMoverTest")


def _same_drive(a, b):
    return os.path.splitdrive(a)[0].lower() == os.path.splitdrive(b)[0].lower()


def _mirror(src, dst, link):
    """Make dst hold the same files as src. Returns how many files changed."""
    changed = 0
    want = set()
    for dp, dirs, files in os.walk(src):
        rel = os.path.relpath(dp, src)
        out = os.path.normpath(os.path.join(dst, rel))
        os.makedirs(out, exist_ok=True)
        want.add(os.path.normcase(out))
        for f in files:
            a, b = os.path.join(dp, f), os.path.join(out, f)
            want.add(os.path.normcase(b))
            sa = os.stat(a)
            try:
                sb = os.stat(b)
                if sb.st_size == sa.st_size and int(sb.st_mtime) == int(sa.st_mtime):
                    continue
                os.remove(b)  # unlink only: the source keeps its data
            except FileNotFoundError:
                pass
            if link:
                os.link(a, b)
            else:
                shutil.copy2(a, b)
            changed += 1
    for dp, dirs, files in os.walk(dst, topdown=False):
        for f in files:
            b = os.path.join(dp, f)
            if os.path.normcase(b) not in want:
                os.remove(b)
                changed += 1
        if os.path.normcase(dp) not in want and not os.listdir(dp):
            os.rmdir(dp)
    return changed


def server_maps(by_id, ids, maps):
    """The server's Map= for default.txt's mod list and maps block.

    The game's new world screen passes "DEFAULT", and MapGroups splits the
    maps into worlds and loads only Kentucky's, with vanilla under the mod
    maps. A server takes Map= as is: every listed map goes into one world.
    Maps of another world (Prince Edward Island, Moscow) then sit on
    Kentucky's cells, and without vanilla every cell that leans on it has
    no lot header. Either way the load crashes where the game's doesn't.
    loadorder.stack is that MapGroups order."""
    state = {"groups": [], "originals": {}, "by_id": by_id,
             "vanilla_cells": {None}}  # any non-empty set keeps vanilla in the list
    folders, _ = stack(state, {}, ids, maps)
    return [name for name, _ in folders]


def prepare(cfg, root, log=print):
    """Mirror the active mods into root/mods and write the server's ini.
    Returns the mod ids default.txt names that no installed folder provides."""
    ids, maps = read_modlist(os.path.join(cfg["mods_dir"], "default.txt"))
    by_id = {}
    for m in scan_mods(cfg):
        by_id.setdefault(m.id, m)  # workshop first, then local, like the game
    missing = [i for i in ids if i not in by_id]
    folders = list(dict.fromkeys(by_id[i].folder for i in ids if i in by_id))

    mods = os.path.join(root, "mods")
    os.makedirs(mods, exist_ok=True)
    names = {}
    for folder in folders:
        name = re.sub(r"[^A-Za-z0-9_.&' -]", "_", os.path.basename(folder))
        if name.lower() in {n.lower() for n in names.values()}:
            name += "_" + str(len(names))
        names[folder] = name
    for old in os.listdir(mods):
        p = os.path.join(mods, old)
        if not os.path.isdir(p):
            os.remove(p)  # the server's loaded.txt
        elif old not in names.values():
            shutil.rmtree(p)  # mirrors only: links and copies
    changed = 0
    for folder, name in names.items():
        changed += _mirror(folder, os.path.join(mods, name), link=_same_drive(folder, root))
    log(f"Mirrored {len(folders)} mods ({changed} files changed)")

    # A new world every run.
    for sub in ("Saves", "db", "Server", "Logs", "Lua"):
        shutil.rmtree(os.path.join(root, sub), ignore_errors=True)
    os.makedirs(os.path.join(root, "Server"))
    found = [i for i in ids if i in by_id]
    ini = "\n".join([
        "Mods=" + ";".join(found),
        "Map=" + ";".join(server_maps(by_id, ids, maps)),
        "WorkshopItems=",
        "Public=false",
        "Open=true",
        "UPnP=false",
        f"DefaultPort={PORT}",
        f"UDPPort={PORT + 1}",
        "PauseEmpty=true",
        "",
    ])
    with open(os.path.join(root, "Server", NAME + ".ini"), "w", encoding="utf-8") as f:
        f.write(ini)
    return missing


def run(cfg, root, log=print, timeout=TIMEOUT, stop=None):
    """Start the server, wait for the world to load or fail, stop it.
    Returns (ok, lines): lines are the log's error lines, or the reason it
    stopped. stop: a threading.Event that ends the run early."""
    game = cfg["game_dir"]
    java = os.path.join(game, "jre64", "bin", "java.exe")
    args = [java, "--enable-native-access=ALL-UNNAMED", "--add-exports=java.base/jdk.internal.misc=ALL-UNNAMED",
            "-XX:+UseZGC", "-XX:-CreateCoredumpOnCrash", "-XX:-OmitStackTraceInFastThrow", "-Xmx8192m",
            "-Djava.awt.headless=true", "-Djava.library.path=./natives/;./natives/win64/;./",
            "-cp", "./;projectzomboid.jar", "zombie.network.GameServer",
            f"-cachedir={root}", "-nosteam", "-servername", NAME, "-adminpassword", "mapmover",
            "-ip", "127.0.0.1", "-port", str(PORT), "-udpport", str(PORT + 1)]
    console = os.path.join(root, "server-console.txt")
    out = open(os.path.join(root, "stdout.txt"), "wb")
    proc = subprocess.Popen(args, cwd=game, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    log(f"Server started (pid {proc.pid}), loading the world...")
    t0 = time.time()
    ok, why = None, ""
    try:
        while ok is None:
            time.sleep(3)
            text = _read(console)
            if STARTED in text:
                ok = True
            elif STOPPED in text:
                ok, why = False, "the server stopped while loading"
            elif proc.poll() is not None:
                ok, why = False, f"the server quit (exit code {proc.returncode})"
            elif time.time() - t0 > timeout:
                ok, why = False, f"no result after {timeout // 60} minutes"
            elif stop is not None and stop.is_set():
                ok, why = False, "stopped before the world finished loading"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        out.close()
    log(f"Finished in {time.time() - t0:.0f} s")
    # server-console.txt drops its start once it grows; stdout has it all.
    return ok, ([why] if why else []) + errors(_read(os.path.join(root, "stdout.txt")) or _read(console))


def errors(text):
    """Distinct error lines, each "Exception thrown" with the exception after it."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        # Log lines only, not stack frames. The server walks every mod's
        # media/AnimSets and media/actiongroups and logs each one missing.
        if not LOGLINE.match(line) or not BAD.search(line) or "visitFileFailed" in line:
            continue
        if line.rstrip().endswith("Exception thrown") and i + 1 < len(lines):
            line = line.rstrip() + ": " + lines[i + 1].strip()
        line = re.sub(r"^\w+ *: \w+ +f:\d+ st:[\d,]+(?: at [^>]*)?> ?", "", line.strip())
        if line not in out:
            out.append(line)
    return out


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def main():
    cfg = load_config()
    root = cache_root(cfg)
    missing = prepare(cfg, root)
    if missing:
        print("Not installed, left out:", ", ".join(missing))
    ok, lines = run(cfg, root)
    print("World loaded" if ok else "World load FAILED")
    for line in lines[:60]:
        print("  " + line[:300])
    if len(lines) > 60:
        print(f"  ... {len(lines) - 60} more in {os.path.join(root, 'server-console.txt')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
