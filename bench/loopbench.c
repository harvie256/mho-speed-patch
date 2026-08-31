/* loopbench -- on-scope loopback SCPI readout benchmark for Rigol MHO/DHO.
 *
 * Runs ON the scope, connecting to 127.0.0.1:5555, so the network wire is
 * removed and what remains is the pure on-device production rate (the thing
 * the mho-speed-patch targets). Mirrors bench.py's setup_readout/read_all.
 *
 * Usage: loopbench [depth] [BYTE|WORD] [chunk_points] [repeats]
 *   defaults: 1000000 WORD 250000 3
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>

static int sock_fd = -1;

static double now(void){ struct timespec ts; clock_gettime(CLOCK_MONOTONIC,&ts);
    return ts.tv_sec + ts.tv_nsec/1e9; }

static void die(const char*m){ perror(m); exit(1); }

static void send_cmd(const char*cmd){
    char buf[256]; int n=snprintf(buf,sizeof buf,"%s\n",cmd);
    if(write(sock_fd,buf,n)!=n) die("write");
}

/* read exactly n bytes */
static int recv_exact(unsigned char*p,int n){
    int got=0; while(got<n){ int r=read(sock_fd,p+got,n-got);
        if(r<=0) return got; got+=r; } return got;
}

/* query: send cmd, read one line (until \n), strip newline */
static void query(const char*cmd,char*out,int outsz){
    send_cmd(cmd); int i=0; char c;
    while(i<outsz-1){ int r=read(sock_fd,&c,1); if(r<=0) break;
        if(c=='\n') break; out[i++]=c; }
    out[i]=0;
}

/* read an IEEE 488.2 definite-length block payload; returns byte count */
static long read_block(const char*cmd){
    send_cmd(cmd);
    unsigned char h; if(recv_exact(&h,1)!=1||h!='#'){fprintf(stderr,"no block hdr\n");exit(1);}
    unsigned char nd; recv_exact(&nd,1); int ndig=nd-'0';
    unsigned char lenb[16]; recv_exact(lenb,ndig); lenb[ndig]=0;
    long len=atol((char*)lenb);
    /* drain payload into a reusable buffer */
    static unsigned char*buf=NULL; static long bufsz=0;
    if(len>bufsz){ free(buf); buf=malloc(len); bufsz=len; if(!buf) die("malloc"); }
    long got=recv_exact(buf,len);
    unsigned char nl; recv_exact(&nl,1); /* trailing newline */
    return got;
}

static double tb_for_depth(long d){
    /* MDEPth AUTO derives depth from window x srate (max 1 GSa/s single ch):
       points = 10*(s/div)*srate  =>  s/div = d/(10*1e9) = d*1e-10.
       Floor at 1e-6 s/div. */
    double tb = d*1e-10; if(tb<1e-6) tb=1e-6; return tb;
}

int main(int argc,char**argv){
    long depth = argc>1?atol(argv[1]):1000000;
    const char*fmt = argc>2?argv[2]:"WORD";
    long chunk = argc>3?atol(argv[3]):250000;
    int repeats = argc>4?atoi(argv[4]):3;
    int width = (strcasecmp(fmt,"WORD")==0)?2:1;
    setvbuf(stdout,NULL,_IONBF,0);

    struct sockaddr_in a; memset(&a,0,sizeof a);
    a.sin_family=AF_INET; a.sin_port=htons(5555);
    inet_pton(AF_INET,"127.0.0.1",&a.sin_addr);
    sock_fd=socket(AF_INET,SOCK_STREAM,0); if(sock_fd<0) die("socket");
    if(connect(sock_fd,(struct sockaddr*)&a,sizeof a)<0) die("connect");
    int one=1; setsockopt(sock_fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof one);

    char resp[128];
    query("*IDN?",resp,sizeof resp);
    fprintf(stderr,"IDN: %s\n",resp);

    /* setup_readout */
    double tb=tb_for_depth(depth);
    char c[128];
    send_cmd(":TRIGger:SWEep AUTO");
    send_cmd(":STOP");
    snprintf(c,sizeof c,":TIMebase:MAIN:SCALe %g",tb); send_cmd(c);
    send_cmd(":ACQuire:MDEPth AUTO");
    send_cmd(":RUN");
    double window=10*tb; double wsleep=window+0.8; if(wsleep<1.2)wsleep=1.2; if(wsleep>6.0)wsleep=6.0;
    usleep((useconds_t)(wsleep*1e6));
    send_cmd(":STOP"); usleep(200000);
    send_cmd(":WAVeform:SOURce CHANnel1");
    send_cmd(":WAVeform:MODE RAW");
    snprintf(c,sizeof c,":WAVeform:FORMat %s",fmt); send_cmd(c);
    query("*OPC?",resp,sizeof resp);
    query(":ACQuire:MDEPth?",resp,sizeof resp);
    long points=(long)atof(resp);
    fprintf(stderr,"stored points: %ld  fmt=%s width=%d chunk=%ld\n",points,fmt,width,chunk);

    /* warmup + timed repeats */
    for(int rep=-1; rep<repeats; rep++){
        long got=0, start=1; double t0=now();
        while(start<=points){
            long stop=start+chunk-1; if(stop>points) stop=points;
            snprintf(c,sizeof c,":WAVeform:STARt %ld",start); send_cmd(c);
            snprintf(c,sizeof c,":WAVeform:STOP %ld",stop); send_cmd(c);
            long n=read_block(":WAVeform:DATA?");
            got+=n; long np=n/width; if(np==0) break; start+=np;
        }
        double dt=now()-t0;
        if(rep<0) fprintf(stderr,"warmup: %ld bytes in %.3fs\n",got,dt);
        else printf("read %d: %ld bytes  %.3fs  %.2f MB/s\n",
                    rep,got,dt,got/dt/1e6);
    }
    close(sock_fd);
    return 0;
}
