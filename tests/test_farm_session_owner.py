import argparse
import subprocess
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from farm_protocol import read_json
from farm_session_owner import SessionOwner


class SessionOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.args = argparse.Namespace(state_root=root, comfy_root=root, worker="Worker", gpu_uuid="GPU-test",
            borrowed_url="", borrowed_input_root=None, borrowed_output_root=None, borrowed_temp_root=None,
            comfy_port=8198, broker_port=8202, exclusive_worker=True, idle_seconds=1)
        self.owner = SessionOwner(self.args)
        self.owner.owned = Mock()
        self.owner.owned.process.poll.return_value = None
        self.owner.reservation = {"token": "token", "prompt_id": "own-prompt", "job_id": "job",
            "reserved_at": time.time(), "last_client_at": time.monotonic() - 30}
        self.owner.status = Mock(return_value={"state": "running"})
        self.owner.check_identity = Mock()

    def test_missing_worker_plugin_reports_404_immediately_and_stops_owned_child(self):
        self.owner.owned = None
        self.owner.reservation = None
        self.args.custom_nodes = []
        self.args.extra_args = []
        process = Mock()
        process.process.poll.return_value = None
        responses = [urllib.error.URLError("refused"),
            urllib.error.HTTPError(self.owner.url + "/deadline/session", 404, "Missing", {}, None)]
        with patch("farm_session_owner.comfy_request", side_effect=responses), \
                patch("farm_session_owner.subprocess.check_output", return_value="0, GPU-test\n"), \
                patch("farm_session_owner.OwnedProcess", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "lacks ComfyUI-Deadline-Plugin"):
                self.owner.reserve({"worker":"Worker","gpu_uuid":"GPU-test"})
        process.close.assert_called_once()
        self.assertIsNone(self.owner.owned)

    def test_stale_heartbeat_cannot_stop_current_assignment(self):
        self.owner.assignment_active = Mock(return_value=True)
        with patch("farm_session_owner.comfy_request") as request:
            self.owner.idle_tick()
        request.assert_not_called()
        self.owner.owned.close.assert_not_called()
        self.assertIsNotNone(self.owner.reservation)

    def test_deadline_timeout_preserves_active_ownership(self):
        with patch("farm_session_owner.subprocess.run", side_effect=subprocess.TimeoutExpired("deadline", 10)), patch("farm_session_owner.comfy_request") as request:
            self.owner.idle_tick()
        request.assert_not_called()
        self.owner.owned.close.assert_not_called()

    def test_lost_assignment_interrupts_only_reserved_prompt(self):
        self.owner.assignment_active = Mock(return_value=False)
        with patch("farm_session_owner.comfy_request") as request:
            self.owner.idle_tick()
        request.assert_called_once_with(self.owner.url + "/api/jobs/own-prompt/cancel", {})
        self.owner.owned.close.assert_not_called()

    def test_finished_prompt_remains_reserved_while_task_publishes(self):
        self.owner.status.return_value = {"state": "finished"}
        self.owner.assignment_active = Mock(return_value=True)
        self.owner.idle_tick()
        self.assertIsNotNone(self.owner.reservation)
        self.owner.owned.close.assert_not_called()

    def test_shared_worker_releases_gpu_after_abandoned_finished_task(self):
        self.args.exclusive_worker = False
        self.owner.status.return_value = {"state": "finished"}
        self.owner.assignment_active = Mock(return_value=False)
        child = self.owner.owned
        self.owner.idle_tick()
        child.close.assert_called_once()
        self.assertIsNone(self.owner.reservation)
        self.assertIsNone(read_json(self.args.state_root / "reservation.json")["reservation"])

    def test_borrowed_idle_endpoint_is_never_terminated(self):
        self.owner.borrowed, self.owner.owned, self.owner.reservation = True, None, None
        self.owner.last_active = time.monotonic() - 10
        self.owner.stop = Mock()
        with patch("farm_session_owner.comfy_request", return_value={"queue_running": [], "queue_pending": []}):
            self.owner.idle_tick()
        self.owner.stop.assert_not_called()

    def test_idle_owned_endpoint_checks_queue_before_shutdown(self):
        self.owner.reservation = None
        self.owner.last_active = time.monotonic() - 10
        child = self.owner.owned
        with patch("farm_session_owner.comfy_request", return_value={"queue_running": ["artist"], "queue_pending": []}):
            self.owner.idle_tick()
        child.close.assert_not_called()
        with patch("farm_session_owner.comfy_request", return_value={"queue_running": [], "queue_pending": []}):
            self.owner.idle_tick()
        child.close.assert_called_once()

    def test_borrowed_off_host_endpoint_is_rejected(self):
        self.args.borrowed_url = "http://other-host:8188"
        with self.assertRaisesRegex(ValueError, "worker-local"):
            SessionOwner(self.args)

    def test_dead_child_clears_durable_reservation(self):
        self.owner.owned.process.poll.return_value = 1
        self.owner.idle_tick()
        self.assertIsNone(read_json(self.args.state_root / "reservation.json")["reservation"])
