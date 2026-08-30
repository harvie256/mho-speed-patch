#!/usr/bin/env python3
"""waveform_gui.py -- grab a raw waveform from a Rigol MHO/DHO scope and plot it,
showing how long the transfer took.

A tiny Tk + matplotlib front end over rigol_mho.Scope. Pick a channel, format
and depth, hit Capture: it arms a fresh acquisition, reads the record (timing
only the data transfer), scales it with the preamble, and plots volts vs time.
The status line reports points, bytes, transfer time and MB/s -- handy for
seeing the mho-speed-patch effect (run patch_scope.py first).

Requires: numpy, matplotlib (`pip install numpy matplotlib`). Tk ships with
Python. SCPI access is pure stdlib via rigol_mho.py.
"""
import argparse
import os
import sys
import threading
import time

import numpy as np

import rigol_mho


# ---- capture (GUI-independent, testable) -----------------------------------
def capture(host: str, channel: int = 1, fmt: str = "WORD",
            mode: str = "RAW", stop: bool = True, max_points: int = 0,
            progress=None):
    """Read the waveform currently in the scope's memory. Returns (times, volts, stats).

    This does NOT change the scope's acquisition setup -- no timebase, memory
    depth, sweep or trigger changes. It optionally :STOPs the scope (required to
    read deep RAW memory) and reads whatever record is there. Set the scope up
    yourself (timebase, depth, trigger) before capturing.

    mode="RAW"    reads the full acquisition memory (:ACQuire:MDEPth? points).
    mode="NORMal" reads the ~1000 on-screen points.
    max_points>0  caps how many points are read (client-side only; the scope's
                  settings are untouched).

    stats: dict(points, nbytes, width, xfer_s, mbps, srate).
    Only the :WAV:DATA? transfer is timed (xfer_s).
    """
    fmt = fmt.upper()
    mode = mode.upper()
    width = 2 if fmt == "WORD" else 1

    def note(msg):
        if progress:
            progress(msg)

    with rigol_mho.Scope(host=host, timeout=60.0) as scope:
        if stop:
            note("stopping acquisition...")
            scope.stop()          # freeze the current record; RAW needs a stop
            scope.query("*OPC?")

        # These are readout settings, not acquisition settings -- they select
        # what/how we read, they don't change the captured waveform.
        scope.write(f":WAVeform:SOURce CHANnel{channel}")
        scope.write(f":WAVeform:MODE {mode}")
        scope.write(f":WAVeform:FORMat {fmt}")
        pre = scope.preamble()

        if mode.startswith("RAW"):
            total = int(float(scope.query(":ACQuire:MDEPth?")))
        else:
            total = pre.points  # ~1000 on-screen points
        if max_points and max_points > 0:
            total = min(total, max_points)

        # Read the record; one large request is fastest over LAN.
        note(f"transferring {total:,} points...")
        buf = bytearray()
        start = 1
        chunk = max(total, 1)          # single request
        t0 = time.perf_counter()
        while start <= total:
            end = min(start + chunk - 1, total)
            scope.write(f":WAVeform:STARt {start}")
            scope.write(f":WAVeform:STOP {end}")
            data = scope.query_block(":WAVeform:DATA?")
            if not data:
                break
            buf += data
            got = len(data) // width
            if got == 0:
                break
            start += got
        xfer_s = time.perf_counter() - t0

    # decode + scale with numpy (fast, off the wire clock)
    if fmt == "WORD":
        raw = np.frombuffer(bytes(buf), dtype="<u2").astype(np.float64)
    else:
        raw = np.frombuffer(bytes(buf), dtype=np.uint8).astype(np.float64)
    volts = (raw - pre.yorigin - pre.yreference) * pre.yincrement
    times = pre.xorigin + (np.arange(raw.size) - pre.xreference) * pre.xincrement

    nbytes = len(buf)
    stats = dict(points=raw.size, nbytes=nbytes, width=width,
                 xfer_s=xfer_s, mbps=(nbytes / xfer_s / 1e6 if xfer_s else 0.0),
                 srate=(1.0 / pre.xincrement if pre.xincrement else 0.0))
    return times, volts, stats


