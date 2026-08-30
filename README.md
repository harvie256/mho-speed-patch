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
| **`patch_scope.py`** | **one command from your PC**: connect, root, provision frida-server, inject, hold |
| **`PATCHING.md`** | the working patch: what's slow, where, and how to apply it |
| **`FINDINGS.md`** | full bottleneck investigation (profiling, dead ends, corrections) |
| `mho_speed_patch.js` | the Frida patch (bulk-copy `toWord`/`toByte` + `append` memcpy) |
| `apply_patch.py` | lower-level: attach the patch to an already-provisioned scope |
| `rigol_mho.py` | dependency-free SCPI helper (LAN socket + USB-TMC) + CLI |
| `bench.py` | readout benchmark (size sweep, HTTP-ceiling, recv-timeline) |
| `tools/loopbench.c` | **on-scope** loopback benchmark: measures pure on-device throughput with the network wire removed (proves the wire is not the limit) |
| `install-udev-rule.sh` | udev rule for USB-TMC access |
| `install_ssh_key.sh` | add your SSH key for root access (optional) |
| `tools/` | investigation utilities: `probe.js`, `bench_append.cpp`, `loadgen.py` |

## Quick start

You need only `adb` (Android platform-tools) and Python on your PC, plus the
scope's IP. The scope exposes unauthenticated network ADB on port 55555 out of
the box, so there's nothing to enable on it.

```
pip install frida
python3 patch_scope.py <scope-ip>
```

That's it. `patch_scope.py` connects over ADB, gets root, downloads and starts a
matching `frida-server` on the scope (cached after the first run), injects the
patch, and then **stays running** — leave the window open while you capture.
Press **Ctrl-C** to revert the scope to stock behaviour.

* Nothing is written to the scope's firmware; the patch is in-memory only and
  also disappears on reboot.
* For best results read each record in **one large `:WAV:DATA?` request**, not
  many small chunks (~60 ms saved per avoided round-trip).
* `apply_patch.py` is the lower-level path if you've already put `frida-server`
  on the scope yourself. See `PATCHING.md` for details and measured results.

> **Note:** the patch reverts the moment `patch_scope.py` disconnects (Frida
> unloads its hooks). A fire-and-forget variant that survives disconnect is
> planned — see PATCHING.md "Persistent variant".

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
