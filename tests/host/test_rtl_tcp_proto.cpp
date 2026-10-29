/*
 * Host tests for examples/rtl_tcp_server/main/rtl_tcp_proto.hpp: command
 * decoding, TCP-segment reassembly and the dongle greeting. No ESP-IDF.
 */

#include <cstdint>
#include <cstdio>
#include <vector>

#include "rtl_tcp_proto.hpp"

static int g_failed = 0;
static int g_passed = 0;

#define CHECK(cond)                                                            \
    do {                                                                       \
        if (!(cond)) {                                                         \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);        \
            g_failed++;                                                        \
        } else {                                                               \
            g_passed++;                                                        \
        }                                                                      \
    } while (0)

static void test_be32_roundtrip()
{
    uint8_t b[4];

    rtl_tcp::put_be32(b, 0x01020304u);
    CHECK(b[0] == 1 && b[1] == 2 && b[2] == 3 && b[3] == 4);
    CHECK(rtl_tcp::get_be32(b) == 0x01020304u);
    rtl_tcp::put_be32(b, 0xfffffff6u);
    CHECK(rtl_tcp::as_signed(rtl_tcp::get_be32(b)) == -10);
}

static void test_decode_command()
{
    /* set freq 100 MHz: 0x05F5E100 */
    const uint8_t raw[5] = {0x01, 0x05, 0xF5, 0xE1, 0x00};
    const rtl_tcp::Command c = rtl_tcp::decode_command(raw);

    CHECK(c.id == rtl_tcp::kSetFreq);
    CHECK(c.param == 100000000u);
}

static void test_dongle_info()
{
    uint8_t info[rtl_tcp::kDongleInfoSize];

    rtl_tcp::encode_dongle_info(info, rtl_tcp::kTunerR828D, 28);
    CHECK(info[0] == 'R' && info[1] == 'T' && info[2] == 'L' && info[3] == '0');
    CHECK(rtl_tcp::get_be32(info + 4) == 6u);
    CHECK(rtl_tcp::get_be32(info + 8) == 28u);
    CHECK(sizeof(info) == 12);
}

static std::vector<rtl_tcp::Command> feed_in_pieces(const std::vector<uint8_t> &stream,
                                                    size_t piece)
{
    rtl_tcp::CommandParser parser;
    std::vector<rtl_tcp::Command> out;

    for (size_t off = 0; off < stream.size(); off += piece) {
        const size_t n = (stream.size() - off < piece) ? (stream.size() - off) : piece;
        parser.feed(stream.data() + off, n,
                    [&](const rtl_tcp::Command &c) { out.push_back(c); });
    }
    return out;
}

static void test_parser_reassembly()
{
    /* freq 100 MHz, rate 2.048 MS/s, gain -5 */
    const std::vector<uint8_t> stream = {
        0x01, 0x05, 0xF5, 0xE1, 0x00,
        0x02, 0x00, 0x1F, 0x40, 0x00,
        0x04, 0xFF, 0xFF, 0xFF, 0xFB,
    };

    for (size_t piece : {size_t{1}, size_t{2}, size_t{3}, size_t{4}, size_t{5}, size_t{7},
                         size_t{15}, size_t{64}}) {
        const auto cmds = feed_in_pieces(stream, piece);

        CHECK(cmds.size() == 3);
        if (cmds.size() == 3) {
            CHECK(cmds[0].id == rtl_tcp::kSetFreq && cmds[0].param == 100000000u);
            CHECK(cmds[1].id == rtl_tcp::kSetSampleRate && cmds[1].param == 2048000u);
            CHECK(cmds[2].id == rtl_tcp::kSetGain && rtl_tcp::as_signed(cmds[2].param) == -5);
        }
    }
}

static void test_parser_partial_and_reset()
{
    rtl_tcp::CommandParser parser;
    int count = 0;
    const uint8_t part[3] = {0x0e, 0x00, 0x00};

    parser.feed(part, sizeof(part), [&](const rtl_tcp::Command &) { count++; });
    CHECK(count == 0);
    parser.reset();                 /* a new connection must not inherit a half command */

    const uint8_t full[5] = {0x0e, 0x00, 0x00, 0x00, 0x01};
    parser.feed(full, sizeof(full), [&](const rtl_tcp::Command &c) {
        count++;
        CHECK(c.id == rtl_tcp::kSetBiasTee && c.param == 1u);
    });
    CHECK(count == 1);
}

int main()
{
    test_be32_roundtrip();
    test_decode_command();
    test_dongle_info();
    test_parser_reassembly();
    test_parser_partial_and_reset();

    std::printf("rtl_tcp_proto tests: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
