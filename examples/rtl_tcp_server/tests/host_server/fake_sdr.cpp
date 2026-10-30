/*
 * Host harness for rtl_tcp_server.cpp: the real server code, a fake esp_rtl_sdr driver that
 * "receives" the reference test signal from pattern.py instead of a dongle, and a main().
 *
 *   rtl_tcp_host --pattern FILE [--port N] [--pace]
 *
 * --pace   deliver samples in real time (rate set by the client); use this to look at the signal
 *          in SDR++ / GQRX. Without it the fake delivers as fast as the server asks, which is what the
 *          integrity tests want.
 * Stream start (esp_rtl_sdr_start_hz) rewinds the signal to sample 0, like a fresh capture.
 */
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <mutex>
#include <vector>

#include "esp_rtl_sdr.h"
#include "rtl_tcp_server.h"

static const uint32_t kN = 16384;              /* must match pattern.py */
static const uint32_t kColSamples = kN * 250;

struct esp_rtl_sdr_handle {};

namespace {
std::mutex g_mu;
std::vector<uint8_t> g_table;                  /* per column: crossfade block, steady block, 2N bytes each */
uint32_t g_cols = 0;
esp_rtl_sdr_state_t g_state = ESP_RTL_SDR_STATE_IDLE;
uint32_t g_rate = 0;
uint64_t g_produced = 0;                       /* bytes delivered since start */
timespec g_t0;
bool g_pace = false;

double now_s()
{
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

void fill(uint8_t *dst, uint64_t pos, size_t n)
{
    const uint64_t col_bytes = 2ull * kColSamples;
    const size_t block = 2 * (size_t)kN;
    while (n > 0) {
        const uint32_t col = (uint32_t)((pos / col_bytes) % g_cols);
        const uint64_t in_col = pos % col_bytes;
        const uint8_t *base = &g_table[(size_t)col * 2 * block];
        const uint8_t *src;
        size_t chunk;
        if (in_col < block) {                       /* crossfade from the previous column */
            src = base + in_col;
            chunk = block - (size_t)in_col;
        } else {                                    /* steady block, periodic */
            const size_t off = (size_t)(in_col % block);
            src = base + block + off;
            chunk = block - off;
        }
        if (chunk > n) chunk = n;
        memcpy(dst, src, chunk);
        dst += chunk; pos += chunk; n -= chunk;
    }
}
}  // namespace

const char *esp_rtl_sdr_err_to_name(esp_err_t err)
{
    return err == ESP_OK ? "ESP_OK" : err == ESP_RTL_SDR_ERR_TIMEOUT ? "TIMEOUT" : "ERR";
}

esp_err_t esp_rtl_sdr_get_device_info(esp_rtl_sdr_handle_t, esp_rtl_sdr_device_info_t *out)
{
    memset(out, 0, sizeof(*out));
    out->present = true;
    return ESP_OK;
}

esp_rtl_sdr_profile_t esp_rtl_sdr_get_profile(esp_rtl_sdr_handle_t) { return ESP_RTL_SDR_PROFILE_BLOG_V4; }

esp_err_t esp_rtl_sdr_get_tuner_gains(esp_rtl_sdr_handle_t, int *out, size_t max, size_t *count)
{
    static const int gains[28] = {0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229,
                                  254, 280, 297, 328, 338, 364, 372, 386, 402, 421, 434, 439, 445, 480};
    if (count) *count = 28;
    for (size_t i = 0; out && i < max && i < 28; i++) out[i] = gains[i];
    return ESP_OK;
}

bool esp_rtl_sdr_normalize_frequency(uint32_t in_hz, uint32_t *out_hz)
{
    if (in_hz == 0 || in_hz > 1766000000u) return false;
    *out_hz = in_hz;
    return true;
}

bool esp_rtl_sdr_is_rate_supported(uint32_t r)
{
    return (r >= 225001 && r <= 300000) || (r >= 900001 && r <= 3200000);
}

esp_err_t esp_rtl_sdr_start_hz(esp_rtl_sdr_handle_t, uint32_t, uint32_t rate)
{
    std::lock_guard<std::mutex> l(g_mu);
    g_rate = rate;
    g_produced = 0;
    clock_gettime(CLOCK_MONOTONIC, &g_t0);
    g_state = ESP_RTL_SDR_STATE_STREAMING;
    return ESP_OK;
}

esp_err_t esp_rtl_sdr_stop(esp_rtl_sdr_handle_t, uint32_t)
{
    std::lock_guard<std::mutex> l(g_mu);
    g_state = ESP_RTL_SDR_STATE_IDLE;
    return ESP_OK;
}

esp_err_t esp_rtl_sdr_reset(esp_rtl_sdr_handle_t) { return esp_rtl_sdr_stop(nullptr, 0); }

esp_rtl_sdr_state_t esp_rtl_sdr_get_state(esp_rtl_sdr_handle_t)
{
    std::lock_guard<std::mutex> l(g_mu);
    return g_state;
}

esp_err_t esp_rtl_sdr_read(esp_rtl_sdr_handle_t, uint8_t *buf, size_t max, uint32_t timeout_ms, size_t *out)
{
    *out = 0;
    for (double waited = 0;; waited += 0.002) {
        {
            std::lock_guard<std::mutex> l(g_mu);
            if (g_state != ESP_RTL_SDR_STATE_STREAMING) return ESP_ERR_INVALID_STATE;
            size_t n = max & ~(size_t)1;
            if (g_pace) {
                timespec t = g_t0;
                const double el = now_s() - (t.tv_sec + t.tv_nsec * 1e-9);
                const uint64_t due = (uint64_t)(el * g_rate) * 2;
                const uint64_t avail = due > g_produced ? due - g_produced : 0;
                if (avail < n) n = (size_t)(avail & ~1ull);
            }
            if (n > 0) {
                fill(buf, g_produced, n);
                g_produced += n;
                *out = n;
                return ESP_OK;
            }
        }
        if (waited * 1000 >= timeout_ms) return ESP_RTL_SDR_ERR_TIMEOUT;
        timespec ts = {0, 2000000};
        nanosleep(&ts, nullptr);
    }
}

#define OK_STUB(sig) esp_err_t sig { return ESP_OK; }
OK_STUB(esp_rtl_sdr_set_center_freq(esp_rtl_sdr_handle_t, uint32_t))
OK_STUB(esp_rtl_sdr_set_freq_correction(esp_rtl_sdr_handle_t, int))
OK_STUB(esp_rtl_sdr_set_tuner_gain_mode(esp_rtl_sdr_handle_t, esp_rtl_sdr_gain_mode_t))
OK_STUB(esp_rtl_sdr_set_tuner_gain(esp_rtl_sdr_handle_t, int))
OK_STUB(esp_rtl_sdr_set_rtl_agc(esp_rtl_sdr_handle_t, bool))
OK_STUB(esp_rtl_sdr_set_bias_tee(esp_rtl_sdr_handle_t, bool))

void esp_rtl_sdr_stream_stats_default(esp_rtl_sdr_stream_stats_t *out) { memset(out, 0, sizeof(*out)); }

esp_err_t esp_rtl_sdr_get_stream_stats(esp_rtl_sdr_handle_t, esp_rtl_sdr_stream_stats_t *out)
{
    std::lock_guard<std::mutex> l(g_mu);
    out->effective_sample_rate = g_rate;
    return ESP_OK;
}

int main(int argc, char **argv)
{
    /* lwIP on the board reports a closed peer as an error, a POSIX host kills the process with SIGPIPE. */
    signal(SIGPIPE, SIG_IGN);
    const char *pattern = nullptr;
    int port = 1234;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--pattern") && i + 1 < argc) pattern = argv[++i];
        else if (!strcmp(argv[i], "--port") && i + 1 < argc) port = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--pace")) g_pace = true;
        else { fprintf(stderr, "usage: %s --pattern FILE [--port N] [--pace]\n", argv[0]); return 2; }
    }
    FILE *f = pattern ? fopen(pattern, "rb") : nullptr;
    uint8_t head[16];
    if (!f || fread(head, 1, 16, f) != 16 || memcmp(head, "RTLP", 4) != 0) {
        fprintf(stderr, "cannot read pattern file (make it with: python3 pattern.py write FILE)\n");
        return 2;
    }
    uint32_t n, cs, cols;
    memcpy(&n, head + 4, 4); memcpy(&cs, head + 8, 4); memcpy(&cols, head + 12, 4);
    if (n != kN || cs != kColSamples || cols == 0) { fprintf(stderr, "pattern file does not match\n"); return 2; }
    g_cols = cols;
    g_table.resize((size_t)cols * 4 * kN);
    if (fread(g_table.data(), 1, g_table.size(), f) != g_table.size()) { fprintf(stderr, "short file\n"); return 2; }
    fclose(f);
    fprintf(stderr, "rtl_tcp host: %u columns, %s\n", cols, g_pace ? "real-time pacing" : "unpaced");
    static esp_rtl_sdr_handle h;
    rtl_tcp_server_run(&h, port);
    return 0;
}
