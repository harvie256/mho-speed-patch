# mho-speed-patch

Tools and a working patch to speed up **raw waveform readout** on Rigol
MHO/DHO oscilloscopes (investigated on an **MHO934**, firmware `00.01.00`;
Rockchip RK3399 / Android).

The scope's `:WAVeform:DATA?` response is built one byte at a time into a
`std::vector<char>`, which caps deep-memory readout at ~1.5–2 MB/s regardless of
the transport. This repo documents how that was found and ships a runtime patch
that replaces the byte-by-byte build with a bulk `memcpy` — **~5× faster
on-device** (measured wire-free on loopback: ~1.6 → ~8.5 MB/s), byte-identical
output, reversible.

## Contents

| file | what |
|------|------|
| **`PATCHING.md`** | the working patch: what's slow, where, and how to apply it |
| **`FINDINGS.md`** | full bottleneck investigation (profiling, dead ends, corrections) |
| `mho_speed_patch.js` | the Frida patch (bulk-copy `CApiWave::toWord`/`toByte`) |
| `apply_patch.py` | attaches the patch to the running scope app |
| `rigol_mho.py` | dependency-free SCPI helper (LAN socket + USB-TMC) + CLI |
| `bench.py` | readout benchmark (size sweep, HTTP-ceiling, recv-timeline) |
| `tools/loopbench.c` | **on-scope** loopback benchmark: measures pure on-device throughput with the network wire removed (proves the wire is not the limit) |
| `install-udev-rule.sh` | udev rule for USB-TMC access |
| `install_ssh_key.sh` | add your SSH key for root access (optional) |
| `tools/` | investigation utilities: `probe.js`, `bench_append.cpp`, `loadgen.py` |

## Quick start (the patch)

1. Get a root shell on the scope over ADB (it listens on TCP **55555**):
   `adb connect <scope-ip>:55555`
2. Put a matching `frida-server` (arm64) on the scope and run it as root.
3. `pip install frida-tools` here, then:
   ```
   python3 apply_patch.py
   ```
   Leave it running; Ctrl-C detaches and restores the original code.

See `PATCHING.md` for the details and the measured results.

## Status

Verified on MHO934 fw `00.01.00`: **byte-identical output, stable under sustained
load, ~5× faster on-device** (loopback, wire removed: ~1.6 → ~8.5 MB/s median;
peaks >11 MB/s). Both per-byte stages (`toWord`/`toByte` and the framing `append`)
are fixed. See `PATCHING.md` for the method and `FINDINGS.md` for how it was found.

Over Wi-Fi you'll see less (~1.6×) because at ~8 MB/s the **scope now outruns the
Wi-Fi link** — the bottleneck has moved to the wire, so a wired/Ethernet client
is now worth it (it wasn't before the patch).

## Not included

Rigol's `libscope-auklet.so` / `base.apk` are proprietary and **not** in this
repo — extract your own from your scope. The Android NDK and `frida-server` are
downloaded separately (see `PATCHING.md`).

## Safety

The Frida patch is **reversible** — it lives only while `apply_patch.py` is
attached, makes no changes to `/system`, and worst case crashes the scope app
(which restarts). It does not touch the device firmware on disk.
