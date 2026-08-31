# MHO934 raw-sample readout: where the bottleneck actually is

**Question:** when reading deep-memory samples off the MHO934, what caps the
rate? Online guesses say "the SoC" without evidence. Focus depth: **1 Mpt**.

**Answer:** the wall is **inefficient userspace code in the scope's SCPI/waveform
library** (`libscope-auklet.so`). The worker thread builds the response buffer by
**appending the waveform one byte at a time into a `std::vector<char>` /
`RByteArray`** (`RByteArray::append` -> `vector<char>::__construct_one_at_end`
with per-element allocator construct/destroy), paying that overhead for all
~2,000,000 bytes of a 1 Mpt WORD read. simpleperf attributes ~92% of the worker
thread's cycles to `libscope-auklet.so`, dominated by those per-element vector
ops plus `CApiWave::toWord`. One Cortex-A72 core saturates delivering ~1.2-1.9 MB/s.

It is NOT the network, NOT TCP (the finished buffer is sent efficiently in ~1 MB
`write()` chunks), NOT the kernel/GPIO, and — crucially — **NOT a lack of SoC
capacity**: the hardware is a 6-core Rockchip RK3399 with 4 GB RAM (GB/s memcpy)
and cores sit idle during a read. The online "it's the SoC" guess is wrong: the
silicon is capable and underused; the limit is an O(bytes) per-byte buffer-build
in the firmware. This explains every observation: byte-proportional (one
push_back per byte, so BYTE format is 2x faster than WORD), single-thread
CPU-bound, and ~50% of the wall-clock as time-to-first-byte (the buffer being
built before the efficient chunked send begins).

A secondary waste: a second app thread busy-waits in `pselect6(0, NULL..., &tiny)`
~1.5 million times/sec during a read, burning a whole extra core for nothing.

## Update — proven wire-free, root cause pinned to `append`, and a ~5× fix

Later work removed the two remaining ambiguities and produced a much larger,
verified speedup. See `docs/PATCHING.md` for the patch itself.

* **The wire is definitively not the limit.** A loopback benchmark run *on the
  scope* (`bench/loopbench.c`, client → `127.0.0.1:5555`) gives the same ~1.5 MB/s
  baseline as Wi-Fi. With the network reduced to a memory copy, throughput is
  unchanged — so it's on-device, full stop. A future Ethernet link buys nothing
  *until* the on-device rate is raised.

* **There are two per-byte stages, both bottlenecked on the same primitive.**
  `RByteArray::append(char const*, int)` copies **byte-by-byte via a per-element
  construct loop, not `memcpy`** — confirmed by disassembly (a loop that
  increments a counter and calls the vector construct per iteration). So:
  (1) `CApiWave::toWord`/`toByte` call `append(&src[i], 1)` 2 M times, and
  (2) the SCPI framing calls one bulk `append(data, ~2 MB)` that still loops 2 M
  times *internally*. Fixing only (1) — the original patch — gave ~1.9×; (2) was
  the hidden majority.

* **Fixing both → ~5× on-device**, byte-identical, stable:

  | (1 Mpt WORD, on-scope loopback, single request) | throughput |
  |---|---|
  | baseline | ~1.6 MB/s |
  | stage 1 (`toWord`/`toByte`) | ~3.1 MB/s (~1.9×) |
  | **stages 1+2 (`+append`)** | **~7–11.6 MB/s, median ~8.5 (~5×)** |

* **The bottleneck has now moved to the wire.** At ~8 MB/s the scope outruns the
  ~2–4 MB/s Wi-Fi link, so over Wi-Fi the same patch shows only ~1.6×. Ethernet
  now matters. Chunk size is a second, free lever: reading in one large request
  instead of many saves ~60 ms of command round-trips per extra chunk.

