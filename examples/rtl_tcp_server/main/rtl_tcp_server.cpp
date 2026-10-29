/*
 * rtl_tcp protocol server on top of the esp_rtl_sdr public API.
 *
 * Data path:  driver pull ring --esp_rtl_sdr_read()--> sender task --> TCP
 * Commands:   TCP --> serve_client() --> shadow Settings --> paced applier
 *
 * Why the commands are not applied the moment they arrive: clients such as
 * SDR++ fire a burst of ~10 commands right after connecting (frequency, gain,
 * AGC, bias-T, ...). Gain/AGC/bias-T are queued to the driver's delivery task
 * while a retune runs on the calling task, so a burst makes two tasks use EP0
 * at once and the dongle STALLs. Therefore:
 *   - the stream is only started once the client has been quiet for a moment,
 *     so the initial burst just fills the shadow `Settings`;
 *   - afterwards each change is applied at most one per kEp0SpacingMs, and
 *     repeated changes of the same setting (tuning drag) are coalesced.
 * A sample-rate change needs stop()/start() (driver requirement).
 */
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "rtl_tcp_proto.hpp"
#include "rtl_tcp_server.h"

static const char *TAG = "rtl_tcp";

static constexpr EventBits_t kEvSenderDone = BIT0;
static constexpr size_t kMaxGains = 32;
static constexpr uint32_t kReadTimeoutMs = 50;
static constexpr uint32_t kStatusPeriodMs = 5000;
static constexpr uint32_t kEp0SpacingMs = 200;         /* min gap between driver setter calls */
static constexpr uint32_t kQuietBeforeStartMs = 300;   /* client silence that ends the initial burst */
static constexpr uint32_t kMaxWaitBeforeStartMs = 2000;
static constexpr uint32_t kSendStallMs = 5000;         /* peer that stops reading for this long is dropped */

/* Persist across clients, like rtl_tcp keeps the dongle state. */
struct Settings {
    uint32_t freq_hz = CONFIG_RTLTCP_DEFAULT_FREQ_HZ;
    uint32_t rate_sps = CONFIG_RTLTCP_DEFAULT_RATE_SPS;
    bool manual_gain = false;
    bool gain_explicit = false;
    int gain_tenth_db = 0;
    int gain_index = -1;       /* resolved against the gain table when applied */
    int ppm = 0;
    bool rtl_agc = false;
    bool bias_tee = false;
};
static Settings s_set;

enum : uint32_t {
    kDirtyFreq = 1u << 0,
    kDirtyGain = 1u << 1,
    kDirtyAgc = 1u << 2,
    kDirtyBias = 1u << 3,
    kDirtyPpm = 1u << 4,
    kDirtyRate = 1u << 5,
};

struct Session {
    esp_rtl_sdr_handle_t sdr;
    int sock;
    SemaphoreHandle_t mtx;    /* serialises read() against stop/start */
    EventGroupHandle_t ev;
    volatile bool stop;
    volatile bool streaming;  /* read by the sender task */
    bool started;             /* command-loop view: stream has been started */
    uint32_t dirty;           /* settings changed but not yet sent to the driver */
    TickType_t last_cmd;
    TickType_t last_ep0;
};

static uint32_t tuner_type_for(esp_rtl_sdr_profile_t profile)
{
    switch (profile) {
    case ESP_RTL_SDR_PROFILE_BLOG_V4:
        return rtl_tcp::kTunerR828D;
    case ESP_RTL_SDR_PROFILE_BLOG_V4L:
    case ESP_RTL_SDR_PROFILE_BLOG_V3:
    case ESP_RTL_SDR_PROFILE_NOOELEC_SMART_V5:
        return rtl_tcp::kTunerR820T;
    default:
        return rtl_tcp::kTunerUnknown;
    }
}

static void warn_if(const char *what, esp_err_t err)
{
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "%s: %s", what, esp_rtl_sdr_err_to_name(err));
    }
}

/* Queue what has to be re-sent after a (re)start; the paced applier does it. */
static void mark_post_start_dirty(Session *s)
{
    s->dirty = kDirtyGain;
    if (s_set.rtl_agc) {
        s->dirty |= kDirtyAgc;
    }
    if (s_set.bias_tee) {
        s->dirty |= kDirtyBias;
    }
}

