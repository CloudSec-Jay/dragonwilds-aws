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
            "RSDW_OWNER_ID": "76561190000000000", "RSDW_PORT": "7777",
            "RSDW_BEACON_PORT": "8888", "RSDW_SERVER_NAME": "My Server",
            "RSDW_WORLD_NAME": "My World",
            "RSDW_PASSWORD": ' spaces $dollar `ticks` \\backslash "quotes" = #hash ',
            "RSDW_ADMINS": "76561190000000000",
            "RSDW_ADMIN_PASSWORD": "manage-this-server",
            "RSDW_ADDITIONAL_ARGS": "",
            "RSDW_AUTO_STOP_ON_UPDATE": "true",
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

    def test_required_values_may_not_be_empty(self):
        self.secret["RSDW_PASSWORD"] = ""
        self.assertEqual(len(self.program.input_value(self.secret).all()), 10)
        for key in (
            "RSDW_OWNER_ID", "RSDW_PORT", "RSDW_BEACON_PORT", "RSDW_SERVER_NAME",
            "RSDW_WORLD_NAME", "RSDW_ADMINS", "RSDW_ADMIN_PASSWORD",
            "RSDW_AUTO_STOP_ON_UPDATE"
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.program.input_value(dict(self.secret, **{key: ""})).all()

    def test_empty_optional_values_are_explicit(self):
        for key in ("RSDW_PASSWORD", "RSDW_ADDITIONAL_ARGS"):
            self.secret[key] = ""
        self.assertEqual(len(self.program.input_value(self.secret).all()), 10)

    def test_runtime_network_and_update_values_are_validated(self):
        for key, value in (
            ("RSDW_PORT", "7778"),
            ("RSDW_BEACON_PORT", "9999"),
            ("RSDW_AUTO_STOP_ON_UPDATE", "TRUE"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.program.input_value(dict(self.secret, **{key: value})).all()


if __name__ == "__main__":
    unittest.main()
