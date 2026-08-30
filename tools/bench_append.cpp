// Model the scope's SCPI-response buffer build for a 1 Mpt WORD read (2,000,000
// bytes), comparing the current per-byte vector append to the fixes.
#include <vector>
#include <cstring>
#include <cstdio>
#include <chrono>
using namespace std::chrono;

static const size_t N = 2'000'000;   // bytes in a 1 Mpt WORD response

template<class F> double ms(F f){ auto a=steady_clock::now(); f(); auto b=steady_clock::now();
  return duration_cast<duration<double,std::milli>>(b-a).count(); }

int main(){
  std::vector<unsigned char> src(N);
  for(size_t i=0;i<N;i++) src[i]=(unsigned char)i;
  const int REP=20;
  double tA=0,tB=0,tC=0; volatile size_t sink=0;

  // A: current -- push_back per byte, NO reserve (geometric regrow + per-elem construct)
  for(int r=0;r<REP;r++) tA+=ms([&]{ std::vector<unsigned char> v;
      for(size_t i=0;i<N;i++) v.push_back(src[i]); sink+=v.size(); });
  // B: reserve() first, then push_back per byte (removes reallocation)
  for(int r=0;r<REP;r++) tB+=ms([&]{ std::vector<unsigned char> v; v.reserve(N);
      for(size_t i=0;i<N;i++) v.push_back(src[i]); sink+=v.size(); });
  // C: bulk -- reserve + single insert/memcpy (the proper fix)
  for(int r=0;r<REP;r++) tC+=ms([&]{ std::vector<unsigned char> v; v.reserve(N);
      v.insert(v.end(), src.begin(), src.end()); sink+=v.size(); });

  auto mbps=[&](double tot){ return (double)N*REP/1e6/(tot/1000.0); };
  printf("buffer build for %zu bytes, %d reps (ratios are architecture-robust)\n\n",N,REP);
  printf("  A per-byte push_back (no reserve): %7.2f ms/build  %8.1f MB/s\n", tA/REP, mbps(tA));
  printf("  B reserve()+push_back per byte   : %7.2f ms/build  %8.1f MB/s  (%.1fx vs A)\n", tB/REP, mbps(tB), tA/tB);
  printf("  C reserve()+bulk insert/memcpy   : %7.2f ms/build  %8.1f MB/s  (%.1fx vs A)\n", tC/REP, mbps(tC), tA/tC);
  return (int)(sink&1);
}
