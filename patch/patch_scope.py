#!/usr/bin/env python3
"""patch_scope.py -- one-command in-memory readout speedup for Rigol MHO/DHO scopes.

Run this on your PC before doing captures. It:
  1. connects to the scope over network ADB (port 55555),
  2. gets root (`adb root`, falling back to `su`),
  3. downloads + pushes + starts a matching frida-server (cached, version-locked),
  4. injects mho_speed_patch.js into com.rigol.scope,
  5. tunes the scope for a wired link (see below; --no-tune to skip), and
  6. STAYS RUNNING -- leave this window open while you capture.

The patch makes the scope *marshal* fast; the tuning makes it actually *overlap*
marshalling with the send, so the wire stops idling. Two knobs, both measured:

  * TCP send buffer 110 KB -> 4 MB. The app write()s the response in 1 MiB
    chunks; with the stock 110 KB buffer every write blocks until the wire
    drains it, so the CPU cannot start building the next chunk.
  * Pin the SCPI readout threads to the A72 big cores (cpu4-5). By default the
    scheduler leaves them migrating over the 1.4 GHz A53s while the GUI owns
    both 1.8 GHz A72s. GUI threads are left alone, so the UI stays responsive.

Together these take a 1 Mpt WORD read from ~5.3 to ~6.9 MB/s, and a 10 Mpt read
to ~10.8 MB/s on a sustained connection -- 92% of 100 Mbit line rate. A one-shot
capture on a fresh connection lands ~15% lower. See FINDINGS.md "Update 2".

Nothing is written to the scope's firmware. Both the patch and the tuning live
only while this tool is attached: press Ctrl-C (or `kill` it, or reboot the
scope) to revert to stock behaviour.

Prerequisites on the PC:
  * `adb` on PATH (or pass --adb / drop platform-tools next to this script)
  * `pip install frida`     (the Python binding; frida-tools not required)

Usage (from the repo root):
  python3 patch/patch_scope.py 172.30.188.217
  python3 patch/patch_scope.py 172.30.188.217 --frida-server ./frida-server
"""
import argparse
import hashlib
import lzma
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

APP = "com.rigol.scope"
ADB_PORT = 55555
SCPI_PORT = 5555
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # repo root; patch/ lives one level down
DEV_FS = "/data/local/tmp/frida-server"
# Wired-link tuning (see FINDINGS.md "Update 2"). All in-memory, all reverted
# on exit; the scope also clears them on reboot.
TUNED_WMEM = "4096 2097152 4194304"   # tcp_wmem: 110 KB max -> 4 MB
TUNED_WMAX = "4194304"                # net.core.wmem_max
BIG_CORES = "30"                      # cpu4-5 mask: the RK3399's two A72s
ALL_CORES = "3f"                      # cpu0-5: stock, unrestricted
WORKER_NICE = "-20"                   # readout threads; app default is -10
STOCK_NICE = "-10"
RETUNE_EVERY = 30.0                   # seconds; self-heals if the app restarts
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
    for base in (ROOT, HERE):
        local = os.path.join(base, "platform-tools", "adb")
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

    def _run(self, args, timeout=60, **kw):
        # capture_output waits for EOF on the pipes, not for the command to
        # exit -- anything left holding the device-side stdout/stderr (a
        # daemon, say) would block us forever. The timeout is the backstop;
        # commands that spawn daemons must also redirect their stdio (see
        # ensure_frida_server).
        try:
            return subprocess.run([self.adb, "-s", self.serial, *args],
                                  capture_output=True, text=True,
                                  timeout=timeout, **kw)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args, 124, stdout="", stderr=f"adb timed out after {timeout}s")

    def raw(self, args, timeout=60, **kw):
        try:
            return subprocess.run([self.adb, *args], capture_output=True,
                                  text=True, timeout=timeout, **kw)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args, 124, stdout="", stderr=f"adb timed out after {timeout}s")

    def shell(self, cmd, root=False, timeout=60):
        if root:
            cmd = f"su -c '{cmd}'"
        return self._run(["shell", cmd], timeout=timeout)

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
    # -D daemonizes so it survives the adb shell exiting. The stdio redirect is
    # what stops us hanging: the daemon inherits adb's stdout/stderr pipes, and
    # capture_output waits for those to reach EOF, so without </dev/null and
    # >/dev/null the launch call blocks forever even though the server started.
    adb.shell(f"{DEV_FS} -D </dev/null >/dev/null 2>&1", root=True, timeout=20)
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


def current_pid(adb):
    """Live pid of the app, or None. Unlike app_pid() this never exits: it runs
    inside the watch loop, where a momentarily absent app is not fatal."""
    try:
        for how in (dict(root=True), dict(root=False)):
            out = adb.shell(f"pidof {APP}", **how).stdout.strip().split()
            if out and out[0].isdigit():
                return int(out[0])
    except Exception:
        pass
    return None


def app_pid(adb):
    for how in (dict(root=True), dict(root=False)):
        pid = adb.shell(f"pidof {APP}", **how).stdout.strip().split()
        if pid and pid[0].isdigit():
            return int(pid[0])
    die(f"{APP} is not running on the scope")


# --- wired-link tuning -------------------------------------------------------
# Note: every shell snippet below avoids single quotes -- Adb.shell(root=True)
# wraps the command in su -c '...'.

