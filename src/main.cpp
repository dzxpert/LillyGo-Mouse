#include <Adafruit_TinyUSB.h>
#include <Preferences.h>
#include <USB.h>

#include "TFT/Display.h"
#include "USB/USBMouseCommands.h"

#if ARDUINO_USB_MODE || ARDUINO_USB_CDC_ON_BOOT || ARDUINO_USB_MSC_ON_BOOT || ARDUINO_USB_DFU_ON_BOOT
#error This firmware needs USB OTG with automatic USB interfaces disabled.
#endif

namespace
{
    namespace MouseCommands = Devices::USB::MouseCommands;
    constexpr uint8_t RID_MOUSE = 1;
    constexpr uint8_t desc_hid_report[] =
    {
        TUD_HID_REPORT_DESC_MOUSE(HID_REPORT_ID(RID_MOUSE)),
        HID_USAGE_PAGE_N(HID_USAGE_PAGE_VENDOR, 2),
        HID_USAGE(0x01),
        HID_COLLECTION(HID_COLLECTION_APPLICATION),
            HID_REPORT_ID(MouseCommands::ReportId)
            HID_LOGICAL_MIN(0),
            HID_LOGICAL_MAX_N(255, 2),
            HID_REPORT_SIZE(8),
            HID_REPORT_COUNT(MouseCommands::ReportSize),
            HID_USAGE(0x02),
            HID_INPUT(HID_DATA | HID_VARIABLE | HID_ABSOLUTE),
            HID_USAGE(0x03),
            HID_OUTPUT(HID_DATA | HID_VARIABLE | HID_ABSOLUTE),
            HID_REPORT_ID(MouseCommands::SessionReportId)
            HID_REPORT_COUNT(MouseCommands::SessionReportSize),
            HID_USAGE(0x04),
            HID_FEATURE(HID_DATA | HID_VARIABLE | HID_ABSOLUTE),
        HID_COLLECTION_END
    };

    Adafruit_USBD_HID hid;
    bool usbStarted = false;
    char serial[19];

    const char* usbStatus()
    {
        if (!hid.isValid()) return "HID init failed";
        if (!usbStarted) return "USB init failed";
        if (!tud_connected()) return "no host";
        if (!TinyUSBDevice.mounted()) return "enumerating";
        if (TinyUSBDevice.suspended()) return "suspended";
        return "ready";
    }
}

void setup()
{
    Preferences prefs;
    prefs.begin("usbarmyknife"); // Reuse the existing HID PIN when upgrading from the original build.
    Display::begin();
    MouseCommands::begin(prefs);
    prefs.end();

    TinyUSBDevice.clearConfiguration();
    TinyUSBDevice.setID(0xcafe, 0x4041);
    TinyUSBDevice.setDeviceVersion(0x0300);
    TinyUSBDevice.setManufacturerDescriptor("LILYGO");
    TinyUSBDevice.setProductDescriptor("T-Dongle S3 Mouse");
    snprintf(serial, sizeof(serial), "MOUSE-%012llX", static_cast<unsigned long long>(ESP.getEfuseMac()));
    TinyUSBDevice.setSerialDescriptor(serial);

    hid.setPollInterval(2);
    hid.setReportDescriptor(desc_hid_report, sizeof(desc_hid_report));
    hid.setStringDescriptor("USB Mouse");
    hid.enableOutEndpoint(true);
    hid.setReportCallback(MouseCommands::getReport, MouseCommands::setReport);
    if (hid.begin())
    {
        usbStarted = USB.begin() && tud_inited();
        if (usbStarted)
        {
            tud_disconnect();
            delay(250);
            tud_connect();
        }
    }
}

void loop()
{
    if (usbStarted)
    {
        MouseCommands::service(hid, RID_MOUSE);
    }
    static const char* previousStatus = nullptr;
    static unsigned long lastUpdate = 0;
    if (!previousStatus || millis() - lastUpdate >= 500)
    {
        lastUpdate = millis();
        const char* status = usbStatus();
        if (!previousStatus || strcmp(status, previousStatus) != 0)
        {
            char line[27];
            snprintf(line, sizeof(line), "USB: %-21s", status);
            Display::write(0, 0, line);
            previousStatus = status;
        }
    }
    delay(1);
}
