#pragma once
/* Host stub of the few FreeRTOS pieces rtl_tcp_server.cpp uses: 1 tick = 1 ms, tasks are threads. */
#include <stdint.h>

typedef uint32_t TickType_t;
typedef uint32_t EventBits_t;
typedef int BaseType_t;
typedef unsigned UBaseType_t;

#define pdTRUE 1
#define pdFALSE 0
#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
#define portMAX_DELAY 0xFFFFFFFFu
#define BIT0 (1u << 0)