static esp_err_t start_stream(Session *s)
{
    warn_if("set_freq_correction", esp_rtl_sdr_set_freq_correction(s->sdr, s_set.ppm));
    esp_err_t err = esp_rtl_sdr_start_hz(s->sdr, s_set.freq_hz, s_set.rate_sps);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "start_hz(%u Hz, %u sps): %s", (unsigned)s_set.freq_hz,
                 (unsigned)s_set.rate_sps, esp_rtl_sdr_err_to_name(err));
        return err;
    }
    mark_post_start_dirty(s);
    s->last_ep0 = xTaskGetTickCount();
    s->streaming = true;
    s->started = true;
    return ESP_OK;
}

/* Rate changes need stop/start; the sender is parked on the mutex meanwhile. */
static void restart_stream(Session *s)
{
    xSemaphoreTake(s->mtx, portMAX_DELAY);
    s->streaming = false;
    warn_if("stop", esp_rtl_sdr_stop(s->sdr, 0));
    if (start_stream(s) != ESP_OK) {
        s->stop = true;
    }
    xSemaphoreGive(s->mtx);
}

static void apply_gain(esp_rtl_sdr_handle_t sdr)
{
    if (s_set.gain_index >= 0) {
        int gains[kMaxGains];
        size_t n = 0;

        if (esp_rtl_sdr_get_tuner_gains(sdr, gains, kMaxGains, &n) == ESP_OK &&
            static_cast<size_t>(s_set.gain_index) < n) {
            s_set.gain_tenth_db = gains[s_set.gain_index];
        }
    }
    if (s_set.manual_gain && s_set.gain_explicit) {
        ESP_LOGI(TAG, "apply gain %d (tenth dB)", s_set.gain_tenth_db);
        warn_if("set_tuner_gain", esp_rtl_sdr_set_tuner_gain(sdr, s_set.gain_tenth_db));
    } else if (s_set.manual_gain) {
        ESP_LOGI(TAG, "apply gain mode manual");
        warn_if("set_tuner_gain_mode(manual)",
                esp_rtl_sdr_set_tuner_gain_mode(sdr, ESP_RTL_SDR_GAIN_MODE_MANUAL));
    } else {
        ESP_LOGI(TAG, "apply gain mode auto");
        warn_if("set_tuner_gain_mode(auto)",
                esp_rtl_sdr_set_tuner_gain_mode(sdr, ESP_RTL_SDR_GAIN_MODE_AUTO));
    }
}

/* Send one pending change to the driver (the caller enforces the spacing). */
static void apply_one(Session *s)
{
    esp_rtl_sdr_handle_t sdr = s->sdr;

    if (s->dirty & kDirtyRate) {
        ESP_LOGI(TAG, "apply sample rate %u (restart)", (unsigned)s_set.rate_sps);
        s->dirty = 0;
        restart_stream(s);          /* applies frequency and ppm, re-queues the rest */
    } else if (s->dirty & kDirtyGain) {
        s->dirty &= ~kDirtyGain;
        apply_gain(sdr);
    } else if (s->dirty & kDirtyAgc) {
        s->dirty &= ~kDirtyAgc;
        ESP_LOGI(TAG, "apply RTL AGC %d", (int)s_set.rtl_agc);
        warn_if("set_rtl_agc", esp_rtl_sdr_set_rtl_agc(sdr, s_set.rtl_agc));
    } else if (s->dirty & kDirtyBias) {
        s->dirty &= ~kDirtyBias;
        ESP_LOGW(TAG, "apply bias tee %d", (int)s_set.bias_tee);
        warn_if("set_bias_tee", esp_rtl_sdr_set_bias_tee(sdr, s_set.bias_tee));
    } else if (s->dirty & kDirtyPpm) {
        s->dirty &= ~kDirtyPpm;
        ESP_LOGI(TAG, "apply ppm %d", s_set.ppm);
        warn_if("set_freq_correction", esp_rtl_sdr_set_freq_correction(sdr, s_set.ppm));
        s->dirty |= kDirtyFreq;     /* ppm takes effect on the next tune */
    } else if (s->dirty & kDirtyFreq) {
        s->dirty &= ~kDirtyFreq;
        ESP_LOGI(TAG, "apply freq %u", (unsigned)s_set.freq_hz);
        warn_if("set_center_freq", esp_rtl_sdr_set_center_freq(sdr, s_set.freq_hz));
    }
    s->last_ep0 = xTaskGetTickCount();
}

