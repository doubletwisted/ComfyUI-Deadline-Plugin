from __future__ import annotations

import os
from pathlib import Path
import re
import socket
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from test_reuse_endpoint import ComfyUIError, new_plugin, plugin_module


class WorkerStartupTests(unittest.TestCase):
    def test_worker_gpu_affinity_drives_both_fixed_port_and_launch_device(self):
        ports = []
        with tempfile.TemporaryDirectory() as temp:
            for gpu in (0, 1, 4, 7):
                with self.subTest(gpu=gpu):
                    plugin = new_plugin(
                        OverrideGpuAffinity=lambda: True,
                        GpuAffinity=lambda: [gpu],
                        GetThreadNumber=lambda: 0,
                        _is_port_in_use=lambda port: False,
                        temp_dir=temp,
                    )
                    port = plugin._calculate_comfyui_port()
                    self.assertEqual(port, str(8188 + gpu * 100))
                    self.assertEqual(plugin.comfyui_api_url, "http://127.0.0.1:" + port)
                    arguments = plugin._build_comfyui_arguments("C:/Comfy/ComfyUI/main.py")
                    self.assertIn(f"--port {port}", arguments)
                    self.assertIn(f"--cuda-device {gpu}", arguments)
                    ports.append(port)
        self.assertEqual(len(set(ports)), 4)

    def test_occupied_gpu_port_fails_without_switching_ports(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            plugin = new_plugin(
                OverrideGpuAffinity=lambda: True,
                GpuAffinity=lambda: [0],
                GetThreadNumber=lambda: 0,
                GetPluginInfoEntryWithDefault=lambda key, default: str(port) if key == "ComfyUIPort" else default,
            )
            with self.assertRaisesRegex(ComfyUIError, f"{port} is already in use"):
                plugin._calculate_comfyui_port()
            self.assertEqual(plugin.comfyui_port, str(port))

    def test_fixed_ports_need_no_lock_folder(self):
        plugin = new_plugin(_get_cuda_device_arg=lambda: "--cuda-device 1", _is_port_in_use=lambda port: False)
        with patch.object(plugin_module.os, "makedirs", side_effect=PermissionError("access denied")) as mkdir, \
                patch("builtins.open", side_effect=PermissionError("access denied")) as open_file:
            self.assertEqual(plugin._calculate_comfyui_port(), "8288")
            mkdir.assert_not_called()
            open_file.assert_not_called()

    def test_invalid_fixed_port_fails_before_launch(self):
        for port in (0, 65536):
            with self.subTest(port=port):
                with self.assertRaisesRegex(ComfyUIError, "outside the TCP port range"):
                    new_plugin()._check_launch_port(port)

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

    def test_reused_instance_launch_does_not_check_its_occupied_port_or_build_arguments(self):
        plugin = new_plugin(
            use_existing_comfyui=True,
            _resolve_comfyui_install_path=lambda: "C:/Comfy",
            _create_dummy_command=lambda: "reuse waiter",
            _check_launch_port=MagicMock(),
            _build_comfyui_arguments=MagicMock(),
        )
        with patch.object(plugin_module.os.path, "exists", return_value=True):
            self.assertEqual(plugin.RenderArgument(), "reuse waiter")
        plugin._check_launch_port.assert_not_called()
        plugin._build_comfyui_arguments.assert_not_called()


if __name__ == "__main__":
    unittest.main()
