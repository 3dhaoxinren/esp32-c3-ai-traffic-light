#pragma once
// Station only. Credentials go from the physical USB port to NVS, never over TCP.
#include "LanConfig.h"
#ifndef HOST_QA
#include <WiFi.h>
#include <Preferences.h>
#include <lwip/sockets.h>
#include <errno.h>
#endif
namespace toyLan {
constexpr uint16_t PORT=8080;
static LanProfile profile{};
#ifndef HOST_QA
static WiFiServer server(PORT,2);
static IPAddress listeningIP;
static uint32_t nextAttempt=0;
static bool pendingStart=false;
class Tx : public Print {
 public:
  char bytes[320]; size_t used=0,pos=0; bool bad=false;
  void reset(){used=pos=0;bad=false;}
  size_t write(uint8_t b) override {return write(&b,1);}
  size_t write(const uint8_t *p,size_t n) override {
    if(n>sizeof(bytes)-used){bad=true;return 0;}
    memcpy(bytes+used,p,n);used+=n;return n;
  }
};
struct Peer { WiFiClient socket; LineBuffer<576> input{}; Tx tx; uint32_t txSince=0; };
static Peer peers[2];
static void drop(Peer &p){p.socket.stop();p.input={};p.tx.reset();}
static void closeAll(){for(auto &p:peers)drop(p);if(server)server.end();listeningIP=IPAddress();}
static bool ready(){return WiFi.status()==WL_CONNECTED && (uint32_t)WiFi.localIP()!=0 && bool(server);}
#else
static bool ready(){return false;}
#endif
static void begin(){
#ifndef HOST_QA
  WiFi.persistent(false);WiFi.setAutoReconnect(false);WiFi.setHostname("ai-lamp-c3");
  Preferences p;if(p.begin("toylan",true)){
    if(p.getBytesLength("profile")==sizeof(profile))p.getBytes("profile",&profile,sizeof(profile));
    p.end();
  }
  if(!lanCfg::valid(profile))lanCfg::wipe(&profile,sizeof profile);
  pendingStart=profile.ssid[0];
#endif
}
static bool save(const LanProfile &p){
  if(!memcmp(&profile,&p,sizeof p))return true;
#ifndef HOST_QA
  Preferences prefs;if(!prefs.begin("toylan",false))return false;
  bool ok=prefs.putBytes("profile",&p,sizeof p)==sizeof p;prefs.end();if(!ok)return false;
  closeAll();WiFi.disconnect(false,false);pendingStart=true;
#endif
  profile=p;return true;
}
static bool forget(){
#ifndef HOST_QA
  Preferences prefs;if(!prefs.begin("toylan",false))return false;
  bool ok=!prefs.isKey("profile")||prefs.remove("profile");prefs.end();if(!ok)return false;
  closeAll();WiFi.disconnect(true,false);pendingStart=false;
#endif
  lanCfg::wipe(&profile,sizeof profile);return true;
}
static void status(){
  reply->print(F("OK NET fw=1.2.0 state="));reply->print(!profile.ssid[0]?F("UNCONFIGURED"):ready()?F("ONLINE"):F("JOINING"));
  reply->print(F(" ip="));
#ifndef HOST_QA
  if(ready())reply->print(WiFi.localIP());else
#endif
  reply->print(F("0.0.0.0"));
  reply->println(F(" port=8080"));
}
static bool command(const char *line,bool usb){
  if(strncmp(line,"NET ",4)!=0)return false;
  if(!usb){reply->println(F("ERR NET USB only"));return true;}
  if(!strcmp(line,"NET STATUS")){status();return true;}
  if(!strcmp(line,"NET ON")){
    // Compatible with the universal PC client's existing "prepare Wi-Fi" button.
    if(ready()){
      reply->print(F("NET READY: C3 TCP "));
#ifndef HOST_QA
      reply->print(WiFi.localIP());
#endif
      reply->println(F(":8080"));
    }else reply->println(profile.ssid[0]?F("NET unconfirmed: C3 joining WiFi; query NET STATUS"):F("NET unconfirmed: C3 needs USB WiFi setup"));
    return true;
  }
  if(!strcmp(line,"NET FORGET")){reply->println(forget()?F("OK NET forgotten"):F("ERR NET save"));return true;}
  if(!strncmp(line,"NET SET ",8)){
    LanProfile next{};bool valid=lanCfg::parse(line+8,next);
    if(!valid) reply->println(F("ERR NET invalid profile"));
    else reply->println(save(next)?F("OK NET saved"):F("ERR NET save"));
    lanCfg::wipe(&next,sizeof next);return true;
  }
  reply->println(F("ERR NET use USB setup or NET STATUS"));return true;
}
static void tick(){
#ifndef HOST_QA
  if(!profile.ssid[0])return;
  uint32_t now=millis();
  if(pendingStart || WiFi.status()!=WL_CONNECTED || !(uint32_t)WiFi.localIP()){
    if(server)closeAll();
    if(pendingStart || int32_t(now-nextAttempt)>=0){
      pendingStart=false;nextAttempt=now+20000u;
      WiFi.begin(profile.ssid,profile.secret);
    }
    return;
  }
  if(!server || listeningIP!=WiFi.localIP()){
    closeAll();server.begin();server.setNoDelay(true);listeningIP=WiFi.localIP();
    if(!server)return;
  }
  auto fresh=server.accept();
  if(fresh){
    // Raw lamp protocol is for the same trusted subnet (no port forwarding).
    uint32_t mask=uint32_t(WiFi.subnetMask());
    bool allowed=mask && ((uint32_t(fresh.remoteIP())&mask)==(uint32_t(listeningIP)&mask));
    bool kept=false;if(allowed)for(auto &p:peers)if(!p.socket.connected()){
      drop(p);p.socket=fresh;p.socket.setNoDelay(true);kept=true;break;
    }
    if(!kept)fresh.stop();
  }
  for(auto &p:peers){
    if(!p.socket.connected())continue;
    if(p.tx.pos<p.tx.used){
      // Never let a stalled TCP reader block USB, the button, or the LED lease.
      int n=::send(p.socket.fd(),p.tx.bytes+p.tx.pos,p.tx.used-p.tx.pos,MSG_DONTWAIT);
      if(n>0){p.tx.pos+=size_t(n);if(p.tx.pos==p.tx.used)p.tx.reset();}
      else if(n==0 || (errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) || uint32_t(now-p.txSince)>2000)drop(p);
      continue;
    }
    drain(p.socket,p.input,p.tx,false);
    if(p.tx.bad)drop(p); else if(p.tx.used)p.txSince=now;
  }
#endif
}
}