/* Only records the request; nothing here touches the driver. */
static void handle_command(Session *s, const rtl_tcp::Command &c)
{
    const uint32_t p = c.param;

    switch (c.id) {
    case rtl_tcp::kSetFreq: {
        uint32_t q = 0;

        if (!esp_rtl_sdr_normalize_frequency(p, &q)) {
            ESP_LOGW(TAG, "set freq %u out of range, ignored", (unsigned)p);
            break;
        }
        s_set.freq_hz = q;
        s->dirty |= kDirtyFreq;
        break;
    }
    case rtl_tcp::kSetSampleRate:
        ESP_LOGI(TAG, "set sample rate %u", (unsigned)p);
        if (!esp_rtl_sdr_is_rate_supported(p)) {
            ESP_LOGW(TAG, "sample rate %u not supported, ignored", (unsigned)p);
        } else if (p != s_set.rate_sps) {
            s_set.rate_sps = p;
            s->dirty |= kDirtyRate;
        }
        break;
    case rtl_tcp::kSetGainMode:
        ESP_LOGI(TAG, "set gain mode %u (%s)", (unsigned)p, p ? "manual" : "auto");
        s_set.manual_gain = p != 0;
        s->dirty |= kDirtyGain;
        break;
    case rtl_tcp::kSetGain:
        ESP_LOGI(TAG, "set gain %d (tenth dB)", (int)rtl_tcp::as_signed(p));
        s_set.manual_gain = true;
        s_set.gain_explicit = true;
        s_set.gain_index = -1;
        s_set.gain_tenth_db = rtl_tcp::as_signed(p);
        s->dirty |= kDirtyGain;
        break;
    case rtl_tcp::kSetGainByIndex:
        ESP_LOGI(TAG, "set gain index %u", (unsigned)p);
        s_set.manual_gain = true;
        s_set.gain_explicit = true;
        s_set.gain_index = static_cast<int>(p);
        s->dirty |= kDirtyGain;
        break;
    case rtl_tcp::kSetFreqCorrection: {
        const int ppm = rtl_tcp::as_signed(p);

        ESP_LOGI(TAG, "set ppm %d", ppm);
        if (ppm < ESP_RTL_SDR_PPM_MIN || ppm > ESP_RTL_SDR_PPM_MAX) {
            ESP_LOGW(TAG, "ppm out of range, ignored");
            break;
        }
        s_set.ppm = ppm;
        s->dirty |= kDirtyPpm;
        break;
    }
    case rtl_tcp::kSetAgcMode:
        ESP_LOGI(TAG, "set RTL AGC %u", (unsigned)p);
        s_set.rtl_agc = p != 0;
        s->dirty |= kDirtyAgc;
        break;
    case rtl_tcp::kSetBiasTee:
        ESP_LOGW(TAG, "set bias tee %u", (unsigned)p);
        s_set.bias_tee = p != 0;
        s->dirty |= kDirtyBias;
        break;
    case rtl_tcp::kSetIfGain:
    case rtl_tcp::kSetTestMode:
    case rtl_tcp::kSetDirectSampling:
    case rtl_tcp::kSetOffsetTuning:
    case rtl_tcp::kSetRtlXtal:
    case rtl_tcp::kSetTunerXtal:
        ESP_LOGD(TAG, "command 0x%02x not supported by esp_rtl_sdr, ignored", c.id);
        break;
    default:
        ESP_LOGD(TAG, "unknown command 0x%02x", c.id);
        break;
    }
}

