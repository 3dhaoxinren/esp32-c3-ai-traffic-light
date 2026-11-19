#pragma once
#define ESP_OK 0
#define ESP_PARTITION_TYPE_DATA 0
#define ESP_PARTITION_SUBTYPE_DATA_SPIFFS 1
struct esp_partition_t{unsigned size;};inline const esp_partition_t *esp_partition_find_first(int,int,const char*){static esp_partition_t p{1024};return &p;}
extern bool dirty;
inline int esp_partition_read(const esp_partition_t*,unsigned,void *p,size_t n){memset(p,dirty?0:255,n);return 0;}
