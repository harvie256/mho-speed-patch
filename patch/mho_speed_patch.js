'use strict';
// mho-speed-patch: speed up Rigol MHO/DHO waveform readout by replacing the
// oscilloscope's byte-by-byte SCPI response build with bulk memcpy.
//
// There are TWO per-byte stages on the WAV:DATA? path, both bottlenecked on the
// SAME primitive -- RByteArray::append(char const*, int), whose stock body copies
// one byte per loop iteration via a per-element std::vector<char> construct
// (NOT memcpy). So a "bulk" append(data, 2 MB) still costs O(2,000,000):
//
//   Stage 1  CApiWave::toWord/toByte(this, src, n)
//            builds the sample buffer with `for i: ret.append(&src[i], 1)`
//            -> 2,000,000 one-byte append() calls per 1 Mpt WORD read.
//   Stage 2  the SCPI framing copies that buffer into the response with a single
//            bulk append(data, ~2 MB) -- but the stock append loops per byte
//            internally, so it is ALSO O(bytes).
//
// This patch fixes both:
//   * toWord/toByte: each called ONCE per read -> intercept, neuter the loop
//     (n=0 on entry), bulk-fill the returned RByteArray on exit. So toWord no
//     longer calls append() 2M times.
//   * append(char*,int): intervene ONLY on large calls (>= APPEND_THRESH, i.e.
//     the framing copy). Small appends (headers, other traffic) run the stock
//     code untouched, keeping the blast radius to ~2 calls/read. For a large
//     call we neuter the stock loop and do a proper grow+memcpy ourselves.
//
// RByteArray is a std::vector<char>: +0 __begin_, +8 __end_, +16 __end_cap_.
// The returned RByteArray (toWord/toByte) arrives via the AArch64 indirect-
// result register x8; append's args are this=x0/a0, src=x1/a1, n=x2/a2.
//
// Verified on MHO934 fw 00.01.00: byte-identical output (md5 matches an
// unpatched read), stable under sustained load, ~5x on-device throughput
// (loopback: ~1.6 -> ~8.5 MB/s). Symbols are resolved by name so this survives
// minor rebuilds.
//
// SAFETY: append() is called app-wide on multiple threads. Replacing it wholesale
// (Interceptor.replace) corrupts memory and crashes the app -- do NOT do that.
// The large-only attach approach below is what was verified safe.

const APPEND_THRESH = 65536;   // only rewrite appends this size or larger

const LIB = 'libscope-auklet.so';
const mod = Process.getModuleByName(LIB);
const libc = Process.getModuleByName('libc.so');
const malloc = new NativeFunction(libc.getExportByName('malloc'), 'pointer', ['size_t']);
const free   = new NativeFunction(libc.getExportByName('free'),   'void',    ['pointer']);
const memcpy = new NativeFunction(libc.getExportByName('memcpy'), 'pointer', ['pointer', 'pointer', 'size_t']);

function resolve(sym) {
  try { return mod.getExportByName(sym); }
  catch (e) { return mod.findExportByName ? mod.findExportByName(sym) : null; }
}

// --- Stage 1: CApiWave::toWord (k=2) / toByte (k=1) ---------------------------
const TARGETS = { '_ZN8CApiWave6toWordEPhi': 2, '_ZN8CApiWave6toByteEPhi': 1 };
let patched = 0;
for (const [sym, k] of Object.entries(TARGETS)) {
  const addr = resolve(sym);
  if (!addr) { console.log('[mho-patch] symbol not found: ' + sym); continue; }
  Interceptor.attach(addr, {
    onEnter: function () {
      this.ret = this.context.x8;                   // RByteArray* (sret)
      this.src = this.context.x1;                   // source bytes
      this.nbytes = this.context.x2.toInt32() * k;  // total output bytes
      this.context.x2 = ptr(0);                     // neuter per-byte loop
    },
    onLeave: function () {
      const n = this.nbytes;
      if (n <= 0) return;
      const buf = malloc(n);
      memcpy(buf, this.src, n);
      const r = this.ret;                           // vector<char>
      r.writePointer(buf);
      r.add(8).writePointer(buf.add(n));
      r.add(16).writePointer(buf.add(n));
    }
  });
  console.log('[mho-patch] bulk-patched ' + sym + ' @ ' + addr + ' (k=' + k + ')');
  patched++;
}

// --- Stage 2: RByteArray::append(char const*, int), large calls only ----------
const apAddr = resolve('_ZN10RByteArray6appendEPKci');
if (!apAddr) {
  console.log('[mho-patch] append symbol not found; stage-2 skipped');
} else {
  Interceptor.attach(apAddr, {
    onEnter: function (args) {
      this.self = args[0];                          // RByteArray* (vector<char>)
      this.src = args[1];
      this.n = args[2].toInt32();
      this.big = this.n >= APPEND_THRESH;
      if (this.big) this.context.x2 = ptr(0);       // neuter stock per-byte loop
    },
    onLeave: function () {
      if (!this.big) return;                        // small appends ran stock
      const self = this.self, src = this.src, n = this.n;
      const begin = self.readPointer();
      const end = self.add(8).readPointer();
      const cap = self.add(16).readPointer();
      const size = begin.isNull() ? 0 : end.sub(begin).toInt32();
      const capacity = begin.isNull() ? 0 : cap.sub(begin).toInt32();
      if (size + n <= capacity) {                   // fits: append in place
        memcpy(end, src, n);
        self.add(8).writePointer(end.add(n));
      } else {                                       // grow (>= size+n, doubling)
        let newcap = size + n;
        const dbl = capacity * 2;
        if (dbl > newcap) newcap = dbl;
        const nb = malloc(newcap);
        if (size > 0) memcpy(nb, begin, size);
        memcpy(nb.add(size), src, n);
        if (!begin.isNull()) free(begin);           // libc++ alloc == malloc on bionic
        self.writePointer(nb);
        self.add(8).writePointer(nb.add(size + n));
        self.add(16).writePointer(nb.add(newcap));
      }
    }
  });
  console.log('[mho-patch] append memcpy-grow active (large-only, thresh=' + APPEND_THRESH + ')');
}

console.log('[mho-patch] ' + patched + ' converter(s) + append patched; readout built with memcpy.');
