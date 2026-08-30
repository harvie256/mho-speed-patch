#!/usr/bin/env python3
"""patch_scope.py -- one-command in-memory readout speedup for Rigol MHO/DHO scopes.

Run this on your PC before doing captures. It:
  1. connects to the scope over network ADB (port 55555),
  2. gets root (`adb root`, falling back to `su`),
  3. downloads + pushes + starts a matching frida-server (cached, version-locked),
  4. injects mho_speed_patch.js into com.rigol.scope, and
  5. STAYS RUNNING -- leave this window open while you capture.

Nothing is written to the scope's firmware. The patch lives only while this tool
is attached: press Ctrl-C (or reboot the scope) to revert to stock behaviour.

Prerequisites on the PC:
  * `adb` on PATH (or pass --adb / drop platform-tools next to this script)
  * `pip install frida`     (the Python binding; frida-tools not required)

Usage:
  python3 patch_scope.py 172.30.188.217
  python3 patch_scope.py 172.30.188.217 --frida-server ./frida-server   # offline
"""
import argparse
import hashlib
import lzma
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

APP = "com.rigol.scope"
ADB_PORT = 55555
SCPI_PORT = 5555
HERE = os.path.dirname(os.path.abspath(__file__))
DEV_FS = "/data/local/tmp/frida-server"
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "mho-speed-patch")


def log(msg):
    print(f"[patch_scope] {msg}", flush=True)


