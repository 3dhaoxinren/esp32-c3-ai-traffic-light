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
    else if(!strcmp(b,"QA EXPIRE")){fakeClock+=16000;updateAiPattern();printf("OK QA pending=%u\n",unsigned(audio.pending));}
    else {
      fromUSB=strncmp(b,"LAN:",4)!=0;
      Serial.out[0]=0;handleLine(b+(fromUSB?0:4));
      fputs(Serial.out,stdout);
    }
    puts("@END");fflush(stdout);
  }
}
