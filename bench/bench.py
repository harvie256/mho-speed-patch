"""Transport-decomposition benchmark for Rigol MHO934 raw-sample readout.

The question: when reading deep-memory samples, which stage is the wall --
the network, the USB link, or the scope's own SoC (memory copy + SCPI
serialization)?

Method: run the *identical* readout over two transports to the same box --
LAN raw-socket (port 5555) and USB-TMC (/dev/usbtmcN) -- across a wide range
of transfer sizes. Both share the on-device stages (acquisition memory copy,
SCPI block serialization) but differ entirely in transport (TCP/Wi-Fi vs USB
bulk). Comparing them partitions on-device cost from transport cost.

For each size we fit  T(N) = c + N/R :
    c = fixed per-query overhead (round trips, arming, command parse)
    R = steady-state throughput ceiling for that transport

Transfer time is measured with the acquisition already STOPped, so we time
pure readout of the stored record -- not the acquisition itself.

Usage:
    python3 bench.py --transport both --sweep
    python3 bench.py --transport usb  --depth 1000000 --repeats 5
    python3 bench.py --transport lan  --chunk 250000 --depth 10000000
"""

from __future__ import annotations

import argparse
import os
import socket
import statistics
import sys
import time
from dataclasses import dataclass, field

USB_DEV = os.environ.get("RIGOL_USBTMC", "/dev/usbtmc2")
LAN_HOST = os.environ.get("RIGOL_HOST", "172.30.188.217")
LAN_PORT = int(os.environ.get("RIGOL_PORT", "5555"))


# --------------------------------------------------------------------------
# Transports: identical interface, different wire.
# --------------------------------------------------------------------------

class Transport:
    name = "base"

    def write(self, cmd: str) -> None: ...
    def query(self, cmd: str) -> str: ...
    def read_block(self, cmd: str) -> bytes: ...
    def close(self) -> None: ...
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class LANTransport(Transport):
    name = "lan"

    def __init__(self, host=LAN_HOST, port=LAN_PORT, timeout=30.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)

    def write(self, cmd: str) -> None:
        self.sock.sendall(cmd.encode() + b"\n")

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            c = self.sock.recv(min(1 << 20, n - len(buf)))
            if not c:
                raise IOError("connection closed")
            buf += c
        return bytes(buf)

    def query(self, cmd: str) -> str:
        self.write(cmd)
        buf = bytearray()
        while not buf.endswith(b"\n"):
            c = self.sock.recv(4096)
            if not c:
                raise IOError("connection closed")
            buf += c
        return buf.decode(errors="replace").strip()

    def read_block(self, cmd: str) -> bytes:
        self.write(cmd)
        if self._recv_exact(1) != b"#":
            raise IOError("no block header")
        nd = int(self._recv_exact(1))
        ln = int(self._recv_exact(nd))
        data = self._recv_exact(ln)
        self._recv_exact(1)  # trailing newline
        return data

    def close(self):
        try: self.sock.close()
        except OSError: pass


class USBTransport(Transport):
    """USB-TMC via the kernel char device.

    The kernel usbtmc driver handles TMC bulk framing; we just write the
    command and read the IEEE 488.2 block payload back. Each os.read returns
    one driver transfer, so large blocks are accumulated in a loop.
    """
    name = "usb"

    def __init__(self, dev=USB_DEV):
        self.dev = dev
        self.fd = os.open(dev, os.O_RDWR)
        self._buf = bytearray()

    def write(self, cmd: str) -> None:
        os.write(self.fd, cmd.encode() + b"\n")

    def _fill(self, n: int) -> None:
        # Read until at least n bytes are buffered.
        while len(self._buf) < n:
            chunk = os.read(self.fd, 1 << 20)
            if not chunk:
                raise IOError("usbtmc read returned nothing")
            self._buf += chunk

    def query(self, cmd: str) -> str:
        self.write(cmd)
        chunk = os.read(self.fd, 4096)
        return chunk.decode(errors="replace").strip()

    def read_block(self, cmd: str) -> bytes:
        self._buf = bytearray()
        self.write(cmd)
        self._fill(2)
        if self._buf[0:1] != b"#":
            raise IOError(f"no block header (got {self._buf[:8]!r})")
        nd = int(self._buf[1:2])
        self._fill(2 + nd)
        ln = int(self._buf[2:2 + nd])
        header = 2 + nd
        self._fill(header + ln)
        return bytes(self._buf[header:header + ln])

    def close(self):
        try: os.close(self.fd)
        except OSError: pass


