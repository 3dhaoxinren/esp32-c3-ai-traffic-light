#pragma once
/* ESP32-C3 + MAX98357A + HY-035 | USB CDC / optional Wi-Fi | B=5 C=6 D=7 K1=10 */
#include <Arduino.h>
#include <ctype.h>
#include <string.h>
#include "ToyLedDriver.h"
#include "PcmAudio.h"
#ifndef HOST_QA
#include <Preferences.h>
#include <esp_random.h>
#endif
static wire::Audio audio;
static uint8_t readCount(){
#ifndef HOST_QA
  Preferences p;if(!p.begin("toysound",true))return 3;
  uint8_t n=p.getUChar("buttons",3);p.end();return n<=32?n:3;
#else
  return 3;
#endif
}
static bool writeCount(uint8_t n){
  if(n==audio.buttonCount)return true;
#ifndef HOST_QA
  Preferences p;if(!p.begin("toysound",false))return false;
  bool ok=p.putUChar("buttons",n)==1;p.end();if(!ok)return false;
#endif
  audio.configure(n);return true;
}
void tickAudio() {
  const auto cmd=audio.tick(millis(),digitalRead(wire::BUTTON)==LOW);
  if(cmd.id==0x12)pcm::play(cmd.arg);
  pcm::tick();
}

const uint8_t OFF = 0, RED = 1, YELLOW = 2, GREEN = 3;
const uint8_t AI_OFFLINE = 0, AI_IDLE = 1, AI_RUNNING = 2, AI_WAITING = 3, AI_ERROR = 4, AI_DONE = 5;
constexpr size_t AI_STATES = 6;
static uint16_t stateTracks[AI_STATES] = {};
const uint8_t SOLID = 0, BREATHE = 1, SLOW = 2, FAST = 3;
const unsigned long AI_LEASE_MS = 15000UL, AI_FRAME_MS = 4UL, AI_BREATH_MS = 2400UL;
uint8_t color = OFF, aiState = AI_OFFLINE, aiLevel = 0, aiColor = OFF, aiEffect = SOLID;
bool automatic = false, aiActive = false, aiCustom = false;
unsigned long phaseStarted = 0, aiStarted = 0, aiLastHeartbeat = 0, aiLastFrame = 0;

template<size_t N> struct LineBuffer {
  char data[N];
  uint16_t length;
  bool discard;
  unsigned long lastByte;
};
LineBuffer<576> usbLine = {};
#ifdef HOST_QA
using LampReply=USBMock;
#else
using LampReply=Print;
#endif
static LampReply *reply=&Serial;
static bool fromUSB=true;
static void handleLine(char *line);
template<class Link,size_t N> static void drain(Link &link,LineBuffer<N> &buf,LampReply &out,bool usb);
#include "ToyLan.h"

static int stateId(const char *s) {
  static const char *const names[AI_STATES]={"OFFLINE","IDLE","RUNNING","WAITING","ERROR","DONE"};
  for(size_t i=0;i<AI_STATES;++i) if(strcmp(s,names[i])==0)return int(i);
  return -1;
}
static bool saveTrack(uint8_t state,uint16_t no) {
  if(state>=AI_STATES||no>3000)return false;
  if(stateTracks[state]==no)return true;
#ifndef HOST_QA
  uint16_t next[AI_STATES]; memcpy(next,stateTracks,sizeof next); next[state]=no;
  Preferences p;
  if(!p.begin("aivoice",false))return false;
  const bool ok=p.putBytes("tracks",next,sizeof next)==sizeof next;p.end();
  if(!ok)return false;
#endif
  stateTracks[state]=no;return true;
}
static void readTracks() {
#ifndef HOST_QA
  Preferences p;
  if(!p.begin("aivoice",true))return;
  if(p.getBytesLength("tracks")==sizeof stateTracks)
    if(p.getBytes("tracks",stateTracks,sizeof stateTracks)!=sizeof stateTracks)
      memset(stateTracks,0,sizeof stateTracks);
  p.end();
  for(auto &n:stateTracks)if(n>3000)n=0;
#endif
}
static void speakState(uint8_t state) {
  if(state<AI_STATES && stateTracks[state])audio.requestTrack(stateTracks[state]);
}

void setColor(uint8_t next) {
  color = next;
  aiLevel = next == OFF ? 0 : 255;
  phaseStarted = millis();
  toyRequest(next, aiLevel, true);
}

void printBits() {
  reply->print(F(" R=")); reply->print(color == RED ? 1 : 0);
  reply->print(F(" Y=")); reply->print(color == YELLOW ? 1 : 0);
  reply->print(F(" G=")); reply->print(color == GREEN ? 1 : 0);
}

