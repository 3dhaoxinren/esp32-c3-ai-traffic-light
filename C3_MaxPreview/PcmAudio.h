#pragma once
#include <Arduino.h>
#include <LittleFS.h>
#include <esp_partition.h>
#include <esp_random.h>
#include <driver/i2s_std.h>
#include <esp_rom_crc.h>
#include <Preferences.h>
namespace pcm {
// Offline build only. 16 kHz, S16LE mono on flash; duplicate to both I2S slots.
static i2s_chan_handle_t tx=nullptr;
static bool mounted=false, i2sReady=false;
static File reader, writer;
static uint32_t key=0, expected=0, at=0, check=0, crc=0, last=0, slot=0, playing=0;
static int16_t stereo[256]; static size_t sent=0, pending=0;
static char path[24]; static const char *tmp="/pcm-upload.tmp";
static const unsigned LIMIT=1024000;
static uint8_t gain=7,defaultGain=7, auditionGain=255;
// Preserve the original 0..5 levels; add headroom up to unscaled 16-bit PCM.
struct GainScale { uint8_t num, den; };
static constexpr GainScale levels[]={{0,1},{1,16},{1,12},{1,8},{1,6},{1,4},{1,3},{1,2},{3,4},{1,1}};
static constexpr unsigned GAIN_LEVELS=sizeof(levels)/sizeof(levels[0]);
static const char *lastPlay="idle";
static uint8_t stored[(3000+8)/8]={};
static uint8_t live[12288];
static constexpr uint32_t LIVE_MAX=16000UL*2*300;
static uint32_t liveKey=0,liveAt=0,liveLast=0;
static size_t liveRead=0,liveUsed=0;
static bool liveEnd=false,livePlaying=false;
static void name(char* out,unsigned id){snprintf(out,24,"/track%04u.pcm",id);}
static bool hasTrack(unsigned id){return id>=1&&id<=3000&&(stored[id/8]&(1u<<(id%8)));}
static void markTrack(unsigned id,bool present){
  if(id<1||id>3000)return;
  if(present)stored[id/8]|=1u<<(id%8);else stored[id/8]&=~(1u<<(id%8));
}
static unsigned trackId(File &f){
  const char *n=f.name();if(*n=='/')++n;
  if(f.isDirectory()||strlen(n)!=13||strncmp(n,"track",5)||strcmp(n+9,".pcm"))return 0;
  unsigned id=0;
  for(int i=5;i<9;++i){if(n[i]<'0'||n[i]>'9')return 0;id=id*10+unsigned(n[i]-'0');}
  const auto bytes=f.size();return id>=1&&id<=3000&&bytes&&!(bytes&1)&&bytes<=LIMIT?id:0;
}
static bool storeGain(uint8_t val){
  if(val!=defaultGain){
    Preferences p;if(!p.begin("pcmaudio",false))return false;
    const bool ok=p.putUChar("gain",val)==1;p.end();if(!ok)return false;
  }
  gain=defaultGain=val;return true;
}
static bool formatBlank(){
  const esp_partition_t *p=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,ESP_PARTITION_SUBTYPE_DATA_SPIFFS,"spiffs");
  if(!p)return false;
  uint8_t b[512]; for(size_t i=0;i<p->size;i+=sizeof b){
    if(esp_partition_read(p,i,b,sizeof b)!=ESP_OK)return false;
    for(uint8_t c:b)if(c!=0xff)return false;
  }
  return LittleFS.format() && LittleFS.begin(false);
}
static void begin(){
  mounted=LittleFS.begin(false);
  if(mounted && LittleFS.exists(tmp))LittleFS.remove(tmp);
  if(mounted){
    File root=LittleFS.open("/","r");
    if(root&&root.isDirectory())for(File f=root.openNextFile();f;f=root.openNextFile())markTrack(trackId(f),true);
  }
  Preferences prefs;
  if(prefs.begin("pcmaudio",true)){
    uint8_t val=prefs.getUChar("gain",7);prefs.end();
    if(val<GAIN_LEVELS)gain=defaultGain=val;
  }
  i2s_chan_config_t channel=I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO,I2S_ROLE_MASTER);
  channel.auto_clear=true;
  channel.dma_desc_num=4;channel.dma_frame_num=128;
  if(i2s_new_channel(&channel,&tx,nullptr)!=ESP_OK)return;
  i2s_std_config_t conf={};
  conf.clk_cfg=I2S_STD_CLK_DEFAULT_CONFIG(16000);
  conf.slot_cfg=I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,I2S_SLOT_MODE_STEREO);
  conf.gpio_cfg.mclk=I2S_GPIO_UNUSED;conf.gpio_cfg.bclk=GPIO_NUM_4;conf.gpio_cfg.ws=GPIO_NUM_0;
  conf.gpio_cfg.dout=GPIO_NUM_3;conf.gpio_cfg.din=I2S_GPIO_UNUSED;
  if(i2s_channel_init_std_mode(tx,&conf)==ESP_OK && i2s_channel_enable(tx)==ESP_OK)i2sReady=true;
}
static void stop(){if(reader)reader.close();pending=sent=playing=0;liveKey=0;liveUsed=0;liveEnd=livePlaying=false;auditionGain=255;}
static bool play(unsigned id,uint8_t temporary=255){
  if(liveKey)return false;
  stop(); if(!mounted||!i2sReady){lastPlay="not-ready";return false;}
  char filename[24];name(filename,id);reader=LittleFS.open(filename,"r");
  if(!reader){lastPlay="missing";return false;} auditionGain=temporary;playing=id;lastPlay="playing";return true;
}
static void abortUpload(){writer.close();if(mounted)LittleFS.remove(tmp);key=0;}
static void tick(){
  if(key && uint32_t(millis()-last)>15000)abortUpload();
  if(liveKey && !liveEnd && uint32_t(millis()-liveLast)>8000){stop();lastPlay="stream-timeout";}
  if(!reader && !liveKey)return;
  if(sent==pending){
    int16_t mono[128];size_t bytes=0;
    if(liveKey){
      if(!livePlaying && liveUsed<4096 && !liveEnd)return;
      livePlaying=true;
      bytes=liveUsed<sizeof mono?liveUsed:sizeof mono;
      if(!bytes){
        if(liveEnd){stop();lastPlay="ended";}
        else livePlaying=false;
        return;
      }
      const size_t tail=sizeof live-liveRead;
      const size_t first=tail<bytes?tail:bytes;
      memcpy(mono,live+liveRead,first);
      if(bytes>first)memcpy((uint8_t*)mono+first,live,bytes-first);
      liveRead=(liveRead+bytes)%sizeof live;liveUsed-=bytes;
    }else{
      bytes=reader.read((uint8_t*)mono,sizeof mono);
      if(bytes<2){stop();lastPlay="ended";return;}
    }
    const GainScale scale=levels[auditionGain<GAIN_LEVELS?auditionGain:gain];
    for(size_t i=0;i<bytes/2;i++)
      stereo[2*i]=stereo[2*i+1]=int32_t(mono[i])*scale.num/scale.den;
    sent=0;pending=(bytes/2)*4;
  }
  size_t n=0;i2s_channel_write(tx,(const uint8_t*)stereo+sent,pending-sent,&n,0);
  sent+=n;
}
static bool number(const char *s,int base,uint32_t &v){
  if(!s||!*s||strlen(s)>10||strspn(s,base==16?"0123456789ABCDEF":"0123456789")!=strlen(s))return false;
  char *end;unsigned long long raw=strtoull(s,&end,base);if(*end||raw>UINT32_MAX)return false;
  v=(uint32_t)raw;return true;
}
static int nibble(char c){return c>='0'&&c<='9'?c-'0':c>='A'&&c<='F'?10+c-'A':-1;}
// A page of 16 entries keeps the complete reply below the host's 320-byte limit.
static void list(unsigned after,Print &out){
  if(!mounted){out.println("ERR PCM storage not mounted");return;}
  File root=LittleFS.open("/","r");
  if(!root||!root.isDirectory()){out.println("ERR PCM inventory");return;}
  struct Entry {unsigned no;unsigned size;};
  Entry page[17]={};size_t count=0;
  for(File f=root.openNextFile();f;f=root.openNextFile()){
    const unsigned id=trackId(f);
    const size_t bytes=f.size();
    if(id<=after)continue;
    size_t i=0;while(i<count&&page[i].no<id)++i;
    if(i==17)continue;
    if(count<17)++count;
    for(size_t j=count-1;j>i;--j)page[j]=page[j-1];
    page[i]={id,unsigned(bytes)};
  }
  out.printf("OK PCM next=%u entries=",count>16?page[15].no:0);
  if(!count)out.print('-');
  for(size_t i=0;i<count&&i<16;++i){if(i)out.print(',');out.printf("%u:%u",page[i].no,page[i].size);}
  out.println();
}
static bool erase(unsigned id){
  if(!mounted||key||liveKey)return false;
  char file[24];name(file,id);
  if(playing==id)stop();
  if(!LittleFS.remove(file))return false;
  markTrack(id,false);return true;
}
static bool cmd(char *line,bool usb,Print &out){
  if(strncmp(line,"PCM ",4))return false;
  char *args[6],*rest=nullptr;unsigned argc=0;
  for(char *p=strtok_r(line+4," ",&rest);p&&argc<6;p=strtok_r(nullptr," ",&rest))args[argc++]=p;
  uint32_t a=0,b=0,c=0;
  if(argc==1&&!strcmp(args[0],"HELLO"))out.println("OK PCM MAX98357A_V1");
  else if(argc==1&&!strcmp(args[0],"FEATURES"))
    out.println("OK PCM features=LIST,USB_DELETE,LIVE,GAIN_SAVE,BUTTONS"
#ifndef HOST_QA
      ",LAN_READ"
#endif
    );
  else if(argc==2&&!strcmp(args[0],"LIST")&&number(args[1],10,a)&&a<=3000)list(a,out);
  else if((argc==2||(argc==3&&number(args[2],10,c)&&c<GAIN_LEVELS))&&!strcmp(args[0],"LIVE")&&!strcmp(args[1],"START")){
    if(!i2sReady||key||liveKey)out.println("ERR PCM stream busy or amp not ready");
    else{stop();liveKey=esp_random();if(!liveKey)liveKey=1;liveAt=0;liveRead=0;liveLast=millis();
      auditionGain=argc==3?uint8_t(c):255;
      lastPlay="streaming";out.printf("OK PCM stream=%08lX\n",(unsigned long)liveKey);}
  }else if(argc==5&&!strcmp(args[0],"LIVE")&&!strcmp(args[1],"DATA")&&number(args[2],16,a)&&a&&a==liveKey&&!liveEnd&&number(args[3],10,b)&&b==liveAt){
    const char *h=args[4];const size_t n=strlen(h);
    if(!n||n>512||(n%4)||n/2>LIVE_MAX-liveAt||strspn(h,"0123456789ABCDEF")!=n)out.println("ERR PCM stream frame");
    else if(n/2>sizeof live-liveUsed){liveLast=millis();out.println("OK PCM stream wait");}
    else{
      size_t wr=(liveRead+liveUsed)%sizeof live;
      for(size_t i=0;i<n;i+=2){live[wr]=uint8_t((nibble(h[i])<<4)|nibble(h[i+1]));wr=(wr+1)%sizeof live;}
      liveUsed+=n/2;liveAt+=n/2;liveLast=millis();
      out.printf("OK PCM stream at=%lu\n",(unsigned long)liveAt);
    }
  }else if(argc==3&&!strcmp(args[0],"LIVE")&&number(args[2],16,a)&&a&&a==liveKey){
    if(!strcmp(args[1],"END")){liveEnd=true;out.println("OK PCM stream draining");}
    else if(!strcmp(args[1],"ABORT")){stop();lastPlay="stopped";out.println("OK PCM stream stopped");}
    else out.println("ERR PCM stream command");
  }
  else if(argc==1&&!strcmp(args[0],"STATUS"))out.printf("OK PCM fs=%u i2s=%u playing=%lu last=%s gain=%u default_gain=%u\n",unsigned(mounted),unsigned(i2sReady),(unsigned long)playing,lastPlay,unsigned(gain),unsigned(defaultGain));
  else if(argc==2 && ((usb&&!strcmp(args[0],"INFO"))||!strcmp(args[0],"META")) && number(args[1],10,a) && a>=1 && a<=3000){
    char src[24];name(src,unsigned(a));File f=LittleFS.open(src,"r");
    if(!f)out.println("ERR PCM track not found");
    else {out.printf("OK PCM bytes=%lu\n",(unsigned long)f.size());f.close();}
  }else if(usb && argc==4 && !strcmp(args[0],"READ") && number(args[1],10,a) && a>=1 && a<=3000 &&
             number(args[2],10,b) && number(args[3],10,c) && c>=1 && c<=120){
    char src[24];name(src,unsigned(a));File f=LittleFS.open(src,"r");
    if(!f || b>=f.size() || c>f.size()-b || !f.seek(b))out.println("ERR PCM range or missing track");
    else {
      uint8_t data[120];const size_t got=f.read(data,c);
      if(got!=c)out.println("ERR PCM read");
      else {char encoded[241];static const char hexdigits[]="0123456789ABCDEF";
        for(size_t i=0;i<got;++i){encoded[2*i]=hexdigits[data[i]>>4];encoded[2*i+1]=hexdigits[data[i]&15];}
        encoded[2*got]=0;out.printf("OK PCM off=%lu hex=%s\n",(unsigned long)b,encoded);
      }
    }
    if(f)f.close();
  }
  else if(argc==2&&!strcmp(args[0],"GAIN")&&number(args[1],10,a)&&a<GAIN_LEVELS){
    gain=(uint8_t)a;out.printf("OK PCM gain=%u\n",unsigned(gain));
  }
  else if(argc==3&&!strcmp(args[0],"GAIN")&&!strcmp(args[1],"SAVE")&&number(args[2],10,a)&&a<GAIN_LEVELS){
    if(storeGain(uint8_t(a)))out.printf("OK PCM default_gain=%u\n",unsigned(a));
    else out.println("ERR PCM volume save failed");
  }
  else if(argc==1&&!strcmp(args[0],"INIT")&&usb){
    mounted=mounted||formatBlank();out.println(mounted?"OK PCM ready":"ERR PCM not blank or mount failed");
  }else if(argc==4&&!strcmp(args[0],"START")&&number(args[1],10,a)&&number(args[2],10,b)&&number(args[3],16,c)){
    if(!mounted||!i2sReady||key||liveKey||!a||a>3000||!b||(b&1)||b>LIMIT||
       LittleFS.totalBytes()-LittleFS.usedBytes()<b+16384)out.println("ERR PCM busy, storage or length");
    else{
      writer=LittleFS.open(tmp,"w");if(!writer)out.println("ERR PCM open");
      else{at=crc=0;expected=b;check=c;slot=a;last=millis();key=esp_random();if(!key)key=1;out.printf("OK PCM token=%08lX\n",(unsigned long)key);}
    }
  }else if(argc==4&&!strcmp(args[0],"DATA")&&number(args[1],16,a)&&a&&a==key&&number(args[2],10,b)&&at==b){
    const char *s=args[3];size_t n=strlen(s);uint8_t data[256];
    bool ok=n>=2&&!(n%2)&&n<=512&&n/2<=expected-at;
    for(unsigned i=0;ok&&i<n/2;i++){int h=nibble(s[i*2]),l=nibble(s[i*2+1]);if(h<0||l<0)ok=false;else data[i]=(h<<4)|l;}
    if(!ok)out.println("ERR PCM hex/size");
    else if(writer.write(data,n/2)!=n/2){abortUpload();out.println("ERR PCM write");}
    else{at+=n/2;last=millis();crc=esp_rom_crc32_le(crc,data,n/2);out.printf("OK PCM at=%lu\n",(unsigned long)at);}
  }else if(argc==2&&!strcmp(args[0],"END")&&number(args[1],16,a)&&a&&a==key){
    writer.close();name(path,slot);
    if(at!=expected||crc!=check){abortUpload();out.println("ERR PCM crc/size");}
    else if(!LittleFS.rename(tmp,path)){abortUpload();out.println("ERR PCM commit");}
    else{markTrack(slot,true);key=0;out.println("OK PCM saved");}
  }else if((argc==2||(argc==3&&number(args[2],10,b)&&b<GAIN_LEVELS))&&!strcmp(args[0],"PLAY")&&number(args[1],10,a)&&a>=1&&a<=3000)
    out.println(play(a,argc==3?uint8_t(b):255)?"OK PCM playing":"ERR PCM missing or not ready");
  else if(argc==1&&!strcmp(args[0],"STOP")){stop();lastPlay="stopped";out.println("OK PCM stopped");}
  else out.println("ERR PCM command, token or offset");
  return true;
}
}
