#pragma once
#include <stdint.h>
namespace wire {
constexpr uint8_t B=5,C=6,D=7, BUTTON=10, UART_TX=3;
constexpr uint8_t VOLUME=5;
constexpr uint32_t MASK=(1UL<<B)|(1UL<<C)|(1UL<<D);
struct Pair { uint8_t hi,lo; };
constexpr Pair OFFPAIR{0,0};
struct Frame { Pair center,side; uint8_t level; };
inline Frame frame(uint8_t color,uint8_t level){
  switch(color){
    case 1: return {{D,C},{C,B},level}; // center red + side red
    case 2: return {{C,D},OFFPAIR,level}; // center yellow only
    case 3: return {{D,B},{B,C},level}; // center green + side green
    default: return {OFFPAIR,OFFPAIR,0};
  }
}
// Per-phase pulse-density, not independent red/green outputs. At full light
// each pair repeats at 2 kHz for a nominal 250 us slot.
struct Scan {
  uint8_t phase=0; uint16_t accum[2]={0,0};
  Pair next(Frame f) {
    phase^=1;
    uint8_t p=f.side.hi ? phase:0;
    Pair r=p?f.side:f.center;
    if(!f.level || !r.hi) return OFFPAIR;
    accum[p]+=f.level;
    if(accum[p]<255) return OFFPAIR;
    accum[p]-=255; return r;
  }
};
struct AudioCommand {uint8_t id; uint16_t arg;};
inline void packet(AudioCommand c,uint8_t b[10]){
  b[0]=0x7e;b[1]=0xff;b[2]=6;b[3]=c.id;b[4]=0;
  b[5]=uint8_t(c.arg>>8);b[6]=uint8_t(c.arg);
  uint16_t sum=uint16_t(0-(b[1]+b[2]+b[3]+b[4]+b[5]+b[6]));
  b[7]=uint8_t(sum>>8);b[8]=uint8_t(sum);b[9]=0xef;
}
struct Audio {
  bool sample=false, stable=false;
  uint16_t pending=0, lastButton=0, lastSent=0;
  uint8_t buttonCount=3; bool manualPending=false;
  uint32_t rng=0x791b31u;
  void seed(uint32_t v){rng=v?v:0x791b31u;}
  uint32_t randomWord(){rng^=rng<<13;rng^=rng>>17;rng^=rng<<5;return rng;}
  void configure(uint8_t n){buttonCount=n;lastButton=0;if(!manualPending && pending>1)pending=0;}
  void requestButton(){
    if(!buttonCount || manualPending || pending==1)return;
    uint16_t t;
    if(buttonCount==1)t=2;
    else if(lastButton>=2 && lastButton<=1+buttonCount) {
      t=2+randomWord()%(buttonCount-1);if(t>=lastButton)++t;
    }else t=2+randomWord()%buttonCount;
    pending=t;
  }
  uint8_t stage=0;
  uint32_t edgeAt=0,stageAt=0,lastPlay=0,presses=0,plays=0;
  void begin(uint32_t now,bool down){sample=stable=down;edgeAt=stageAt=now;}
  void request(){pending=1;manualPending=true;}
  void requestTrack(uint16_t n){pending=n;manualPending=true;}
  AudioCommand tick(uint32_t now,bool down){
    if(down!=sample){sample=down;edgeAt=now;}
    if(sample!=stable && uint32_t(now-edgeAt)>=35){
      stable=sample;
      if(stable){++presses;requestButton();}
    }
    if(stage==0 && uint32_t(now-stageAt)>=3000){
      ++stage;stageAt=now;return {0x09,2}; // TF, NO ACK
    }
    if(stage==1 && uint32_t(now-stageAt)>=500){
      ++stage;stageAt=now;return {0x06,VOLUME};
    }
    if(stage==2 && uint32_t(now-stageAt)>=200) ++stage;
    if(stage==3 && pending && (!plays || uint32_t(now-lastPlay)>=160)){
      lastSent=pending;pending=0;if(!manualPending && lastSent>1)lastButton=lastSent;manualPending=false;lastPlay=now;++plays;return {0x12,lastSent}; // /mp3/0001.mp3, not file copy order
    }
    return {0,0};
  }
};
}