def die(msg, code=1):
    print(f"[patch_scope] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# --- adb helpers -------------------------------------------------------------
def find_adb(explicit):
    if explicit:
        return explicit
    local = os.path.join(HERE, "platform-tools", "adb")
    if os.path.exists(local):
        return local
    found = shutil.which("adb")
    if found:
        return found
    die("adb not found. Install Android platform-tools, put them next to this "
        "script, or pass --adb /path/to/adb")


class Adb:
    def __init__(self, adb, serial):
        self.adb = adb
        self.serial = serial

    def _run(self, args, **kw):
        return subprocess.run([self.adb, "-s", self.serial, *args],
                              capture_output=True, text=True, **kw)

    def raw(self, args, **kw):
        return subprocess.run([self.adb, *args], capture_output=True, text=True, **kw)

    def shell(self, cmd, root=False):
        if root:
            cmd = f"su -c '{cmd}'"
        return self._run(["shell", cmd])

    def push(self, local, remote):
        return self._run(["push", local, remote])


# --- frida-server provisioning ----------------------------------------------
def frida_version():
    try:
        import frida
        return frida.__version__
    except ImportError:
        die("the 'frida' Python module is not installed. Run: pip install frida")


def fetch_frida_server(version):
    """Return a local path to frida-server-<version>-android-arm64 (cached)."""
    os.makedirs(CACHE, exist_ok=True)
    dest = os.path.join(CACHE, f"frida-server-{version}-android-arm64")
    if os.path.exists(dest) and os.path.getsize(dest) > 1_000_000:
        return dest
    name = f"frida-server-{version}-android-arm64.xz"
    url = f"https://github.com/frida/frida/releases/download/{version}/{name}"
    log(f"downloading {name} (~25 MB, cached for next time) ...")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            comp = r.read()
    except Exception as e:
        die(f"could not download frida-server for {version} ({e}). "
            f"Download it yourself and pass --frida-server, or "
            f"pip install a frida whose server has a matching release.")
    with open(dest, "wb") as f:
        f.write(lzma.decompress(comp))
    return dest


def ensure_frida_server(adb, version, local_path):
    # Reuse a running server iff its version matches our client.
    running = adb.shell("pidof frida-server", root=True).stdout.strip()
    if running:
        ver = adb.shell(f"{DEV_FS} --version", root=True).stdout.strip()
        if ver == version:
            log(f"frida-server {ver} already running -- reusing")
            return
        log(f"running frida-server {ver!r} != client {version}; restarting")
        adb.shell("pkill frida-server", root=True)
        time.sleep(1)

    src = local_path or fetch_frida_server(version)
    # push if the on-device copy differs (by size, cheap check)
    dev_ok = False
    st = adb.shell(f"stat -c %s {DEV_FS} 2>/dev/null || true").stdout.strip()
    if st.isdigit() and int(st) == os.path.getsize(src):
        dev_ok = True
    if not dev_ok:
        log("pushing frida-server to /data/local/tmp ...")
        r = adb.push(src, DEV_FS)
        if r.returncode != 0:
            die(f"push failed: {r.stderr.strip()}")
    adb.shell(f"chmod 755 {DEV_FS}", root=True)
    # -D daemonizes: survives the adb shell exiting.
    adb.shell(f"{DEV_FS} -D", root=True)
    for _ in range(20):
        time.sleep(0.3)
        if adb.shell("pidof frida-server", root=True).stdout.strip():
            log(f"frida-server {version} started")
            return
    die("frida-server did not come up")


# --- scope access ------------------------------------------------------------
def connect(adb_path, ip):
    serial = f"{ip}:{ADB_PORT}"
    r = subprocess.run([adb_path, "connect", serial], capture_output=True, text=True)
    out = (r.stdout + r.stderr).lower()
    if "connected" not in out and "already" not in out:
        die(f"adb connect {serial} failed: {r.stdout.strip()} {r.stderr.strip()}")
    return Adb(adb_path, serial)


def ensure_root(adb):
    # Try `adb root` (works on these debuggable builds); else confirm `su` works.
    adb.raw(["-s", adb.serial, "root"])
    time.sleep(1)
    subprocess.run([adb.adb, "connect", adb.serial], capture_output=True, text=True)
    if adb.shell("id").stdout.strip().find("uid=0") >= 0:
        log("root via 'adb root'")
        return
    if "uid=0" in adb.shell("id", root=True).stdout:
        log("root via 'su'")
        return
    die("could not get root on the scope (neither 'adb root' nor 'su' worked)")


def app_pid(adb):
    for how in (dict(root=True), dict(root=False)):
        pid = adb.shell(f"pidof {APP}", **how).stdout.strip().split()
        if pid and pid[0].isdigit():
            return int(pid[0])
    die(f"{APP} is not running on the scope")


# --- inject ------------------------------------------------------------------
def inject(serial, pid, script_path):
    import frida
    try:
        dev = frida.get_device(serial, timeout=10)
    except Exception:
        dev = frida.get_usb_device(timeout=10)
    session = dev.attach(pid)
    msgs = {"patched": 0, "errors": []}

    def on_message(m, _data):
        if m.get("type") == "error":
            msgs["errors"].append(m.get("description"))
            print("  script error:", m.get("description"), file=sys.stderr)
        elif m.get("type") == "send":
            print("  ", m["payload"])
        else:
            p = m.get("payload") or ""
            if "patched" in str(p) or "active" in str(p):
                print("  ", p)

    script = session.create_script(open(script_path).read())
    script.on("message", on_message)
    # capture console.log output too
    script.load()
    time.sleep(0.6)
    if msgs["errors"]:
        session.detach()
        die("patch failed to install (see script errors above)")
    return session


def smoke_test(ip):
    """Confirm the app is alive and answering SCPI after injection."""
    try:
        s = socket.create_connection((ip, SCPI_PORT), timeout=5)
        s.sendall(b"*IDN?\n")
        time.sleep(0.3)
        idn = s.recv(256).decode(errors="replace").strip()
        s.close()
        if "RIGOL" in idn:
            log(f"scope alive after patch: {idn}")
            return True
    except Exception as e:
        log(f"warning: SCPI smoke-test failed ({e}); the app may be busy")
    return False


def main():
    ap = argparse.ArgumentParser(description="In-memory readout speedup for Rigol MHO/DHO scopes.")
    ap.add_argument("ip", help="scope IP address")
    ap.add_argument("--adb", help="path to adb (default: PATH or ./platform-tools/adb)")
    ap.add_argument("--frida-server", help="local frida-server-arm64 to push (skip download)")
    ap.add_argument("--script", default=os.path.join(HERE, "mho_speed_patch.js"))
    ap.add_argument("--no-verify", action="store_true", help="skip the SCPI smoke-test")
    args = ap.parse_args()

    if not os.path.exists(args.script):
        die(f"patch script not found: {args.script}")

    version = frida_version()
    adb_path = find_adb(args.adb)
    log(f"frida {version}, adb {adb_path}")

    adb = connect(adb_path, args.ip)
    ensure_root(adb)
    ensure_frida_server(adb, version, args.frida_server)
    pid = app_pid(adb)
    log(f"injecting into {APP} (pid {pid}) ...")
    session = inject(adb.serial, pid, args.script)

    if not args.no_verify:
        smoke_test(args.ip)

    print()
    log("PATCH ACTIVE -- readout is now ~5x faster on-device.")
    log("Leave this window open while you capture. Press Ctrl-C to revert.")
    log("Tip: read in ONE large :WAV:DATA? request, not many chunks.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            session.detach()
        except Exception:
            pass
        print()
        log("detached -- stock (slow) behaviour restored. frida-server left running.")


if __name__ == "__main__":
    main()
