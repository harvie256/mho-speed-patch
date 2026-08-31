import socket, time, sys
dur=float(sys.argv[1]) if len(sys.argv)>1 else 10
s=socket.create_connection(("172.30.188.217",5555),timeout=40); s.settimeout(40)
def w(c): s.sendall(c.encode()+b"\n")
def q(c):
    w(c); b=bytearray()
    while not b.endswith(b"\n"): b+=s.recv(4096)
    return b.decode().strip()
def rblk():
    b=bytearray()
    def need(n):
        while len(b)<n: b.extend(s.recv(min(1<<20,n-len(b))))
    need(2); nd=int(b[1:2]); need(2+nd); ln=int(b[2:2+nd]); need(2+nd+ln); s.recv(1); return ln
w(":TRIG:SWEep AUTO"); w(":STOP"); w(":TIM:MAIN:SCAL 5e-3"); w(":ACQ:MDEP 1000000")
w(":RUN"); time.sleep(1.2); w(":STOP"); time.sleep(0.2)
w(":WAV:SOUR CHAN1"); w(":WAV:MODE RAW"); w(":WAV:FORM WORD"); q("*OPC?")
end=time.time()+dur; n=0; b=0
while time.time()<end:
    w(":WAV:STAR 1"); w(":WAV:STOP 1000000"); w(":WAV:DATA?"); b+=rblk(); n+=1
print(f"did {n} reads, {b/1e6:.1f} MB in ~{dur}s = {b/1e6/dur:.2f} MB/s")
s.close()