def make_transport(kind: str) -> Transport:
    if kind == "lan":
        return LANTransport()
    if kind == "usb":
        return USBTransport()
    raise ValueError(kind)


# --------------------------------------------------------------------------
# Benchmark core
# --------------------------------------------------------------------------

@dataclass
class Result:
    transport: str
    depth: int
    fmt: str
    points: int
    nbytes: int
    times: list[float] = field(default_factory=list)

    @property
    def best(self) -> float: return min(self.times)
    @property
    def median(self) -> float: return statistics.median(self.times)
    @property
    def mbps_best(self) -> float: return self.nbytes / self.best / 1e6
    @property
    def mbps_med(self) -> float: return self.nbytes / self.median / 1e6


def _timebase_for_depth(depth: int) -> float:
    """A s/div slow enough that the window can hold `depth` points.

    Achievable stored points depend on the acquisition window (10 x s/div).
    These values were verified to fill memory to the requested depth.
    """
    if depth <= 100_000:
        return 5e-4
    if depth <= 1_000_000:
        return 5e-3
    if depth <= 10_000_000:
        return 5e-2
    return 5e-1


def setup_readout(t: Transport, depth: int, fmt: str, source=1,
                  acq_timeout: float = 30.0) -> int:
    """Acquire a record of `depth` points, then arm RAW readout.

    Two non-obvious things about this scope (see project memory):

    * Setting :ACQ:MDEPth alone does NOT change the stored samples -- depth
      applies to the *next* acquisition.
    * :SINGle + :TFORce TRUNCATES the record to ~10k points. The reliable way
      to fill deep memory is AUTO sweep + :RUN, let it fill (>= one window),
      then :STOP.

    Returns the TRUE stored point count (:WAV:STOP? after the RAW re-derive),
    which is what reads must be driven from. Neither :WAVeform:POINts? (a
    stale echo of :WAV:STOP) nor :ACQ:MDEPth? (merely the setting) is reliable.
    The acquisition time is outside the measured readout.
    """
    t.write(":TRIGger:SWEep AUTO")
    t.write(":STOP")
    t.write(f":TIMebase:MAIN:SCALe {_timebase_for_depth(depth):g}")
    t.write(f":ACQuire:MDEPth {depth}")
    window = 10 * _timebase_for_depth(depth)

    # A :STOP issued mid-acquisition leaves a PARTIALLY filled record -- e.g.
    # 8.94 of 10 Mpt -- and that short range then persists for every later
    # reader, because nothing re-derives a range that *undershoots* the depth
    # (waveform_gui only re-derives an overrun). So wait for a full window with
    # headroom, verify the record actually filled, and retry with a longer wait
    # rather than silently benchmarking a short record.
    deadline = time.monotonic() + acq_timeout
    stored = 0
    for attempt in range(1, 5):
        t.write(":RUN")
        time.sleep(min(max(window * 1.5 + 0.5, 1.0), 10.0) * attempt)
        t.write(":STOP")
        time.sleep(0.2)
        stored = _arm_raw(t, source, fmt)
        if stored >= depth or time.monotonic() > deadline:
            break
    if stored < depth:
        print(f"# warning: depth {depth:,} requested but only {stored:,} "
              f"points stored; benchmarking the short record", file=sys.stderr)
    return stored


def _arm_raw(t: Transport, source: int, fmt: str) -> int:
    """Arm RAW readout and return the true stored point count."""
    t.write(f":WAVeform:SOURce CHANnel{source}")
    # Only the NORMal -> RAW *transition* re-derives :WAV:STARt/:STOP to the
    # full record. Writing RAW while already in RAW is a no-op, so a previous
    # run's stale (often out-of-range) window persists and every subsequent
    # :WAV:STARt/:STOP write is rejected with -200,"Command execute failed".
    t.write(":WAVeform:MODE NORMal")
    t.write(":WAVeform:MODE RAW")
    t.write(f":WAVeform:FORMat {fmt}")
    t.query("*OPC?")
    # :ACQ:MDEPth is the *setting*; the record actually stored is usually
    # smaller (the acquisition window may not fill it -- e.g. 9.57 Mpt for a
    # 10 Mpt setting). The NORMal -> RAW transition above re-derives
    # :WAV:STOP to the true stored count, so that is what we must read to.
    # Reading past the end does NOT error and does NOT return 0 bytes: the
    # scope hands back exactly one point per request, which walks the caller
    # forward 2 bytes at a time for hours.
    return int(float(t.query(":WAVeform:STOP?")))


