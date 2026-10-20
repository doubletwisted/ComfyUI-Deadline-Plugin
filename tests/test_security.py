from __future__ import annotations

import importlib.util
import configparser
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import deadline_submit
import spool_protocol
from deadline_submit import DeadlineCommandHelper, DeadlineJobSubmitter, DeadlineSubmitNode, InputAssetStager
from test_reuse_endpoint import ComfyUIError, new_plugin, plugin_module


def directory_link(link, target):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def load_script(name, directory="maintenance"):
    path = Path(__file__).resolve().parents[1] / "scripts" / directory / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SubmissionSecurityTests(unittest.TestCase):
    def job_config(self, root):
        return dict(batch_count=1, chunk_size=1, priority=50, pool="none", group="none", job_name="Render",
                    output_directory=str(root / "output"), input_directory=str(root / "input"),
                    input_manifest=str(root / "manifest.json"), submission_id="test")

    def test_job_metadata_cannot_inject_deadline_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for key in ("job_name", "comment", "department", "pool", "group", "output_directory"):
                for separator in ("\n", "\r", "\r\n", "\x00"):
                    with self.subTest(key=key, separator=separator):
                        config = self.job_config(root)
                        config[key] = "innocent" + separator + "PreJobScript=evil.py"
                        with self.assertRaisesRegex(ValueError, "newlines or NUL"):
                            DeadlineJobSubmitter({}, config)._write_job_info(str(root / "job.txt"))

    def test_plugin_paths_cannot_inject_deadline_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for key in ("input_directory", "input_manifest", "submission_id"):
                config = self.job_config(root)
                config[key] = "value\nWorkerMode=True"
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, "newlines or NUL"):
                    DeadlineJobSubmitter({}, config)._write_plugin_info(str(root / "plugin.txt"))

    def test_valid_job_preserves_unicode_and_equals_signs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.job_config(root)
            config.update(job_name="ujęcie = café", comment="x=y", machine_list="GPU01,GPU02")
            path = root / "job.txt"
            DeadlineJobSubmitter({}, config)._write_job_info(str(path))
            entries = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())
            self.assertEqual(entries["Plugin"], "ComfyUI")
            self.assertEqual(entries["Name"], "ujęcie = café")
            self.assertEqual(entries["Comment"], "x=y")
            self.assertEqual(entries["Whitelist"], "GPU01,GPU02")

    def test_deadline_command_uses_argument_vector_without_shell(self):
        process = MagicMock(returncode=0)
        process.communicate.return_value = (b"JobID=test", b"")
        arguments = ["job & echo hacked.txt", "plugin file.txt"]
        with patch.object(DeadlineCommandHelper, "get_deadline_command", return_value="C:/Deadline/deadlinecommand.exe"), \
                patch.object(deadline_submit.subprocess, "Popen", return_value=process) as popen:
            DeadlineCommandHelper.call_deadline_command(arguments)
        self.assertEqual(popen.call_args.args[0], ["C:/Deadline/deadlinecommand.exe"] + arguments)
        self.assertIs(popen.call_args.kwargs["shell"], False)

    def test_output_is_limited_to_local_default_and_operator_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local = root / "local-output"
            farm = root / "farm"
            folders = types.SimpleNamespace(get_output_directory=lambda: str(local))
            settings = {"output_roots": [str(farm)], "input_roots": []}
            with patch.dict(sys.modules, {"folder_paths": folders}), patch.object(DeadlineCommandHelper, "get_farm_settings", return_value=settings):
                for target in (local / "take", farm / "take"):
                    self.assertEqual(DeadlineSubmitNode()._prepare_output_directory(str(target)), str(target))
                    self.assertTrue(target.is_dir())
                outside = root / "other-host"
                with self.assertRaisesRegex(ValueError, "approved storage"):
                    DeadlineSubmitNode()._prepare_output_directory(str(outside))
                self.assertFalse(outside.exists())

    def test_unapproved_share_is_rejected_before_filesystem_access(self):
        with patch.object(deadline_submit.os.path, "realpath") as resolve:
            with self.assertRaises(ValueError):
                deadline_submit._approved_path(r"\\unapproved.invalid\share\image.png", [r"C:\renders"])
            resolve.assert_not_called()

    def test_windows_device_stream_and_traversal_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            for relative in ("../outside.png", "nested/../../outside.png", "NUL.png", "CON", "image.png:stream", "trailing.", "nested\\..\\outside.png"):
                with self.subTest(relative=relative), self.assertRaises(ValueError):
                    deadline_submit._approved_path(os.path.join(temp, relative), [temp])

    def test_stager_rejects_unapproved_absolute_media_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "private" / "credentials.png"
            source.parent.mkdir()
            source.write_bytes(b"private data")
            stager = InputAssetStager(str(root / "output"), "test", "test")
            stager._get_local_input_directory = lambda: str(root / "input")
            with patch.object(DeadlineCommandHelper, "get_farm_settings", return_value={"input_roots": []}), self.assertRaises(ValueError):
                stager.stage_referenced_assets({"1": {"class_type": "VHS_LoadImagePath", "inputs": {"image": str(source)}}})
            self.assertFalse((root / "output").exists())

    def test_stager_rejects_input_junction_to_private_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            private = root / "private"
            input_dir.mkdir()
            private.mkdir()
            (private / "secret.png").write_bytes(b"secret")
            directory_link(input_dir / "link", private)
            stager = InputAssetStager(str(root / "output"), "test", "test")
            stager._get_local_input_directory = lambda: str(input_dir)
            with self.assertRaises(ValueError):
                stager.stage_referenced_assets({"1": {"class_type": "LoadImage", "inputs": {"image": "link/secret.png"}}})

    def test_stager_rejects_destination_junction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            private = root / "private"
            output.mkdir()
            private.mkdir()
            directory_link(output / ".deadline-inputs", private)
            stager = InputAssetStager(str(output), "test", "test")
            stager._get_local_input_directory = lambda: str(root)
            with self.assertRaises(ValueError):
                stager.stage_referenced_assets({})
            self.assertEqual(list(private.iterdir()), [])

    def test_submission_packages_valid_staged_inputs_inside_approved_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs, output = root / "input", root / "output"
            inputs.mkdir()
            (inputs / "source.png").write_bytes(b"media")
            folders = types.SimpleNamespace(get_input_directory=lambda: str(inputs), get_output_directory=lambda: str(root / "local-output"))
            prompt = {"1": {"class_type": "LoadImage", "inputs": {"image": "source.png"}},
                      "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "render"}}}
            captured = []

            def submit(submitter):
                config = submitter.job_config
                captured.append(config)
                job_files = root / "job-files"
                job_files.mkdir()
                job_info, plugin_info, auxiliary = submitter._create_submission_files(str(job_files))
                self.assertTrue(all(Path(path).is_file() for path in [job_info, plugin_info] + auxiliary))
                return True, "test-job-id"

            settings = {"output_roots": [str(output)], "input_roots": []}
            with patch.dict(sys.modules, {"folder_paths": folders}), \
                    patch.object(DeadlineCommandHelper, "get_farm_settings", return_value=settings), \
                    patch.object(DeadlineJobSubmitter, "submit_job", submit):
                result = DeadlineSubmitNode().submit_to_deadline(str(output), 1, 1, 50, "none", "none", "Render", prompt=prompt)
            self.assertEqual(result, ("test-job-id",))
            staged = Path(captured[0]["input_directory"])
            self.assertEqual(staged.parent, output / ".deadline-inputs")
            self.assertEqual((staged / "source.png").read_bytes(), b"media")


class FarmSettingsTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows UNC share roots")
    def test_unc_share_root_remains_rooted_after_realpath(self):
        maintenance = load_script("ComfyModelsSync")
        candidate = r"\\NAS02\grajdolek\project\image.png"
        for root in ("\\\\NAS02\\grajdolek", "\\\\NAS02\\grajdolek\\"):
            with self.subTest(root=root), patch.object(os.path, "realpath", side_effect=lambda path: path.rstrip("\\")):
                self.assertEqual(deadline_submit._approved_path(candidate, [root]), candidate)
                self.assertEqual(plugin_module.approved_path(candidate, [root]), candidate)
                self.assertEqual(maintenance.checked_model_path(root, candidate), candidate)
                with self.assertRaises(ValueError):
                    deadline_submit._approved_path(r"\\NAS02\grajdolek-other\private.png", [root])

    def studio_policy(self):
        config = configparser.RawConfigParser()
        config.read(Path(__file__).resolve().parents[1] / "plugins" / "ComfyUI" / "ComfyUI.param")
        policy = config["AllowedStorageRoots"]["Default"]
        module = load_script("read_farm_settings", directory="")
        settings = {"output_roots": module.storage_locations(policy), "input_roots": module.storage_locations(policy),
                    "drive_mappings": {"G:": r"\\NAS02\grajdolek", "X:": r"\\NAS02\Comfy", "Y:": r"\\NAS04\3d-02"}}
        return policy, settings

    @unittest.skipUnless(os.name == "nt", "Windows studio storage policy")
    def test_studio_defaults_allow_both_forms_and_restrict_x_subfolders(self):
        policy, settings = self.studio_policy()
        plugin = new_plugin(GetConfigEntryWithDefault=lambda key, default: policy if key == "AllowedStorageRoots" else default,
                            _resolve_comfyui_install_path=lambda: r"C:\Comfy")
        roots = plugin._storage_roots("output")
        allowed = [r"G:\project\render", r"Y:\project\render", r"X:\input\project", r"X:\outputs\project", r"X:\playground\project",
                   r"\\NAS02\grajdolek\project\render", r"\\NAS04\3d-02\project\render", r"\\NAS02\Comfy\input\project",
                   r"\\NAS02\Comfy\outputs\project", r"\\NAS02\Comfy\playground\project"]
        denied = ["X:\\", r"X:\models\secret", r"X:\outputs-other\project", r"\\NAS02\Comfy\models\secret",
                  r"\\NAS02\Comfy\outputs-other\project", r"\\unapproved.invalid\share\file", r"C:\private\secret"]
        with patch.object(os.path, "realpath", side_effect=lambda path: path):
            for path in allowed:
                with self.subTest(path=path):
                    self.assertEqual(plugin_module.approved_path(path, roots), path)
                    candidate = deadline_submit._farm_path(path, settings)
                    self.assertTrue(deadline_submit._approved_path(candidate, settings["output_roots"]))
            for path in denied:
                with self.subTest(path=path), self.assertRaises(ComfyUIError):
                    plugin_module.approved_path(path, roots)
                with self.subTest(path=path), self.assertRaises(ValueError):
                    deadline_submit._approved_path(deadline_submit._farm_path(path, settings), settings["output_roots"])

    @unittest.skipUnless(os.name == "nt", "Windows mapped network paths")
    def test_submission_stores_unc_output_and_input_paths(self):
        _, settings = self.studio_policy()
        folders = types.SimpleNamespace(get_output_directory=lambda: r"C:\Comfy\output")
        captured = []

        def submit(submitter):
            captured.append(submitter.job_config)
            return True, "unc-test-job"

        with patch.dict(sys.modules, {"folder_paths": folders}), \
                patch.object(DeadlineCommandHelper, "get_farm_settings", return_value=settings), \
                patch.object(os.path, "realpath", side_effect=lambda path: path), \
                patch.object(os.path, "isdir", return_value=True), \
                patch.object(deadline_submit.os, "makedirs") as mkdir, \
                patch.object(InputAssetStager, "stage_referenced_assets", return_value=(r"\\NAS02\Comfy\outputs\shot\.deadline-inputs\test", r"\\NAS02\Comfy\outputs\manifest.json", [])), \
                patch.object(DeadlineJobSubmitter, "submit_job", submit):
            result = DeadlineSubmitNode().submit_to_deadline(r"X:\outputs\shot", 1, 1, 50, "none", "none", "Render",
                                                           prompt={"1": {"class_type": "SaveImage", "inputs": {}}})
        self.assertEqual(result, ("unc-test-job",))
        self.assertEqual(captured[0]["output_directory"], r"\\NAS02\Comfy\outputs\shot")
        self.assertTrue(captured[0]["input_directory"].startswith(r"\\NAS02\Comfy\outputs"))
        mkdir.assert_called_once_with(r"\\NAS02\Comfy\outputs\shot", exist_ok=True)

    @unittest.skipUnless(os.name == "nt", "Windows mapped network paths")
    def test_absolute_media_source_is_resolved_to_allowed_unc(self):
        _, settings = self.studio_policy()
        stager = InputAssetStager(r"X:\outputs\shot", "test", "test")
        with patch.object(DeadlineCommandHelper, "get_farm_settings", return_value=settings), \
                patch.object(os.path, "realpath", side_effect=lambda path: path), \
                patch.object(os.path, "isfile", return_value=True):
            result = stager._resolve_input_file(r"G:\project\source.png", r"C:\Comfy\input", True, True)
        self.assertEqual(result, (r"G:\project\source.png", r"\\NAS02\grajdolek\project\source.png"))

    @unittest.skipUnless(os.name == "nt", "Windows mapped network paths")
    def test_mapped_rules_discover_their_unc_equivalents(self):
        module = load_script("read_farm_settings", directory="")
        config = {"AllowedStorageRoots": "G:\\*;X:\\input\\*;X:\\outputs\\*;X:\\playground\\*;Y:\\*"}
        plugin_config = types.SimpleNamespace(GetConfigEntryWithDefault=lambda key, default: config.get(key, default))
        aliases = {"G:": r"\\NAS02\grajdolek", "X:": r"\\NAS02\Comfy", "Y:": r"\\NAS04\3d-02"}
        with patch.object(module.RepositoryUtils, "GetPluginConfig", return_value=plugin_config, create=True), \
                patch.object(module.PathUtils, "GetMappedDriveNetworkPath", side_effect=lambda drive: aliases.get(drive, "")) as mapping, \
                patch("builtins.print") as printed:
            module.__main__()
        settings = json.loads(printed.call_args.args[0].split("=", 1)[1])
        self.assertEqual(settings["drive_mappings"], aliases)
        self.assertEqual(mapping.call_count, 3)
        self.assertIn("\\\\NAS02\\Comfy\\input\\", settings["output_roots"])
        self.assertNotIn("\\\\NAS02\\Comfy\\", settings["output_roots"])

    def test_allowed_full_output_path_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            farm = root / "farm"
            folders = types.SimpleNamespace(get_output_directory=lambda: str(root / "local"))
            settings = {"output_roots": [str(farm)], "input_roots": [str(farm)]}
            with patch.dict(sys.modules, {"folder_paths": folders}), patch.object(DeadlineCommandHelper, "get_farm_settings", return_value=settings):
                full_path = str(farm / "projects" / "shot010" / "output")
                self.assertEqual(DeadlineSubmitNode()._prepare_output_directory(full_path), full_path)
                with self.assertRaises(ValueError):
                    DeadlineSubmitNode()._prepare_output_directory("../private")
                with self.assertRaises(ValueError):
                    DeadlineSubmitNode()._prepare_output_directory(str(root / "private"))
                self.assertFalse((root / "private").exists())

    def test_blank_and_relative_output_paths_are_not_reinterpreted(self):
        with patch.object(DeadlineCommandHelper, "get_farm_settings") as settings, \
                patch.object(deadline_submit.os, "makedirs") as mkdir:
            for path in ("", "shot010", "../private", "X:shot010"):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "full farm-visible path"):
                    DeadlineSubmitNode()._prepare_output_directory(path)
            settings.assert_not_called()
            mkdir.assert_not_called()

    def test_unapproved_full_path_is_rejected_before_staging_or_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folders = types.SimpleNamespace(get_output_directory=lambda: str(root / "local"))
            settings = {"output_roots": [str(root / "allowed")], "input_roots": []}
            with patch.dict(sys.modules, {"folder_paths": folders}), \
                    patch.object(DeadlineCommandHelper, "get_farm_settings", return_value=settings), \
                    patch.object(InputAssetStager, "stage_referenced_assets") as stage, \
                    patch.object(DeadlineJobSubmitter, "submit_job") as submit, \
                    patch.object(deadline_submit.os, "makedirs") as mkdir:
                with self.assertRaisesRegex(ValueError, "approved storage"):
                    DeadlineSubmitNode().submit_to_deadline(str(root / "denied"), 1, 1, 50, "none", "none", "Render", prompt={})
                stage.assert_not_called()
                submit.assert_not_called()
                mkdir.assert_not_called()

    def test_settings_read_failure_does_not_allow_unapproved_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folders = types.SimpleNamespace(get_output_directory=lambda: str(root / "local"))
            with patch.dict(sys.modules, {"folder_paths": folders}), \
                    patch.object(DeadlineCommandHelper, "get_farm_settings", side_effect=RuntimeError("Deadline unavailable")), \
                    patch.object(deadline_submit.os, "makedirs") as mkdir:
                with self.assertRaisesRegex(RuntimeError, "Deadline unavailable"):
                    DeadlineSubmitNode()._prepare_output_directory(str(root / "other"))
                mkdir.assert_not_called()

    def test_config_reader_uses_fixed_script_and_validates_response(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = {"output_roots": [temp], "input_roots": []}
            output = "Deadline startup message\nCOMFY_DEADLINE_FARM_SETTINGS=" + json.dumps(settings) + "\n"
            with patch.object(DeadlineCommandHelper, "call_deadline_command", return_value=output) as command:
                self.assertEqual(DeadlineCommandHelper.get_farm_settings(), settings)
            arguments = command.call_args.args[0]
            self.assertEqual(arguments[0], "-ExecuteScriptNoGui")
            self.assertEqual(Path(arguments[1]).name, "read_farm_settings.py")
            self.assertTrue(Path(arguments[1]).is_file())
            for output in ("no settings", "COMFY_DEADLINE_FARM_SETTINGS={}"):
                with self.subTest(output=output), patch.object(DeadlineCommandHelper, "call_deadline_command", return_value=output):
                    with self.assertRaises((RuntimeError, ValueError)):
                        DeadlineCommandHelper.get_farm_settings()

    def test_submitter_and_worker_read_same_admin_wildcard_policy(self):
        module = load_script("read_farm_settings", directory="")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            allowed = root / "projects"
            config = {"DefaultOutputDirectory": str(root / "renders"), "AllowedStorageRoots": str(allowed) + os.sep + "*"}
            plugin_config = types.SimpleNamespace(GetConfigEntryWithDefault=lambda key, default: config.get(key, default))
            with patch.object(module.RepositoryUtils, "GetPluginConfig", return_value=plugin_config, create=True), patch("builtins.print") as printed:
                module.__main__()
            settings = json.loads(printed.call_args.args[0].split("=", 1)[1])
            self.assertEqual(settings["input_roots"], settings["output_roots"])
            plugin = new_plugin(GetConfigEntryWithDefault=plugin_config.GetConfigEntryWithDefault,
                                _resolve_comfyui_install_path=lambda: str(root / "install"))
            for kind in ("input", "output"):
                roots = plugin._storage_roots(kind)
                for path in settings[kind + "_roots"]:
                    self.assertIn(path, roots)
                candidate = str(allowed / "a" / "b" / "image.png")
                self.assertEqual(plugin_module.approved_path(candidate, roots), candidate)
                self.assertEqual(deadline_submit._approved_path(candidate, settings[kind + "_roots"]), candidate)
                with self.assertRaises(ComfyUIError):
                    plugin_module.approved_path(str(root / "projects-other" / "image.png"), roots)

    @unittest.skipUnless(os.name == "nt", "Windows drive policy")
    def test_admin_can_allow_whole_x_y_and_g_drives(self):
        module = load_script("read_farm_settings", directory="")
        raw = "X:\\*\nY:\\*\nG:\\*"
        roots = module.storage_locations(raw)
        self.assertEqual(roots, ["X:\\", "Y:\\", "G:\\"])
        plugin = new_plugin(GetConfigEntryWithDefault=lambda key, default: raw if key == "AllowedStorageRoots" else default,
                            _resolve_comfyui_install_path=lambda: "C:\\Comfy")
        worker_roots = plugin._storage_roots("output")
        with patch.object(os.path, "realpath", side_effect=lambda path: path):
            for drive in ("X", "Y", "G"):
                candidate = drive + ":\\projects\\shot010\\output\\image.png"
                self.assertEqual(deadline_submit._approved_path(candidate, roots), candidate)
                self.assertEqual(plugin_module.approved_path(candidate, worker_roots), candidate)
            with self.assertRaises(ValueError):
                deadline_submit._approved_path("C:\\private\\file.png", roots)
            with self.assertRaises(ComfyUIError):
                plugin_module.approved_path("C:\\private\\file.png", worker_roots)

    def test_unsupported_globs_and_relative_rules_are_rejected(self):
        module = load_script("read_farm_settings", directory="")
        for raw in ("*", "relative/*", "X:drive-relative", r"X:\project*\renders", r"X:\**", r"X:\project?\renders"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                module.storage_locations(raw)
            plugin = new_plugin(GetConfigEntryWithDefault=lambda key, default: raw if key == "AllowedStorageRoots" else default,
                                _resolve_comfyui_install_path=lambda: "C:/Comfy")
            with self.assertRaises(ComfyUIError):
                plugin._storage_roots("output")


class WorkerSecurityTests(unittest.TestCase):
    def test_worker_rejects_unapproved_job_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            farm = root / "farm"
            config = {"AllowedStorageRoots": str(farm) + os.sep + "*"}
            plugin = new_plugin(_resolve_comfyui_install_path=lambda: str(root / "install"),
                                GetConfigEntryWithDefault=lambda key, default: config.get(key, default))
            plugin._setup_custom_output_directory(str(farm / "render"))
            self.assertTrue((farm / "render").is_dir())
            outside = root / "outside"
            with self.assertRaises(ComfyUIError):
                plugin._setup_custom_output_directory(str(outside))
            self.assertFalse(outside.exists())

    def test_worker_rejects_input_path_outside_operator_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entries = {"JobInputDirectory": str(root / "private"), "SubmissionId": "test"}
            plugin = new_plugin(_resolve_comfyui_install_path=lambda: str(root / "install"),
                                GetConfigEntryWithDefault=lambda key, default: default,
                                GetPluginInfoEntryWithDefault=lambda key, default: entries.get(key, default))
            with self.assertRaisesRegex(ComfyUIError, "approved storage"):
                plugin._setup_input_directory()

    def test_history_cannot_copy_through_source_or_target_junctions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            gui, target, private = (root / name for name in ("gui", "target", "private"))
            for folder in (gui, target, private):
                folder.mkdir()
            (private / "secret.png").write_bytes(b"secret")
            directory_link(gui / "link", private)
            plugin = new_plugin(use_existing_comfyui=True, custom_output_dir_specified=True,
                                reuse_gui_output_root=str(gui), comfyui_output_dir=str(target))
            records = {"1": {"images": [{"filename": "secret.png", "subfolder": "link", "type": "output"}]}}
            with self.assertRaises(ComfyUIError):
                plugin._log_output_information(records)
            self.assertEqual(list(target.iterdir()), [])
            (gui / "safe").mkdir()
            (gui / "safe" / "out.png").write_bytes(b"render")
            directory_link(target / "safe", private)
            records["1"]["images"][0].update(filename="out.png", subfolder="safe")
            with self.assertRaises(ComfyUIError):
                plugin._log_output_information(records)
            self.assertFalse((private / "out.png").exists())

    def test_workflow_files_are_limited_to_job_auxiliaries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auxiliary = root / "job-files"
            auxiliary.mkdir()
            workflow = auxiliary / "workflow.json"
            workflow.write_text("{}")
            plugin = new_plugin(temp_dir=str(root / "temp"), GetDataFilename=lambda: str(auxiliary / "prompt.json"))
            self.assertEqual(plugin._resolve_workflow_file_path("workflow.json"), str(workflow))
            for path in ("../private.json", str(root / "private.json"), r"\\unapproved.invalid\share\secret.json"):
                with self.subTest(path=path), self.assertRaises(ComfyUIError):
                    plugin._resolve_workflow_file_path(path)

    def test_output_prefix_cannot_escape_worker_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = new_plugin(comfyui_output_dir=temp)
            for prefix in ("../private", r"..\private", r"C:\private", "name:stream"):
                with self.subTest(prefix=prefix), self.assertRaises(ComfyUIError):
                    plugin._queue_prompt({"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix}}}, {})

    def test_worker_preflight_checks_links_and_absolute_file_loaders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staged, private = root / "staged", root / "private"
            staged.mkdir()
            private.mkdir()
            (staged / "source.png").write_bytes(b"media")
            (private / "secret.png").write_bytes(b"private")
            directory_link(staged / "link", private)
            definitions = {"LoadImage": {"input": {"required": {"image": ["STRING", {}]}}},
                           "VHS_LoadImagePath": {"input": {"required": {"image": ["STRING", {}]}}},
                           "SaveImage": {"input": {}, "output_node": True}}
            plugin = new_plugin(comfyui_input_dir=str(staged), comfyui_output_dir=str(root / "output"), object_info=definitions)
            for class_type, value in (("LoadImage", "link/secret.png"), ("VHS_LoadImagePath", str(private / "secret.png"))):
                prompt = {"1": {"class_type": class_type, "inputs": {"image": value}}, "2": {"class_type": "SaveImage", "inputs": {}}}
                with self.subTest(class_type=class_type), self.assertRaisesRegex(ComfyUIError, "invalid staged path"):
                    plugin._preflight_worker_payload(prompt)
            prompt["1"]["inputs"]["image"] = str(staged / "source.png")
            _, outputs = plugin._preflight_worker_payload(prompt)
            self.assertEqual(outputs, ["2"])

    def test_spool_request_cannot_override_job_output_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "request.json"
            package.write_text(json.dumps({"resultRoot": str(root / "private")}))
            plugin = new_plugin(temp_dir=temp, GetDataFilename=lambda: str(root / "prompt.json"), client_id="test",
                                comfyui_output_dir=str(root / "output"),
                                GetPluginInfoEntryWithDefault=lambda key, default: "request.json" if key == "SpoolRequestFile" else default)
            with patch.object(plugin, "http_request") as request, self.assertRaises(ComfyUIError):
                plugin._queue_prompt({"1": {"class_type": "SaveImage", "inputs": {}}}, {})
            request.assert_not_called()

    def test_windows_launch_arguments_round_trip_without_extra_options(self):
        with tempfile.TemporaryDirectory(prefix="deadline job ") as temp:
            target = str(Path(temp) / "output with spaces") + os.sep
            plugin = new_plugin(temp_dir=temp, comfyui_output_dir=target, custom_output_dir_specified=True,
                                _get_cuda_device_arg=lambda: "--cuda-device 0")
            arguments = plugin._build_comfyui_arguments("C:/Comfy/ComfyUI/main.py")
            # Use Python itself as the Windows CRT argument parser.
            code = "import json, sys; print(json.dumps(sys.argv[1:]))"
            command = subprocess.list2cmdline([sys.executable, "-c", code]) + " " + arguments
            if os.name == "nt":
                parsed = json.loads(subprocess.check_output(command, text=True))
                self.assertEqual(parsed[parsed.index("--output-directory") + 1], target)
                self.assertEqual(parsed.count("--output-directory"), 1)
            plugin.comfyui_output_dir = target + '" --extra-model-paths-config evil.yaml'
            with self.assertRaises(ComfyUIError):
                plugin._build_comfyui_arguments("C:/Comfy/ComfyUI/main.py")

    def test_waiter_runs_plain_script_and_propagates_completion(self):
        with tempfile.TemporaryDirectory(prefix="waiter job ") as temp:
            plugin = new_plugin(temp_dir=temp, submit_workflow=lambda: None)
            with patch.object(plugin_module.threading, "Thread"):
                arguments = plugin._create_dummy_command()
            self.assertIn("reuse_waiter.py", arguments)
            for success in (True, False):
                plugin._signal_reuse_waiter(success)
                script = Path(plugin_module.__file__).with_name("reuse_waiter.py")
                completed = subprocess.run([sys.executable, "-s", str(script), plugin.reuse_completion_marker], capture_output=True, timeout=10)
                self.assertEqual(completed.returncode, 0 if success else 1, completed.stderr)
                self.assertFalse(Path(plugin.reuse_completion_marker).exists())

    def test_http_rejects_nonlocal_endpoints_without_opening_connection(self):
        plugin = new_plugin()
        for url in ("https://127.0.0.1:8188/prompt", "http://example.com:8188/prompt", "http://user:secret@localhost:8188/prompt", "file:///etc/passwd"):
            with self.subTest(url=url), patch.object(plugin_module.urllib.request, "build_opener") as opener:
                with self.assertRaises(ComfyUIError):
                    plugin.http_request(url)
                opener.assert_not_called()

    def test_http_does_not_follow_redirects_or_use_environment_proxy(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://unapproved.invalid/leak")
                else:
                    self.send_response(200)
                self.end_headers()
                if self.path != "/redirect":
                    self.wfile.write(b'{"ok": true}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_port)
            plugin = new_plugin()
            with patch.dict(os.environ, {"http_proxy": "http://unapproved.invalid:1234", "no_proxy": ""}):
                self.assertEqual(plugin.http_request(base + "/ok")["json"](), {"ok": True})
                with self.assertRaisesRegex(ComfyUIError, "redirects"):
                    plugin.http_request(base + "/redirect")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class SpoolSecurityTests(unittest.TestCase):
    def test_successful_spool_receipt_round_trip_and_path_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompt = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "render"}}}
            package = dict(schemaVersion=2, variationIndex=0, projectId="project", clipId="clip", takeId="take",
                           attemptId="attempt", submissionId="submission", fingerprint="fingerprint",
                           stagedPrompt=prompt, assets=[], resultRoot=str(root), result={"nodeId": "1", "group": "images", "index": 0})
            execution = spool_protocol.prepare_execution(package, prompt, root, 0)
            relative = Path(execution["prompt"]["1"]["inputs"]["filename_prefix"] + "_00001_.png")
            image = root / relative
            image.parent.mkdir(parents=True)
            image.write_bytes(b"rendered image")
            history = {"status": {"completed": True, "status_str": "success"}, "prompt": [0, "prompt-id", execution["prompt"]],
                       "outputs": {"1": {"images": [{"filename": image.name, "subfolder": relative.parent.as_posix(), "type": "output"}]}}}
            receipt = spool_protocol.finalize(package, execution, "prompt-id", history, {"output": str(root)}, "worker", "job")
            primary = spool_protocol.verify(package, receipt, root)
            self.assertEqual((root / primary["path"]).read_bytes(), b"rendered image")
            receipt["artifacts"][0]["path"] = "../private.png"
            with self.assertRaises(ValueError):
                spool_protocol.verify(package, receipt, root)
            execution = spool_protocol.prepare_execution(package, prompt, root, 0)
            history["prompt"][2] = execution["prompt"]
            (root / "another-job.png").write_bytes(b"another job's image")
            history["outputs"]["1"]["images"][0].update(filename="another-job.png", subfolder="")
            with self.assertRaisesRegex(ValueError, "another execution"):
                spool_protocol.finalize(package, execution, "prompt-id", history, {"output": str(root)}, "worker", "job")

    def test_relative_paths_reject_windows_and_posix_escape_forms(self):
        with tempfile.TemporaryDirectory() as temp:
            for path in ("../escape", r"..\escape", "/absolute", r"C:\absolute", r"\\server\share\file", "file:stream", "NUL.png"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    spool_protocol.contained(temp, path)
            self.assertEqual(spool_protocol.contained(temp, "safe/file.png"), Path(temp).resolve() / "safe" / "file.png")

    def test_attempt_identifiers_cannot_rewrite_output_namespaces(self):
        for attempt in ("../outside", "nested/attempt", "nested\\attempt", "name:stream", ""):
            with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                spool_protocol.saver_prefix({"attemptId": attempt}, "execution", "node")

    def test_worker_and_submitter_use_identical_protocol(self):
        root = Path(spool_protocol.__file__).parent
        self.assertEqual((root / "spool_protocol.py").read_bytes(), (root / "plugins" / "ComfyUI" / "spool_protocol.py").read_bytes())


class MaintenanceSecurityTests(unittest.TestCase):
    def test_model_list_cannot_escape_source_root_or_destination_root(self):
        module = load_script("ComfyModelsSync")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, destination, outside = (root / name for name in ("models", "destination", "models-other"))
            for folder in (source, destination, outside):
                folder.mkdir()
            for path in (outside / "model.safetensors", source / ".." / "secret", Path(r"\\unapproved.invalid\share\model")):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    module.checked_model_path(str(source), str(path))
            directory_link(destination / "link", outside)
            with self.assertRaises(ValueError):
                module.checked_model_path(str(destination), str(destination / "link" / "model.safetensors"))
            self.assertEqual(module.checked_model_path(str(source), str(source / "safe.safetensors")), str(source / "safe.safetensors"))

    def test_maintenance_metadata_cannot_inject_job_entries(self):
        module = load_script("submit_comfy_sync")
        args = types.SimpleNamespace(pool="none", group="none", region="", allowlist="", user_name="",
                                     worker_deadline_command="deadlinecommand", additional_arguments="")
        with self.assertRaises(ValueError):
            module.build_job_info(args, "sync\nPreJobScript=evil.py", "comment")
        with self.assertRaises(ValueError):
            module.build_plugin_info(args, 'script.py" -ExecuteScript evil.py')
        args.additional_arguments = "value\nShellExecute=True"
        with self.assertRaises(ValueError):
            module.build_plugin_info(args, "script.py")


if __name__ == "__main__":
    unittest.main()
