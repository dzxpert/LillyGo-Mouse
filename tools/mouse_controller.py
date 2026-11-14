"""Send authenticated movement commands to the LILYGO mouse over wired USB HID."""

import argparse
import ctypes
from ctypes import wintypes
import getpass
import hmac
import json
import math
import os
import struct
import sys
import time

import hid


VID, PID = 0xCAFE, 0x4041
USAGE_PAGE, USAGE = 0xFF00, 0x01
COMMAND_REPORT, SESSION_REPORT = 2, 3
PROTOCOL_VERSION = 2
MOVE, RESULT = 1, 2
NONCE_SIZE, TAG_SIZE = 24, 24
HEADER = struct.Struct("<BBIbb")


class MouseError(RuntimeError):
    """The dongle or Windows couldn't complete the requested operation."""


def _find_device(serial: str | None) -> dict:
    matches = [
        device for device in hid.enumerate(VID, PID)
        if (device.get("usage_page"), device.get("usage")) == (USAGE_PAGE, USAGE)
        and (serial is None or device.get("serial_number") == serial)
    ]
    if not matches:
        raise MouseError("Wired mouse control collection missing. Flash the USB HID mouse build and reconnect.")
    if len(matches) > 1:
        choices = ", ".join(str(device.get("serial_number")) for device in matches)
        raise MouseError(f"Multiple dongles found. Select one with --serial ({choices}).")
    return matches[0]


def status(serial: str | None = None) -> dict:
    """Identify the vendor collection without starting a movement session."""
    device = _find_device(serial)
    return {
        "usb_status": "control collection found",
        "serial": device.get("serial_number"),
        "product": device.get("product_string"),
        "vid": f"{VID:04X}",
        "pid": f"{PID:04X}",
    }


if sys.platform == "win32":
    _get_cursor = ctypes.WinDLL("user32", use_last_error=True).GetPhysicalCursorPos
    _get_cursor.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _get_cursor.restype = wintypes.BOOL


