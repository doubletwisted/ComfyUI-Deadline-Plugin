"""The Python that runs ComfyUI is configurable (issue #4).

ComfyUI portable keeps its interpreter in python_embeded/python.exe; other
installs (Stability Matrix, venvs, conda) do not.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_reuse_endpoint import new_plugin, plugin_module


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _config(**entries):
    return lambda key, default: entries.get(key, default)


class PythonExecutableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _plugin(self, **config):
        failures = []
        plugin = new_plugin(
            comfyui_install_path=None,
            GetConfigEntryWithDefault=_config(**config),
            FailRender=failures.append,
            _set_deadline_environment_variables=lambda: None,
        )
        return plugin, failures

    def test_default_is_the_portable_embedded_python(self):
        install = self.root / "ComfyUI_windows_portable"
        _touch(install / "ComfyUI" / "main.py")
        embedded = _touch(install / "python_embeded" / "python.exe")
        plugin, failures = self._plugin(ComfyUIPath=str(install))

        self.assertEqual(plugin.RenderExecutable(), os.path.normpath(str(embedded)))
        self.assertEqual(failures, [])

    def test_relative_python_executable_resolves_against_the_installation_path(self):
        # Stability Matrix layout: Data/Packages/ComfyUI/{main.py,venv}, no python_embeded.
        packages = self.root / "StabilityMatrix" / "Data" / "Packages"
        _touch(packages / "ComfyUI" / "main.py")
        venv_python = _touch(packages / "ComfyUI" / "venv" / "Scripts" / "python.exe")
        plugin, failures = self._plugin(
            ComfyUIPath=str(packages),
            ComfyUIPythonExecutable=os.path.join("ComfyUI", "venv", "Scripts", "python.exe"),
        )

        self.assertEqual(plugin.RenderExecutable(), os.path.normpath(str(venv_python)))
        self.assertEqual(plugin.comfyui_install_path, os.path.normpath(str(packages)))
        self.assertEqual(failures, [])

    def test_absolute_python_executable_is_used_and_may_be_quoted(self):
        install = self.root / "Comfy"
        _touch(install / "ComfyUI" / "main.py")
        shared_python = _touch(self.root / "envs" / "comfy" / "python.exe")
        plugin, failures = self._plugin(
            ComfyUIPath=str(install),
            ComfyUIPythonExecutable=f'"{shared_python}"',
        )

        self.assertEqual(plugin.RenderExecutable(), os.path.normpath(str(shared_python)))
        self.assertEqual(failures, [])

    def test_missing_configured_python_fails_clearly_instead_of_using_the_embedded_one(self):
        install = self.root / "ComfyUI_windows_portable"
        _touch(install / "ComfyUI" / "main.py")
        _touch(install / "python_embeded" / "python.exe")
        missing = self.root / "nowhere" / "python.exe"
        plugin, failures = self._plugin(ComfyUIPath=str(install), ComfyUIPythonExecutable=str(missing))

        self.assertEqual(plugin.RenderExecutable(), "")
        self.assertEqual(len(failures), 1)
        self.assertIn("ComfyUI Python Executable", failures[0])
        self.assertIn(str(missing), failures[0])

    def test_launch_arguments_still_run_the_installation_main_py(self):
        packages = self.root / "Packages"
        main_py = _touch(packages / "ComfyUI" / "main.py")
        _touch(packages / "ComfyUI" / "venv" / "Scripts" / "python.exe")
        plugin, failures = self._plugin(
            ComfyUIPath=str(packages),
            ComfyUIPythonExecutable=os.path.join("ComfyUI", "venv", "Scripts", "python.exe"),
        )
        plugin.comfyui_port = "8188"
        plugin.temp_dir = str(self.root / "task-temp")
        plugin._check_launch_port = lambda port: None
        plugin._get_cuda_device_arg = lambda: ""
        plugin.GetBooleanPluginInfoEntryWithDefault = lambda key, default: default

        arguments = plugin.RenderArgument()

        self.assertIn(os.path.normpath(str(main_py)), arguments)
        self.assertEqual(failures, [])

    def test_legacy_endpoint_owner_must_run_the_configured_python(self):
        packages = self.root / "Packages"
        _touch(packages / "ComfyUI" / "main.py")
        venv_python = _touch(packages / "ComfyUI" / "venv" / "Scripts" / "python.exe")
        plugin, _ = self._plugin(
            ComfyUIPythonExecutable=os.path.join("ComfyUI", "venv", "Scripts", "python.exe"),
        )
        plugin.comfyui_install_path = str(packages)
        netstat = "  TCP    127.0.0.1:8188         0.0.0.0:0              LISTENING       4242\n"
        process = json.dumps({
            "ProcessId": 4242,
            "ExecutablePath": str(venv_python),
            "CommandLine": f'"{venv_python}" -s "{packages / "ComfyUI" / "main.py"}" --port 8188',
        })

        with patch.object(plugin_module.platform, "system", return_value="Windows"), \
                patch.object(plugin_module.subprocess, "check_output", side_effect=[netstat, process]):
            identity = plugin._local_port_process_identity(8188)

        self.assertEqual(identity.get("pid"), 4242)


if __name__ == "__main__":
    unittest.main()
