# Speeding up MHO/DHO waveform readout — the working patch

**Target:** Rigol MHO934, firmware `00.01.00` (Rockchip RK3399, Android). Likely
applies to the MHO900 / DHO800 / DHO900 family (shared `libscope-auklet.so`).

## What's slow, in one sentence

The SCPI waveform response (`:WAVeform:DATA?`) is assembled **one byte at a
time** into a `std::vector<char>`, because the underlying primitive
`RByteArray::append(char const*, int)` copies byte-by-byte via a per-element
construct loop (it is **not** a `memcpy`). See `docs/FINDINGS.md` for the full
investigation.

## Two stages, one root cause

Both slow stages bottleneck on that same `append`:

| stage | where | cost for 1 Mpt WORD |
|-------|-------|--------------------|
| 1. sample build | `CApiWave::toWord`/`toByte` loop `ret.append(&src[i], 1)` | 2,000,000 one-byte appends |
| 2. response framing | SCPI layer does one bulk `append(data, ~2 MB)` | 1 call, but append loops 2,000,000× **internally** |

Fixing only stage 1 (the original patch) got ~1.9×; stage 2 was the hidden
majority. Fixing both gets ~5× (see results below). The key realisation:
`append(ptr, n)` is O(n) *per byte* even when called in bulk, so a single
`append(data, 2 MB)` is just as slow as 2 M one-byte appends.

## Measuring on-device (wire removed)

`bench/loopbench.c` is a tiny SCPI client that runs **on the scope** and connects
to `127.0.0.1:5555`, so the network is loopback (a memory copy) and what you
measure is the pure on-device production rate. Cross-compile with the NDK and push:

```
export ANDROID_NDK=/path/to/android-ndk-r27c
NDK=$ANDROID_NDK/toolchains/llvm/prebuilt/linux-x86_64/bin   # darwin-x86_64 on macOS
$NDK/aarch64-linux-android30-clang -O2 -o loopbench bench/loopbench.c
adb push loopbench /data/local/tmp/ && adb shell "su -c 'chmod 755 /data/local/tmp/loopbench'"
adb shell "su -c '/data/local/tmp/loopbench 1000000 WORD 1000000 6'"   # depth fmt chunk reps
```

Loopback baseline ≈ Wi-Fi baseline (~1.5 MB/s) — proof the wire was never the
limit. Read in **one large request** (chunk = depth): each extra chunk costs
~60 ms of command round-trips.

## Where the loop lives (measured, not guessed)

`CApiWave::toWord(unsigned char* src, int n)` — and its BYTE-format sibling
`CApiWave::toByte` — do literally this:

```cpp
RByteArray ret;                       // empty std::vector<char>
for (int i = 0; i < k*n; i++)         // k = 2 for WORD, 1 for BYTE
    ret.append(&src[i], 1);           // one byte per call
return ret;                           // no transformation — a plain copy
```

Call counts during **one** 1 Mpt WORD read (measured with a Frida probe,
`bench/probe.js`):

| function | calls per read |
|----------|---------------:|
| `CApiWave::toWord` | **1** |
| `RByteArray::append(char const*, int)` | **2,000,193** |
| `vector<char>::__construct_one_at_end` | 4,005,548 |

So the per-byte loop is in `toWord`, which is called **once**. That distinction
is the whole game (below).

## The patch (`mho_speed_patch.js`)

`RByteArray` is a plain `std::vector<char>` (`+0 begin`, `+8 end`, `+16 cap`).
Symbols are resolved **by name** so the patch survives minor rebuilds.

**Stage 1 — `toWord`/`toByte`** (called *once* per read; the returned object is
passed via the AArch64 indirect-result register `x8`): intercept, zero `n` on
entry so the original loop appends nothing, and on exit fill the returned
`RByteArray` with one `malloc`+`memcpy`.

