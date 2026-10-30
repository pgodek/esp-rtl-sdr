#pragma once
#include <pthread.h>
#include <time.h>

#include "FreeRTOS.h"

struct HostEventGroup {
    pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
    pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
    EventBits_t bits = 0;
};
typedef HostEventGroup *EventGroupHandle_t;

inline EventGroupHandle_t xEventGroupCreate(void) { return new HostEventGroup(); }
inline void vEventGroupDelete(EventGroupHandle_t g) { delete g; }

inline EventBits_t xEventGroupSetBits(EventGroupHandle_t g, EventBits_t b)
{
    pthread_mutex_lock(&g->mu);
    g->bits |= b;
    const EventBits_t now = g->bits;
    pthread_cond_broadcast(&g->cv);
    pthread_mutex_unlock(&g->mu);
    return now;
}

inline EventBits_t xEventGroupWaitBits(EventGroupHandle_t g, EventBits_t want, BaseType_t clear, BaseType_t all,
                                       TickType_t ticks)
{
    timespec dl;
    clock_gettime(CLOCK_REALTIME, &dl);
    dl.tv_sec += ticks / 1000u;
    dl.tv_nsec += (long)(ticks % 1000u) * 1000000L;
    if (dl.tv_nsec >= 1000000000L) { dl.tv_sec++; dl.tv_nsec -= 1000000000L; }
    pthread_mutex_lock(&g->mu);
    for (;;) {
        const bool ok = all ? ((g->bits & want) == want) : ((g->bits & want) != 0);
        if (ok) break;
        if (ticks == portMAX_DELAY) pthread_cond_wait(&g->cv, &g->mu);
        else if (pthread_cond_timedwait(&g->cv, &g->mu, &dl) != 0) break;
    }
    const EventBits_t r = g->bits;
    if (clear) g->bits &= ~want;
    pthread_mutex_unlock(&g->mu);
    return r;
}