* **Method notes / traps:** `Interceptor.replace` of the shared `append`
  primitive crashes the app (it's called app-wide on many threads) — use `attach`
  and gate on size (only the ~2 large framing calls/read). Per-thread CPU deltas
  (`/proc/1199/task/*/stat`) showed the read is ~85% CPU-bound on the worker
  thread; the live-waveform GUI (`RenderThread` + `task_plot_wave` + mali) burns
  ~3 more cores continuously but turning the trace display off did not help
  throughput.

NOTE: an earlier revision of this file blamed a GPIO kernel driver
(`hdcode_gpio`) for PIO readout. That was wrong -- it came from profiling the
busy-wait thread, whose kernel PCs simpleperf mislabeled onto random modules
(`[hdcode_gpio]`, then `[usbtmc_dev]`). `get_hdcode` actually just reads 4 GPIO
strap pins (a hardware ID). The corrected root cause is the per-byte vector
append above, found by profiling the real worker thread and mapping IPs to
kallsyms by hand.

## Headline numbers (1 Mpt, WORD = 2 MB, over LAN)

| Metric | Value |
|--------|-------|
| Overall readout | ~1.25-1.6 MB/s |
| Time-to-first-byte (device prep) | ~675-1095 ms (≈50% of total) |
| Streaming burst rate (wire, active) | 4.4 MB/s (bursts >9 MB/s seen) |
| Mid-stream stalls | ~150-470 ms across 6-23 gaps |
| **Wire idle waiting on device** | **~65% of the read** |

## The three experiments that localize it

1. **Transport is not the limit.** The scope's HTTP server (port 80) moves data
   over the *identical* TCP/Wi-Fi path but bypasses the acquisition engine:
   **~1.9-2.3 MB/s**, and SCPI streaming *bursts* hit 4.4 MB/s. Since HTTP and
   the burst rate both exceed the ~1.25 MB/s SCPI average, the wire has spare
   capacity — it is not the wall. (`bench.py --http`)

2. **Per-sample serialization is not the limit.** BYTE (1 Mpt = 1 MB) reads in
   ~half the time of WORD (1 Mpt = 2 MB), so the two show *identical* MB/s. Cost
   scales with **bytes**, not sample **count** — if per-sample formatting were
   the wall, both formats (same sample count) would take the same time. They
   don't. (`bench.py --format BYTE/WORD`)

3. **It's byte-proportional on-device prep.** Instrumenting every recv() shows
   ~half the wall-clock elapses *before the first byte* (TTFB), and TTFB scales
   ~2x from BYTE(1MB) to WORD(2MB): ~550 ms vs ~1070 ms ≈ 1.9 MB/s internal
   marshalling. The rest is bursty streaming punctuated by device stalls.
   (`bench.py --timeline`)

## On-device profiling (via ADB, port 55555)

SSH is public-key-only with Rigol's own dev keys pre-installed (no usable
password login). The community route is **ADB over TCP 55555** (the device runs
Android): `adb connect <ip>:55555; adb shell` gives a shell. Hardware:
Rockchip RK3399 (2x A72 @1.8GHz + 4x A53 @1.4GHz), 4 GB RAM, Linux 4.4.

Per-thread CPU of `com.rigol.scope` (PID 1199), cumulative over the read window
(the accurate method; instantaneous `top` under-samples the bursts):

| thread | idle (no reads) | during 1 Mpt reads |
|--------|----------------:|-------------------:|
| **Thread-8** (SCPI readout) | **1%** | **93% of one core** |
| RenderThread (GUI) | 79% | 77% |
| task_plot_wave (GUI) | 55% | 51% |
| mali-cmar-backe (GPU) | 30% | 27% |

Thread-8 is idle when we don't read and pegs one core when we do -> it *is* the
readout, and it is CPU-bound. The GUI threads are unchanged by SCPI load (the
display constantly redraws at ~1.6 cores regardless). System-wide, ~1.6 cores
stay idle during reads -> the readout is not parallelized and the SoC is not
saturated.

### simpleperf: finding the real hot loop (and a methodology trap)

The readout is a two-thread pair, both idle until a read starts:
- **Thread-8**: a busy-wait -- `pselect6(n=0, NULL, NULL, NULL, &tiny)` ~1.5M
  times/sec (confirmed with `simpleperf stat` on syscall tracepoints). Does zero
  I/O. It pegs a core, so CPU-delta thread-selection keeps picking it -- a trap.
  Its cycles are in the kernel `select`/`pselect` path; simpleperf mislabels
  those kernel PCs onto whatever module (`[hdcode_gpio]`, `[usbtmc_dev]`), which
  is how the first analysis went wrong.