void printColor(uint8_t value) {
  if (value == RED) reply->print(F("RED"));
  else if (value == YELLOW) reply->print(F("YELLOW"));
  else if (value == GREEN) reply->print(F("GREEN"));
  else reply->print(F("OFF"));
}

void printState() {
  if (aiActive) {
    reply->print(F("OK AI state="));
    if (aiState == AI_IDLE) reply->print(F("IDLE"));
    else if (aiState == AI_RUNNING) reply->print(F("RUNNING"));
    else if (aiState == AI_WAITING) reply->print(F("WAITING"));
    else if (aiState == AI_ERROR) reply->print(F("ERROR"));
    else if (aiState == AI_DONE) reply->print(F("DONE"));
    else reply->print(F("OFFLINE"));
    printBits();
    reply->print(F(" level=")); reply->print(aiLevel);
    reply->print(F(" lease=")); reply->print(AI_LEASE_MS);
    if (aiCustom) {
      reply->print(F(" color=")); printColor(aiColor);
      reply->print(F(" effect="));
      if (aiEffect == BREATHE) reply->print(F("BREATHE"));
      else if (aiEffect == SLOW) reply->print(F("SLOW"));
      else if (aiEffect == FAST) reply->print(F("FAST"));
      else reply->print(F("SOLID"));
    }
  } else {
    reply->print(F("OK LIGHT mode=")); reply->print(automatic ? F("AUTO") : F("MANUAL"));
    reply->print(F(" color=")); printColor(color); printBits();
  }
  reply->println();
}

uint8_t breathingLevel(unsigned long elapsed) {
  unsigned long ramp = elapsed % AI_BREATH_MS;
  if (ramp > AI_BREATH_MS / 2) ramp = AI_BREATH_MS - ramp;
  uint32_t x = ramp * 1024UL / (AI_BREATH_MS / 2);
  uint32_t eased = (x * x * (3072UL - 2UL * x) + 524288UL) >> 20;
  return 20 + ((235UL * eased * eased + 524288UL) >> 20);
}

void applyAiOutput(uint8_t next, uint8_t level) {
  color = next;
  aiLevel = level;
  toyRequest(next, level, false);
}

void updateAiPattern() {
  if (!aiActive) return;
  const unsigned long now = millis();
  if (aiState != AI_OFFLINE && now - aiLastHeartbeat >= AI_LEASE_MS) {
    aiState = AI_OFFLINE; aiColor = OFF; aiEffect = SOLID;
    aiStarted = now;
    speakState(AI_OFFLINE);
  }
  if (now - aiLastFrame < AI_FRAME_MS) return;
  aiLastFrame = now;
  const unsigned long elapsed = now - aiStarted;
  uint8_t next = OFF, level = 0;
  if (aiState != AI_OFFLINE && aiColor != OFF) {
    next = aiColor;
    if (aiEffect == BREATHE) level = breathingLevel(elapsed);
    else if (aiEffect == SLOW) level = elapsed % 1000UL < 500UL ? 255 : 0;
    else if (aiEffect == FAST) level = elapsed % 400UL < 200UL ? 255 : 0;
    else level = 255;
    if (!level) next = OFF;
  }
  if (next != color || level != aiLevel) applyAiOutput(next, level);
}

void setAiState(uint8_t next, uint8_t nextColor, uint8_t effect, bool custom) {
  const unsigned long now = millis();
  const bool entered = !aiActive || aiState != next;
  if (entered || aiColor != nextColor || aiEffect != effect) {
    aiStarted = now;
    aiLastFrame = now - AI_FRAME_MS;
  }
  aiActive = true; automatic = false;
  aiState = next; aiColor = nextColor; aiEffect = effect; aiCustom = custom;
  aiLastHeartbeat = now;
  toyRefreshLease(next != AI_OFFLINE);
  updateAiPattern();
  if(entered)speakState(next);
}

void error(const __FlashStringHelper *message) { reply->println(message); }