def read_stock_tuning(adb):
    """Snapshot what we are about to change, so revert puts back exactly that."""
    wmem = adb.shell("cat /proc/sys/net/ipv4/tcp_wmem").stdout.strip()
    wmax = adb.shell("cat /proc/sys/net/core/wmem_max").stdout.strip()
    # tabs -> spaces; the sysctl accepts either but we echo it back verbatim
    wmem = " ".join(wmem.split())
    if not wmem or not wmax.isdigit():
        return None
    return wmem, wmax


def apply_tuning(adb, wmem, wmax, mask, nice):
    """Set the send-buffer sysctls and pin the SCPI readout threads.

    Returns the number of threads pinned, or -1 if the app was not found.
    The readout threads are a fixed pool created at app start (they are named
    Thread-N and are not spawned per connection), so one pass is enough -- but
    main() re-runs this periodically so it self-heals if the app restarts.
    """
    snippet = (
        f'P=$(pidof {APP}); '
        f'if [ -z "$P" ]; then echo -1; exit 0; fi; '
        f'echo "{wmem}" > /proc/sys/net/ipv4/tcp_wmem; '
        f'echo {wmax} > /proc/sys/net/core/wmem_max; '
        f'n=0; '
        f'for t in /proc/$P/task/*/; do '
        f'  case $(cat $t/comm 2>/dev/null) in '
        f'    Thread-*) tid=$(basename $t); '
        f'              taskset -p {mask} $tid >/dev/null 2>&1 && n=$((n+1)); '
        f'              renice -n {nice} -p $tid >/dev/null 2>&1 ;; '
        f'  esac; '
        f'done; '
        f'echo $n'
    )
    out = adb.shell(snippet).stdout.strip().split()
    if not out or not out[-1].lstrip("-").isdigit():
        out = adb.shell(snippet, root=True).stdout.strip().split()
    return int(out[-1]) if out and out[-1].lstrip("-").isdigit() else -1


def tune(adb):
    """Apply the wired-link tuning. Returns (stock, npinned) for revert."""
    stock = read_stock_tuning(adb)
    if stock is None:
        log("warning: could not read the scope's TCP settings; skipping tuning")
        return None, 0
    n = apply_tuning(adb, TUNED_WMEM, TUNED_WMAX, BIG_CORES, WORKER_NICE)
    if n < 0:
        log("warning: app not running; tuning not applied")
        return stock, 0
    log(f"tuned for wired readout: tcp_wmem -> 4 MB, {n} readout thread(s) "
        f"pinned to cpu4-5 (A72)")
    return stock, n


def untune(adb, stock):
    """Put the send buffers and thread placement back the way we found them."""
    if not stock:
        return
    wmem, wmax = stock
    apply_tuning(adb, wmem, wmax, ALL_CORES, STOCK_NICE)


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
    ap.add_argument("--no-tune", action="store_true",
                    help="skip the wired-link tuning (send buffer + core pinning)")
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

    stock, npinned = (None, 0)
    if not args.no_tune:
        stock, npinned = tune(adb)

    if not args.no_verify:
        smoke_test(args.ip)

    # Ctrl-C already unwinds to the finally below; make a plain `kill` (and
    # `timeout`) do the same, so the scope is never left tuned and patched.
    signal.signal(signal.SIGTERM,
                  lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    print()
    log("PATCH ACTIVE -- readout is now ~10x faster on-device.")
    if npinned:
        log("TUNED -- 20 MB single-request read: 2.8 MB/s stock -> ~28.5 MB/s "
            "over USB gigabit Ethernet (~15% less for a one-shot capture).")
    log("Leave this window open while you capture. Press Ctrl-C to revert.")
    log("Tip: read in ONE large :WAV:DATA? request, not many chunks -- the "
        "fixed ~120 ms per request is what caps small reads, not throughput.")
    try:
        while True:
            time.sleep(RETUNE_EVERY)
            # The app can restart under us (crash, low-memory kill, user action).
            # The Frida script dies with it, but tuning below still succeeds
            # against the new process -- so without this check we would keep
            # reporting a healthy "re-tuned" line while the scope quietly ran
            # STOCK, unpatched code. Re-inject instead.
            live = current_pid(adb)
            if live and live != pid:
                log(f"app restarted ({pid} -> {live}); re-injecting the patch ...")
                try:
                    session.detach()
                except Exception:
                    pass
                try:
                    session = inject(adb.serial, live, args.script)
                    pid = live
                    npinned = 0          # force a re-tune line for the new process
                    log("re-injected -- patch active again")
                except Exception as e:
                    log(f"warning: re-injection failed ({e}); scope is running "
                        f"STOCK until this is resolved")
                    pid = live
            if stock:
                # cheap and idempotent; re-applies if the app has restarted
                n = apply_tuning(adb, TUNED_WMEM, TUNED_WMAX, BIG_CORES, WORKER_NICE)
                if n > 0 and n != npinned:
                    log(f"re-tuned: {n} readout thread(s) pinned")
                    npinned = n
    except KeyboardInterrupt:
        pass
    finally:
        try:
            session.detach()
        except Exception:
            pass
        if stock:
            untune(adb, stock)
        print()
        log("detached -- stock (slow) behaviour and scope tuning restored. "
            "frida-server left running.")


if __name__ == "__main__":
    main()
