import contextlib
import hmac
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from lab import main
from lab.manage import edit_settings, read_settings, render_settings, replace_text


class ManagementTests(unittest.TestCase):
    def test_short_password_reset_preserves_key_and_file_mode(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, "KEY", Path(tmp)):
            with patch("getpass.getpass", return_value="a"):
                main.initialize()
            path = Path(tmp) / "identity.json"
            path.chmod(0o640)
            original = json.loads(path.read_text())
            encrypted = Fernet(original["key"].encode()).encrypt(b"fixture config")
            with patch("getpass.getpass", side_effect=["b", "b"]):
                main.reset_password()
            changed = json.loads(path.read_text())
            self.assertNotEqual(changed["salt"], original["salt"])
            self.assertEqual(changed["key"], original["key"])
            self.assertEqual(Fernet(changed["key"].encode()).decrypt(encrypted), b"fixture config")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            salt = bytes.fromhex(changed["salt"])
            self.assertTrue(hmac.compare_digest(changed["password"], main.digest("b", salt).hex()))
            self.assertFalse(hmac.compare_digest(changed["password"], main.digest("a", salt).hex()))
            with patch("getpass.getpass", side_effect=["c", "d"]), self.assertRaises(SystemExit):
                main.reset_password()
            self.assertEqual(json.loads(path.read_text()), changed)

    def test_settings_form_validates_and_preserves_other_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings"
            path.write_text('# fixture\nLAB_PORT=8765\nLAB_LIVE=0\nLAB_AGENT="test agent"\n')
            path.chmod(0o640)
            output = io.StringIO()
            with patch("builtins.input", side_effect=["2", "1", "s", "5", "https://example.org/check",
                                                      "1", "۹۸۷۶", "9", "s"]), \
                    patch("getpass.getpass", return_value="fixture-secret"), contextlib.redirect_stdout(output):
                self.assertTrue(edit_settings(path))
            text = path.read_text()
            values, _, _ = read_settings(text)
            self.assertIn('# fixture\n', text)
            self.assertIn('LAB_AGENT="test agent"\n', text)
            self.assertEqual((values["LAB_PORT"], values["LAB_LIVE"]), ("9876", "1"))
            self.assertEqual(values["LAB_BOT_TOKEN"], "fixture-secret")
            self.assertNotIn("fixture-secret", output.getvalue())
            self.assertIn("Settings were not saved", output.getvalue())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            with self.assertRaises(ValueError):
                replace_text(path, "LAB_PORT=1\n", expected="")
            self.assertEqual(path.read_text(), text)

    def test_environment_quote_roundtrip(self):
        value = 'spaces $ ` " \\'
        encoded = render_settings([], {}, {"LAB_X": value})
        self.assertEqual(read_settings(encoded)[0]["LAB_X"], value)
