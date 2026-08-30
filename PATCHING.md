# Speeding up MHO/DHO waveform readout — the working patch

**Target:** Rigol MHO934, firmware `00.01.00` (Rockchip RK3399, Android). Likely
applies to the MHO900 / DHO800 / DHO900 family (shared `libscope-auklet.so`).

## What's slow, in one sentence

The SCPI waveform response (`:WAVeform:DATA?`) is assembled **one byte at a
time** into a `std::vector<char>`, so a 1 Mpt WORD read makes ~2,000,000
`RByteArray::append(&byte, 1)` calls (each going through the non-inlined
libc++ `push_back` / `__construct_one_at_end` chain). See `FINDINGS.md` for the
full investigation.

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
`tools/probe.js`):

| function | calls per read |
|----------|---------------:|
| `CApiWave::toWord` | **1** |
| `RByteArray::append(char const*, int)` | **2,000,193** |
| `vector<char>::__construct_one_at_end` | 4,005,548 |

So the per-byte loop is in `toWord`, which is called **once**. That distinction
is the whole game (below).

## The patch

Replace the per-byte loop with a single bulk `memcpy`. `RByteArray` is a plain
`std::vector<char>` (`+0 begin`, `+8 end`, `+16 cap`); the returned object is
passed via the AArch64 indirect-result register `x8`. The patch
(`mho_speed_patch.js`) intercepts `toWord`/`toByte`, zeroes `n` on entry so the
original loop appends nothing, and on exit fills the returned `RByteArray` with
one `malloc`+`memcpy`. Symbols are resolved **by name**
(`_ZN8CApiWave6toWordEPhi`, `_ZN8CApiWave6toByteEPhi`) so it survives minor
rebuilds.

### Why hook `toWord` and NOT `append`

A Frida `Interceptor` adds per-call bridge overhead. Hooking `append` (2M
calls/read) made the read **slower** (34 s) — the overhead is multiplied 2M
times. `toWord` is called **once per read**, so intercepting it is essentially
free. Rule: hook the outermost function that runs once, never the hot inner one.

## Results (verified on MHO934 fw 00.01.00)

Byte-for-byte identical output (md5 matches an unpatched read), 1 Mpt WORD:

| | throughput | speedup |
|---|---|---|
| baseline | ~1.6–1.8 MB/s | 1× |
| patched (`toWord`+`toByte` bulk) | ~1.9–2.2 MB/s | **~1.2–1.3×** |

**Honest limitation:** this is a real, correctness-verified speedup but only
~1.2×, not the ~7× a pure buffer-build micro-benchmark predicts
(`tools/bench_append.cpp`). `toWord` is only *one* per-byte stage; the profile
shows more serialization elsewhere (~57% inlined "unknown", a second ~2M
`push_back` batch). Removing `toWord`'s loop reclaims its share (~15–25% of the
read); the remaining stages need the same treatment to approach the wire limit
(~4.4 MB/s burst). Contributions welcome.

## How to run it

Prereqs:
1. Root shell on the scope over ADB — `adb connect <ip>:55555` (the Android
   debug bridge is on TCP **55555**; SSH is pubkey-only with Rigol's own keys).
   See `FINDINGS.md`.
2. `frida-server` (arm64, matching your `frida-tools` version) on the scope:
   ```
   adb shell "su -c '/data/local/tmp/frida-server -D &'"
   ```
3. `pip install frida-tools` on your machine.

Apply (stays active while running; Ctrl-C restores the original):
```
python3 apply_patch.py            # attach to com.rigol.scope over adb
```
Then read waveforms as usual (`rigol_mho.py`, `bench.py`, or your own SCPI).

## Adapting to another firmware / offsets

The patch resolves functions by mangled symbol name, so no offsets are
hard-coded. If a build ever strips or renames them, re-derive with the NDK:
```
llvm-nm -DC libscope-auklet.so | grep 'CApiWave::to'      # find toWord/toByte
llvm-objdump -d --start-address=<addr> libscope-auklet.so # confirm the loop
```
Extract `libscope-auklet.so` from your own scope's `base.apk`
(`/data/app/com.rigol.scope-*/base.apk`) — it is **not** included here
(proprietary).

## Persistent (no-Frida) variant — future work

For a patch that survives reboots without a resident Frida process, the same
change can be baked into `libscope-auklet.so` on disk (rewrite `toWord`'s loop
into a bulk `append(src, k*n)` + a bulk `append`), then repackaged into the APK
or replaced on `/system`. This modifies proprietary firmware and risks bricking;
back up the original `.so` first. Not yet implemented here.
