# LillyMouse USB

Use a LILYGO T-Dongle S3 as a programmable USB mouse on Windows. The dongle
registers a standard mouse collection and a vendor-defined HID command
collection on the same USB interface. The Python controller sends commands over
that USB connection and can read the Windows cursor for position feedback.

## What you need

- A LILYGO T-Dongle S3 with the built-in display.
- Windows, Python 3.11 or later, and Git for fetching the firmware dependencies.
- PlatformIO Core and the Python HID dependency. Install both as shown below.

## Flash the dongle

From the repository root in PowerShell or Command Prompt:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install platformio -r tools/mouse-requirements.txt
.\.venv\Scripts\pio.exe run
```

Unplug the dongle, then hold **BOOT** while plugging it in to enter its upload
mode. Run:

```powershell
.\.venv\Scripts\pio.exe run -t upload
```

If PlatformIO cannot select the port, run `.\.venv\Scripts\pio.exe device list`
and add `--upload-port COMx` to the upload command. After upload, unplug the
dongle and plug it back in normally.

The display shows USB status and a four-digit PIN. The firmware saves the PIN
across reboots and reuses the PIN from the earlier LillyMouse build if present.
Keep any leading zeros when entering it.

## Move the mouse

```powershell
.\.venv\Scripts\python.exe tools/mouse_controller.py status
.\.venv\Scripts\python.exe tools/mouse_controller.py position
.\.venv\Scripts\python.exe tools/mouse_controller.py move 120 120 --measure
.\.venv\Scripts\python.exe tools/mouse_controller.py move-to 500 300
```

| Command | Meaning |
| --- | --- |
| `status` | Finds the wired command collection and prints the dongle serial number. |
| `position` | Reads the Windows cursor in physical screen pixels. |
| `move DX DY` | Sends one relative HID report. Each axis accepts −127 to 127 counts. Positive values move right or down. |
| `move DX DY --measure` | Prints the sent counts, cursor before and after, and measured pixel displacement. |
| `move-to X Y` | Uses repeated cursor readings and relative corrections to approach a screen coordinate within two pixels by default. |

Windows pointer speed and acceleration can change how many pixels a set of HID
counts moves the cursor. Display edges can also limit the displacement. Keep
the physical mouse still while taking a measurement.

Movement commands prompt for the PIN or read it from the `LILLY_MOUSE_PIN`
environment variable. If several dongles are connected, place
`--serial <serial>` before the subcommand.

### Use from Python

```python
from getpass import getpass
from tools.mouse_controller import MouseController

with MouseController(getpass("Dongle PIN: ")) as mouse:
    print(mouse.move_measured(20, -5))
    print(mouse.position())
    print(mouse.move_to(500, 300))
```

## USB command protocol

The development USB ID is `CAFE:4041`. The command collection uses usage page
`0xFF00` and usage `0x01`. Reports below start with their one-byte report ID.

| Direction | ID | Payload |
| --- | --- | --- |
| Feature read | 3 | Protocol version `2` and a 24-byte session nonce. |
| Host → dongle | 2 | Version `2`, opcode `1`, little-endian unsigned 32-bit sequence, signed byte `dx`, signed byte `dy`, and a 24-byte HMAC tag. |
| Dongle → host | 2 | Version `2`, opcode `2`, the same sequence, status byte, zero byte, and a 24-byte HMAC tag. |

The tag is the first 24 bytes of HMAC-SHA256 over the nonce followed by the
eight-byte payload header, using the four ASCII PIN digits as the key. Begin
each session by reading feature report 3. Send increasing sequence numbers and
wait for the acknowledgment before sending the next command. Status `0`
means TinyUSB accepted the mouse report, and `1` means the command expired.
The controller leaves any timed-out move unreplayed. A captured command lets
someone test all 10,000 PINs offline.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s test/python -v
```

The tests use a simulated HID device. To check a flashed dongle, run `status`
and a measured move. Windows should list a mouse collection and a vendor-defined
collection under the same USB device during normal operation.

## Credits

The firmware is derived from [USB Army Knife](https://github.com/i-am-shodan/USBArmyKnife).
See [LICENSE](LICENSE) for its MIT license and copyright notice.