- **Thread-15**: the real worker. It does NOT top CPU sampling because it blocks
  in `tcp_sendmsg` during the send. ftrace on `sys_enter_write` caught it doing
  the actual transfer as two ~1 MB `write()` calls (`count=0xfa000`, `0xee48c`).

Profiling **Thread-15** (`simpleperf record -t <tid> -g`, kernel syms via
`kptr_restrict=0`) and reading `--sort symbol`:

| Overhead | Symbol (in `libscope-auklet.so`, 92.5% of cycles) |
|---------:|---------------------------------------------------|
| 9.0% | `vector<char>::__construct_one_at_end<char const&>` |
| 4.7% | `allocator_traits<char>::construct<char,...>` |
| 4.6% | `allocator<char>::construct<char,...>` |
| 4.3% | `vector<char>::_ConstructTransaction` |
| 4.2% | `RByteArray::append(char const*, int)` |
| ~11% | matching allocator `destroy`/`__construct`/`~_ConstructTransaction` |
| 1.1% | `CApiWave::toWord(unsigned char*, int)` |
| 57% | `unknown` (inlined in `libscope-auklet.so`) |

The named symbols are all `std::vector<char>` **per-element** append. The buffer
is grown one byte at a time. **That is the hot loop.**

Tooling note: simpleperf's kernel *module* labels are unreliable here; the fix
was dumping raw sample IPs (`simpleperf dump`) and mapping them against
`/proc/kallsyms` by hand. `ftrace` (`/sys/kernel/debug/tracing`, no install) was
what pinned the real send path and the busy-wait's `n=0` pselect.

## Practical consequences

- **Use BYTE, not WORD, when 8-bit resolution suffices** — it ~halves readout
  time (half the bytes to marshal on-device).
- **Fewer, larger requests win.** 1x1M beats 4x250k beats 10x100k; each request
  adds ~0.1-0.15 s. Per-request fixed overhead ~83 ms.
- **The wire is not worth optimizing** (wired ethernet, TCP tuning) — it's 65%
  idle. The gain ceiling is the on-device prep rate.
- Expected USB-TMC gain is **modest**, not dramatic: USB would speed only the
  streaming half; the byte-proportional prep floor (~50% of the time) is
  transport-independent. Predict maybe 1.3-1.5x, not 5x.

## What we could not measure

- **USB-TMC comparison**: the port stalls on any single read >~250k points and
  the jam survives cable replug AND reboot (control channel works, bulk data
  channel stays dead). Could not get a clean USB readout. This fragility is
  itself a firmware finding (00.01.00).
- **On-device profiling** (which SoC resource caps the 1.9 MB/s prep — memory
  bandwidth, a single-threaded copy, flash, etc.) needs a shell. SSH (port 22,
  OpenSSH 7.1) is open but we have no credentials; no ADB mode in the UI.

## Reproduce

    python3 bench/bench.py --http                        # transport ceiling
    python3 bench/bench.py --timeline --depth 1000000    # prep-vs-stream breakdown
    python3 bench/bench.py --transport lan --depths 100000,1000000 --format WORD
    python3 bench/bench.py --transport lan --depth 1000000 --format BYTE

## Fix validation (what actually helps)

You can't source-patch the compiled proprietary `libscope-auklet.so` (and the
per-element path spans multiple call sites, so there's no single site to
binary-patch), so this models the fix instead. `bench/bench_append.cpp` builds a 2 MB
buffer three ways, compiled with the **NDK's libc++ (the same STL the scope
uses) and run ON the scope's RK3399**:

At -O0 (matches the scope's binary -- per-element construct calls are NOT inlined,
as seen in the profile):

| strategy | RK3399 throughput | vs current |
|----------|------------------:|-----------:|
| A per-byte push_back, no reserve (**current**) | 3.3 MB/s | 1x |
| B **reserve()** + per-byte push_back | 5.7 MB/s | **1.7x** |
| C reserve() + **bulk insert/memcpy** | 22.9 MB/s | **7.0x** |

