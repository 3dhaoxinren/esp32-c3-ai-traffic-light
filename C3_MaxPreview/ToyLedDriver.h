#pragma once
#include "Engine.h"
#ifndef HOST_QA
#include <esp_timer.h>
#include <driver/gpio.h>
#include <esp_rom_gpio.h>
#include <soc/gpio_sig_map.h>
#include <soc/soc.h>
#include <soc/gpio_reg.h>
#if !CONFIG_IDF_TARGET_ESP32C3 || !ARDUINO_USB_CDC_ON_BOOT || !ARDUINO_USB_MODE
#error Build for ESP32-C3 with native hardware USB CDC enabled
#endif
static portMUX_TYPE mux=portMUX_INITIALIZER_UNLOCKED;
#define LOCK() portENTER_CRITICAL(&mux)
#define UNLOCK() portEXIT_CRITICAL(&mux)
static uint32_t scanMs(){return uint32_t(esp_timer_get_time()/1000);}
static void applyPair(wire::Pair p) {
  REG_WRITE(GPIO_ENABLE_W1TC_REG,wire::MASK); // all three Hi-Z first
  if(!p.hi) return;
  REG_WRITE(GPIO_OUT_W1TC_REG,wire::MASK);
  REG_WRITE(GPIO_OUT_W1TS_REG,1UL<<p.hi);
  REG_WRITE(GPIO_ENABLE_W1TS_REG,(1UL<<p.hi)|(1UL<<p.lo));
}
#else
#define LOCK() ((void)0)
#define UNLOCK() ((void)0)
static uint32_t scanMs(){return millis();}
static wire::Pair lastOut{};
static void applyPair(wire::Pair p){lastOut=p;}
#endif
static wire::Frame wanted{};
static wire::Scan scanner;
static bool armed=false;
static uint32_t deadline=0;
static void scanTick(void*){
  LOCK();
  if(armed && int32_t(scanMs()-deadline)>=0){wanted={};armed=false;}
  applyPair(scanner.next(wanted)); UNLOCK();
}
static void toyBegin(){
#ifndef HOST_QA
  gpio_config_t cfg{};cfg.pin_bit_mask=wire::MASK;cfg.mode=GPIO_MODE_INPUT;
  cfg.pull_up_en=GPIO_PULLUP_DISABLE;cfg.pull_down_en=GPIO_PULLDOWN_DISABLE;
  for(uint8_t pin:{wire::B,wire::C,wire::D}) gpio_reset_pin(gpio_num_t(pin));
  ESP_ERROR_CHECK(gpio_config(&cfg)); applyPair(wire::OFFPAIR);
  for(uint8_t pin:{wire::B,wire::C,wire::D}) esp_rom_gpio_connect_out_signal(pin,SIG_GPIO_OUT_IDX,false,false);
  static esp_timer_handle_t timer;
  esp_timer_create_args_t a{};a.callback=scanTick;a.name="toy-led";a.skip_unhandled_events=true;
  ESP_ERROR_CHECK(esp_timer_create(&a,&timer));ESP_ERROR_CHECK(esp_timer_start_periodic(timer,250));
#endif
}
static void toyRequest(uint8_t color,uint8_t level,bool manual){
  LOCK(); wanted=wire::frame(color,level); if(manual)armed=false;
  if(!level || !color) applyPair(wire::OFFPAIR); UNLOCK();
}
static void toyRefreshLease(bool enabled){
  LOCK();armed=enabled;deadline=scanMs()+15000;UNLOCK();
}
static bool toyProbe(char hi,char lo){
  if(hi<'B'||hi>'D'||lo<'B'||lo>'D'||hi==lo)return false;
  const uint8_t pins[]={wire::B,wire::C,wire::D};
  LOCK(); applyPair(wire::OFFPAIR); wanted={{pins[hi-'B'],pins[lo-'B']},wire::OFFPAIR,255};
  deadline=scanMs()+2000;armed=true;UNLOCK();return true;
}
