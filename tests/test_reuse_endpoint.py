"""Isolated read-only tests for the deployed ComfyUI Deadline plugin.

The production plugin is imported from C:\\AI... with minimal Deadline/System
stubs. This file intentionally does not patch or copy production source.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "plugins" / "ComfyUI" / "ComfyUI.py"


def _install_import_stubs():
    deadline = types.ModuleType("Deadline")
    plugins = types.ModuleType("Deadline.Plugins")
    scripting = types.ModuleType("Deadline.Scripting")
    system = types.ModuleType("System")
    diagnostics = types.ModuleType("System.Diagnostics")

    class DeadlinePlugin:
        pass

    class PluginType:
        Simple = "Simple"

    class RepositoryUtils:
        @staticmethod
        def CheckPathMapping(value):
            return value

        @staticmethod
        def GetSlaveSettings(worker, force_refresh):
            raise RuntimeError("no fake per-worker settings configured")

    plugins.DeadlinePlugin = DeadlinePlugin
    plugins.PluginType = PluginType
    scripting.RepositoryUtils = RepositoryUtils
    scripting.SystemUtils = object()
    scripting.FileUtils = object()
    diagnostics.ProcessPriorityClass = types.SimpleNamespace(BelowNormal="BelowNormal")
    system.Diagnostics = diagnostics

    sys.modules.update({
        "Deadline": deadline,
        "Deadline.Plugins": plugins,
        "Deadline.Scripting": scripting,
        "System": system,
        "System.Diagnostics": diagnostics,
    })


def _load_plugin_module():
    _install_import_stubs()
    name = "current_comfyui_deadline_plugin_under_test"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


plugin_module = _load_plugin_module()
ComfyUI = plugin_module.ComfyUI
ComfyUIError = plugin_module.ComfyUIError


def new_plugin(**attrs):
    plugin = object.__new__(ComfyUI)
    plugin.logs = []
    plugin.endpoint_policy_active = False
    plugin.reuse_gui_input_root = ""
    plugin.configured_comfyui_api_url = ""
    plugin.configured_launch_gpu_uuid = ""
    plugin.use_existing_comfyui = False
    plugin.server_started = False
    plugin.prompt_id = "p"
    plugin.prompt_ids = []
    plugin.completed_prompts = set()
    plugin.current_tracking_index = 0
    plugin.prompts_executed = 0
    plugin.chunk_size = 1
    plugin.thread_running = True
    plugin.task_completed = False
    plugin.workflow_submitted = False
    plugin.comfyui_input_dir = ""
    plugin.comfyui_output_dir = ""
    plugin.custom_output_dir_specified = False
    plugin.reuse_completion_marker = ""
    plugin.comfyui_install_path = ""
    plugin.reuse_gui_output_root = ""
    plugin.GetSlaveName = lambda: "M21"
    plugin.GetPluginInfoEntryWithDefault = lambda key, default: default
    plugin.LogInfo = lambda message: plugin.logs.append(("info", message))
    plugin.LogWarning = lambda message: plugin.logs.append(("warning", message))
    for key, value in attrs.items():
        setattr(plugin, key, value)
    return plugin


class ReuseEndpointCurrentTests(unittest.TestCase):
    def test_endpoint_policy_accepts_local_http_and_is_case_insensitive(self):
        plugin = new_plugin(
            GetConfigEntryWithDefault=lambda key, default: json.dumps({
                "m21": {
                    "ComfyUIApiUrl": "http://127.0.0.1:8188/",
                    "LaunchGpuUuid": "GPU-abc",
                }
            })
        )
        plugin._load_worker_endpoint_policy()
        self.assertTrue(plugin.endpoint_policy_active)
        self.assertEqual(plugin.configured_comfyui_api_url, "http://127.0.0.1:8188")
        self.assertEqual(plugin.configured_launch_gpu_uuid, "GPU-abc")

    def test_per_worker_settings_take_precedence_over_global_json(self):
        class Settings:
            def GetSlaveExtraInfoKeyValueWithDefault(self, key, default):
                return {
                    "ComfyUIApiUrl": "http://localhost:8188",
                    "ComfyUILaunchGpuUuid": "GPU-worker",
                }.get(key, default)

        old_reader = plugin_module.RepositoryUtils.GetSlaveSettings
        plugin_module.RepositoryUtils.GetSlaveSettings = staticmethod(lambda worker, force: Settings())
        try:
            plugin = new_plugin(
                GetConfigEntryWithDefault=lambda key, default: "{invalid global json"
            )
            plugin._load_worker_endpoint_policy()
            self.assertTrue(plugin.endpoint_policy_active)
            self.assertEqual(plugin.configured_comfyui_api_url, "http://localhost:8188")
            self.assertEqual(plugin.configured_launch_gpu_uuid, "GPU-worker")
        finally:
            plugin_module.RepositoryUtils.GetSlaveSettings = old_reader

    def test_endpoint_policy_rejects_nonlocal_or_nonhttp_urls(self):
        for endpoint in (
            "https://127.0.0.1:8188",
            "http://0.0.0.0:8188",
            "http://127.0.0.1:8188/api",
            "http://worker-name:8188",
        ):
            plugin = new_plugin(
                GetConfigEntryWithDefault=lambda key, default, endpoint=endpoint: json.dumps({
                    "M21": {"ComfyUIApiUrl": endpoint}
                })
            )
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ComfyUIError):
                    plugin._load_worker_endpoint_policy()

    def test_known_bug_endpoint_policy_accepts_query_and_zero_port(self):
        """Regression detector: accepted values later produce malformed API/port use."""
        for endpoint in ("http://127.0.0.1:8188?x=1", "http://127.0.0.1:0"):
            plugin = new_plugin(
                GetConfigEntryWithDefault=lambda key, default, endpoint=endpoint: json.dumps({
                    "M21": {"ComfyUIApiUrl": endpoint}
                })
            )
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ComfyUIError):
                    plugin._load_worker_endpoint_policy()

    def test_configured_endpoint_reuses_verified_server(self):
        plugin = new_plugin(
            configured_comfyui_api_url="http://127.0.0.1:8188",
            endpoint_policy_active=True,
            http_request=lambda url, **kwargs: {
                "status_code": 200,
                "json": lambda: {"devices": [{"name": "GPU"}]},
            },
        )
        plugin._configure_policy_endpoint()
        self.assertTrue(plugin.use_existing_comfyui)
        self.assertTrue(plugin.server_started)
        self.assertEqual(plugin.comfyui_port, "8188")

    def test_unverified_occupied_endpoint_fails_closed(self):
        plugin = new_plugin(
            configured_comfyui_api_url="http://127.0.0.1:8188",
            endpoint_policy_active=True,
            http_request=lambda url, **kwargs: {"status_code": 503, "json": lambda: {}},
            _is_port_in_use=lambda port: True,
        )
        with self.assertRaises(ComfyUIError):
            plugin._configure_policy_endpoint()

    def test_fallback_launch_resolves_uuid_and_honors_deadline_affinity(self):
        plugin = new_plugin(
            endpoint_policy_active=True,
            use_existing_comfyui=False,
            configured_launch_gpu_uuid="GPU-B",
            OverrideGpuAffinity=lambda: True,
            GpuAffinity=lambda: [1],
            GetThreadNumber=lambda: 0,
        )
        old_check_output = plugin_module.subprocess.check_output
        plugin_module.subprocess.check_output = lambda *args, **kwargs: "0, GPU-A\n1, GPU-B\n"
        try:
            self.assertEqual(plugin._get_cuda_device_arg(), "--cuda-device 1")
        finally:
            plugin_module.subprocess.check_output = old_check_output

    def test_fallback_launch_rejects_uuid_outside_deadline_affinity(self):
        plugin = new_plugin(
            endpoint_policy_active=True,
            use_existing_comfyui=False,
            configured_launch_gpu_uuid="GPU-B",
            OverrideGpuAffinity=lambda: True,
            GpuAffinity=lambda: [0],
            GetThreadNumber=lambda: 0,
        )
        old_check_output = plugin_module.subprocess.check_output
        plugin_module.subprocess.check_output = lambda *args, **kwargs: "0, GPU-A\n1, GPU-B\n"
        try:
            with self.assertRaises(ComfyUIError):
                plugin._get_cuda_device_arg()
        finally:
            plugin_module.subprocess.check_output = old_check_output

    def test_staged_input_staging_preserves_safe_relative_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "nested").mkdir()
            inside = root / "nested" / "clip.mov"
            inside.write_bytes(b"input")
            outside = root.parent / (root.name + "_outside.mov")
            outside.write_bytes(b"outside")
            try:
                gui_input = root / "gui_input"
                gui_input.mkdir()
                plugin = new_plugin(comfyui_input_dir=str(root), reuse_gui_input_root=str(gui_input), submission_id="smoke")
                absolute = str(inside)
                workflow = {
                    "1": {"class_type": "LoadVideo", "inputs": {
                        "file": "nested/clip.mov",
                        "traversal": "../" + outside.name,
                        "missing": "missing.mov",
                        "already_absolute": absolute,
                    }}
                }
                (root / "other").mkdir()
                (root / "other" / "clip.mov").write_bytes(b"other")
                workflow["2"] = {"class_type": "LoadVideo", "inputs": {"file": "other/clip.mov"}}
                plugin._stage_reused_endpoint_inputs(workflow)
                values = workflow["1"]["inputs"]
                self.assertEqual(values["file"], "deadline/smoke/nested/clip.mov")
                self.assertEqual((gui_input / values["file"]).read_bytes(), b"input")
                self.assertEqual(workflow["2"]["inputs"]["file"], "deadline/smoke/other/clip.mov")
                self.assertEqual((gui_input / workflow["2"]["inputs"]["file"]).read_bytes(), b"other")
                self.assertEqual(values["traversal"], "../" + outside.name)
                self.assertEqual(values["missing"], "missing.mov")
                self.assertEqual(values["already_absolute"], absolute)
            finally:
                outside.unlink(missing_ok=True)

    def test_output_copy_succeeds_and_preserves_subfolder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / "install"
            source = install / "ComfyUI" / "output" / "video" / "out.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"video")
            target = root / "job-output"
            plugin = new_plugin(
                use_existing_comfyui=True,
                custom_output_dir_specified=True,
                comfyui_install_path=str(install),
                comfyui_output_dir=str(target),
                reuse_gui_output_root=str(install / "ComfyUI" / "output"),
            )
            plugin._log_output_information({"20": {"videos": [{
                "filename": "out.mp4", "subfolder": "video", "type": "output"
            }]}})
            self.assertEqual((target / "video" / "out.mp4").read_bytes(), b"video")

    def test_output_copy_rejects_traversal_and_missing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / "install"
            output = install / "ComfyUI" / "output"
            output.mkdir(parents=True)
            plugin = new_plugin(
                use_existing_comfyui=True,
                custom_output_dir_specified=True,
                comfyui_install_path=str(install),
                comfyui_output_dir=str(root / "job-output"),
                reuse_gui_output_root=str(output),
            )
            cases = [
                {"images": [{"filename": "escape.png", "subfolder": "..", "type": "output"}]},
                {"videos": [{"filename": "missing.mp4", "subfolder": "", "type": "output"}]},
                {},
            ]
            for outputs in cases:
                with self.subTest(outputs=outputs):
                    with self.assertRaises(ComfyUIError):
                        plugin._log_output_information({"1": outputs})

    def test_waiter_marker_helper_writes_atomic_success_and_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = os.path.join(temp, "marker.json")
            plugin = new_plugin(reuse_completion_marker=marker)
            plugin._signal_reuse_waiter(False)
            self.assertEqual(json.loads(Path(marker).read_text()), {"success": False})
            plugin._signal_reuse_waiter(True)
            self.assertEqual(json.loads(Path(marker).read_text()), {"success": True})

    def test_known_bug_submit_failure_does_not_signal_waiter(self):
        """Regression detector: current source leaves the dummy process waiting."""
        with tempfile.TemporaryDirectory() as temp:
            plugin = new_plugin(reuse_completion_marker=os.path.join(temp, "marker.json"))
            plugin.load_and_validate_workflow = lambda: (_ for _ in ()).throw(ComfyUIError("synthetic queue failure"))
            plugin.AbortRender = lambda message: plugin.logs.append(("abort", message))
            signaled = []
            plugin._signal_reuse_waiter = lambda success: signaled.append(bool(success))
            plugin.submit_workflow()
            self.assertEqual(signaled, [False], "failure must release the Deadline dummy waiter")


if __name__ == "__main__":
    unittest.main(verbosity=2)
