import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from farm_backend import FarmBackend


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.command = Mock(return_value="COMFY_NATIVE_FARM=" + json.dumps({"workers": [{"name": "M28"}], "pools": ["test"], "groups": ["none"]}))
        self.backend = FarmBackend(Path(self.temp.name) / "settings.json", self.command)

    def test_cache_is_read_only_and_copy_isolated(self):
        first = self.backend.discover()
        first["workers"].clear()
        self.assertEqual(self.backend.discover()["workers"], [{"name": "M28"}])
        self.assertEqual(self.command.call_count, 2)
        self.assertEqual(self.command.call_args_list[0].args[0], "-ExecuteScript")
        self.assertEqual(self.command.call_args_list[1].args[0], "-GetCurrentUserName")

    def test_failed_refresh_retains_last_good_workers(self):
        good = self.backend.discover()
        self.command.side_effect = TimeoutError("repository unavailable")
        failed = self.backend.discover(True)
        self.assertFalse(failed["connected"])
        self.assertEqual(failed["workers"], good["workers"])
        self.assertEqual(failed["last_refresh"], good["last_refresh"])
        self.assertIn("unavailable", failed["message"])

    def test_malformed_snapshot_is_a_diagnostic(self):
        self.command.return_value = 'COMFY_NATIVE_FARM={"workers":null}'
        result = self.backend.discover()
        self.assertFalse(result["connected"])
        self.assertIn("Invalid", result["message"])

    def test_plugin_directory_is_part_of_discovery_cache_identity(self):
        self.backend.discover(plugin_directory="\\\\farm\\first\\plugins")
        self.backend.discover(plugin_directory="\\\\farm\\first\\plugins")
        self.assertEqual(self.command.call_count, 2)
        self.backend.discover(plugin_directory="\\\\farm\\second\\plugins")
        self.assertEqual(self.command.call_count, 4)
        self.assertEqual(self.command.call_args_list[-2].args[-1], "\\\\farm\\second\\plugins")

    def test_failed_plugin_path_change_cannot_reuse_old_plugin_availability(self):
        self.command.return_value = 'COMFY_NATIVE_FARM={"workers":[],"pools":[],"groups":[],"plugin_available":true}'
        self.backend.discover(plugin_directory="\\\\farm\\first\\plugins")
        self.command.side_effect = TimeoutError("unavailable")
        failed = self.backend.discover(plugin_directory="\\\\farm\\second\\plugins")
        self.assertFalse(failed["connected"])
        self.assertFalse(failed["plugin_available"])

    def test_optimistic_settings_conflict_cannot_overwrite(self):
        original = dict(self.backend.settings)
        self.backend.configure({**original, "priority": 60})
        with self.assertRaisesRegex(ValueError, "changed elsewhere"):
            self.backend.configure({**original, "priority": 90}, expected=original)
        self.assertEqual(self.backend.settings["priority"], 60)

    def test_matching_snapshot_can_apply(self):
        original = dict(self.backend.settings)
        self.backend.configure({**original, "priority": 90}, expected=original)
        self.assertEqual(self.backend.settings["priority"], 90)