**Stage 2 — `append(char const*, int)`** (the framing copy): intercept **only
large calls** (`n >= 65536`); zero `n` on entry to neuter the stock per-byte
loop, then do a proper grow + `memcpy` on exit. Small appends (headers, other
app traffic) run the stock code untouched.

### Two rules learned the hard way

* **Never hook the hot inner call, and never `Interceptor.replace` a hot shared
  primitive.** Hooking `append` on *every* call (2 M/read) made the read take
  34 s — Frida's per-call bridge overhead × 2 M. And a wholesale
  `Interceptor.replace` of `append` (it's called app-wide, on many threads)
  corrupted memory and **crashed/reset the scope app**. The safe approach is
  `attach` + intervene only on the large calls (~2/read), leaving the primitive
  otherwise intact.
* Hook the outermost function that runs once (`toWord`), and for the shared
  primitive, gate on size so the blast radius stays tiny.

## Results (verified on MHO934 fw 00.01.00)

Byte-for-byte identical output (md5 matches an unpatched read), stable under
sustained load. 1 Mpt WORD, **on-device loopback** (network wire removed),
single-request reads:

| | throughput | speedup |
|---|---|---|
| baseline | ~1.6 MB/s | 1× |
| stage 1 only (`toWord`/`toByte`) | ~3.1 MB/s | ~1.9× |
| **stages 1+2 (`+append`)** | **~7–11.6 MB/s (median ~8.5)** | **~5×** |

Over Wi-Fi the measured speedup is smaller (~1.6×) because at ~8 MB/s the scope
now **outruns the ~2–4 MB/s Wi-Fi link** — the bottleneck has moved to the wire.
That's the headline finding: before the patch the scope couldn't even saturate
Wi-Fi, so a faster link was pointless; after it, a wired/Ethernet client pays off.

After both stages, the produce path (`getWfmData` + `toFormat`) is ~16 ms/read
and per-byte `append` is gone; remaining time is real bulk work + transport.

## How to run it

Prereqs:
1. Root shell on the scope over ADB — `adb connect <ip>:55555` (the Android
   debug bridge is on TCP **55555**; SSH is pubkey-only with Rigol's own keys).
   See `docs/FINDINGS.md`.
2. `frida-server` (arm64, matching your `frida-tools` version) on the scope:
   ```
   adb shell "su -c '/data/local/tmp/frida-server -D &'"
   ```
3. `pip install frida-tools` on your machine.

Apply (stays active while running; Ctrl-C restores the original):
```
python3 apply_patch.py            # attach to com.rigol.scope over adb
```
Then read waveforms as usual (`capture/rigol_mho.py`, `bench/bench.py`, or your own SCPI).

## Adapting to another firmware / offsets

The patch resolves functions by mangled symbol name, so no offsets are
hard-coded. If a build ever strips or renames them, re-derive with the NDK:
```
llvm-nm -DC libscope-auklet.so | grep -E 'CApiWave::to(Word|Byte)|RByteArray::append'
llvm-objdump -d --start-address=<addr> libscope-auklet.so # confirm the per-byte loop
```
The three symbols the patch needs: `_ZN8CApiWave6toWordEPhi`,
`_ZN8CApiWave6toByteEPhi`, `_ZN10RByteArray6appendEPKci`.
Extract `libscope-auklet.so` from your own scope's `base.apk`
(`/data/app/com.rigol.scope-*/base.apk`) — it is **not** included here
(proprietary).

## Persistent (no-Frida) variant — future work

For a patch that survives reboots without a resident Frida process, the single
highest-value change is to rewrite **`RByteArray::append(char const*, int)`** on
disk so its body is a `memcpy` grow instead of the per-element construct loop —
that one primitive is the root cause of *both* stages, so fixing it there fixes
everything at once (and the `toWord` loop becomes cheap for free). Bake it into
`libscope-auklet.so`, then repackage into the APK or replace on `/system`. This
modifies proprietary firmware and risks bricking; back up the original `.so`
first. Not yet implemented here.