def position() -> tuple[int, int]:
    """Read the Windows desktop cursor in physical screen pixels."""
    if sys.platform != "win32":
        raise MouseError("Cursor position requires Windows.")

    point = wintypes.POINT()
    if not _get_cursor(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return point.x, point.y


class MouseController:
    """Keep one USB HID session open for a sequence of moves."""

    def __init__(self, pin: str, *, serial: str | None = None, timeout: float = 1.0):
        if not isinstance(pin, str) or len(pin) != 4 or not pin.isascii() or not pin.isdecimal():
            raise ValueError("Enter the four-digit PIN shown on the dongle, including leading zeros.")
        self._pin = pin.encode("ascii")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("The command timeout must be positive and finite.")
        device = _find_device(serial)
        self._device = hid.device()
        try:
            self._device.open_path(device["path"])
            session = bytes(self._device.get_feature_report(SESSION_REPORT, NONCE_SIZE + 2))
            if len(session) != NONCE_SIZE + 2 or session[:2] != bytes((SESSION_REPORT, PROTOCOL_VERSION)):
                raise MouseError("Incompatible HID session report. Flash the PIN-enabled firmware.")
        except (OSError, MouseError) as exc:
            self.close()
            raise MouseError(f"Could not start a HID session: {exc}") from exc
        self._nonce = session[2:]
        self._timeout = timeout
        self._seq = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        if self._device is not None:
            self._device.close()
            self._device = None

    def _tag(self, header: bytes) -> bytes:
        return hmac.digest(self._pin, self._nonce + header, "sha256")[:TAG_SIZE]

    def move(self, dx: int, dy: int) -> None:
        """Send one HID report with relative counts in [-127, 127]."""
        self._move(dx, dy, self._timeout)

    def move_measured(self, dx: int, dy: int) -> dict:
        """Send a relative move and measure the Windows cursor displacement."""
        before = self.position()
        self.move(dx, dy)
        # The firmware acknowledges report submission. Give Windows time to apply it.
        time.sleep(0.050)
        after = self.position()
        return {
            "sent_counts": (dx, dy),
            "before_px": before,
            "after_px": after,
            "delta_px": (after[0] - before[0], after[1] - before[1]),
        }

    def _move(self, dx: int, dy: int, timeout: float) -> None:
        if any(type(delta) is not int or not -127 <= delta <= 127
               for delta in (dx, dy)):
            raise ValueError("dx and dy must be integers between -127 and 127.")
        if self._device is None:
            raise MouseError("The HID session is closed. Open a new controller.")
        if self._seq == 0xffffffff:
            raise MouseError("Sequence exhausted. Open a new connection.")

        self._seq += 1
        header = HEADER.pack(PROTOCOL_VERSION, MOVE, self._seq, dx, dy)
        command = bytes((COMMAND_REPORT,)) + header + self._tag(header)
        deadline = time.monotonic() + timeout
        try:
            if self._device.write(command) != len(command):
                raise OSError("Incomplete HID write")
            result = self._read_result(deadline)
        except (OSError, ValueError) as exc:
            self.close()
            raise MouseError(
                "No valid move acknowledgment. Check the PIN and USB connection. "
                "The move will not be replayed."
            ) from exc

        if result != 0:
            raise MouseError(f"Dongle rejected move {self._seq} (expired).")

    def _read_result(self, deadline: float) -> int:
        report_size = 1 + HEADER.size + TAG_SIZE
        while (remaining := deadline - time.monotonic()) > 0:
            report = bytes(self._device.read(report_size, max(1, math.ceil(remaining * 1000))))
            if not report:
                break
            if len(report) != report_size or report[0] != COMMAND_REPORT:
                continue
            header, tag = report[1:1 + HEADER.size], report[1 + HEADER.size:]
            if not hmac.compare_digest(tag, self._tag(header)):
                continue
            version, opcode, seq, result, reserved = HEADER.unpack(header)
            if (version, opcode, seq, reserved) == (PROTOCOL_VERSION, RESULT, self._seq, 0):
                if result in (0, 1):
                    return result
                raise ValueError("Unexpected move result")
        raise TimeoutError("HID acknowledgment timed out")

    @staticmethod
    def position() -> tuple[int, int]:
        return position()

    def move_to(self, x: int, y: int, *, tolerance: int = 2,
                timeout: float = 3.0) -> tuple[int, int]:
        """Use measured desktop position to approach a target in physical pixels."""
        if any(type(value) is not int for value in (x, y, tolerance)):
            raise ValueError("Coordinates and tolerance must be integers.")
        if tolerance < 0 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Tolerance must be nonnegative and timeout positive and finite.")

        deadline = time.monotonic() + timeout
        best_error = math.inf
        attempts_without_progress = 0
        while True:
            current_x, current_y = self.position()
            error_x, error_y = x - current_x, y - current_y
            error = max(abs(error_x), abs(error_y))
            if error <= tolerance:
                return current_x, current_y
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MouseError(f"Target timed out at ({current_x}, {current_y}).")
            if error < best_error:
                best_error = error
                attempts_without_progress = 0
            elif attempts_without_progress >= 8:
                raise MouseError(f"Cursor stopped approaching the target at ({current_x}, {current_y}).")
            attempts_without_progress += 1

            # Limit each correction and shrink it near the target.
            scale = min(0.5, 96 / error)
            dx = round(error_x * scale)
            dy = round(error_y * scale)
            if dx == dy == 0:
                dx = (error_x > 0) - (error_x < 0)
                dy = (error_y > 0) - (error_y < 0)
            self._move(dx, dy, min(self._timeout, remaining))
            time.sleep(max(0.0, min(0.015, deadline - time.monotonic())))


def _relative_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, received {value!r}") from exc
    if not -127 <= count <= 127:
        raise argparse.ArgumentTypeError("must be between -127 and 127")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", help="Dongle serial number when more than one is plugged in")
    commands = parser.add_subparsers(dest="command", required=True)
    move = commands.add_parser("move", help="Send relative HID counts")
    move.add_argument("dx", type=_relative_count)
    move.add_argument("dy", type=_relative_count)
    move.add_argument("--measure", action="store_true", help="Print Windows cursor coordinates before and after")
    commands.add_parser("position", help="Read the Windows cursor position")
    commands.add_parser("status", help="Read the dongle's USB status without moving the mouse")
    target = commands.add_parser("move-to", help="Move to physical screen pixels")
    target.add_argument("x", type=int)
    target.add_argument("y", type=int)

    args = parser.parse_args()
    try:
        if args.command == "position":
            print(*position())
        elif args.command == "status":
            print(json.dumps(status(args.serial), indent=2))
        else:
            pin = os.environ.get("LILLY_MOUSE_PIN") or getpass.getpass("Dongle PIN: ")
            with MouseController(pin, serial=args.serial) as mouse:
                if args.command == "move":
                    if args.measure:
                        print(json.dumps(mouse.move_measured(args.dx, args.dy), indent=2))
                    else:
                        mouse.move(args.dx, args.dy)
                else:
                    print(*mouse.move_to(args.x, args.y))
    except EOFError:
        print("No PIN was provided. Set LILLY_MOUSE_PIN or run in an interactive terminal.",
              file=sys.stderr)
        return 1
    except (MouseError, OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