static bool send_all(int sock, const uint8_t *p, size_t n)
{
    TickType_t stalled_since = 0;

    while (n > 0) {
        const int w = send(sock, p, n, 0);

        if (w < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == ENOMEM) {
                /* The peer is not draining the socket (unplugged cable, dead
                 * client). SO_SNDTIMEO already waited; give up after a while
                 * instead of retrying forever so the next client can connect. */
                if (stalled_since == 0) {
                    stalled_since = xTaskGetTickCount();
                } else if (xTaskGetTickCount() - stalled_since >= pdMS_TO_TICKS(kSendStallMs)) {
                    ESP_LOGW(TAG, "send stalled for %u ms, dropping client", (unsigned)kSendStallMs);
                    return false;
                }
                vTaskDelay(1);
                continue;
            }
            ESP_LOGW(TAG, "send failed: errno %d", errno);
            return false;
        }
        stalled_since = 0;
        p += w;
        n -= static_cast<size_t>(w);
    }
    return true;
}

static void log_status(Session *s, uint64_t sent_bytes, uint64_t last_bytes)
{
    esp_rtl_sdr_stream_stats_t st;

    esp_rtl_sdr_stream_stats_default(&st);
    if (esp_rtl_sdr_get_stream_stats(s->sdr, &st) != ESP_OK) {
        return;
    }
    ESP_LOGI(TAG, "tcp %.2f MB/s, usb %u sps, ring overrun %u blk, slot starve %u blk, usb err %u",
             (double)(sent_bytes - last_bytes) / (kStatusPeriodMs / 1000.0) / 1e6,
             (unsigned)st.effective_sample_rate, (unsigned)st.ring_overrun_blocks,
             (unsigned)st.slot_starve_blocks, (unsigned)st.usb_transfer_errors);
}

static void sender_task(void *arg)
{
    Session *s = static_cast<Session *>(arg);
    uint8_t *buf = static_cast<uint8_t *>(malloc(CONFIG_RTLTCP_SEND_CHUNK));
    uint64_t sent = 0, last_sent = 0;
    TickType_t last_log = xTaskGetTickCount();

    while (buf && !s->stop) {
        size_t n = 0;
        esp_err_t err = ESP_ERR_INVALID_STATE;

        xSemaphoreTake(s->mtx, portMAX_DELAY);
        if (s->streaming) {
            err = esp_rtl_sdr_read(s->sdr, buf, CONFIG_RTLTCP_SEND_CHUNK, kReadTimeoutMs, &n);
        }
        xSemaphoreGive(s->mtx);

        if (err == ESP_OK && n > 0) {
            if (!send_all(s->sock, buf, n)) {
                s->stop = true;
                break;
            }
            sent += n;
        } else if (err != ESP_OK && err != ESP_RTL_SDR_ERR_TIMEOUT) {
            if (!s->streaming) {
                vTaskDelay(pdMS_TO_TICKS(10));   /* parked by a rate change / not started */
            } else if (esp_rtl_sdr_get_state(s->sdr) != ESP_RTL_SDR_STATE_STREAMING) {
                ESP_LOGW(TAG, "stream ended (%s), dongle lost?", esp_rtl_sdr_err_to_name(err));
                s->stop = true;
            }
        }

        if (xTaskGetTickCount() - last_log >= pdMS_TO_TICKS(kStatusPeriodMs)) {
            log_status(s, sent, last_sent);
            last_sent = sent;
            last_log = xTaskGetTickCount();
        }
    }
    free(buf);
    xEventGroupSetBits(s->ev, kEvSenderDone);
    vTaskDelete(nullptr);
}