static void handleLine(char *line) {
  while (*line == ' ' || *line == '\t') ++line;
  size_t n = strlen(line);
  while (n && (line[n-1] == ' ' || line[n-1] == '\t')) line[--n] = 0;
  if (!n) return;
  for (char *p = line; *p; ++p) *p = toupper((unsigned char)*p);
  if(pcm::cmd(line,fromUSB,*reply))return;
  if(toyLan::command(line,fromUSB))return;
  if(strcmp(line,"SOUND STATE STATUS")==0){
    reply->printf("OK SOUND STATE offline=%u idle=%u running=%u waiting=%u error=%u done=%u\n",
      unsigned(stateTracks[0]),unsigned(stateTracks[1]),unsigned(stateTracks[2]),unsigned(stateTracks[3]),unsigned(stateTracks[4]),unsigned(stateTracks[5]));
    return;
  }
  if(strncmp(line,"SOUND STATE ",12)==0){
    char *rest=nullptr;
    char *name=strtok_r(line+12," ",&rest),*track=strtok_r(nullptr," ",&rest);
    const int state=name?stateId(name):-1;
    uint32_t no=0;
    if(state<0||!track||strtok_r(nullptr," ",&rest)||!pcm::number(track,10,no)||no>3000)
      reply->println(F("ERR SOUND STATE expected state and track 0..3000"));
    else if(!saveTrack(uint8_t(state),uint16_t(no)))reply->println(F("ERR SOUND STATE save failed"));
    else reply->printf("OK SOUND STATE track=%u\n",unsigned(no));
    return;
  }
  if(strncmp(line,"SOUND PLAY ",11)==0){
    const char *arg=line+11;size_t n=strlen(arg);
    if(!n||n>4||strspn(arg,"0123456789")!=n||atoi(arg)<1||atoi(arg)>3000){reply->println(F("ERR SOUND track 1..3000"));return;}
    unsigned track=unsigned(atoi(arg));audio.requestTrack(uint16_t(track));
    char number[5];snprintf(number,sizeof number,"%04u",track);
    reply->print(F("OK SOUND queued track="));reply->println(number);return;
  }
  if(strncmp(line,"SOUND COUNT ",12)==0){
    const char *t=line+12;size_t len=strlen(t);
    if(!len||len>2||strspn(t,"0123456789")!=len||atoi(t)>32){reply->println(F("ERR SOUND count 0..32"));return;}
    const uint8_t count=atoi(t);if(!writeCount(count)){reply->println(F("ERR SOUND save failed"));return;}
    reply->print(F("OK SOUND count="));reply->println(unsigned(count));return;
  }
  if(strcmp(line,"SOUND FINISH")==0 || strcmp(line,"SOUND TEST")==0){audio.request();reply->println(F("OK SOUND queued track=0001"));return;}
  if(strcmp(line,"SOUND STATUS")==0){
    reply->print(F("OK SOUND stage="));reply->print(unsigned(audio.stage));
    reply->print(F(" presses="));reply->print(audio.presses);
    reply->print(F(" commands_sent="));reply->print(audio.plays);
    reply->print(F(" count="));reply->print(unsigned(audio.buttonCount));reply->print(F(" last="));reply->print(unsigned(audio.lastSent));reply->println(F(" ack=none"));return;
  }
  if (strncmp(line, "LIGHT ", 6) != 0 && strcmp(line, "LIGHT") != 0) {
    error(F("ERR use LIGHT HELP (USB/TCP; no AT/SCREEN/VOICE)")); return;
  }
  char *argument = line + 5;
  while (*argument == ' ' || *argument == '\t') ++argument;
  if (strcmp(argument, "HELP") == 0) {
    reply->println(F("HELP LIGHT RED|YELLOW|GREEN|OFF|AUTO|STATUS; AI IDLE|RUNNING|WAITING|ERROR|DONE|OFFLINE")); return;
  }
  if (strcmp(argument, "STATUS") == 0) {
    reply->print(fromUSB?F("CAPS TOY_SOUND_V2 count="):F("CAPS TOY_SOUND_LAN_V1 count="));reply->println(unsigned(audio.buttonCount));
    reply->println(F("CAPS TOY_PLAY_V1 max=3000"));
    updateAiPattern(); printState(); return; }
  if (strncmp(argument, "TEST ", 5) == 0) {
    if (strlen(argument) != 7 || !toyProbe(argument[5], argument[6])) {
      error(F("ERR LIGHT TEST expects two distinct nodes B/C/D")); return;
    }
    aiActive = false; automatic = false; color = OFF; aiLevel = 0;
    reply->print(F("OK TEST pair=")); reply->print(argument[5]); reply->print(argument[6]);
    reply->println(F(" timeout=2000")); return;
  }
  if (strncmp(argument, "AI ", 3) == 0) {
    char *rest;
    char *state = strtok_r(argument + 3, " \t", &rest);
    char *colorArg = strtok_r(NULL, " \t", &rest);
    char *effectArg = strtok_r(NULL, " \t", &rest);
    if (!state || (colorArg && !effectArg) || strtok_r(NULL, " \t", &rest)) {
      error(F("ERR LIGHT expected AI STATE [COLOR EFFECT]")); return;
    }
    uint8_t nextAi;
    if (strcmp(state, "IDLE") == 0) nextAi = AI_IDLE;
    else if (strcmp(state, "RUNNING") == 0) nextAi = AI_RUNNING;
    else if (strcmp(state, "WAITING") == 0) nextAi = AI_WAITING;
    else if (strcmp(state, "ERROR") == 0) nextAi = AI_ERROR;
    else if (strcmp(state, "DONE") == 0) nextAi = AI_DONE;
    else if (strcmp(state, "OFFLINE") == 0) nextAi = AI_OFFLINE;
    else { error(F("ERR LIGHT unknown AI state")); return; }
    uint8_t nextColor = (nextAi == AI_IDLE || nextAi == AI_DONE) ? GREEN : nextAi == AI_ERROR ? RED : nextAi == AI_OFFLINE ? OFF : YELLOW;
    uint8_t effect = nextAi == AI_RUNNING ? BREATHE : nextAi == AI_WAITING ? SLOW : nextAi == AI_ERROR ? FAST : SOLID;
    if (colorArg) {
      if (strcmp(colorArg, "RED") == 0) nextColor = RED;
      else if (strcmp(colorArg, "YELLOW") == 0) nextColor = YELLOW;
      else if (strcmp(colorArg, "GREEN") == 0) nextColor = GREEN;
      else if (strcmp(colorArg, "OFF") == 0) nextColor = OFF;
      else { error(F("ERR LIGHT unknown color")); return; }
      if (strcmp(effectArg, "SOLID") == 0) effect = SOLID;
      else if (strcmp(effectArg, "BREATHE") == 0) effect = BREATHE;
      else if (strcmp(effectArg, "SLOW") == 0) effect = SLOW;
      else if (strcmp(effectArg, "FAST") == 0) effect = FAST;
      else { error(F("ERR LIGHT unknown effect")); return; }
      if ((effect == BREATHE && nextColor != YELLOW && nextColor != GREEN) ||
          (nextColor == OFF && effect != SOLID) || (nextAi == AI_OFFLINE && nextColor != OFF)) {
        error(F("ERR LIGHT unsupported color/effect")); return;
      }
    }
    setAiState(nextAi, nextColor, effect, colorArg != NULL); printState(); return;
  }
  uint8_t next;
  if (strcmp(argument, "RED") == 0) next = RED;
  else if (strcmp(argument, "YELLOW") == 0) next = YELLOW;
  else if (strcmp(argument, "GREEN") == 0) next = GREEN;
  else if (strcmp(argument, "OFF") == 0) next = OFF;
  else if (strcmp(argument, "AUTO") == 0) {
    aiActive = false; automatic = true; setColor(RED); printState(); return;
  } else { error(F("ERR LIGHT unknown command; use LIGHT HELP")); return; }
  aiActive = false; automatic = false; setColor(next); printState();
}

