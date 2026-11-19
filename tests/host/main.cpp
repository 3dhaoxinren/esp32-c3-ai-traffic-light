#include <string>
#include "ToyController.h"
USBMock Serial;
UARTMock Serial1;
uint32_t fakeClock=0;
bool keyDown=false, dirty=false;
unsigned int capturedPin=0,baud=0,rxPin=0,txPin=0;
int main(){
  setup();
  char b[650];
  while(fgets(b,sizeof(b),stdin)){
    b[strcspn(b,"\r\n")]=0;
    if(!strcmp(b,"QA TICK")){fakeClock+=1000;for(int i=0;i<512;++i)tickAudio();printf("OK QA pending=%u plays=%lu\n",unsigned(audio.pending),(unsigned long)audio.plays);}
    else if(!strcmp(b,"QA BUTTON")){
      keyDown=false;tickAudio();fakeClock+=60;tickAudio();
      keyDown=true;tickAudio();fakeClock+=180;tickAudio();
      printf("OK QA last=%u plays=%lu\n",audio.lastSent,(unsigned long)audio.plays);
    }
    else if(!strcmp(b,"QA EXPIRE")){fakeClock+=16000;updateAiPattern();printf("OK QA pending=%u\n",unsigned(audio.pending));}
    else {
      const bool usb=strncmp(b,"LAN:",4)!=0;
      USBMock input;
      const auto msg=std::string(b+(usb?0:4))+"\r\n";
      input.feed(msg.c_str());Serial.out[0]=0;
      LineBuffer<576> buf={};
      while(input.available())drain(input,buf,Serial,usb);
      fputs(Serial.out,stdout);
    }
    puts("@END");fflush(stdout);
  }
}
