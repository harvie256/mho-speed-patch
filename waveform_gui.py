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
def _timebase_for_depth(depth: int) -> float:
    # MDEPth AUTO derives depth from window x sample-rate (<=1 GSa/s single ch):
    # points = 10*(s/div)*srate  =>  s/div = depth/(10*1e9). Floor to keep a sane
    # window. See tools/loopbench.c / project notes on the deep-memory quirk.
    return max(depth * 1e-10, 1e-8)


def capture(host: str, channel: int = 1, fmt: str = "WORD",
            depth: int = 1_000_000, mode: str = "RAW",
            acquire: bool = True, progress=None):
    """Capture one trace. Returns (times, volts, stats).

    stats: dict(points, nbytes, width, xfer_s, mbps, srate, acq_s).
    Only the :WAV:DATA? transfer is timed (xfer_s); arming is separate (acq_s).
    """
    fmt = fmt.upper()
    mode = mode.upper()
    width = 2 if fmt == "WORD" else 1

    def note(msg):
        if progress:
            progress(msg)

    with rigol_mho.Scope(host=host, timeout=60.0) as scope:
        acq_s = 0.0
        if mode.startswith("RAW") and acquire:
            note("arming acquisition...")
            t = time.perf_counter()
            scope.write(f":CHANnel{channel}:DISPlay ON")
            scope.write(":TRIGger:SWEep AUTO")
            scope.write(":STOP")
            scope.write(f":TIMebase:MAIN:SCALe {_timebase_for_depth(depth):g}")
            scope.write(":ACQuire:MDEPth AUTO")
            scope.write(":RUN")
            window = 10 * _timebase_for_depth(depth)
            time.sleep(min(max(window + 0.8, 1.2), 6.0))
            scope.write(":STOP")
            scope.query("*OPC?")
            acq_s = time.perf_counter() - t

        scope.write(f":WAVeform:SOURce CHANnel{channel}")
        scope.write(f":WAVeform:MODE {mode}")
        scope.write(f":WAVeform:FORMat {fmt}")
        pre = scope.preamble()

        if mode.startswith("RAW"):
            total = int(float(scope.query(":ACQuire:MDEPth?")))
        else:
            total = pre.points  # ~1000 on-screen points

        # Read the whole record; one large request is fastest over LAN.
        note(f"transferring {total:,} points...")
        buf = bytearray()
        start = 1
        chunk = max(total, 1)          # single request
        t0 = time.perf_counter()
        while start <= total:
            stop = min(start + chunk - 1, total)
            scope.write(f":WAVeform:STARt {start}")
            scope.write(f":WAVeform:STOP {stop}")
            data = scope.query_block(":WAVeform:DATA?")
            if not data:
                break
            buf += data
            got = len(data) // width
            start += got
            if got == 0:
                break
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
                 srate=(1.0 / pre.xincrement if pre.xincrement else 0.0),
                 acq_s=acq_s)
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
    if s.get("acq_s"):
        line += f"   [+{s['acq_s']*1000:.0f} ms arming]"
    return line


# ---- GUI --------------------------------------------------------------------
def run_gui(default_host):
    import tkinter as tk
    from tkinter import ttk
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import (
        FigureCanvasTkAgg, NavigationToolbar2Tk)

    DEPTHS = [("Screen (~1k)", 1000, "NORMal"),
              ("10 k", 10_000, "RAW"),
              ("100 k", 100_000, "RAW"),
              ("1 M", 1_000_000, "RAW"),
              ("10 M", 10_000_000, "RAW")]

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

    ttk.Label(bar, text="Depth:").pack(side=tk.LEFT)
    depth_var = tk.StringVar(value=DEPTHS[3][0])
    ttk.Combobox(bar, textvariable=depth_var, values=[d[0] for d in DEPTHS],
                 width=12, state="readonly").pack(side=tk.LEFT, padx=(2, 10))

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

    status = tk.StringVar(value="Ready. Tip: run patch_scope.py for ~5x faster reads.")
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

    def worker(host, ch, fmt, depth, mode):
        try:
            times, volts, stats = capture(
                host, ch, fmt, depth, mode,
                progress=lambda m: root.after(0, set_status, m))
            root.after(0, on_result, times, volts, stats, None)
        except Exception as e:
            root.after(0, on_result, None, None, None, str(e))

    def do_capture():
        label = depth_var.get()
        depth, mode = next((d, m) for (n, d, m) in DEPTHS if n == label)
        capture_btn.config(state=tk.DISABLED, text="Capturing…")
        set_status("connecting...")
        threading.Thread(target=worker, daemon=True,
                         args=(host_var.get().strip(), ch_var.get(),
                               fmt_var.get(), depth, mode)).start()

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
    ap.add_argument("--depth", type=int, default=1_000_000)
    args = ap.parse_args(argv)

    if args.headless:
        mode = "NORMal" if args.depth <= 1200 else "RAW"
        _, _, stats = capture(args.host, args.channel, args.format,
                              args.depth, mode, progress=print)
        print(_fmt_stats(stats))
        return 0
    run_gui(args.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