At -O2 (if Rigol also raised the opt level): A 134, B 210 (1.6x), C 511 MB/s (3.8x).

Sanity check: the modeled per-byte build (3.3 MB/s on RK3399) brackets the
scope's observed ~1.9 MB/s build rate well -- the scope is a bit slower due to
its extra `CApiWave::toWord` + `RByteArray` layers and the core stolen by the
pselect6 busy-wait. This confirms the mechanism.

Key correction to the original plan: **reserve() alone only buys ~1.7x**. The
scope's cost is the per-byte allocator construct/destroy call chain, not
reallocation; reserve() only removes reallocation. The real fix Rigol needs is a
**bulk copy** (memcpy/insert/assign of the whole sample block), ~7x here.

Projected overall impact: the byte-by-byte build is the ~50% time-to-first-byte
portion of a read. A 7x-faster build makes it near-free, leaving the readout
bounded by the next limit (efficient ~1 MB socket writes near the ~4.4 MB/s wire
burst, plus the pselect6 busy-wait) -- so expect a ~2-3x overall readout speedup,
with the build no longer the wall. Only Rigol can ship this in firmware.

Toolchain for reuse: Android NDK r27c at `/home/derryn/opt/android-ndk-r27c`.
Build+run:
    NDK=/home/derryn/opt/android-ndk-r27c/toolchains/llvm/prebuilt/linux-x86_64/bin
    $NDK/aarch64-linux-android30-clang++ -O0 -std=c++17 -static-libstdc++ \
        bench/bench_append.cpp -o bench_arm && adb push bench_arm /data/local/tmp/ && \
        adb shell /data/local/tmp/bench_arm

## Update 2 — over Ethernet: where the *remaining* gap went

With the patch applied and the scope on a wired **100 Mbit** link (`eth0`,
full duplex, no errors), readout measured **~5.3 MB/s** against an on-device
loopback figure of ~8.5-11 MB/s. The missing ~4 MB/s was not the wire.

**The wire is blameless.** `dd if=/dev/zero | nc` from the scope to the PC over
the same link gives **11.75 MB/s** (94.0 Mbit/s) — line rate for 100 Mbit after
framing. Instrumenting every `recv()` of a real `:WAV:DATA?` shows the SCPI
stream hits **11.8 MB/s while it is actually moving** — identical to the raw
ceiling. There is nothing to win on the wire; it was simply **idle 55-65% of the
read**.

**The scope never marshals and sends at the same time.** Stalls in the recv
timeline land exactly on **1 MiB boundaries** (999 KiB, 1999 KiB, 2999 KiB...),
matching the ~1 MiB `write()` chunks seen earlier under ftrace. The app runs a
strict `build 1 MiB -> write 1 MiB -> build 1 MiB -> ...` loop, so device time
and wire time **add** instead of overlapping:

    1 / (1/8.4 MB/s device + 1/11.8 MB/s wire) = 4.9 MB/s   ~= what we measured

Two host-side causes, both fixable at runtime with no firmware change
(now folded into `patch/patch_scope.py`):

1. **`tcp_wmem` max is 110208 bytes** while the app writes 1 MiB at a time. Every
   `write()` blocks in `tcp_sendmsg` until the wire drains it, so the CPU cannot
   start the next chunk. Raising the send buffer to 4 MB lets `write()` return
   immediately and the next chunk builds while the kernel drains the previous
   one. (8 MB read: 6.7 -> 8.7 MB/s.)

2. **The readout thread runs on the little cores.** Sampling `/proc/<pid>/task/*/stat`
   during a read shows the worker (`Thread-12` here; the name varies per session)
   migrating across cpu1/2/3/5 — mostly the **1.416 GHz A53s** — while
   `task_plot_wave` owns cpu4 and `RenderThread` owns cpu5, the two **1.8 GHz
   A72s**. The GUI burns ~1.6 cores continuously and wins both big cores; the
   readout gets ~63% of a little one, plus a cache-cold migration per chunk.
   `taskset`-ing the worker to `cpu4-5` roughly halves marshalling time and drops
   TTFB from ~230 ms to ~120 ms. Both governors already sit at max frequency and
   the SoC is at 52 C, so it is placement, not clocks or thermals.

   Pinning the *GUI* threads down to the A53s as well buys nothing further, so
   leave them alone and keep the UI responsive.