def read_all(t: Transport, points: int, width: int, chunk_points: int):
    """Read `points` samples in chunks. Returns (nbytes, elapsed_seconds)."""
    got = 0
    start = 1
    t0 = time.perf_counter()
    while start <= points:
        stop = min(start + chunk_points - 1, points)
        want = stop - start + 1
        t.write(f":WAVeform:STARt {start}")
        t.write(f":WAVeform:STOP {stop}")
        data = t.read_block(":WAVeform:DATA?")
        got += len(data)
        n = len(data) // width
        start += n
        # A short read means we hit the end of the stored record. Past the end
        # the scope returns one point per request rather than an error or an
        # empty block, so a bare `n == 0` guard never fires and the loop would
        # crawl forward 2 bytes at a time.
        if n < want:
            break
    dt = time.perf_counter() - t0
    return got, dt


def bench_one(t: Transport, depth: int, fmt: str, chunk_points: int,
              repeats: int, warmup: bool = True) -> Result:
    width = 2 if fmt.upper() == "WORD" else 1
    # setup_readout returns the true stored depth (:WAV:POIN? is unreliable)
    points = setup_readout(t, depth, fmt)
    r = Result(t.name, depth, fmt.upper(), points, points * width)
    if warmup:
        read_all(t, points, width, chunk_points)
    for _ in range(repeats):
        nbytes, dt = read_all(t, points, width, chunk_points)
        r.nbytes = nbytes
        r.times.append(dt)
    return r


def fit_c_R(results: list[Result]):
    """Least-squares fit T = c + N/R over (bytes, best_time)."""
    xs = [r.nbytes for r in results]
    ys = [r.best for r in results]
    n = len(xs)
    if n < 2:
        return None
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom          # seconds per byte
    intercept = (sy - slope * sx) / n            # seconds
    R = (1.0 / slope / 1e6) if slope > 0 else float("inf")
    return intercept, R                          # (fixed seconds, MB/s ceiling)


DEFAULT_SWEEP = [1_000, 10_000, 100_000, 1_000_000, 10_000_000]


# --------------------------------------------------------------------------
# Diagnostic: recv-timeline decomposition (LAN only)
#
# Splits a readout into (a) time-to-first-byte -- the device preparing the
# record before it streams anything -- and (b) the streaming phase, including
# mid-stream stalls where the device pauses to marshal the next batch. This is
# what localizes the bottleneck to on-device data preparation vs the wire.
# --------------------------------------------------------------------------

def timeline(depth: int, fmt: str, source: int = 1, stall_ms: float = 20.0):
    width = 2 if fmt.upper() == "WORD" else 1
    t = LANTransport()
    points = setup_readout(t, depth, fmt, source)
    target = points * width
    t.write(f":WAVeform:STARt 1")
    t.write(f":WAVeform:STOP {points}")
    t.write(":WAVeform:DATA?")
    sock = t.sock
    t0 = time.perf_counter()
    got = 0
    first = None
    last = None
    stalls = []  # (byte_offset, gap_ms)
    while got < target:
        d = sock.recv(1 << 16)
        now = time.perf_counter() - t0
        if not d:
            break
        if first is None:
            first = now
        else:
            gap = now - last
            if gap * 1000 >= stall_ms:
                stalls.append((got, gap * 1000))
        last = now
        got += len(d)
    total = time.perf_counter() - t0
    t.close()

    stream_time = total - first
    stall_sum = sum(g for _, g in stalls) / 1000
    active = stream_time - stall_sum
    print(f"# recv timeline  {fmt} {points:,} pts ({target/1e6:.2f} MB) over LAN\n")
    print(f"  time-to-first-byte (device prep): {first*1000:8.1f} ms  "
          f"({first/total*100:.0f}% of total)")
    print(f"  streaming phase                 : {stream_time*1000:8.1f} ms")
    print(f"    - mid-stream stalls (>{stall_ms:g}ms): {stall_sum*1000:8.1f} ms "
          f"in {len(stalls)} gaps")
    print(f"    - active transfer               : {active*1000:8.1f} ms "
          f"-> {(target-65536)/active/1e6:.2f} MB/s on the wire")
    print(f"  total                           : {total*1000:8.1f} ms  "
          f"-> {target/total/1e6:.2f} MB/s overall")
    idle = first + stall_sum
    print(f"\n  wire idle waiting on device: {idle*1000:.0f} ms "
          f"({idle/total*100:.0f}% of the read)")
    if stalls:
        print("  stall locations:",
              ", ".join(f"{o//1000}k:{g:.0f}ms" for o, g in stalls))


