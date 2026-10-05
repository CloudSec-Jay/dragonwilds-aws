"""Exercise the bootstrap's actual jq filter with Podman's literal env format."""

from pathlib import Path
import unittest

import jq


class SecretFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = (Path(__file__).resolve().parents[1] / "ansible/files/dragonwilds-bootstrap.sh").read_text()
        cls.program = jq.compile(script.split("jq -er '", 1)[1].split("' >", 1)[0])

    def setUp(self):
        self.secret = {
            "RSDW_OWNER_ID": "76561190000000000", "RSDW_WORLD_NAME": "My World",
            "RSDW_PASSWORD": ' spaces $dollar `ticks` \\backslash "quotes" = #hash ',
            "RSDW_ADMIN_PASSWORD": "",
        }

    def test_values_roundtrip_literally_including_empty_password(self):
        lines = self.program.input_value(self.secret).all()
        self.assertEqual(dict(line.split("=", 1) for line in lines), self.secret)

    def test_reject_line_injection_and_nul(self):
        for value in ["test\nRSDW_OWNER_ID=attacker", "test\rvalue", "nul\x00value"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.program.input_value(dict(self.secret, RSDW_PASSWORD=value)).all()

    def test_missing_or_nonstring_values_fail(self):
        for value in [None, 123, True, [], {}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.program.input_value(dict(self.secret, RSDW_OWNER_ID=value)).all()
        del self.secret["RSDW_OWNER_ID"]
        with self.assertRaises(ValueError):
            self.program.input_value(self.secret).all()


if __name__ == "__main__":
    unittest.main()
