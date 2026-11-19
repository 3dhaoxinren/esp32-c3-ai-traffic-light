#pragma once
#include <string>
#include <cstdio>

struct Preferences {
  std::string scope;
  bool readonly=false;
  bool begin(const char *ns,bool ro){scope=std::string(".nvs-")+ns+"-";readonly=ro;return true;}
  void end(){}
  size_t getBytesLength(const char *key){
    FILE *f=fopen((scope+key).c_str(),"rb");if(!f)return 0;
    fseek(f,0,SEEK_END);long n=ftell(f);fclose(f);return n>0?size_t(n):0;
  }
  size_t getBytes(const char *key,void *value,size_t n){
    FILE *f=fopen((scope+key).c_str(),"rb");if(!f)return 0;
    size_t r=fread(value,1,n,f);fclose(f);return r;
  }
  size_t putBytes(const char *key,const void *value,size_t n){
    if(readonly)return 0;
    FILE *f=fopen((scope+key).c_str(),"wb");if(!f)return 0;
    size_t r=fwrite(value,1,n,f);fclose(f);return r;
  }
  unsigned char getUChar(const char *key,unsigned char fallback){
    unsigned char b=0;return getBytes(key,&b,1)==1?b:fallback;
  }
  size_t putUChar(const char *key,unsigned char b){return putBytes(key,&b,1);}
};