Measured on the MHO934, 100 Mbit wired, patch active, median of 7 reads on a
warmed connection (see the cold-connection note below):

| config | 2 MB (1 Mpt WORD) | 20 MB (10 Mpt WORD) | wire idle @20 MB |
|---|---:|---:|---:|
| stock kernel + scheduler | 5.3-6.0 MB/s | ~7 MB/s | ~45% |
| + 4 MB `tcp_wmem` | 6.2 MB/s | 8.7 MB/s | 26% |
| **+ worker pinned to A72** | **6.9 MB/s** | **10.8 MB/s** | **8%** |
| raw TCP ceiling on this link | 11.75 MB/s | 11.75 MB/s | — |

**After tuning the readout is wire-bound.** The whole read collapses to a simple,
predictive model:

    read time  ~=  120 ms  +  bytes / 11.8 MB/s

That ~120 ms is why *rate* looks bad on small reads and has nothing to do with
throughput. It breaks down as ~26 ms of SCPI command latency (a bare `*OPC?`
round trip costs **24.5 ms** on a 1 ms-RTT link — the SCPI parser services
commands on a slow tick, not on arrival) plus ~90 ms building the first 1 MiB,
which by definition cannot overlap with a wire that has nothing on it yet. TTFB
is flat at ~117 ms from 1 Mpt all the way to 10 Mpt, confirming it saturates once
the first chunk is full.

Consequences:

* **Ask for everything in one request.** 20 MB in a single `:WAV:DATA?` runs at
  **10.8 MB/s — 92% of line rate**. The same bytes as 2 MB requests run at 6.9.
* **You never have to name a point count -- but the range goes stale.** Entering
  RAW mode makes the scope set `:WAV:STARt`/`:STOP` to the full memory depth
  itself, which is why you normally never type a number. The catch is that only
  the **NORMal -> RAW transition** re-derives them: writing `:WAV:MODE RAW` while
  already in RAW is a no-op. And `:WAV:STOP` does not follow `:ACQ:MDEPth`, so a
  range left from a deeper setup just persists. The scope then **pads rather than
  clamps**: 1 Mpt in memory with `:WAV:STOP 10000000` returns a full 20 MB block,
  18 MB of it garbage, in 8.5 s, with no error. `:WAV:POINts?` and the preamble
  both echo the stale STOP, so neither catches it; `MAX`/`MAXimum`/`DEFault` are
  rejected (-200 / -120). `capture/waveform_gui.py` checks `:WAV:STOP?` against
  `:ACQ:MDEPth?` and, if the range overruns the record, bounces the mode to let
  the scope re-derive it -- while leaving a deliberate sub-range alone.

* **Never put `:WAVeform:STARt`/`:STOP` inside a timed region.** Each costs
  ~39 ms of device service time; the pair adds ~43 ms. `capture/waveform_gui.py` used to
  send them inside its transfer timer, which made it report ~5.8 MB/s where the
  raw benchmark reported ~6.9 for the identical transfer. It no longer sends them
  at all -- the readout range is now the operator's to set, and one bare
  `:WAV:DATA?` is the only thing inside the clock.

* **A cold connection reads ~15% slower than a warm one.** Successive 20 MB reads
  on one socket climb 8.9 -> 9.3 -> 9.6 -> 10.1 -> ~10.8 MB/s and stay there, so
  a one-shot capture on a fresh connection gets ~8.9-9.2 while the sustained
  figure is ~10.8. It is not TCP: forcing the scope's send buffer to start
  pre-grown (`tcp_wmem` default = max) changes nothing, and slow-start over
  13,700 segments would be over in milliseconds. The likeliest cause is the app
  faulting in a fresh ~20 MB response buffer each time until the allocator starts
  retaining it. Quote the warm number for throughput and the cold one for
  single-shot capture latency -- they measure different things.
* **Gigabit would not help much** and this scope only has a 100 Mbit PHY anyway:
  the device marshals at ~8-9 MB/s once pinned, so ~11.8 MB/s of wire is already
  slightly more than it can feed.
