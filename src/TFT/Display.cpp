#include "Display.h"

#include <LovyanGFX.hpp>

namespace
{
    class DongleDisplay : public lgfx::LGFX_Device
    {
        lgfx::Panel_ST7735S panel;
        lgfx::Bus_SPI bus;
        lgfx::Light_PWM backlight;

    public:
        DongleDisplay()
        {
            auto spi = bus.config();
            spi.spi_host = SPI3_HOST;
            spi.spi_mode = 0;
            spi.freq_write = 27000000;
            spi.freq_read = 16000000;
            spi.pin_sclk = 5;
            spi.pin_mosi = 3;
            spi.pin_miso = -1;
            spi.pin_dc = 2;
            spi.spi_3wire = true;
            spi.use_lock = false;
            spi.dma_channel = SPI_DMA_CH_AUTO;
            bus.config(spi);
            panel.setBus(&bus);

            auto screen = panel.config();
            screen.pin_cs = 4;
            screen.pin_rst = 1;
            screen.pin_busy = -1;
            screen.panel_width = 80;
            screen.panel_height = 160;
            screen.offset_rotation = 1;
            screen.offset_x = 26;
            screen.offset_y = 1;
            screen.memory_width = 132;
            screen.memory_height = 160;
            screen.dummy_read_pixel = 8;
            screen.dummy_read_bits = 1;
            screen.readable = true;
            screen.invert = true;
            screen.rgb_order = false;
            screen.dlen_16bit = false;
            screen.bus_shared = true;
            panel.config(screen);

            auto light = backlight.config();
            light.pin_bl = 38;
            light.invert = true;
            light.freq = 12000;
            light.pwm_channel = 7;
            backlight.config(light);
            panel.setLight(&backlight);
            setPanel(&panel);
        }
    };

    DongleDisplay lcd;
}

void Display::begin()
{
    lcd.init();
    lcd.setBrightness(128);
    lcd.setTextSize(1);
    lcd.clear(TFT_BLACK);
    lcd.display();
}

void Display::write(int x, int y, const char* text)
{
    lcd.setCursor(x, y);
    lcd.setTextColor(TFT_WHITE, TFT_BLACK);
    lcd.println(text);
    lcd.display();
}
