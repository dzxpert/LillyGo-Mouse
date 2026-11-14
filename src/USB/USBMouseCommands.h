#pragma once

#include <Adafruit_TinyUSB.h>
#include <Preferences.h>

namespace Devices::USB::MouseCommands
{
    constexpr uint8_t ReportId = 2;
    constexpr uint8_t SessionReportId = 3;
    constexpr uint8_t ReportSize = 32;
    constexpr uint8_t SessionReportSize = 25;

    void begin(Preferences& prefs);
    uint16_t getReport(uint8_t id, hid_report_type_t type, uint8_t* buffer, uint16_t length);
    void setReport(uint8_t id, hid_report_type_t type, const uint8_t* buffer, uint16_t length);
    void service(Adafruit_USBD_HID& hid, uint8_t mouseReportId);
}
