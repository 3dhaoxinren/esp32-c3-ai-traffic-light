#pragma once
#include <Arduino.h>
#include <LittleFS.h>
#include <esp_partition.h>
#include <esp_random.h>
#include <driver/i2s_std.h>
#include <esp_rom_crc.h>
namespace pcm {
// Offline build only. 16 kHz, S16LE mono on flash; duplicate to both I2S slots.
static i2s_chan_handle_t tx=nullptr;
static bool mounted=false, i2sReady=false;
static File reader, writer;
static uint32_t key=0, expected=0, at=0, check=0, crc=0, last=0, slot=0, playing=0;
static int16_t stereo[256]; static size_t sent=0, pending=0;
static char path[24]; static const char *tmp="/pcm-upload.tmp";
static const unsigned LIMIT=1024000;
static uint8_t gain=7; // Requested boot default; changes stay in RAM
// Preserve the original 0..5 levels; add headroom up to unscaled 16-bit PCM.
struct GainScale { uint8_t num, den; };
static constexpr GainScale levels[]={{0,1},{1,16},{1,12},{1,8},{1,6},{1,4},{1,3},{1,2},{3,4},{1,1}};
static constexpr unsigned GAIN_LEVELS=sizeof(levels)/sizeof(levels[0]);
static const char *lastPlay="idle";
static void name(char* out,unsigned id){snprintf(out,24,"/track%04u.pcm",id);}
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
static void stop(){if(reader)reader.close();pending=sent=playing=0;}
static bool play(unsigned id){
  stop(); if(!mounted||!i2sReady){lastPlay="not-ready";return false;}
  char filename[24];name(filename,id);reader=LittleFS.open(filename,"r");
  if(!reader){lastPlay="missing";return false;} playing=id;lastPlay="playing";return true;
}
static void abortUpload(){writer.close();if(mounted)LittleFS.remove(tmp);key=0;}
static void tick(){
  if(key && uint32_t(millis()-last)>15000)abortUpload();
  if(!reader)return;
  if(sent==pending){
    int16_t mono[128];size_t bytes=reader.read((uint8_t*)mono,sizeof mono);
    if(bytes<2){stop();lastPlay="ended";return;}
    const GainScale scale=levels[gain];
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
static bool cmd(char *line,bool usb,Print &out){
  if(strncmp(line,"PCM ",4))return false;
  char *args[6],*rest=nullptr;unsigned argc=0;
  for(char *p=strtok_r(line+4," ",&rest);p&&argc<6;p=strtok_r(nullptr," ",&rest))args[argc++]=p;
  uint32_t a=0,b=0,c=0;
  if(argc==1&&!strcmp(args[0],"HELLO"))out.println("OK PCM MAX98357A_V1");
  else if(argc==1&&!strcmp(args[0],"STATUS"))out.printf("OK PCM fs=%u i2s=%u playing=%lu last=%s gain=%u\n",unsigned(mounted),unsigned(i2sReady),(unsigned long)playing,lastPlay,unsigned(gain));
  else if(usb && argc==2 && !strcmp(args[0],"INFO") && number(args[1],10,a) && a>=1 && a<=3000){
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
  else if(argc==1&&!strcmp(args[0],"INIT")&&usb){
    mounted=mounted||formatBlank();out.println(mounted?"OK PCM ready":"ERR PCM not blank or mount failed");
  }else if(argc==4&&!strcmp(args[0],"START")&&number(args[1],10,a)&&number(args[2],10,b)&&number(args[3],16,c)){
    if(!mounted||!i2sReady||key||!a||a>3000||!b||(b&1)||b>LIMIT||
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
    else{key=0;out.println("OK PCM saved");}
  }else if(argc==2&&!strcmp(args[0],"PLAY")&&number(args[1],10,a)&&a>=1&&a<=3000)out.println(play(a)?"OK PCM playing":"ERR PCM missing or not ready");
  else if(argc==1&&!strcmp(args[0],"STOP")){stop();lastPlay="stopped";out.println("OK PCM stopped");}
  else out.println("ERR PCM command, token or offset");
  return true;
}
}
