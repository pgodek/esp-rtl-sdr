#pragma once
/* Force-included when the example's server is built for the host. */
#include <netinet/in.h>
#include <netinet/tcp.h>
#if defined(__APPLE__) && !defined(TCP_KEEPIDLE)
#define TCP_KEEPIDLE TCP_KEEPALIVE
#endif
