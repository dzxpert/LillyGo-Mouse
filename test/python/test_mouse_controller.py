import hmac
import io
import json
import sys
import unittest
from unittest.mock import patch

from tools import mouse_controller as controller


PIN = "0042"
NONCE = bytes(range(24))
DEVICE = {
    "path": b"command-collection",
    "usage_page": 0xFF00,
    "usage": 1,
    "serial_number": "MOUSE-123",
    "product_string": "T-Dongle S3 Mouse",
}


class FakeHid:
    def __init__(self):
        self.writes = []
        self.replies = []
        self.closed = False
        self.acknowledge = True
        self.result = 0
        self.position = [0, 0]

    def open_path(self, path):
        if path != DEVICE["path"]:
            raise OSError("Opened the wrong collection")

    def get_feature_report(self, report_id, length):
        assert (report_id, length) == (3, 26)
        return bytes((3, 2)) + NONCE

    def write(self, report):
        self.writes.append(report)
        header = report[1:9]
        expected = hmac.digest(PIN.encode("ascii"), NONCE + header, "sha256")[:24]
        if self.acknowledge and report[0] == 2 and report[9:] == expected:
            version, _, seq, dx, dy = controller.HEADER.unpack(header)
            if self.result == 0:
                self.position[0] += dx
                self.position[1] += dy
            response = controller.HEADER.pack(version, 2, seq, self.result, 0)
            tag = hmac.digest(PIN.encode("ascii"), NONCE + response, "sha256")[:24]
            self.replies.append(bytes((2,)) + response + tag)
        return len(report)

    def read(self, length, timeout_ms):
        assert length == 33 and timeout_ms > 0
        return self.replies.pop(0) if self.replies else []

    def close(self):
        self.closed = True


class WiredMouseTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeHid()
        self.enumerate = self.enterContext(patch.object(controller.hid, "enumerate", return_value=[
            {**DEVICE, "path": b"mouse", "usage_page": 1, "usage": 2},
            DEVICE,
        ]))
        self.open_hid = self.enterContext(patch.object(controller.hid, "device", return_value=self.device))

    def test_status_finds_vendor_collection_without_opening_it(self):
        result = controller.status("MOUSE-123")
        self.assertEqual(result["serial"], "MOUSE-123")
        self.enumerate.assert_called_once_with(0xCAFE, 0x4041)
        self.open_hid.assert_not_called()

    def test_multiple_devices_require_explicit_serial(self):
        self.enumerate.return_value.append({**DEVICE, "serial_number": "MOUSE-456"})
        with self.assertRaisesRegex(controller.MouseError, "Multiple dongles"):
            controller.status()
        self.assertEqual(controller.status("MOUSE-456")["serial"], "MOUSE-456")

    def test_missing_device(self):
        self.enumerate.return_value = []
        with self.assertRaisesRegex(controller.MouseError, "control collection missing"):
            controller.status()

    def test_authenticated_move_and_bounds(self):
        with controller.MouseController(PIN) as mouse:
            mouse.move(127, -127)
            mouse.move(-4, 3)
            self.assertEqual([report[1:9].hex() for report in self.device.writes],
                             ["0201010000007f81", "020102000000fc03"])
            for value in (-128, 128, 1.5, True):
                with self.assertRaises(ValueError):
                    mouse.move(value, 0)
        self.assertTrue(self.device.closed)

    def test_missing_or_invalid_acknowledgment_is_never_replayed(self):
        for bad_pin in (False, True):
            with self.subTest(bad_pin=bad_pin):
                self.device.writes.clear()
                self.device.acknowledge = bad_pin
                pin = "0000" if bad_pin else PIN
                with controller.MouseController(pin) as mouse:
                    with self.assertRaisesRegex(controller.MouseError, "will not be replayed"):
                        mouse.move(20, 0)
                    with self.assertRaisesRegex(controller.MouseError, "session is closed"):
                        mouse.move(20, 0)
                self.assertEqual(len(self.device.writes), 1)

    def test_ignores_forged_acknowledgment(self):
        self.device.replies = [bytes((2,)) + controller.HEADER.pack(2, 2, 1, 0, 0) + bytes(24)]
        self.device.acknowledge = False
        with controller.MouseController(PIN) as mouse:
            with self.assertRaisesRegex(controller.MouseError, "No valid move acknowledgment"):
                mouse.move(20, 0)

    def test_reports_expired_move_and_can_continue(self):
        with controller.MouseController(PIN) as mouse:
            self.device.result = 1
            with self.assertRaisesRegex(controller.MouseError, "expired"):
                mouse.move(20, 0)
            self.device.result = 0
            mouse.move(20, 0)
        self.assertEqual(self.device.position, [20, 0])

    def test_move_to_uses_measured_position(self):
        with controller.MouseController(PIN) as mouse:
            mouse.position = lambda: tuple(self.device.position)
            x, y = mouse.move_to(-280, 430)
        self.assertLessEqual(abs(x + 280), 2)
        self.assertLessEqual(abs(y - 430), 2)

    def test_measured_move_reports_windows_pixels_separately_from_counts(self):
        with controller.MouseController(PIN) as mouse:
            with patch.object(mouse, "position", side_effect=[(-300, 120), (-127, 200)]):
                result = mouse.move_measured(120, 120)
        self.assertEqual(result, {
            "sent_counts": (120, 120),
            "before_px": (-300, 120),
            "after_px": (-127, 200),
            "delta_px": (173, 80),
        })
        self.assertEqual(len(self.device.writes), 1)

    def test_cli_measure_prints_displacement(self):
        args = ["mouse_controller.py", "move", "120", "120", "--measure"]
        with patch.object(sys, "argv", args), patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            with patch.dict(controller.os.environ, {"LILLY_MOUSE_PIN": PIN}):
                with patch.object(controller, "position", side_effect=[(20, 100), (140, 220)]):
                    self.assertEqual(controller.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["delta_px"], [120, 120])

    def test_pin_requires_four_ascii_decimal_digits(self):
        for value in ("", "42", "10000", "abcd", "012 ", "٠١٢٣", 42):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "four-digit PIN"):
                    controller.MouseController(value)
        self.open_hid.assert_not_called()

    def test_old_firmware_requests_reflash(self):
        self.device.get_feature_report = lambda *_: bytes((3, 1)) + NONCE
        with self.assertRaisesRegex(controller.MouseError, "PIN-enabled firmware"):
            controller.MouseController(PIN)
        self.assertTrue(self.device.closed)


if __name__ == "__main__":
    unittest.main()
