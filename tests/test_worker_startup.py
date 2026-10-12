from __future__ import annotations

import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from test_reuse_endpoint import ComfyUIError, new_plugin, plugin_module


class WorkerStartupTests(unittest.TestCase):
    def test_simultaneous_processes_reserve_different_ports_before_servers_start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            start_port = sock.getsockname()[1]
        script = """
import sys
sys.path.insert(0, sys.argv[1])
from test_reuse_endpoint import new_plugin
plugin = new_plugin()
sys.stdin.readline()
print(plugin._reserve_launch_port(int(sys.argv[2])), flush=True)
sys.stdin.readline()
"""
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"PROGRAMDATA": temp}):
            processes = []
            try:
                for _ in range(2):
                    processes.append(subprocess.Popen(
                        [sys.executable, "-c", script, str(Path(__file__).parent), str(start_port)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, env=os.environ.copy(),
                    ))
                for process in processes:
                    process.stdin.write("start\n")
                    process.stdin.flush()
                ports = [int(process.stdout.readline()) for process in processes]
                self.assertEqual(len(set(ports)), 2)
                # The reservations do not prevent ComfyUI's TCP listeners binding.
                for port in ports:
                    with socket.socket() as sock:
                        sock.bind(("127.0.0.1", port))
                # Killing a holder releases its OS lock without deleting the lock file.
                for process in processes:
                    process.terminate()
                    process.wait(timeout=10)
                plugin = new_plugin()
                try:
                    self.assertEqual(plugin._reserve_launch_port(min(ports)), str(min(ports)))
                finally:
                    plugin._release_port_reservation()
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)

    def test_port_taken_during_reservation_is_skipped_and_released(self):
        plugin = new_plugin(_is_port_in_use=MagicMock(side_effect=[False, True, False, False]))
        probe = new_plugin(_is_port_in_use=lambda port: False)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"PROGRAMDATA": temp}):
            try:
                self.assertEqual(plugin._reserve_launch_port(8188), "8288")
                self.assertEqual(probe._reserve_launch_port(8188), "8188")
            finally:
                plugin._release_port_reservation()
                probe._release_port_reservation()

    def test_launch_port_exhaustion_and_range_limit_fail(self):
        plugin = new_plugin(_is_port_in_use=MagicMock(return_value=True))
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"PROGRAMDATA": temp}):
            with self.assertRaises(ComfyUIError):
                plugin._reserve_launch_port(8188)
            self.assertEqual(plugin._is_port_in_use.call_count, plugin_module.MAX_PORT_ATTEMPTS)
            plugin._is_port_in_use.reset_mock()
            with self.assertRaises(ComfyUIError):
                plugin._reserve_launch_port(65500)
            plugin._is_port_in_use.assert_called_once_with(65500)
        self.assertIsNone(plugin.port_reservation)

    def test_new_instances_get_separate_task_databases(self):
        with tempfile.TemporaryDirectory(prefix="deadline job ") as temp:
            plugin = new_plugin(temp_dir=temp, _get_cuda_device_arg=lambda: "--cuda-device 0")
            urls = []
            for _ in range(2):
                arguments = plugin._build_comfyui_arguments("C:/Comfy/ComfyUI/main.py")
                match = re.search(r'--database-url "([^"]+)"', arguments)
                self.assertIsNotNone(match)
                urls.append(match.group(1))
                path = urls[-1].removeprefix("sqlite:///")
                self.assertEqual(Path(path).parent, Path(temp))
                self.assertIn("--cuda-device 0", arguments)
            self.assertNotEqual(*urls)

    def test_starting_server_and_bind_failure_do_not_trigger_submission(self):
        handlers = {}
        def add_handler(pattern):
            handlers[pattern] = types.SimpleNamespace(HandleCallback=MagicMock())
            return handlers[pattern]
        plugin = new_plugin(AddStdoutHandlerCallback=add_handler)
        plugin._setup_stdout_handlers()
        startup_patterns = [pattern for pattern in handlers if "To see the GUI" in pattern]
        self.assertEqual(len(startup_patterns), 1)
        pattern = startup_patterns[0]
        for line in ("Starting server", "Port 8188 is already in use on address 127.0.0.1"):
            self.assertIsNone(re.match(pattern, line))
        match = re.match(pattern, "[INFO] To see the GUI go to: http://127.0.0.1:8188")
        self.assertEqual(match.group(1), "8188")
        plugin.comfyui_port = "8188"
        plugin.GetRegexMatch = match.group
        with patch.object(plugin_module.threading, "Thread") as thread:
            plugin.HandleServerStarted()
            plugin.HandleServerStarted()
            thread.assert_called_once_with(target=plugin.submit_workflow)
            thread.return_value.start.assert_called_once()
        self.assertTrue(plugin.server_started)
        self.assertTrue(plugin.workflow_submitted)

    def test_wrong_port_or_stopped_task_does_not_trigger_submission(self):
        plugin = new_plugin(comfyui_port="8288", GetRegexMatch=lambda group: "8188")
        with patch.object(plugin_module.threading, "Thread") as thread:
            plugin.HandleServerStarted()
            plugin.GetRegexMatch = lambda group: "8288"
            plugin.thread_running = False
            plugin.HandleServerStarted()
            thread.assert_not_called()
        self.assertFalse(plugin.server_started)
        self.assertFalse(plugin.workflow_submitted)

    def test_reused_instance_launch_does_not_reserve_a_port_or_build_arguments(self):
        plugin = new_plugin(
            use_existing_comfyui=True,
            _resolve_comfyui_install_path=lambda: "C:/Comfy",
            _create_dummy_command=lambda: "reuse waiter",
            _reserve_launch_port=MagicMock(),
            _build_comfyui_arguments=MagicMock(),
        )
        with patch.object(plugin_module.os.path, "exists", return_value=True):
            self.assertEqual(plugin.RenderArgument(), "reuse waiter")
        plugin._reserve_launch_port.assert_not_called()
        plugin._build_comfyui_arguments.assert_not_called()


if __name__ == "__main__":
    unittest.main()