def _decimate(times, volts, cols=4000):
    """Min/max envelope so plotting millions of points stays fast but spikes
    survive. Returns (t, v) arrays roughly 2*cols long."""
    n = volts.size
    if n <= 2 * cols:
        return times, volts
    step = n // cols
    m = (n // step) * step
    tv = times[:m:step]
    vb = volts[:m].reshape(-1, step)
    vmin = vb.min(axis=1)
    vmax = vb.max(axis=1)
    # interleave min/max at the bucket's start time
    t = np.repeat(tv, 2)
    v = np.empty(t.size)
    v[0::2] = vmin
    v[1::2] = vmax
    return t, v


def _fmt_stats(s):
    mb = s["nbytes"] / 1e6
    line = (f"{s['points']:,} pts · {mb:.2f} MB in {s['xfer_s']*1000:.0f} ms "
            f"→ {s['mbps']:.2f} MB/s")
    if s.get("srate"):
        line += f"   (fs={s['srate']/1e6:.1f} MSa/s)"
    return line


# ---- GUI --------------------------------------------------------------------
def run_gui(default_host):
    import tkinter as tk
    from tkinter import ttk
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import (
        FigureCanvasTkAgg, NavigationToolbar2Tk)

    READ_MODES = [("Full memory (RAW)", "RAW"),
                  ("Screen (~1k)", "NORMal")]

    root = tk.Tk()
    root.title("Rigol raw-waveform grabber")
    root.geometry("980x640")

    bar = ttk.Frame(root, padding=8)
    bar.pack(side=tk.TOP, fill=tk.X)

    ttk.Label(bar, text="Scope:").pack(side=tk.LEFT)
    host_var = tk.StringVar(value=default_host)
    ttk.Entry(bar, textvariable=host_var, width=16).pack(side=tk.LEFT, padx=(2, 10))

    ttk.Label(bar, text="Ch:").pack(side=tk.LEFT)
    ch_var = tk.IntVar(value=1)
    ttk.Spinbox(bar, from_=1, to=4, width=3, textvariable=ch_var).pack(side=tk.LEFT, padx=(2, 10))

    ttk.Label(bar, text="Format:").pack(side=tk.LEFT)
    fmt_var = tk.StringVar(value="WORD")
    ttk.Combobox(bar, textvariable=fmt_var, values=["WORD", "BYTE"], width=6,
                 state="readonly").pack(side=tk.LEFT, padx=(2, 10))

    ttk.Label(bar, text="Read:").pack(side=tk.LEFT)
    mode_var = tk.StringVar(value=READ_MODES[0][0])
    ttk.Combobox(bar, textvariable=mode_var, values=[m[0] for m in READ_MODES],
                 width=17, state="readonly").pack(side=tk.LEFT, padx=(2, 10))

    ttk.Label(bar, text="Max pts (0=all):").pack(side=tk.LEFT)
    maxpts_var = tk.StringVar(value="0")
    ttk.Entry(bar, textvariable=maxpts_var, width=10).pack(side=tk.LEFT, padx=(2, 10))

    capture_btn = ttk.Button(bar, text="Capture")
    capture_btn.pack(side=tk.LEFT, padx=(4, 0))

    fig = Figure(figsize=(9, 4.5), dpi=100)
    ax = fig.add_subplot(111)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("volts (V)")
    ax.grid(True, alpha=0.3)
    (line,) = ax.plot([], [], lw=0.8)
    canvas = FigureCanvasTkAgg(fig, master=root)
    canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
    NavigationToolbar2Tk(canvas, root)

    status = tk.StringVar(value="Ready. Set the scope up yourself; Capture just "
                          "stops it and reads memory. Tip: patch_scope.py = ~5x faster.")
    ttk.Label(root, textvariable=status, anchor="w",
              relief=tk.SUNKEN, padding=4).pack(side=tk.BOTTOM, fill=tk.X)

    def set_status(msg):
        status.set(msg)

    def on_result(times, volts, stats, err):
        capture_btn.config(state=tk.NORMAL, text="Capture")
        if err:
            set_status(f"ERROR: {err}")
            return
        t, v = _decimate(times, volts)
        line.set_data(t, v)
        ax.relim()
        ax.autoscale_view()
        canvas.draw_idle()
        set_status(_fmt_stats(stats))

    def worker(host, ch, fmt, mode, max_pts):
        try:
            times, volts, stats = capture(
                host, ch, fmt, mode, stop=True, max_points=max_pts,
                progress=lambda m: root.after(0, set_status, m))
            root.after(0, on_result, times, volts, stats, None)
        except Exception as e:
            root.after(0, on_result, None, None, None, str(e))

    def do_capture():
        label = mode_var.get()
        mode = next(m for (n, m) in READ_MODES if n == label)
        try:
            max_pts = int(maxpts_var.get() or "0")
        except ValueError:
            max_pts = 0
        capture_btn.config(state=tk.DISABLED, text="Capturing…")
        set_status("connecting...")
        threading.Thread(target=worker, daemon=True,
                         args=(host_var.get().strip(), ch_var.get(),
                               fmt_var.get(), mode, max_pts)).start()

    capture_btn.config(command=do_capture)
    root.mainloop()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("host", nargs="?",
                    default=os.environ.get("RIGOL_HOST", rigol_mho.DEFAULT_HOST),
                    help="scope IP (default: $RIGOL_HOST or rigol_mho default)")
    ap.add_argument("--headless", action="store_true",
                    help="capture once and print stats, no GUI (for testing)")
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--format", default="WORD")
    ap.add_argument("--mode", default="RAW", help="RAW (full memory) or NORMal (screen)")
    ap.add_argument("--max-points", type=int, default=0,
                    help="cap points read (0 = all; client-side only)")
    args = ap.parse_args(argv)

    if args.headless:
        _, _, stats = capture(args.host, args.channel, args.format,
                              args.mode, stop=True, max_points=args.max_points,
                              progress=print)
        print(_fmt_stats(stats))
        return 0
    run_gui(args.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
