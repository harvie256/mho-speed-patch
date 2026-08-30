'use strict';
const mod = Process.getModuleByName('libscope-auklet.so');
const targets = {
  'append(char*,int) 0x242af0': 0x242af0,
  'append(char)     0x42f4fc': 0x42f4fc,
  'toWord           0x631a7c': 0x631a7c,
  'push_back-ctor   0x243930': 0x243930,
};
const counts = {};
for (const [name, off] of Object.entries(targets)) {
  counts[name] = 0;
  Interceptor.attach(mod.base.add(off), { onEnter: function () { counts[name]++; } });
}
rpc.exports = {
  reset: function () { for (const k in counts) counts[k] = 0; },
  report: function () { return counts; },
};
console.log('[probe] counters attached');
