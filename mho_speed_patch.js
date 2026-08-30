'use strict';
// mho-speed-patch: speed up Rigol MHO/DHO waveform readout by replacing the
// byte-by-byte SCPI response build with a single bulk memcpy.
//
// CApiWave::toWord/toByte(this, unsigned char* src, int n) build the WAV:DATA?
// response by looping `for i in 0..(k*n): ret.append(&src[i], 1)` -- one append
// per byte (2,000,000 calls for a 1 Mpt WORD read). We intercept these
// (each called ONCE per read, so hook overhead is negligible), neuter the loop
// (set n=0 on entry) and bulk-fill the returned RByteArray on exit.
//
// RByteArray is a std::vector<char>: +0 __begin_, +8 __end_, +16 __end_cap_.
// The RByteArray to return is passed via the AArch64 indirect-result reg x8.
//
// Verified on MHO934 fw 00.01.00: byte-identical output, ~1.2x per hooked stage.
// Symbols are resolved by name so this survives minor rebuilds.

const LIB = 'libscope-auklet.so';
const mod = Process.getModuleByName(LIB);
const libc = Process.getModuleByName('libc.so');
const malloc = new NativeFunction(libc.getExportByName('malloc'), 'pointer', ['size_t']);
const memcpy = new NativeFunction(libc.getExportByName('memcpy'), 'pointer', ['pointer', 'pointer', 'size_t']);

// name -> bytes-per-sample multiplier k (toWord emits 2 bytes/sample, toByte 1)
const TARGETS = { '_ZN8CApiWave6toWordEPhi': 2, '_ZN8CApiWave6toByteEPhi': 1 };

let patched = 0;
for (const [sym, k] of Object.entries(TARGETS)) {
  let addr = null;
  try { addr = mod.getExportByName(sym); } catch (e) { addr = mod.findExportByName ? mod.findExportByName(sym) : null; }
  if (!addr) { console.log('[mho-patch] symbol not found: ' + sym); continue; }
  Interceptor.attach(addr, {
    onEnter: function () {
      this.ret = this.context.x8;                       // RByteArray* (sret)
      this.src = this.context.x1;                       // source bytes
      this.nbytes = this.context.x2.toInt32() * k;      // total output bytes
      this.context.x2 = ptr(0);                         // neuter per-byte loop
    },
    onLeave: function () {
      const n = this.nbytes;
      if (n <= 0) return;
      const buf = malloc(n);
      memcpy(buf, this.src, n);
      const r = this.ret;                               // vector<char>
      r.writePointer(buf);
      r.add(8).writePointer(buf.add(n));
      r.add(16).writePointer(buf.add(n));
    }
  });
  console.log('[mho-patch] bulk-patched ' + sym + ' @ ' + addr + ' (k=' + k + ')');
  patched++;
}
console.log('[mho-patch] ' + patched + ' function(s) patched; readout response now built with one memcpy.');
