import importlib.util
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from farm_protocol import RunStore, atomic_json

specification = importlib.util.spec_from_file_location("native_task_under_test", Path(__file__).parents[1] / "plugins/ComfyUI/native_queue_task.py")
task = importlib.util.module_from_spec(specification)
specification.loader.exec_module(task)


class NativeTaskFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.store = RunStore(root / "share")
        self.run_id, self.attempt = str(uuid.uuid4()), str(uuid.uuid4())
        atomic_json(self.store.run(self.run_id) / "receipt.json", {"job_id": "job"})
        atomic_json(self.store.run(self.run_id) / "control.json", {"generation": 0, "status": "pending", "cancel_requested": False, "result": None})
        state = self.store.begin_attempt(self.run_id, self.attempt, "job", "Worker", {"worker": "Worker", "start": 1})
        self.spec = {"root": str(self.store.root), "run_id": self.run_id, "attempt_id": self.attempt,
            "job_id": "job", "generation": state["generation"], "skip": False, "token": "reservation",
            "broker": "http://127.0.0.1:8202", "timeout_seconds": 0, "payload": {"prompt": {}},
            "identity": {"output_root": str(root / "outputs"), "temp_root": str(root / "temp"), "session_id": "session"},
            "expected_outputs": ["1"], "assignment": {"worker": "Worker", "start": 639269075466550000}}

    def history(self, filename):
        return {"outputs": {"1": {"images": [{"filename": filename, "type": "output"}]}}, "status": {"status_str": "success"}}

    def test_missing_output_cannot_publish_success(self):
        with self.assertRaisesRegex(ValueError, "Missing ComfyUI output"):
            task.publish_outputs(self.store, self.spec, self.history("missing.png"))
        self.assertIsNone(self.store.control(self.run_id)["result"])

    def test_no_file_from_required_output_node_fails(self):
        with self.assertRaisesRegex(ValueError, "no validated files"):
            task.publish_outputs(self.store, self.spec, {"outputs": {"1": {"text": ["hello"]}}, "status": {"status_str": "success"}})

    def test_output_traversal_rejected(self):
        with self.assertRaisesRegex(ValueError, "escapes"):
            task.publish_outputs(self.store, self.spec, self.history("../artist.png"))

    def test_timeout_cancels_only_the_reserved_prompt(self):
        def broker(url, body):
            return {"prompt_id": "own-prompt"} if url.endswith("/queue") else {}
        with patch.object(task, "request", side_effect=broker) as request:
            with self.assertRaises(TimeoutError): task.run(self.spec)
        self.assertTrue(any(call.args == (self.spec["broker"] + "/cancel", {"token": "reservation"}) for call in request.call_args_list))
        self.assertTrue(all(call.args[0].startswith(self.spec["broker"]) for call in request.call_args_list))
        self.assertEqual(self.store.control(self.run_id)["status"], "failed")
        self.assertIsNone(self.store.control(self.run_id)["result"])

    def test_assignment_requires_current_worker_status_and_start(self):
        fields = "COMFY_NATIVE_ASSIGNMENT=" + json.dumps({"status": "Rendering", "worker": "Worker", "start": 639269075466550000})
        with patch.object(task.subprocess, "run", return_value=Mock(returncode=0, stdout=fields)):
            self.assertTrue(task.assignment_is_current(self.spec))
        for stale in (fields.replace("Worker", "Other"), fields.replace("Rendering", "Queued"), fields.replace("639269075466550000", "639269075466550001")):
            with patch.object(task.subprocess, "run", return_value=Mock(returncode=0, stdout=stale)):
                self.assertFalse(task.assignment_is_current(self.spec))

    def test_unknown_assignment_never_authorizes_publication(self):
        with patch.object(task.subprocess, "run", side_effect=subprocess.TimeoutExpired("deadline", 30)):
            with self.assertRaises(subprocess.TimeoutExpired): task.assignment_is_current(self.spec)


class TaskOwnershipTests(unittest.TestCase):
    def test_task_owner_resolves_deadline_affinity_and_verifies_its_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = Mock()
            plugin.temp_dir = directory
            plugin._resolve_comfyui_install_path.return_value = directory
            plugin._get_gpu_from_worker_affinity.return_value = "1"
            plugin.GetSlaveName.return_value = "ExistingWorker"
            owner = Mock()
            owner.process.pid = 1234
            owner.process.poll.return_value = None
            with patch("farm_session_owner.OwnedProcess", return_value=owner) as launch, \
                    patch.object(task.subprocess, "check_output", return_value="0, GPU-zero\n1, GPU-one\n"), \
                    patch.object(task, "request", return_value={"owner_pid":1234}):
                config = task.start_task_owner(plugin, startup_seconds=600)
            command = launch.call_args.args[0]
            plugin._set_deadline_environment_variables.assert_called_once()
            self.assertEqual(command[command.index("--gpu-uuid") + 1], "GPU-one")
            self.assertNotIn("--exclusive-worker", command)
            self.assertEqual(command[command.index("--startup-seconds") + 1], "600")
            self.assertEqual(config["gpu_uuid"], "GPU-one")
            plugin.native_owner_log.close()

    def test_cleanup_closes_task_owner_even_when_reservation_cleanup_fails(self):
        plugin = Mock(native_owner=Mock(), native_owner_log=Mock())
        owner, log = plugin.native_owner, plugin.native_owner_log
        with patch.object(task, "cleanup_reservation", side_effect=RuntimeError("broker unavailable")):
            with self.assertRaises(RuntimeError):
                task.cleanup(plugin)
        owner.close.assert_called_once()
        log.close.assert_called_once()
        self.assertIsNone(plugin.native_owner)

    def test_task_owner_does_not_use_another_process_at_its_port(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = Mock(temp_dir=directory)
            plugin._resolve_comfyui_install_path.return_value = directory
            plugin._get_gpu_from_worker_affinity.return_value = "0"
            owner = Mock()
            owner.process.pid = 1234
            owner.process.poll.return_value = None
            with patch("farm_session_owner.OwnedProcess", return_value=owner), \
                    patch.object(task.subprocess, "check_output", return_value="0, GPU-zero\n"), \
                    patch.object(task, "request", return_value={"owner_pid":9999}):
                with self.assertRaisesRegex(RuntimeError, "another process"):
                    task.start_task_owner(plugin)
            with patch.object(task, "cleanup_reservation"):
                task.cleanup(plugin)
            owner.close.assert_called_once()

    def test_reused_sandbox_closes_previous_owner_before_preparing_another_task(self):
        plugin = Mock(native_owner=Mock())
        plugin.GetPluginInfoEntry.return_value = "not-a-run-uuid"
        with patch.object(task, "cleanup") as cleanup:
            with self.assertRaises(ValueError):
                task.prepare(plugin)
        cleanup.assert_called_once_with(plugin)
