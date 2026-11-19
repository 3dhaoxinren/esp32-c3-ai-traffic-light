#pragma once
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define HOST_QA 1
struct __FlashStringHelper{};
#define F(s) reinterpret_cast<const __FlashStringHelper*>(s)
#define LOW 0
#define INPUT_PULLUP 2
#define SERIAL_8N1 6
extern uint32_t fakeClock;extern bool keyDown;extern unsigned int capturedPin,baud,rxPin,txPin;
inline uint32_t millis(){return fakeClock;}
inline void pinMode(int n,int mode){if(mode==INPUT_PULLUP)capturedPin=n;}
inline int digitalRead(int){return !keyDown;}
inline void delay(unsigned long n){fakeClock+=n;}
struct USBMock{
 char input[2048]={},out[8192]={};int rd=0,wr=0;
 void feed(const char* s){rd=0;snprintf(input,sizeof input,"%s",s);wr=strlen(input);out[0]=0;}
 void begin(int){} void setTxTimeoutMs(int){} void setRxBufferSize(int){}
 bool available(){return rd<wr;} int read(){return uint8_t(input[rd++]);}
 void print(const char* s){strncat(out,s,sizeof(out)-strlen(out)-1);}
 void print(char* s){print(static_cast<const char*>(s));}
 void print(const __FlashStringHelper* s){print(reinterpret_cast<const char*>(s));}
 void print(char x){char b[]={x,0};print(static_cast<const char*>(b));}
 template<class T>void print(T v){char b[48];snprintf(b,sizeof b,"%lld",(long long)v);print(static_cast<const char*>(b));}
 void printf(const char *fmt, ...){char tmp[340];va_list ap;va_start(ap,fmt);vsnprintf(tmp,sizeof(tmp),fmt,ap);va_end(ap);print(tmp);}
 void println(){print("\n");} template<class T>void println(T v){print(v);println();}
};
using Print=USBMock;
struct UARTMock{
 uint8_t log[1024]={};size_t used=0;
 void begin(int b,int,int r,int t){baud=b;rxPin=r;txPin=t;}
 size_t write(const uint8_t* b,size_t n){if(used+n>sizeof log)abort();memcpy(log+used,b,n);used+=n;return n;}
};
extern USBMock Serial;extern UARTMock Serial1;
inline char *hostToken(char* s,const char* delim,char** state){
 if(!s)s=*state;if(!s)return nullptr;
 s+=strspn(s,delim);if(!*s){*state=s;return nullptr;}
 char *p=s+strcspn(s,delim);if(*p)*p++=0;*state=p;return s;
}
#define strtok_r hostToken
