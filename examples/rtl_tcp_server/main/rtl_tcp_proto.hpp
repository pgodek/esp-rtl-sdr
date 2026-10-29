/*
 * rtl_tcp wire protocol helpers. Pure functions: no ESP-IDF, no sockets, so
 * they are covered by tests/host/test_rtl_tcp_proto.cpp.
 *
 * Protocol (public, de-facto standard shared by rtl_tcp clients):
 *   server -> client, once: "RTL0", u32 tuner type, u32 gain count (big endian)
 *   server -> client, then: raw CU8 I/Q
 *   client -> server: 5-byte commands, 1 byte id + u32 parameter (big endian)
 */
#pragma once

#include <cstddef>
#include <cstdint>

namespace rtl_tcp {

constexpr size_t kCommandSize = 5;
constexpr size_t kDongleInfoSize = 12;

enum Cmd : uint8_t {
    kSetFreq = 0x01,
    kSetSampleRate = 0x02,
    kSetGainMode = 0x03,      /* 0 = auto, 1 = manual */
    kSetGain = 0x04,          /* tenths of dB */
    kSetFreqCorrection = 0x05,/* ppm, signed */
    kSetIfGain = 0x06,
    kSetTestMode = 0x07,
    kSetAgcMode = 0x08,       /* RTL2832 digital AGC */
    kSetDirectSampling = 0x09,
    kSetOffsetTuning = 0x0a,
    kSetRtlXtal = 0x0b,
    kSetTunerXtal = 0x0c,
    kSetGainByIndex = 0x0d,
    kSetBiasTee = 0x0e,
};

/** Tuner numbers clients expect in the greeting (librtlsdr enum values). */
enum TunerType : uint32_t {
    kTunerUnknown = 0,
    kTunerR820T = 5,
    kTunerR828D = 6,
};

struct Command {
    uint8_t id;
    uint32_t param;
};

inline uint32_t get_be32(const uint8_t *p)
{
    return (static_cast<uint32_t>(p[0]) << 24) | (static_cast<uint32_t>(p[1]) << 16) |
           (static_cast<uint32_t>(p[2]) << 8) | static_cast<uint32_t>(p[3]);
}

inline void put_be32(uint8_t *p, uint32_t v)
{
    p[0] = static_cast<uint8_t>(v >> 24);
    p[1] = static_cast<uint8_t>(v >> 16);
    p[2] = static_cast<uint8_t>(v >> 8);
    p[3] = static_cast<uint8_t>(v);
}

inline Command decode_command(const uint8_t *b)
{
    return Command{b[0], get_be32(b + 1)};
}

inline void encode_dongle_info(uint8_t *out, uint32_t tuner_type, uint32_t gain_count)
{
    out[0] = 'R';
    out[1] = 'T';
    out[2] = 'L';
    out[3] = '0';
    put_be32(out + 4, tuner_type);
    put_be32(out + 8, gain_count);
}

/** Parameter reinterpreted as signed (ppm, gain). */
inline int32_t as_signed(uint32_t v)
{
    return static_cast<int32_t>(v);
}

/** Reassembles commands from arbitrary TCP segment boundaries. */
class CommandParser {
public:
    template <typename F>
    void feed(const uint8_t *data, size_t len, F &&on_command)
    {
        while (len > 0) {
            const size_t take = (kCommandSize - have_ < len) ? (kCommandSize - have_) : len;
            for (size_t i = 0; i < take; ++i) {
                buf_[have_ + i] = data[i];
            }
            have_ += take;
            data += take;
            len -= take;
            if (have_ == kCommandSize) {
                on_command(decode_command(buf_));
                have_ = 0;
            }
        }
    }

    void reset() { have_ = 0; }

private:
    uint8_t buf_[kCommandSize] = {};
    size_t have_ = 0;
};

} // namespace rtl_tcp
