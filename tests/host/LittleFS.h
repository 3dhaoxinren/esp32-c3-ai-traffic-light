#pragma once
#include <string>
#include <memory>
#include <filesystem>
struct File{std::shared_ptr<FILE> f;
 std::string filename;
 std::shared_ptr<std::filesystem::directory_iterator> dir;
 explicit operator bool()const{return bool(f)||bool(dir);}
 bool isDirectory()const{return bool(dir);}
 const char* name()const{return filename.c_str();}
 File openNextFile(){
  if(!dir)return {};
  while(*dir!=std::filesystem::directory_iterator()){
   auto entry=**dir;++*dir;
   if(!entry.is_regular_file())continue;
   std::string file=entry.path().filename().string();
   FILE* handle=fopen(file.c_str(),"rb");
   if(handle)return {std::shared_ptr<FILE>(handle,fclose),file,{}};
  }
  return {};
 }
 bool seek(size_t offset){return f && fseek(f.get(),long(offset),SEEK_SET)==0;}
 size_t size(){const long old=ftell(f.get());fseek(f.get(),0,SEEK_END);const auto n=ftell(f.get());fseek(f.get(),old,SEEK_SET);return size_t(n);}
 void close(){f.reset();}
 size_t read(uint8_t *p,size_t n){return fread(p,1,n,f.get());}
 size_t write(const uint8_t*p,size_t n){return fwrite(p,1,n,f.get());}
};
struct FS{bool ready=false;bool begin(bool){return ready=std::filesystem::exists(".littlefs-ready");}
bool format(){FILE* f=fopen(".littlefs-ready","wb");if(!f)return false;fclose(f);return ready=true;}
bool exists(const char*p){File f=open(p,"r");return bool(f);}
File open(const char*p,const char*m){
 if(std::string(p)=="/")return {{}, "/", std::make_shared<std::filesystem::directory_iterator>(".")};
 std::string mode=std::string(m)+"b";FILE*f=fopen(p+1,mode.c_str());
 return f?File{std::shared_ptr<FILE>(f,fclose),p+1,{}}:File{};
}
bool remove(const char*p){return std::remove(p+1)==0;}
bool rename(const char*a,const char*b){return std::rename(a+1,b+1)==0;}
size_t totalBytes(){return 1400000;}size_t usedBytes(){return 0;}
} LittleFS;