# --------------------------------------------------------------------------
# Diagnostic: HTTP transport ceiling (same TCP/Wi-Fi path, no acquisition)
# --------------------------------------------------------------------------

def http_ceiling(path: str = "/res/header.jpg", iters: int = 20,
                 host: str = LAN_HOST):
    sock = socket.create_connection((host, 80), timeout=15)
    sock.settimeout(15)

    def get():
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                     f"Connection: keep-alive\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += sock.recv(4096)
        head, rest = buf.split(b"\r\n\r\n", 1)
        clen = int(next(l.split(b":")[1] for l in head.split(b"\r\n")
                        if l.lower().startswith(b"content-length")))
        body = rest
        while len(body) < clen:
            body += sock.recv(65536)
        return len(body)

    get()  # warmup
    t0 = time.perf_counter()
    total = sum(get() for _ in range(iters))
    dt = time.perf_counter() - t0
    sock.close()
    print(f"# HTTP ceiling  {path} x{iters}  (same TCP/Wi-Fi path as SCPI)")
    print(f"  {total/1e6:.2f} MB in {dt:.3f}s -> {total/dt/1e6:.2f} MB/s")
    print("  (this is the transport's floor; SCPI readout below this "
          "means the wire is not the bottleneck)")


def run(kinds, depths, fmt, chunk_points, repeats):
    print(f"# fmt={fmt}  chunk={chunk_points} pts  repeats={repeats}  "
          f"usb={USB_DEV}  lan={LAN_HOST}:{LAN_PORT}\n")
    hdr = f"{'transport':9} {'points':>11} {'MB':>8} {'best_s':>9} " \
          f"{'med_s':>9} {'MB/s_best':>10} {'MB/s_med':>9}"
    print(hdr); print("-" * len(hdr))
    by_kind: dict[str, list[Result]] = {k: [] for k in kinds}
    for depth in depths:
        for kind in kinds:
            try:
                with make_transport(kind) as t:
                    r = bench_one(t, depth, fmt, chunk_points, repeats)
                by_kind[kind].append(r)
                print(f"{r.transport:9} {r.points:>11,} {r.nbytes/1e6:>8.2f} "
                      f"{r.best:>9.4f} {r.median:>9.4f} "
                      f"{r.mbps_best:>10.2f} {r.mbps_med:>9.2f}")
            except Exception as e:
                print(f"{kind:9} {depth:>11,}  ERROR: {e}")
        sys.stdout.flush()
    print()
    for kind in kinds:
        fit = fit_c_R(by_kind[kind])
        if fit:
            c, R = fit
            print(f"# {kind}: fixed overhead c = {c*1e3:.2f} ms, "
                  f"throughput ceiling R = {R:.1f} MB/s")


def main(argv=None):
    ap = argparse.ArgumentParser(description="MHO934 readout transport benchmark")
    ap.add_argument("--transport", choices=["lan", "usb", "both"], default="both")
    ap.add_argument("--depth", type=int, help="single depth (else --sweep)")
    ap.add_argument("--sweep", action="store_true", help="sweep default depths")
    ap.add_argument("--depths", type=str, help="comma-separated depths")
    ap.add_argument("--format", default="WORD", choices=["WORD", "BYTE"])
    ap.add_argument("--chunk", type=int, default=1_000_000, help="points/chunk")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--timeline", action="store_true",
                    help="recv-timeline decomposition of one read (LAN)")
    ap.add_argument("--http", action="store_true",
                    help="measure HTTP transport ceiling (same wire)")
    args = ap.parse_args(argv)

    if args.http:
        http_ceiling()
        return 0
    if args.timeline:
        timeline(args.depth or 1_000_000, args.format)
        return 0

    kinds = ["lan", "usb"] if args.transport == "both" else [args.transport]
    if args.depths:
        depths = [int(x) for x in args.depths.split(",")]
    elif args.depth:
        depths = [args.depth]
    else:
        depths = DEFAULT_SWEEP
    run(kinds, depths, args.format, args.chunk, args.repeats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
