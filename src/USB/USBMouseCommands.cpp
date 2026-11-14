#include "USBMouseCommands.h"

#include <array>
#include <atomic>
#include <bootloader_random.h>
#include <device/dcd.h>
#include <esp_attr.h>
#include <esp_random.h>
#include <mbedtls/md.h>
#include <mutex>
#include <optional>

#include "../TFT/Display.h"

extern "C" {
#include <mbedtls/constant_time.h>
}

namespace
{
    using namespace Devices::USB::MouseCommands;

    constexpr uint8_t ProtocolVersion = 2;
    constexpr uint8_t MoveCommand = 1;
    constexpr uint8_t MoveReply = 2;
    constexpr uint32_t MoveLifetimeMs = 250;
    constexpr size_t HeaderSize = 8; // version, opcode, sequence (LE), X, Y
    constexpr size_t TagSize = ReportSize - HeaderSize;
    constexpr char PinSetting[] = "mouseHidPin";
    constexpr uint16_t PinCount = 10000;

    using Packet = std::array<uint8_t, ReportSize>;
    constexpr size_t PinLength = 4;
    char pin[PinLength + 1]{};
    constexpr size_t BootNonceSize = 16;
    constexpr size_t NonceSize = BootNonceSize + sizeof(uint64_t);
    static_assert(NonceSize + 1 == SessionReportSize);
    std::array<uint8_t, NonceSize> nonce{};
    bool pinReady = false;
    uint64_t sessionCounter = 0;
    std::atomic<bool> usbReset{false};
    std::mutex sessionMutex;
    bool sessionActive = false;
    uint32_t lastSequence = 0;

    struct Move
    {
        Packet packet;
        uint32_t receivedAt;
    };

    std::optional<Move> pending;
    std::optional<Packet> reply;

    void clearSessionOnReset()
    {
        if (usbReset.exchange(false))
        {
            sessionActive = false;
            pending.reset();
            reply.reset();
        }
    }

    bool calculateTag(const uint8_t* header, uint8_t* digest)
    {
        std::array<uint8_t, NonceSize + HeaderSize> message;
        memcpy(message.data(), nonce.data(), nonce.size());
        memcpy(message.data() + nonce.size(), header, HeaderSize);
        return mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256),
                              reinterpret_cast<const uint8_t*>(pin), PinLength,
                              message.data(), message.size(), digest) == 0;
    }
}

extern "C" void IRAM_ATTR tud_event_hook_cb(uint8_t, uint32_t eventId, bool)
{
    if (eventId == DCD_EVENT_BUS_RESET || eventId == DCD_EVENT_UNPLUGGED)
    {
        usbReset.store(true);
    }
}

void Devices::USB::MouseCommands::begin(Preferences& prefs)
{
    const bool storedPin = prefs.isKey(PinSetting);
    uint16_t value = prefs.getUShort(PinSetting, PinCount);
    if (storedPin && value >= PinCount)
    {
        Display::write(0, 24, "PIN read failed");
        return;
    }

    // This profile leaves RF and ADC inactive, so temporarily enable the
    // ESP32-S3 internal entropy source for the boot nonce and first PIN.
    bootloader_random_enable();
    esp_fill_random(nonce.data(), BootNonceSize);
    if (!storedPin)
    {
        constexpr uint32_t limit = UINT32_MAX - (UINT32_MAX % PinCount);
        uint32_t random;
        do
        {
            random = esp_random();
        } while (random >= limit);
        value = random % PinCount;
    }
    bootloader_random_disable();

    if (!storedPin && prefs.putUShort(PinSetting, value) != sizeof(value))
    {
        Display::write(0, 24, "PIN storage failed");
        return;
    }

    snprintf(pin, sizeof(pin), "%04u", static_cast<unsigned>(value));
    pinReady = true;
    Display::write(0, 24, "HID control PIN");
    Display::write(0, 36, pin);
}

uint16_t Devices::USB::MouseCommands::getReport(uint8_t id, hid_report_type_t type,
                                               uint8_t* buffer, uint16_t length)
{
    if (id != SessionReportId || type != HID_REPORT_TYPE_FEATURE ||
        length < SessionReportSize || !pinReady)
    {
        return 0;
    }

    std::lock_guard<std::mutex> lock(sessionMutex);
    clearSessionOnReset();
    ++sessionCounter;
    for (size_t i = 0; i < sizeof(sessionCounter); ++i)
    {
        nonce[BootNonceSize + i] = sessionCounter >> (8 * i);
    }
    lastSequence = 0;
    pending.reset();
    reply.reset();
    sessionActive = true;
    buffer[0] = ProtocolVersion;
    memcpy(buffer + 1, nonce.data(), nonce.size());
    return SessionReportSize;
}

void Devices::USB::MouseCommands::setReport(uint8_t id, hid_report_type_t type,
                                           const uint8_t* buffer, uint16_t length)
{
    if (type != HID_REPORT_TYPE_OUTPUT)
    {
        return;
    }
    // TinyUSB passes interrupt OUT reports with their ID still in the payload.
    if (id == 0 && length == ReportSize + 1)
    {
        id = *buffer++;
        --length;
    }
    if (id != ReportId || length != ReportSize ||
        buffer[0] != ProtocolVersion || buffer[1] != MoveCommand ||
        buffer[6] == 0x80 || buffer[7] == 0x80)
    {
        return;
    }

    std::lock_guard<std::mutex> lock(sessionMutex);
    clearSessionOnReset();
    if (!sessionActive || pending || reply)
    {
        return;
    }

    const uint32_t seq = uint32_t(buffer[2]) | (uint32_t(buffer[3]) << 8) |
                         (uint32_t(buffer[4]) << 16) | (uint32_t(buffer[5]) << 24);
    uint8_t digest[32];
    if (seq <= lastSequence || !calculateTag(buffer, digest) ||
        mbedtls_ct_memcmp(buffer + HeaderSize, digest, TagSize) != 0)
    {
        return;
    }

    lastSequence = seq;
    pending = Move{{}, millis()};
    memcpy(pending->packet.data(), buffer, HeaderSize);
}

void Devices::USB::MouseCommands::service(Adafruit_USBD_HID& hid, uint8_t mouseReportId)
{
    std::lock_guard<std::mutex> lock(sessionMutex);
    clearSessionOnReset();
    if (!TinyUSBDevice.mounted() || TinyUSBDevice.suspended())
    {
        pending.reset();
        reply.reset();
        return;
    }
    if (!hid.ready())
    {
        return;
    }
    if (reply)
    {
        if (hid.sendReport(ReportId, reply->data(), reply->size()))
        {
            reply.reset();
        }
        return;
    }
    if (!pending)
    {
        return;
    }

    auto& packet = pending->packet;
    const bool expired = millis() - pending->receivedAt >= MoveLifetimeMs;
    if (!expired && !hid.mouseMove(mouseReportId, static_cast<int8_t>(packet[6]),
                                   static_cast<int8_t>(packet[7])))
    {
        return;
    }

    // Send the acknowledgment only after TinyUSB accepts the mouse report.
    packet[1] = MoveReply;
    packet[6] = expired ? 1 : 0;
    packet[7] = 0;
    uint8_t digest[32];
    if (calculateTag(packet.data(), digest))
    {
        memcpy(packet.data() + HeaderSize, digest, TagSize);
        reply = packet;
    }
    pending.reset();
}
