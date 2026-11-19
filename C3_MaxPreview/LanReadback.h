#pragma once
// Paired LAN readback. The original plaintext PCM READ remains USB-only.
#include <mbedtls/gcm.h>
#include <mbedtls/md.h>
#include <mbedtls/sha256.h>
#include <esp_random.h>

namespace lanRead {
static constexpr size_t BLOCK=1024, TAG=16;
static uint8_t secret[32]={}, fingerprint[16]={};
static bool paired=false;
static const char AUTH[]="ailamp-read-auth-v1";
static const char KEY[]="ailamp-read-key-v1";
static void hex(const uint8_t *data,size_t n,char *out){
  const char digits[]="0123456789ABCDEF";
  for(size_t i=0;i<n;++i){out[2*i]=digits[data[i]>>4];out[2*i+1]=digits[data[i]&15];}
  out[2*n]=0;
}
static bool unhex(const char *s,uint8_t *out,size_t n){
  if(!s||strlen(s)!=2*n)return false;
  for(size_t i=0;i<n;++i){
    int a=pcm::nibble(s[2*i]),b=pcm::nibble(s[2*i+1]);
    if(a<0||b<0)return false;
    out[i]=uint8_t((a<<4)|b);
  }
  return true;
}
static void put32(uint8_t *p,uint32_t n){
  p[0]=uint8_t(n>>24);p[1]=uint8_t(n>>16);p[2]=uint8_t(n>>8);p[3]=uint8_t(n);
}
static bool mac(const char *label,const uint8_t *data,size_t len,uint8_t *out){
  uint8_t buf[64];const size_t k=strlen(label);
  if(k+len>sizeof buf)return false;
  memcpy(buf,label,k);memcpy(buf+k,data,len);
  return mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256),secret,sizeof secret,buf,k+len,out)==0;
}
static bool refreshId(){
  uint8_t sum[32]={};
  const int r=mbedtls_sha256(secret,sizeof secret,sum,0);
  if(!r)memcpy(fingerprint,sum,sizeof fingerprint);
  lanCfg::wipe(sum,sizeof sum);return r==0;
}
static void begin(){
  Preferences p;
  if(!p.begin("audread",true))return;
  paired=p.getBytesLength("key")==sizeof secret && p.getBytes("key",secret,sizeof secret)==sizeof secret;
  p.end();paired=paired&&refreshId();
}
static void pairUsb(const char *offered,Print &out){
  uint8_t candidate[32]={};
  if(!unhex(offered,candidate,sizeof candidate)){out.println("ERR AUDX key format");return;}
  if(!paired){
    Preferences p;
    bool saved=p.begin("audread",false);
    if(saved){saved=p.putBytes("key",candidate,sizeof candidate)==sizeof candidate;p.end();}
    if(saved){memcpy(secret,candidate,sizeof secret);paired=refreshId();}
  }
  lanCfg::wipe(candidate,sizeof candidate);
  if(!paired){out.println("ERR AUDX pair save");return;}
  char value[65];hex(secret,sizeof secret,value);
  out.printf("OK AUDX key=%s\n",value);lanCfg::wipe(value,sizeof value);
}
struct Session{
  File file;
  mbedtls_gcm_context gcm;
  bool keyed=false, challenged=false, streaming=false;
  uint8_t context[18], packet[BLOCK+TAG];
  uint32_t issued=0, lastWrite=0, remaining=0, serial=0;
  size_t pos=0,len=0;
  void reset(){
    if(file)file.close();
    if(keyed)mbedtls_gcm_free(&gcm);
    keyed=challenged=streaming=false;pos=len=remaining=serial=0;
  }
  void challenge(Print &out){
    reset();
    if(!paired){out.println("ERR AUDX pair via USB");return;}
    esp_fill_random(context,12);issued=millis();challenged=true;
    char id[33],nonce[25];hex(fingerprint,sizeof fingerprint,id);hex(context,12,nonce);
    out.printf("OK AUDX id=%s nonce=%s\n",id,nonce);
  }
  void get(char *args,Print &out){
    if(!challenged||uint32_t(millis()-issued)>10000){
      challenged=false;out.println("ERR AUDX challenge");return;
    }
    challenged=false; // single use, including failed authorizations
    char *save=nullptr,*slot=strtok_r(args," ",&save),*length=strtok_r(nullptr," ",&save),*proof=strtok_r(nullptr," ",&save);
    uint32_t n=0,size=0;
    uint8_t supplied[32]={}, expected[32]={};
    bool good=pcm::number(slot,10,n)&&n>=1&&n<=3000&&pcm::number(length,10,size)&&
      size>=2&&size<=pcm::LIMIT&&!(size&1)&&!strtok_r(nullptr," ",&save)&&unhex(proof,supplied,sizeof supplied);
    context[12]=uint8_t(n>>8);context[13]=uint8_t(n);put32(context+14,size);
    const bool made=good&&mac(AUTH,context,sizeof context,expected);
    volatile uint8_t diff=0;
    for(size_t i=0;i<sizeof supplied;++i)diff|=supplied[i]^expected[i];
    lanCfg::wipe(supplied,sizeof supplied);lanCfg::wipe(expected,sizeof expected);
    if(!made||diff){out.println("ERR AUDX auth");return;}
    char path[24];pcm::name(path,n);file=LittleFS.open(path,"r");
    if(!file||file.size()!=size){reset();out.println("ERR AUDX changed");return;}
    uint8_t key[32]={};
    if(!mac(KEY,context,12,key)){reset();out.println("ERR AUDX crypt");return;}
    mbedtls_gcm_init(&gcm);keyed=true;
    int r=mbedtls_gcm_setkey(&gcm,MBEDTLS_CIPHER_ID_AES,key,256);
    lanCfg::wipe(key,sizeof key);
    if(r){reset();out.println("ERR AUDX crypt");return;}
    remaining=size;serial=0;pos=len=0;lastWrite=millis();streaming=true;
    out.printf("OK AUDX bytes=%lu\n",(unsigned long)size);
  }
  bool next(){
    if(pos<len)return true;
    if(!remaining){reset();return true;}
    const size_t n=remaining<BLOCK?remaining:BLOCK;
    uint8_t plain[BLOCK],iv[12]={},aad[22];
    put32(iv+8,serial);memcpy(aad,context,sizeof context);put32(aad+sizeof context,serial);
    const bool ok=file.read(plain,n)==n &&
      !mbedtls_gcm_crypt_and_tag(&gcm,MBEDTLS_GCM_ENCRYPT,n,iv,sizeof iv,aad,sizeof aad,plain,packet,TAG,packet+n);
    lanCfg::wipe(plain,sizeof plain);
    if(!ok){reset();return false;}
    ++serial;remaining-=n;pos=0;len=n+TAG;return true;
  }
};
}
