inline uint32_t esp_rom_crc32_le(uint32_t v,const uint8_t *buf,size_t n){v=~v;while(n--){v^=*buf++;for(int j=0;j<8;j++)v=(v>>1)^(0xedb88320U & (0u-(v&1u)));}return ~v;}