template<class Link,size_t N>
static void drain(Link &link,LineBuffer<N> &buf,LampReply &out,bool usb) {
  // Bound work to one complete line per channel/turn. Never mix fragments or replies.
  if(buf.length && uint32_t(millis()-buf.lastByte)>2000)buf.discard=true;
  unsigned budget=256;
  while(budget-- && link.available()){
    int b=link.read();if(b<0)break;
    char c=char(b);buf.lastByte=millis();
    if(c=='\r'||c=='\n'){
      if(!buf.discard && !buf.length)continue;
      reply=&out;fromUSB=usb;buf.data[buf.length]=0;
      if(buf.discard || (buf.length>79 && (!usb||strncmp(buf.data,"NET SET ",8)!=0) && strncmp(buf.data,"PCM DATA ",9)!=0))
        error(F("ERR LIGHT invalid/overlong line"));
      else handleLine(buf.data);
      lanCfg::wipe(buf.data,sizeof buf.data);buf.length=0;buf.discard=false;
      reply=&Serial;fromUSB=true;break;
    }
    if(!buf.discard){
      if((b<32 && c!='\t')||b>126||buf.length>=N-1)buf.discard=true;else buf.data[buf.length++]=c;
    }
  }
}
void readLines(){drain(Serial,usbLine,Serial,true);}

void updateCycle() {
  if (!automatic) return;
  const unsigned long duration = color == YELLOW ? 2000UL : 5000UL;
  if (millis() - phaseStarted >= duration) setColor(color == RED ? GREEN : color == GREEN ? YELLOW : RED);
}

void setup() {
  toyBegin();setColor(OFF);
  pinMode(wire::BUTTON,INPUT_PULLUP);
  pcm::begin();
  readTracks();
  audio.configure(readCount());
#ifndef HOST_QA
  audio.seed(esp_random());
#endif
  audio.begin(millis(),digitalRead(wire::BUTTON)==LOW);audio.stage=3;
  Serial.setRxBufferSize(1024); // complete hex audio frames can exceed HWCDC default 256
  Serial.begin(115200);
  Serial.setTxTimeoutMs(10);
  toyLan::begin();
  // No unsolicited banner. A USB host may connect later; autonomous key still works.
}
void loop() {
  readLines();updateCycle();updateAiPattern();tickAudio();toyLan::tick();delay(1);
}
