#pragma once
#include <pthread.h>
#include <time.h>
#include <unistd.h>

#include "FreeRTOS.h"

typedef void (*TaskFunction_t)(void *);
typedef void *TaskHandle_t;

inline TickType_t xTaskGetTickCount(void)
{
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (TickType_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

inline void vTaskDelay(TickType_t ticks)
{
    usleep(ticks ? ticks * 1000u : 1000u);
}

struct HostTaskArgs { TaskFunction_t fn; void *arg; };

inline void *host_task_main(void *p)
{
    HostTaskArgs a = *static_cast<HostTaskArgs *>(p);
    delete static_cast<HostTaskArgs *>(p);
    a.fn(a.arg);
    return nullptr;
}

inline BaseType_t xTaskCreatePinnedToCore(TaskFunction_t fn, const char *, uint32_t, void *arg, UBaseType_t,
                                          TaskHandle_t *, BaseType_t)
{
    pthread_t t;
    pthread_attr_t at;
    pthread_attr_init(&at);
    pthread_attr_setdetachstate(&at, PTHREAD_CREATE_DETACHED);
    const int rc = pthread_create(&t, &at, host_task_main, new HostTaskArgs{fn, arg});
    pthread_attr_destroy(&at);
    return rc == 0 ? pdTRUE : pdFALSE;
}

/* The server calls this as the last statement of a task; returning ends the thread. */
inline void vTaskDelete(TaskHandle_t) {}
