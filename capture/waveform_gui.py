#!/usr/bin/env python3
"""waveform_gui.py -- grab a raw waveform from a Rigol MHO/DHO scope and plot it,
showing how long the transfer took.

A tiny Tk + matplotlib front end over rigol_mho.Scope. Pick a format and hit
Capture: it stops the scope, reads whatever record is in memory over the range
you have set (timing only the data transfers), scales each with its own
preamble, and plots volts vs time. By default it reads **every channel the
scope is displaying** and overlays them; pick a single channel to read just
that one. The status line reports points, bytes, transfer time and MB/s --
handy for seeing the mho-speed-patch effect (run patch_scope.py first).

The readout range is yours: this tool never sends :WAVeform:STARt or
:WAVeform:STOP. Set them on the scope and it reads exactly that (it will ask
the scope to re-derive a range that overruns the record, though -- see
capture()).

Channels come from :CHANnel<n>:DISPlay?, because :WAV:SOURce silently refuses a
channel that is switched off: it leaves the previous source selected and reports
no error, so a blind request returns another channel's samples.

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
# Rigol's own trace colours, darkened enough to read on a white plot.
CH_COLOUR = {1: "#c8a000", 2: "#00a6bd", 3: "#c840b8", 4: "#3a6fd8"}


def active_channels(scope) -> list:
    """Channel numbers the scope is currently displaying.

    This is also exactly the set we are allowed to read: :WAV:SOURce silently
    refuses a channel that is switched off -- it leaves the previous source
    selected and reports no error -- so asking for one would hand back the
    wrong channel's samples.
    """
    return [n for n in range(1, 5)
            if scope.query(f":CHANnel{n}:DISPlay?").strip() in ("1", "ON")]


def capture(host: str, channels=None, fmt: str = "WORD",
            mode: str = "RAW", stop: bool = True, max_points: int = 0,
            progress=None):
    """Read the waveform(s) currently in the scope's memory.

    channels: iterable of channel numbers, or None for every displayed channel.

    Returns (traces, stats). traces is a list of dicts, one per channel:
    dict(ch, times, volts, points, nbytes, xfer_s, mbps).

    This does NOT change the scope's acquisition setup -- no timebase, memory
    depth, sweep or trigger changes -- and it does NOT set the readout range:
    :WAVeform:STARt / :WAVeform:STOP are left as you set them, and one single
    :WAVeform:DATA? per channel reads whatever that range covers. It optionally
    :STOPs the scope, which deep RAW reads require.

    mode="RAW"    reads out of the full acquisition memory.
    mode="NORMal" reads out of the ~1000 on-screen points.
    max_points>0  truncates each decoded record client-side; it does NOT shorten
                  the transfer -- narrow :WAVeform:STOP on the scope for that.

    Only the :WAV:DATA? transfers are timed. Nothing else is inside that window:
    :WAV:STARt/:STOP cost ~40 ms each of device service time and selecting a
    source costs ~62 ms, so issuing any of them inside the clock would charge
    setup to the transfer.
    """
    fmt = fmt.upper()
    mode = mode.upper()
    width = 2 if fmt == "WORD" else 1

    def note(msg):
        if progress:
            progress(msg)

    traces = []
    with rigol_mho.Scope(host=host, timeout=60.0) as scope:
        if stop:
            note("stopping acquisition...")
            scope.stop()          # freeze the current record; RAW needs a stop
            scope.query("*OPC?")

        live = active_channels(scope)
        if channels is None:
            want = live
        else:
            want = [int(c) for c in channels]
            off = [c for c in want if c not in live]
            if off:
                raise rigol_mho.ScopeError(
                    f"channel(s) {', '.join(map(str, off))} are switched off; "
                    f"the scope will not read them. Displayed: "
                    f"{', '.join(map(str, live)) or 'none'}")
        if not want:
            raise rigol_mho.ScopeError(
                "no channels are switched on -- nothing to read")

        # Readout settings: they select what/how we read, they don't change the
        # captured waveform. The STARt/STOP range is deliberately not among them.
        scope.write(f":WAVeform:MODE {mode}")
        scope.write(f":WAVeform:FORMat {fmt}")

        # Read back the range you set, so we can say what we are about to fetch.
        first = int(float(scope.query(":WAVeform:STARt?")))
        last = int(float(scope.query(":WAVeform:STOP?")))

        # You never have to name a point count: entering RAW mode makes the
        # scope set :WAV:STARt/:STOP to the full memory depth itself. The catch
        # is that only the NORMal->RAW *transition* re-derives them -- writing
        # RAW while already in RAW is a no-op -- and :WAV:STOP does not follow
        # :ACQ:MDEPth afterwards. So a range left over from a deeper setup just
        # persists, and the scope pads rather than clamps: ask for 10 Mpt with
        # 1 Mpt in memory and you get a 20 MB block, 18 MB of it garbage.
        # (:WAV:POINts? and the preamble are no help -- both echo the stale
        # STOP. MAX/MAXimum/DEFault are rejected: -200 / -120.)
        # So: if the range overruns the record, bounce the mode and let the
        # scope re-derive it. A deliberate sub-range is left alone.
        if mode.startswith("RAW"):
            try:
                depth = int(float(scope.query(":ACQuire:MDEPth?")))
            except ValueError:
                depth = 0          # e.g. AUTO -- nothing to check against
            if depth and last > depth:
                note(f"range 1-{last:,} overruns the {depth:,}-point record; "
                     f"asking the scope to re-derive it")
                scope.write(":WAVeform:MODE NORMal")
                scope.write(":WAVeform:MODE RAW")
                scope.query("*OPC?")
                first = int(float(scope.query(":WAVeform:STARt?")))
                last = int(float(scope.query(":WAVeform:STOP?")))
            # The mirror case: a range that *undershoots* the record. Nothing
            # re-derives it (a :STOP mid-acquisition leaves one behind, and it
            # then persists across captures), and unlike an overrun it is
            # silent -- you just quietly read less than you acquired. We do not
            # touch it, since a narrowed range may well be deliberate, but say
            # so rather than let it pass unnoticed.
            elif depth and (last - first + 1) < depth:
                note(f"note: reading {last - first + 1:,} of the {depth:,}-point "
                     f"record (range {first:,}-{last:,}). If that range is "
                     f"stale rather than deliberate, re-derive it with "
                     f":WAV:MODE NORMal then RAW.")
        expect = max(last - first + 1, 0)

        for i, ch in enumerate(want, 1):
            scope.write(f":WAVeform:SOURce CHANnel{ch}")
            got_src = scope.query(":WAVeform:SOURce?")
            if str(ch) not in got_src:
                raise rigol_mho.ScopeError(
                    f"asked for CHANnel{ch} but the scope selected {got_src}")
            # The preamble is per-source: yincrement/yorigin follow that
            # channel's volts/div, so it must be re-read for every channel.
            pre = scope.preamble()

            note(f"[{i}/{len(want)}] CH{ch}: transferring points "
                 f"{first:,}-{last:,} ({expect:,})...")
            # The *OPC? drains the source select and preamble above, so the
            # timer covers the transfer and nothing else.
            scope.query("*OPC?")
            t0 = time.perf_counter()
            buf = scope.query_block(":WAVeform:DATA?")
            dt = time.perf_counter() - t0

            # decode + scale with numpy (fast, off the wire clock)
            if fmt == "WORD":
                raw = np.frombuffer(bytes(buf), dtype="<u2").astype(np.float64)
            else:
                raw = np.frombuffer(bytes(buf), dtype=np.uint8).astype(np.float64)
            if max_points and max_points > 0:
                raw = raw[:max_points]   # plot cap; the bytes already arrived
            traces.append(dict(
                ch=ch,
                volts=(raw - pre.yorigin - pre.yreference) * pre.yincrement,
                times=pre.xorigin + (np.arange(raw.size) - pre.xreference)
                      * pre.xincrement,
                points=raw.size, xferred=len(buf) // width,
                nbytes=len(buf), xfer_s=dt,
                mbps=(len(buf) / dt / 1e6 if dt else 0.0),
                srate=(1.0 / pre.xincrement if pre.xincrement else 0.0)))

    nbytes = sum(t["nbytes"] for t in traces)
    xfer_s = sum(t["xfer_s"] for t in traces)
    stats = dict(channels=[t["ch"] for t in traces],
                 points=traces[0]["points"] if traces else 0,
                 xferred=traces[0]["xferred"] if traces else 0,
                 nbytes=nbytes, width=width, xfer_s=xfer_s,
                 mbps=(nbytes / xfer_s / 1e6 if xfer_s else 0.0),
                 srate=traces[0]["srate"] if traces else 0.0,
                 start=first, stop=last)
    return traces, stats


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
    chs = s.get("channels") or []
    who = "CH" + "+".join(str(c) for c in chs) if chs else "?"
    per = f"{s.get('xferred', s['points']):,} pts"
    if len(chs) > 1:
        per += f" ×{len(chs)}"
    line = (f"{who}  {per} · {mb:.2f} MB in {s['xfer_s']*1000:.0f} ms "
            f"→ {s['mbps']:.2f} MB/s")
    if s.get("stop"):
        line += f"   [{s['start']:,}-{s['stop']:,}]"
    if s.get("xferred") and s["points"] < s["xferred"]:
        line += f"   (plotting first {s['points']:,})"
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
    ch_var = tk.StringVar(value="active")
    ttk.Combobox(bar, textvariable=ch_var, width=7, state="readonly",
                 values=["active", "1", "2", "3", "4"]).pack(side=tk.LEFT,
                                                             padx=(2, 10))

    ttk.Label(bar, text="Format:").pack(side=tk.LEFT)
    fmt_var = tk.StringVar(value="WORD")
    ttk.Combobox(bar, textvariable=fmt_var, values=["WORD", "BYTE"], width=6,
                 state="readonly").pack(side=tk.LEFT, padx=(2, 10))

    ttk.Label(bar, text="Read:").pack(side=tk.LEFT)
    mode_var = tk.StringVar(value=READ_MODES[0][0])
    ttk.Combobox(bar, textvariable=mode_var, values=[m[0] for m in READ_MODES],
                 width=17, state="readonly").pack(side=tk.LEFT, padx=(2, 10))

    ttk.Label(bar, text="Plot max pts (0=all):").pack(side=tk.LEFT)
    maxpts_var = tk.StringVar(value="0")
    ttk.Entry(bar, textvariable=maxpts_var, width=10).pack(side=tk.LEFT, padx=(2, 10))

    capture_btn = ttk.Button(bar, text="Capture")
    capture_btn.pack(side=tk.LEFT, padx=(4, 0))

    fig = Figure(figsize=(9, 4.5), dpi=100)
    ax = fig.add_subplot(111)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("volts (V)")
    ax.grid(True, alpha=0.3)
    canvas = FigureCanvasTkAgg(fig, master=root)
    canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
    NavigationToolbar2Tk(canvas, root)

    status = tk.StringVar(value="Ready. Set the scope up yourself, including "
                          ":WAV:STARt/:STOP; Capture just stops it and reads that "
                          "range. Tip: run patch_scope.py first.")
    ttk.Label(root, textvariable=status, anchor="w",
              relief=tk.SUNKEN, padding=4).pack(side=tk.BOTTOM, fill=tk.X)

    def set_status(msg):
        status.set(msg)

    def on_result(traces, stats, err):
        capture_btn.config(state=tk.NORMAL, text="Capture")
        if err:
            set_status(f"ERROR: {err}")
            return
        # Channel count varies per capture, so redraw rather than reuse lines.
        ax.clear()
        ax.set_xlabel("time (s)")
        ax.set_ylabel("volts (V)")
        ax.grid(True, alpha=0.3)
        for tr in traces:
            t, v = _decimate(tr["times"], tr["volts"])
            ax.plot(t, v, lw=0.8, label=f"CH{tr['ch']}",
                    color=CH_COLOUR.get(tr["ch"]))
        if len(traces) > 1:
            ax.legend(loc="upper right", fontsize=8, ncol=len(traces))
        ax.relim()
        ax.autoscale_view()
        canvas.draw_idle()
        set_status(_fmt_stats(stats))

    def worker(host, chans, fmt, mode, max_pts):
        try:
            traces, stats = capture(
                host, chans, fmt, mode, stop=True, max_points=max_pts,
                progress=lambda m: root.after(0, set_status, m))
            root.after(0, on_result, traces, stats, None)
        except Exception as e:
            root.after(0, on_result, None, None, str(e))

    def do_capture():
        label = mode_var.get()
        mode = next(m for (n, m) in READ_MODES if n == label)
        try:
            max_pts = int(maxpts_var.get() or "0")
        except ValueError:
            max_pts = 0
        sel = ch_var.get().strip()
        chans = None if sel == "active" else [int(sel)]
        capture_btn.config(state=tk.DISABLED, text="Capturing…")
        set_status("connecting...")
        threading.Thread(target=worker, daemon=True,
                         args=(host_var.get().strip(), chans,
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
    ap.add_argument("--channels", "--channel", dest="channels", default="active",
                    help="'active' (default: every displayed channel) or a "
                         "comma-separated list, e.g. 1,2")
    ap.add_argument("--format", default="WORD")
    ap.add_argument("--mode", default="RAW", help="RAW (full memory) or NORMal (screen)")
    ap.add_argument("--max-points", type=int, default=0,
                    help="truncate the decoded record (0 = all). Client-side "
                         "only -- it does not shorten the transfer; set "
                         ":WAVeform:STOP on the scope for that.")
    args = ap.parse_args(argv)

    sel = args.channels.strip().lower()
    chans = None if sel == "active" else [int(c) for c in sel.split(",") if c]

    if args.headless:
        try:
            traces, stats = capture(args.host, chans, args.format,
                                    args.mode, stop=True,
                                    max_points=args.max_points, progress=print)
        except rigol_mho.ScopeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        for t in traces:
            print(f"  CH{t['ch']}: {t['xferred']:,} pts · "
                  f"{t['nbytes']/1e6:.2f} MB in {t['xfer_s']*1000:.0f} ms "
                  f"→ {t['mbps']:.2f} MB/s")
        print(_fmt_stats(stats))
        return 0
    run_gui(args.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