static void serve_client(esp_rtl_sdr_handle_t sdr, int sock)
{
    Session s = {};
    s.sdr = sdr;
    s.sock = sock;
    s.mtx = xSemaphoreCreateMutex();
    s.ev = xEventGroupCreate();
    if (!s.mtx || !s.ev) {
        ESP_LOGE(TAG, "out of memory");
        goto cleanup;
    }

    {
        const int one = 1;
        setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        const timeval rx_to = {0, 100000};
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &rx_to, sizeof(rx_to));
        const timeval tx_to = {1, 0};
        setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tx_to, sizeof(tx_to));
        /* detect a vanished peer (cable pulled, host asleep) within ~10 s */
        const int idle = 5, intvl = 2, cnt = 3;
        setsockopt(sock, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
        setsockopt(sock, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
        setsockopt(sock, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
        setsockopt(sock, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof(cnt));
    }

    {   /* greeting first: the client waits for it before it sends any command */
        size_t gain_count = 0;
        uint8_t info[rtl_tcp::kDongleInfoSize];

        esp_rtl_sdr_get_tuner_gains(sdr, nullptr, 0, &gain_count);
        rtl_tcp::encode_dongle_info(info, tuner_type_for(esp_rtl_sdr_get_profile(sdr)),
                                    static_cast<uint32_t>(gain_count));
        if (!send_all(sock, info, sizeof(info))) {
            goto cleanup;
        }
    }

    {
        rtl_tcp::CommandParser parser;
        uint8_t rx[64];
        const TickType_t connected = xTaskGetTickCount();

        s.last_cmd = connected;
        while (!s.stop) {
            const int n = recv(sock, rx, sizeof(rx), 0);

            if (n > 0) {
                parser.feed(rx, static_cast<size_t>(n),
                            [&](const rtl_tcp::Command &c) { handle_command(&s, c); });
                s.last_cmd = xTaskGetTickCount();
            } else if (n == 0 || (errno != EAGAIN && errno != EWOULDBLOCK)) {
                ESP_LOGI(TAG, "client disconnected");
                break;
            }

            const TickType_t now = xTaskGetTickCount();

            if (!s.started) {
                /* wait for the client's initial burst to finish, then start once */
                if (now - s.last_cmd >= pdMS_TO_TICKS(kQuietBeforeStartMs) ||
                    now - connected >= pdMS_TO_TICKS(kMaxWaitBeforeStartMs)) {
                    s.dirty = 0;
                    if (start_stream(&s) != ESP_OK) {
                        break;
                    }
                    xTaskCreatePinnedToCore(sender_task, "rtl_tcp_tx", 4096, &s, 5, nullptr, 0);
                }
            } else if (s.dirty != 0 && now - s.last_ep0 >= pdMS_TO_TICKS(kEp0SpacingMs)) {
                apply_one(&s);
            }
        }
    }

    s.stop = true;
    shutdown(sock, SHUT_RDWR);          /* unblock a sender stuck in send() */
    if (s.started) {
        xEventGroupWaitBits(s.ev, kEvSenderDone, pdFALSE, pdTRUE, pdMS_TO_TICKS(7000));
        s.streaming = false;
        warn_if("stop", esp_rtl_sdr_stop(sdr, 0));
        if (esp_rtl_sdr_get_state(sdr) == ESP_RTL_SDR_STATE_FAULT) {
            warn_if("reset after fault", esp_rtl_sdr_reset(sdr));
        }
    }
cleanup:
    if (s.ev) {
        vEventGroupDelete(s.ev);
    }
    if (s.mtx) {
        vSemaphoreDelete(s.mtx);
    }
}

void rtl_tcp_server_run(esp_rtl_sdr_handle_t sdr, int port)
{
    sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);

    const int ls = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    const int one = 1;
    if (ls < 0) {
        ESP_LOGE(TAG, "socket: errno %d", errno);
        return;
    }
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    if (bind(ls, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0 || listen(ls, 1) < 0) {
        ESP_LOGE(TAG, "bind/listen: errno %d", errno);
        close(ls);
        return;
    }
    ESP_LOGI(TAG, "listening on port %d", port);

    for (;;) {
        fd_set rfds;
        timeval tv = {1, 0};

        FD_ZERO(&rfds);
        FD_SET(ls, &rfds);
        if (select(ls + 1, &rfds, nullptr, nullptr, &tv) <= 0) {
            continue;
        }

        sockaddr_in remote;
        socklen_t rlen = sizeof(remote);
        const int cs = accept(ls, reinterpret_cast<sockaddr *>(&remote), &rlen);
        if (cs < 0) {
            continue;
        }
        ESP_LOGI(TAG, "client connected: %s", inet_ntoa(remote.sin_addr));

        esp_rtl_sdr_device_info_t info;
        if (esp_rtl_sdr_get_device_info(sdr, &info) != ESP_OK || !info.present) {
            ESP_LOGW(TAG, "no dongle attached, dropping client");
        } else {
            serve_client(sdr, cs);
        }
        shutdown(cs, SHUT_RDWR);
        close(cs);
    }
}
