#pragma once
#include <string>
#include <memory>
struct File{std::shared_ptr<FILE> f;
 explicit operator bool()const{return bool(f);}
 bool seek(size_t offset){return f && fseek(f.get(),long(offset),SEEK_SET)==0;}
 size_t size(){const long old=ftell(f.get());fseek(f.get(),0,SEEK_END);const auto n=ftell(f.get());fseek(f.get(),old,SEEK_SET);return size_t(n);}
 void close(){f.reset();}
 size_t read(uint8_t *p,size_t n){return fread(p,1,n,f.get());}
 size_t write(const uint8_t*p,size_t n){return fwrite(p,1,n,f.get());}
};
struct FS{bool ready=false;bool begin(bool){return ready;}
bool format(){ready=true;return true;}
bool exists(const char*p){File f=open(p,"r");return bool(f);}
File open(const char*p,const char*m){std::string mode=std::string(m)+"b";FILE*f=fopen(p+1,mode.c_str());return f?File{std::shared_ptr<FILE>(f,fclose)}:File{};}
bool remove(const char*p){return std::remove(p+1)==0;}
bool rename(const char*a,const char*b){return std::rename(a+1,b+1)==0;}
size_t totalBytes(){return 1400000;}size_t usedBytes(){return 0;}
} LittleFS;
