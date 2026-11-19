#pragma once
#include <stdint.h>
#include <string.h>
// A single NVS blob: never publish or mix the two halves of a Wi-Fi profile.
struct LanProfile { char ssid[33]; char secret[65]; };
namespace lanCfg {
inline int digit(char c){
  if(c>='0'&&c<='9')return c-'0';
  if(c>='a'&&c<='f')return c-'a'+10;
  if(c>='A'&&c<='F')return c-'A'+10;
  return -1;
}
inline bool field(const char *p,size_t n,char *dst,size_t cap){
  if(n==0 || n%2 || n/2>=cap)return false;
  for(size_t i=0;i<n;i+=2){
    int a=digit(p[i]),b=digit(p[i+1]);
    if(a<0||b<0||(a==0&&b==0))return false;
    dst[i/2]=char(a*16+b);
  }
  dst[n/2]=0; return true;
}
inline bool valid(const LanProfile &p){
  if(!memchr(p.ssid,0,sizeof p.ssid)||!memchr(p.secret,0,sizeof p.secret)||!p.ssid[0])return false;
  size_t n=strlen(p.secret);
  if(n==64){for(size_t i=0;i<n;++i)if(digit(p.secret[i])<0)return false;}
  return n>=8 && n<=64;
}
inline bool parse(const char *args,LanProfile &out){
  const char *sp=strchr(args,' ');
  if(!sp || !field(args,sp-args,out.ssid,sizeof out.ssid) ||
     !field(sp+1,strlen(sp+1),out.secret,sizeof out.secret))return false;
  return valid(out);
}
inline void wipe(void *p,size_t n){ volatile uint8_t *b=(volatile uint8_t*)p;while(n--)*b++=0; }
}
