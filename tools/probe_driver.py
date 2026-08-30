import frida, time, socket
dev = frida.get_usb_device(timeout=10)
session = dev.attach(1199)
script = session.create_script(open("probe.js").read())
errs = []
script.on('message', lambda m, d: errs.append(m) if m.get("type") == "error" else None)
script.load()
time.sleep(0.5)
if errs:
    print("probe error:", errs); session.detach(); raise SystemExit(1)

# one 1M WORD read, counting only during the read
s = socket.create_connection(("172.30.188.217", 5555), timeout=40); s.settimeout(40)
def w(c): s.sendall(c.encode() + b"\n")
def q(c):
    w(c); b = bytearray()
    while not b.endswith(b"\n"): b += s.recv(4096)
    return b.decode().strip()
def rblk():
    b = bytearray()
    def need(n):
        while len(b) < n: b.extend(s.recv(min(1 << 20, n - len(b))))
    need(2); nd = int(b[1:2]); need(2 + nd); ln = int(b[2:2 + nd]); need(2 + nd + ln); s.recv(1); return ln
w(":TRIG:SWEep AUTO"); w(":STOP"); w(":TIM:MAIN:SCAL 5e-3"); w(":ACQ:MDEP 1000000")
w(":RUN"); time.sleep(1.2); w(":STOP"); time.sleep(0.2)
w(":WAV:SOUR CHAN1"); w(":WAV:MODE RAW"); w(":WAV:FORM WORD"); q("*OPC?")
script.exports_sync.reset()
t0 = time.perf_counter(); w(":WAV:STAR 1"); w(":WAV:STOP 1000000"); w(":WAV:DATA?"); ln = rblk(); dt = time.perf_counter() - t0
c = script.exports_sync.report()
print(f"one 1M WORD read: {ln} bytes in {dt:.3f}s")
print("call counts DURING the read (per 1,000,000 samples / 2,000,000 bytes):")
for k, v in c.items(): print(f"  {v:>10,}  {k}")
s.close(); session.detach()
