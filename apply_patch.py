#!/usr/bin/env python3
"""Apply the mho-speed-patch to a running Rigol MHO/DHO scope via Frida.

The patch (mho_speed_patch.js) replaces the byte-by-byte SCPI waveform-response
build in com.rigol.scope with a single bulk memcpy. It lives only while this
process stays attached -- run it and leave it running; Ctrl-C detaches and
restores the original code.

Prereqs (see README):
  * frida-server (matching frida-tools version) running as root on the scope
  * `pip install frida-tools` on this machine
  * device reachable over adb (`adb connect <ip>:55555`)

Usage:
  python3 apply_patch.py                # attach by process name over USB/adb
  python3 apply_patch.py --pid 1199
  python3 apply_patch.py --host 172.30.188.217   # network frida (frida-server -l 0.0.0.0)
"""
import argparse, sys, time
import frida

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, help="target pid (default: find com.rigol.scope)")
    ap.add_argument("--name", default="com.rigol.scope", help="target process name")
    ap.add_argument("--host", help="frida-server host:port for network attach")
    ap.add_argument("--script", default="mho_speed_patch.js")
    args = ap.parse_args()

    dev = frida.get_device_manager().add_remote_device(args.host) if args.host \
        else frida.get_usb_device(timeout=10)
    target = args.pid if args.pid else args.name
    print(f"attaching to {target} ...")
    session = dev.attach(target)

    errors = []
    def on_message(m, data):
        if m.get("type") == "error":
            errors.append(m); print("SCRIPT ERROR:", m.get("description"), file=sys.stderr)
        elif m.get("type") == "send":
            print("msg:", m["payload"])
    script = session.create_script(open(args.script).read())
    script.on("message", on_message)
    script.load()
    time.sleep(0.4)
    if errors:
        print("patch failed to install", file=sys.stderr); session.detach(); return 1
    print("patch active. Leave this running; Ctrl-C to detach and restore.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        session.detach()
        print("\ndetached (original code restored)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
