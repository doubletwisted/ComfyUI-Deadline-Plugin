import copy
import concurrent.futures
import contextlib
import hashlib
import json
import tempfile
import threading
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from farm_backend import FarmBackend, parse_ini, validate_settings, register_backend
from farm_protocol import RunStore, read_json, atomic_json


class FakeDeadline:
    def __init__(self):
        self.jobs = {}
        self.submissions = 0
        self.lose_submit = False
        self.lose_resume = False
        self.snapshot = {"repository": "\\\\repo\\repository", "plugin_available": True,
            "workers": [{"name": "TestWorker", "enabled": True, "status": "Idle", "session_configured": True,
                "pools": ["test"], "groups": ["none"]}], "pools": ["test"], "groups": ["none"]}

    def __call__(self, *args):
        if args[0] == "-ExecuteScript":
            if args[1].endswith("deadline_job_status.py"):
                return "COMFY_NATIVE_JOBS=" + json.dumps({i:{"Status":self.jobs[i]["Status"] if i in self.jobs else "Deleted"} for i in args[2:]})
            return "COMFY_NATIVE_FARM=" + json.dumps(self.snapshot)
        if args[0].endswith("job.info"):
            self.submissions += 1
            job_id = f"{self.submissions:024x}"
            values = dict(line.split("=", 1) for line in Path(args[0]).read_text().splitlines())
            self.jobs[job_id] = {"Status": "Suspended", "Name": values["Name"], "Plugin": values["Plugin"],
                "run_id": values["ExtraInfoKeyValue1"].split("=", 1)[1]}
            if self.lose_submit: raise TimeoutError("lost submit response")
            return "JobID=" + job_id
        if args[0] == "-GetJobsFilterIniAnd":
            name = args[1].split("=", 1)[1]
            ids = [i for i, j in self.jobs.items() if j["Name"] == name]
        elif args[0] == "-GetJob":
            ids = args[1].split(",")
        elif args[0] == "-GetJobExtraInfoKeyValue": return self.jobs[args[1]]["run_id"]
        elif args[0] == "-ResumeJob":
            self.jobs[args[1]]["Status"] = "Active"
            if self.lose_resume: raise TimeoutError("lost activation response")
            return "Success"
        elif args[0] == "-GetRepositoryPath": return "\\\\repo\\repository"
        elif args[0] == "-GetCurrentUserName": return "artist"
        else: raise AssertionError(args)
        return "\n".join("[" + i + "]\n" + "\n".join(k + "=" + v for k, v in self.jobs[i].items()) for i in ids if i in self.jobs)


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RunStore(self.root / "share")
        self.store.initialize()
        self.fake = FakeDeadline()
        self.backend = FarmBackend(self.root / "settings.json", self.fake)
        self.backend.settings.update(enabled=True, root="\\\\farm\\native", workers="TestWorker")
        self.patches = [patch("farm_backend.RunStore", return_value=self.store),
            patch.dict("sys.modules", {"folder_paths": types.SimpleNamespace(get_input_directory=lambda: str(self.root / "input"))})]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
        self.body = {"prompt": {"1": {"class_type": "SaveImage", "inputs": {"seed": 123}}},
            "extra_data": {"auth_token_comfy_org": "do-not-store", "extra_pnginfo": {"workflow": {"id": "workflow", "nodes": []}}},
            "submission_key": str(uuid.uuid4()), "batch_id": str(uuid.uuid4())}

    def test_transport_retry_and_distinct_clicks(self):
        first = self.backend.submit(self.body)
        self.assertEqual(self.backend.submit(self.body), first)
        second = self.backend.submit({**self.body, "submission_key": str(uuid.uuid4())})
        self.assertNotEqual(first["prompt_id"], second["prompt_id"])
        self.assertEqual(self.fake.submissions, 2)
        stored = read_json(self.store.run(first["prompt_id"]) / "submission.json")
        self.assertNotIn("auth_token_comfy_org", stored["payload"]["extra_data"])
        changed = copy.deepcopy(self.body)
        changed["prompt"]["1"]["inputs"]["seed"] = 124
        with self.assertRaisesRegex(ValueError, "different snapshot"): self.backend.submit(changed)
        self.assertEqual(self.fake.submissions, 2)

    def test_lost_submit_response_is_discovered_without_resubmit_after_restart(self):
        self.fake.lose_submit = True
        with self.assertRaises(TimeoutError): self.backend.submit(self.body)
        self.fake.lose_submit = False
        replacement = FarmBackend(self.root / "other-settings.json", self.fake)
        replacement.settings = dict(self.backend.settings)
        accepted = replacement.submit(self.body)
        self.assertEqual(self.fake.submissions, 1)
        self.assertTrue(read_json(self.store.run(accepted["prompt_id"]) / "receipt.json")["accepted"])

    def test_lost_activation_response_does_not_duplicate_and_background_recovers(self):
        self.fake.lose_resume = True
        with self.assertRaises(TimeoutError): self.backend.submit(self.body)
        self.fake.lose_resume = False
        self.backend.refresh()
        self.assertEqual(len(self.backend.jobs), 1)
        self.assertTrue(next(iter(self.backend.jobs.values()))["deadline"]["accepted"])
        self.assertEqual(self.fake.submissions, 1)

    def test_unknown_submission_remains_unconfirmed_and_cannot_resubmit(self):
        original = self.fake.__call__
        def unavailable(*args):
            if args[0].endswith("job.info"): raise TimeoutError("unknown")
            return original(*args)
        self.backend.command = unavailable
        with self.assertRaises(TimeoutError): self.backend.submit(self.body)
        self.backend.command = self.fake
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"): self.backend.submit(self.body)
        self.assertEqual(self.fake.submissions, 0)

    def test_partial_batch_failure_keeps_earlier_accepted_run(self):
        first = self.backend.submit(self.body)["prompt_id"]
        second = {**self.body, "submission_key": str(uuid.uuid4())}
        self.fake.lose_submit = True
        with self.assertRaises(TimeoutError): self.backend.submit(second)
        self.assertTrue(read_json(self.store.run(first) / "receipt.json")["accepted"])
        self.assertEqual(self.backend.jobs[first]["status"], "pending")
        self.fake.lose_submit = False
        recovered = self.backend.submit(second)["prompt_id"]
        self.assertNotEqual(first, recovered)
        self.assertEqual(self.fake.submissions, 2)

    def test_retry_uses_original_resolved_snapshot(self):
        original = self.backend.submit(self.body)["prompt_id"]
        state = self.store.control(original)
        state["status"] = "failed"
        atomic_json(self.store.run(original) / "control.json", state)
        self.fake.jobs[next(iter(self.fake.jobs))]["Status"] = "Failed"
        self.backend.refresh()
        retried = self.backend.retry(original, str(uuid.uuid4()))["prompt_id"]
        stored = read_json(self.store.run(retried) / "submission.json")
        self.assertEqual(stored["retry_of"], original)
        self.assertEqual(stored["payload"]["prompt"]["1"]["inputs"]["seed"], 123)

    def test_disabling_native_queue_blocks_retry_with_old_enabled_snapshot(self):
        original = self.backend.submit(self.body)["prompt_id"]
        self.backend.settings["enabled"] = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.backend.retry(original, str(uuid.uuid4()))
        self.assertEqual(self.fake.submissions, 1)

    def test_settings_injection_and_local_storage_rejected(self):
        with self.assertRaises(ValueError): validate_settings({"enabled": True, "root": "C:/local", "workers": "Test"})
        with self.assertRaises(ValueError): validate_settings({"pool": "test\nPlugin=CommandLine"})
        with self.assertRaises(ValueError): self.backend.submit({**self.body, "settings": {"scope": "someone-else"}})

    def test_in_flight_submission_blocks_storage_change_without_holding_global_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = self.backend._ensure_submitted
        def blocked(store, submission):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release submission")
            return original(store, submission)
        with patch.object(self.backend, "_ensure_submitted", side_effect=blocked), concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.backend.submit, self.body)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.backend.active_submissions, 1)
                self.assertFalse(self.backend.jobs)
                for changed in ({"root": "\\\\other\\native"}, {"scope": "other-user"}):
                    with self.assertRaisesRegex(ValueError, "active submissions"):
                        self.backend.configure({**self.backend.settings, **changed, "enabled": False})
                # Existing requests keep their snapshot while disabling new ones.
                self.backend.configure({**self.backend.settings, "enabled": False, "priority": 60})
            finally:
                release.set()
            accepted = pending.result(timeout=5)["prompt_id"]
        self.assertEqual(self.backend.active_submissions, 0)
        self.assertIn(accepted, self.backend.jobs)
        self.assertEqual(self.backend.settings["root"], "\\\\farm\\native")
        self.assertEqual(self.backend.settings["priority"], 60)
        with self.assertRaisesRegex(ValueError, "active submissions and runs"):
            self.backend.configure({**self.backend.settings, "root": "\\\\other\\native"})

    def test_failed_submission_releases_storage_change_guard(self):
        with self.assertRaises(ValueError):
            self.backend.submit({**self.body, "submission_key": "invalid"})
        self.assertEqual(self.backend.active_submissions, 0)
        self.backend.configure({**self.backend.settings, "enabled": False, "scope": "other-user"})
        self.assertEqual(self.backend.settings["scope"], "other-user")

    def test_lost_activation_response_blocks_storage_change_and_setup_without_cache(self):
        self.fake.lose_resume = True
        with self.assertRaises(TimeoutError):
            self.backend.submit(self.body)
        self.assertFalse(self.backend.jobs)
        self.assertEqual(self.backend.active_submissions, 0)
        with self.assertRaisesRegex(ValueError, "unconfirmed farm runs"):
            self.backend.configure({**self.backend.settings, "enabled": False, "root": "\\\\other\\native"})
        self.assertFalse(self.backend.configuration_in_progress)

    def test_restart_checks_durable_active_run_before_changing_scope(self):
        self.backend.submit(self.body)
        restarted = FarmBackend(self.root / "restarted.json", self.fake)
        restarted.settings = dict(self.backend.settings)
        self.assertFalse(restarted.jobs)
        with self.assertRaisesRegex(ValueError, "pending or unconfirmed farm runs"):
            restarted.configure({**restarted.settings, "enabled": False, "scope": "other-user"})
        self.assertEqual(restarted.settings["scope"], self.backend.settings["scope"])

    def test_confirmed_terminal_deadline_run_does_not_block_scope_change_after_restart(self):
        self.backend.submit(self.body)
        self.fake.jobs[next(iter(self.fake.jobs))]["Status"] = "Completed"
        restarted = FarmBackend(self.root / "restarted.json", self.fake)
        restarted.settings = dict(self.backend.settings)
        restarted.configure({**restarted.settings, "enabled": False, "scope": "other-user"})
        self.assertEqual(restarted.settings["scope"], "other-user")

    def test_storage_scan_blocks_new_capture_and_does_not_hold_global_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = self.backend._check_durable_runs
        def blocked(settings):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release storage scan")
            return original(settings)
        with patch.object(self.backend, "_check_durable_runs", side_effect=blocked), concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            change = pool.submit(self.backend.configure, {**self.backend.settings, "enabled": False, "scope": "other-user"})
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(ValueError, "configuration is changing"):
                    self.backend.submit(self.body)
            finally:
                release.set()
            change.result(timeout=5)
        self.assertFalse(self.backend.configuration_in_progress)
        self.assertEqual(self.fake.submissions, 0)

    def test_receipt_recovery_is_counted_as_an_active_submission(self):
        self.fake.lose_resume = True
        with self.assertRaises(TimeoutError):
            self.backend.submit(self.body)
        self.fake.lose_resume = False
        original = self.backend._ensure_submitted
        def recovering(store, submission):
            self.assertEqual(self.backend.active_submissions, 1)
            with self.assertRaisesRegex(ValueError, "active submissions"):
                self.backend.configure({**self.backend.settings, "enabled": False, "scope": "other-user"})
            return original(store, submission)
        with patch.object(self.backend, "_ensure_submitted", side_effect=recovering):
            self.backend.refresh()
        self.assertEqual(self.backend.active_submissions, 0)

    def test_retry_snapshot_cannot_enter_another_storage_root(self):
        captured = {**self.backend.settings, "root": "\\\\old\\native"}
        with self.assertRaisesRegex(ValueError, "storage settings"):
            self.backend.submit(self.body, captured=captured)
        self.assertEqual(self.backend.active_submissions, 0)
        self.assertEqual(self.fake.submissions, 0)

    def test_submission_readiness_rejects_unusable_workers_before_writing_records(self):
        for problem in ({"enabled": False}, {"session_error": "Invalid mapping"}, {"groups": ["other"]}):
            with self.subTest(problem=problem):
                self.fake.snapshot["workers"][0].update(enabled=True, session_configured=True, session_error=None, groups=["none"])
                self.fake.snapshot["workers"][0].update(problem)
                self.backend.settings["group"] = "none" if "groups" not in problem else "required"
                self.backend.discovery_checked = 0
                with self.assertRaises(ValueError):
                    self.backend.submit(self.body)
                self.assertEqual(list(self.store.records(self.backend.settings["scope"])), [])
                self.assertEqual(self.fake.submissions, 0)
                self.assertEqual(self.backend.active_submissions, 0)

    def test_native_route_cannot_bypass_missing_plugin_readiness(self):
        self.fake.snapshot["plugin_available"] = False
        with self.assertRaisesRegex(ValueError, "native ComfyUI plugin"):
            self.backend.submit(self.body)
        self.assertEqual(self.fake.submissions, 0)
        self.assertEqual(list(self.store.records(self.backend.settings["scope"])), [])
        self.assertFalse(self.backend.readiness_cache["ready"])

    def test_offline_mapped_worker_can_queue_and_effective_plugin_path_is_recorded(self):
        self.fake.snapshot["workers"][0]["status"] = "Offline"
        self.backend.settings["workers"] = "testworker"
        self.backend.settings["plugin_directory"] = "\\\\farm\\native\\plugin"
        accepted = self.backend.submit(self.body)["prompt_id"]
        stored = read_json(self.store.run(accepted) / "submission.json")
        self.assertEqual(stored["plugin_directory"], "\\\\farm\\native\\plugin")
        job_info = (self.store.run(accepted) / "job.info").read_text()
        self.assertIn("CustomPluginDirectory=\\\\farm\\native\\plugin", job_info)

    def test_readiness_is_read_only_and_legacy_environment_plugin_is_checked(self):
        self.backend.plugin_directory = "\\\\farm\\legacy\\plugins"
        result = self.backend.readiness()
        self.assertTrue(result["ready"])
        self.assertFalse(self.backend.settings_path.exists())
        self.assertEqual(list(self.store.records(self.backend.settings["scope"])), [])
        self.assertEqual(self.fake.submissions, 0)

    def test_unrestricted_cold_run_uses_real_deadline_user_and_no_machine_limit(self):
        self.backend.settings["workers"] = ""
        self.backend.settings["scope"] = "legacy-history"
        self.fake.snapshot["workers"][0]["session_configured"] = False
        accepted = self.backend.submit(self.body)["prompt_id"]
        info = (self.store.run(accepted) / "job.info").read_text()
        self.assertIn("UserName=artist\n", info)
        self.assertNotIn("Whitelist=", info)
        self.assertNotIn("MachineLimit=", info)
        self.assertEqual(read_json(self.store.run(accepted) / "submission.json")["scope"], "legacy-history")

    def test_priority_can_save_during_storage_outage_without_touching_the_share(self):
        self.backend.health = {"connected":False,"message":"Share unavailable"}
        with patch("farm_backend.RunStore") as store:
            self.backend.configure({**self.backend.settings, "priority":75})
        store.assert_not_called()
        self.assertEqual(self.backend.settings["priority"], 75)
        self.assertFalse(self.backend.readiness_cache["ready"])

    def test_cancelled_cold_start_reconciles_after_deadline_stops_its_task(self):
        run_id = self.backend.submit(self.body)["prompt_id"]
        attempt_id = str(uuid.uuid4())
        job_id = next(iter(self.fake.jobs))
        state = self.store.begin_attempt(run_id, attempt_id, job_id, "TestWorker", {"worker":"TestWorker","start":1})
        self.store.update_attempt(run_id, attempt_id, state["generation"], execution_mode="task-owned")
        self.store.cancel(run_id)
        # Killing a startup broker can raise a transport error in PreRenderTasks
        # before Deadline finishes suspending its task. Cancel still wins.
        self.store.update_attempt(run_id, attempt_id, state["generation"], status="failed")
        self.fake.jobs[job_id]["Status"] = "Suspended"
        def command(*args):
            if args[0] == "-SuspendJob":
                raise AssertionError("Already suspended jobs need no repeated suspend")
            if args[0] == "-GetJobTask":
                return "Status=Pending"
            return self.fake(*args)
        self.backend.command = command
        self.backend.refresh()
        self.assertEqual(self.store.control(run_id)["status"], "cancelled")
        self.assertEqual(self.backend.jobs[run_id]["status"], "cancelled")

    def test_deleted_cancelled_job_does_not_issue_control_commands(self):
        run_id = self.backend.submit(self.body)["prompt_id"]
        self.store.cancel(run_id)
        self.fake.jobs.clear()
        self.backend.refresh()
        self.assertIn(run_id, self.backend.jobs)
        self.assertTrue(self.store.control(run_id)["cancel_requested"])
        self.assertEqual(self.backend.jobs[run_id]["status"], "cancelled")
        self.assertEqual(self.backend.jobs[run_id]["deadline"]["status"], "Deleted")
        self.assertEqual(self.store.control(run_id)["generation"], 1)
        self.backend.refresh()
        self.assertEqual(self.store.control(run_id)["generation"], 1)

    def test_removed_job_fences_attempts_and_old_progress_without_resubmission(self):
        run_id = self.backend.submit(self.body)["prompt_id"]
        job_id = next(iter(self.fake.jobs))
        attempt = str(uuid.uuid4())
        state = self.store.begin_attempt(run_id, attempt, job_id, "TestWorker", {"worker":"TestWorker","start":1})
        atomic_json(self.store.run(run_id) / "attempts" / attempt / "progress.json", {
            "run_id":run_id,"generation":1,"sequence":100,"nodes":{"1":{"value":50,"max":100}},"preview_url":"/old-preview"})
        self.fake.jobs.clear()
        self.backend.refresh()
        self.assertEqual(self.backend.jobs[run_id]["status"], "failed")
        self.assertEqual(self.backend.jobs[run_id]["execution_error"]["exception_type"], "DeadlineJobRemoved")
        self.assertEqual(self.backend.progress[0]["generation"], 2)
        self.assertEqual(self.backend.progress[0]["nodes"], {})
        self.assertNotIn("preview_url", self.backend.progress[0])
        self.assertFalse(self.store.update_attempt(run_id, attempt, state["generation"], status="in_progress"))
        self.assertFalse(self.store.commit(run_id, attempt, state["generation"], {"run_id":run_id,"attempt_id":attempt,"generation":1}))
        self.assertIsNone(self.store.begin_attempt(run_id, str(uuid.uuid4()), job_id, "TestWorker", {"worker":"TestWorker","start":2}))
        self.assertEqual(self.fake.submissions, 1)

    def test_missing_job_confirmation_outage_retains_cached_state(self):
        run_id = self.backend.submit(self.body)["prompt_id"]
        self.backend.refresh()
        cached = copy.deepcopy(self.backend.jobs)
        self.fake.jobs.clear()
        def command(*args):
            if args[0] == "-ExecuteScript": raise TimeoutError("Repository unavailable")
            return self.fake(*args)
        self.backend.command = command
        with self.assertRaises(TimeoutError): self.backend.refresh()
        self.assertEqual(self.backend.jobs, cached)
        self.assertEqual(self.store.control(run_id)["generation"], 0)
        self.assertNotIn("deadline_removed", self.store.control(run_id))

    def test_empty_bulk_response_cannot_override_fresh_existing_job(self):
        run_id = self.backend.submit(self.body)["prompt_id"]
        def command(*args):
            if args[0] == "-GetJob": return ""
            return self.fake(*args)
        self.backend.command = command
        self.backend.refresh()
        self.assertEqual(self.backend.jobs[run_id]["status"], "pending")
        self.assertNotIn("deadline_removed", self.store.control(run_id))

    def test_deleted_job_after_lost_activation_does_not_block_other_runs(self):
        self.fake.lose_resume = True
        with self.assertRaises(TimeoutError): self.backend.submit(self.body)
        run_id = next(iter(self.store.records(self.backend.settings["scope"])))["run_id"]
        self.fake.lose_resume = False
        self.fake.jobs.clear()
        other = self.backend.submit({**self.body, "submission_key":str(uuid.uuid4())})["prompt_id"]
        self.backend.refresh()
        self.assertEqual(self.backend.jobs[run_id]["status"], "failed")
        self.assertFalse(self.backend.jobs[run_id]["deadline"]["accepted"])
        self.assertEqual(self.backend.jobs[other]["status"], "pending")
        self.backend.refresh()
        self.assertEqual(self.fake.submissions, 2)

    def test_failed_job_after_lost_activation_does_not_block_other_runs(self):
        self.fake.lose_resume = True
        with self.assertRaises(TimeoutError): self.backend.submit(self.body)
        job_id = next(iter(self.fake.jobs))
        run_id = self.fake.jobs[job_id]["run_id"]
        self.fake.lose_resume = False
        self.fake.jobs[job_id]["Status"] = "Failed"
        other = self.backend.submit({**self.body, "submission_key":str(uuid.uuid4())})["prompt_id"]
        self.backend.refresh()
        self.assertEqual(self.backend.jobs[run_id]["status"], "failed")
        self.assertEqual(self.backend.jobs[other]["status"], "pending")
        self.assertEqual(self.fake.submissions, 2)

    def test_confirmed_removed_run_does_not_block_history_scope_change(self):
        self.backend.submit(self.body)
        self.fake.jobs.clear()
        self.backend.refresh()
        self.backend.configure({**self.backend.settings, "scope":"another-history", "enabled":False})
        self.assertEqual(self.backend.settings["scope"], "another-history")

    def test_cancelling_a_failed_attempt_pending_infrastructure_retry(self):
        import asyncio
        run_id = self.backend.submit(self.body)["prompt_id"]
        job_id = next(iter(self.fake.jobs))
        attempt = str(uuid.uuid4())
        state = self.store.begin_attempt(run_id, attempt, job_id, "TestWorker", {"worker":"TestWorker","start":1})
        self.store.update_attempt(run_id, attempt, state["generation"], status="failed")
        self.backend.refresh()
        self.assertEqual(self.backend.jobs[run_id]["status"], "pending")
        self.assertTrue(asyncio.run(self.backend.cancel(run_id)))
        self.assertTrue(self.backend.jobs[run_id]["deadline"]["cancel_requested"])
        self.assertTrue(self.backend.progress[0]["cancel_requested"])
        self.assertEqual(self.backend.progress[0]["sequence"], 1)

    def test_plugin_directory_requires_a_shared_parent_directory(self):
        with self.assertRaisesRegex(ValueError, "Custom plugin directory"):
            validate_settings({"plugin_directory": "C:/local/plugin"})

    def test_enable_cannot_persist_when_native_plugin_is_missing(self):
        self.fake.snapshot["plugin_available"] = False
        before = dict(self.backend.settings)
        self.backend.settings["enabled"] = False
        # Only health-probe filesystem writes are substituted; job/store records
        # and any settings write still use the real temporary directory.
        def write(path, value):
            if Path(path) == self.backend.settings_path:
                atomic_json(path, value)
        with patch.object(Path, "mkdir"), patch("farm_backend.record_lock", return_value=contextlib.nullcontext()), patch("farm_backend.atomic_json", side_effect=write):
            with self.assertRaisesRegex(ValueError, "native ComfyUI plugin"):
                self.backend.configure(before)
        self.assertFalse(self.backend.settings["enabled"])
        self.assertFalse(self.backend.settings_path.exists())
        self.assertEqual(self.fake.submissions, 0)

    def test_refresh_preserves_late_acceptance_even_with_an_old_creation_time(self):
        accepted = self.backend.submit(self.body)["prompt_id"]
        submission = read_json(self.store.run(accepted) / "submission.json")
        submission["created_at"] = 1
        receipt = read_json(self.store.run(accepted) / "receipt.json")
        self.backend.jobs, self.backend.histories, self.backend.assets = {}, {}, {}
        def late_acceptance(scope):
            self.backend._cache_submission(submission, receipt)
            return []
        with patch.object(self.store, "records", side_effect=late_acceptance):
            self.backend.refresh()
        self.assertIn(accepted, self.backend.jobs)
        self.assertIn(accepted, self.backend.histories)
        self.assertIn(accepted, self.backend.assets)

    def test_acceptance_is_immediately_available_to_native_controls(self):
        accepted = self.backend.submit(self.body)["prompt_id"]
        self.assertEqual(self.backend.jobs[accepted]["status"], "pending")
        self.assertEqual(self.backend.histories[accepted]["prompt"][1], accepted)

    def test_missing_share_identity_retains_cached_jobs_and_prevents_new_submission(self):
        accepted = self.backend.submit(self.body)["prompt_id"]
        (self.store.root / "store.json").rename(self.store.root / "store.offline")
        with self.assertRaisesRegex(RuntimeError, "storage identity"):
            self.backend.refresh()
        self.assertIn(accepted, self.backend.jobs)
        with self.assertRaises(RuntimeError): self.backend.submit({**self.body, "submission_key": str(uuid.uuid4())})
        self.assertEqual(self.fake.submissions, 1)

    def test_corrupt_optional_settings_disable_native_without_breaking_node_import(self):
        path = self.root / "bad-settings.json"
        path.write_text("{bad json")
        backend = FarmBackend(path, self.fake)
        self.assertFalse(backend.settings["enabled"])
        self.assertIn("could not be loaded", backend.health["message"])

    def test_worker_process_never_starts_a_submitter_or_site_bootstrap(self):
        with patch.dict("os.environ", {"DEADLINE_SLAVE_NAME":"Worker"}), patch("farm_backend.FarmBackend") as backend:
            self.assertIsNone(register_backend())
        backend.assert_not_called()

    def test_result_assets_cache_and_terminal_progress_are_native_compatible(self):
        run_id = self.backend.submit(self.body)["prompt_id"]
        job_id = next(iter(self.fake.jobs))
        attempt = str(uuid.uuid4())
        state = self.store.begin_attempt(run_id, attempt, job_id, "Worker", {"worker": "Worker", "start": 1})
        relative = "attempts/" + attempt + "/outputs/1/images/image.png"
        image = self.store.run(run_id) / relative
        image.parent.mkdir(parents=True); image.write_bytes(b"image")
        atomic_json(image.parents[3] / "progress.json", {"run_id": run_id, "generation": 1, "sequence": 1,
            "status": "in_progress", "nodes": {"1": {"value": 1, "max": 1, "prompt_id": "worker-prompt"}}})
        result = {"schema": 1, "run_id": run_id, "attempt_id": attempt, "generation": 1,
            "outputs": {"1": {"images": [{"filename": "image.png", "subfolder": "", "type": "output"}]}},
            "files": [{"path": relative, "size": 5, "sha256": hashlib.sha256(b"image").hexdigest(), "node_id": "1", "output_key": "images", "output_index": 0}]}
        self.store.commit(run_id, attempt, 1, result)
        self.fake.jobs[job_id]["Status"] = "Completed"
        self.backend.output_root = self.root / "local-output"
        self.backend.refresh()
        asset = self.backend.assets[run_id][0]
        self.assertEqual(asset["name"], "image.png")
        self.assertEqual(str(uuid.UUID(asset["id"])), asset["id"])
        self.assertIn("deadline-native", asset["preview_url"])
        self.assertEqual((self.backend.output_root / asset["subfolder"] / asset["filename"]).read_bytes(), b"image")
        event = self.backend.progress[0]
        self.assertEqual(event["sequence"], 6)
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["nodes"]["1"]["prompt_id"], run_id)

    def test_native_output_loader_is_staged_and_retry_survives_original_cache_loss(self):
        output_root = self.root / "artist-output"
        image = output_root / "deadline-native" / "source" / "image.png"
        image.parent.mkdir(parents=True); image.write_bytes(b"image")
        with patch.dict("sys.modules", {"folder_paths": types.SimpleNamespace(
                get_input_directory=lambda: str(self.root / "input"), get_output_directory=lambda: str(output_root))}):
            body = copy.deepcopy(self.body)
            body["prompt"]["2"] = {"class_type": "LoadImage", "inputs": {"image": "deadline-native/source/image.png [output]"}}
            original = self.backend.submit(body)["prompt_id"]
            self.fake.jobs[next(iter(self.fake.jobs))]["Status"] = "Failed"
            self.backend.refresh()
            image.unlink()
            retried = self.backend.retry(original, str(uuid.uuid4()))["prompt_id"]
            original_prompt = read_json(self.store.run(original) / "prompt.json")
            retry_prompt = read_json(self.store.run(retried) / "prompt.json")
            self.assertEqual(original_prompt, retry_prompt)
            self.assertNotIn("/", retry_prompt["2"]["inputs"]["image"])
            self.assertLess(len(retry_prompt["2"]["inputs"]["image"]), 40)
            self.assertTrue((self.store.run(retried) / "inputs" / retry_prompt["2"]["inputs"]["image"]).is_file())


if __name__ == "__main__": unittest.main()
