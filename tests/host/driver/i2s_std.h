#pragma once
#define I2S_CHANNEL_DEFAULT_CONFIG(a,b) {}
#define I2S_STD_CLK_DEFAULT_CONFIG(a) 0
#define I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(a,b) 0
enum{I2S_GPIO_UNUSED=-1, GPIO_NUM_4=4,GPIO_NUM_0=0,GPIO_NUM_3=3};
using i2s_chan_handle_t=void*;
struct i2s_chan_config_t{bool auto_clear;int dma_desc_num,dma_frame_num;};
struct i2s_std_config_t{int clk_cfg,slot_cfg;struct{int mclk,bclk,ws,dout,din;}gpio_cfg;};
inline int i2s_new_channel(i2s_chan_config_t*,void** p,void*){*p=(void*)1;return 0;}
inline int i2s_channel_init_std_mode(void*,i2s_std_config_t*){return 0;}
inline int i2s_channel_enable(void*){return 0;}
inline int i2s_channel_write(void*,const uint8_t*p,size_t n,size_t*sent,int){*sent=n>96?96:n;FILE*f=fopen("wire.bin","ab");fwrite(p,1,*sent,f);fclose(f);return 0;}
