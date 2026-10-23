import ctypes
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from steam_sda.domain.errors import SteamError
from steam_sda.infrastructure.updates.locks import ensure_device_available, write_error


class UpdateLockTests(unittest.TestCase):
    def test_locked_and_inaccessible_modules_fail_closed_with_actionable_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / 'integrations/device'
            device.mkdir(parents=True)
            (device / 'SteamSDADevice.dll').write_bytes(b'synthetic')
            for code in (5, 32, 33):
                with self.subTest(code=code), \
                     patch('steam_sda.infrastructure.updates.locks.write_error', return_value=code), \
                     patch('steam_sda.infrastructure.updates.locks.module_users', return_value='steam.exe'):
                    with self.assertRaisesRegex(SteamError, 'SteamSDADevice.dll'):
                        ensure_device_available(directory)

    @unittest.skipUnless(os.name == 'nt', 'Windows loaded image sharing rules')
    def test_loaded_dll_is_detected_without_writing_and_retry_succeeds_after_unload(self):
        import _ctypes
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / 'integrations/device'
            device.mkdir(parents=True)
            path = device / 'SteamSDADevice.dll'
            # A copied Windows version resource library, never Steam or a hook.
            shutil.copyfile(Path(os.environ['SystemRoot']) / 'System32/version.dll', path)
            expected = path.read_bytes()
            self.assertEqual(write_error(path), 0)
            module = ctypes.WinDLL(str(path))
            try:
                self.assertNotEqual(write_error(path), 0)
                with self.assertRaisesRegex(SteamError, 'SteamSDADevice.dll'):
                    ensure_device_available(directory)
                self.assertEqual(path.read_bytes(), expected)
            finally:
                _ctypes.FreeLibrary(module._handle)
            ensure_device_available(directory)
            self.assertEqual(write_error(path), 0)
            self.assertEqual(path.read_bytes(), expected)
