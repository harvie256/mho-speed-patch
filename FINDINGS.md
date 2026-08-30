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

    python3 bench.py --http                        # transport ceiling
    python3 bench.py --timeline --depth 1000000    # prep-vs-stream breakdown
    python3 bench.py --transport lan --depths 100000,1000000 --format WORD
    python3 bench.py --transport lan --depth 1000000 --format BYTE

## Fix validation (what actually helps)

You can't source-patch the compiled proprietary `libscope-auklet.so` (and the
per-element path spans multiple call sites, so there's no single site to
binary-patch), so this models the fix instead. `bench_append.cpp` builds a 2 MB
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
        bench_append.cpp -o bench_arm && adb push bench_arm /data/local/tmp/ && \
        adb shell /data/local/tmp/bench_arm
