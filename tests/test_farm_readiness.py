import copy
import unittest

from farm_readiness import readiness


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"enabled": True, "root": "\\\\farm\\native", "scope": "artist", "workers": "GPU-Worker", "pool": "gpu", "group": "comfy"}
        self.snapshot = {"connected": True, "message": "Repository connected", "plugin_available": True,
            "storage": {"connected": True, "message": "Storage available"}, "workers": [{"name": "GPU-Worker", "enabled": True,
                "status": "Idle", "session_configured": True, "pools": ["gpu"], "groups": ["comfy"]}]}

    def test_eligible_worker_status_does_not_change_deadline_scheduling(self):
        for status in ("Idle", "Rendering", "Offline"):
            self.snapshot["workers"][0]["status"] = status
            self.assertTrue(readiness(self.settings, self.snapshot)["ready"])

    def test_names_and_pool_group_membership_match_case_insensitively(self):
        self.settings.update(workers="gpu-worker,GPU-WORKER", pool="GPU", group="COMFY")
        result = readiness(self.settings, self.snapshot)
        self.assertTrue(result["ready"])
        self.assertEqual(len(result["workers"]), 1)
        self.assertEqual(result["workers"][0]["name"], "GPU-Worker")

    def test_invalid_worker_selection_has_actionable_reasons(self):
        self.settings["workers"] = "GPU-Worker,Missing"
        self.snapshot["workers"][0].update(enabled=False, session_configured=False, pools=[], groups=[])
        result = readiness(self.settings, self.snapshot)
        self.assertFalse(result["ready"])
        self.assertEqual(result["code"], "workers")
        self.assertIn("outside selected group", result["message"])
        self.assertIn("Missing: not found", result["message"])

    def test_storage_repository_and_plugin_failures_are_distinguished(self):
        for field, code in (("storage", "storage"), ("connected", "repository"), ("plugin_available", "plugin")):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot[field] = {"connected": False, "message": "Storage unavailable"} if field == "storage" else False
            self.assertEqual(readiness(self.settings, snapshot)["code"], code)

    def test_local_storage_is_rejected_even_when_health_claims_connected(self):
        self.settings["root"] = "C:/local"
        self.assertEqual(readiness(self.settings, self.snapshot)["code"], "storage")

    def test_global_configuration_error_blocks_even_a_registered_worker(self):
        self.snapshot["configuration_error"] = "Malformed NativeWorkerSessions JSON"
        result = readiness(self.settings, self.snapshot)
        self.assertFalse(result["ready"])
        self.assertEqual(result["code"], "session_configuration")

    def test_empty_allowlist_uses_deadline_scheduling_and_admin_disable_blocks(self):
        self.settings["workers"] = ", ,"
        self.assertTrue(readiness(self.settings, self.snapshot)["ready"])
        self.settings["enabled"] = False
        self.assertEqual(readiness(self.settings, self.snapshot)["code"], "enabled")

    def test_existing_workers_do_not_require_session_owners(self):
        self.snapshot["workers"][0]["session_configured"] = False
        self.assertTrue(readiness(self.settings, self.snapshot)["ready"])

    def test_deadline_user_is_independent_of_history_scope(self):
        self.settings["scope"] = "legacy-history"
        self.snapshot["deadline_user"] = "artist"
        self.snapshot["workers"][0].update(scheduling_mode="UserJobs", user_job_names=["artist"])
        self.assertTrue(readiness(self.settings, self.snapshot)["ready"])

    def test_user_scope_changes_cannot_create_ready_jobs_the_worker_will_never_take(self):
        worker = self.snapshot["workers"][0]
        worker.update(scheduling_mode="UserJobs", user_job_names=["Artist-comfy"])
        self.settings["scope"] = "ARTIST-COMFY"
        self.assertTrue(readiness(self.settings, self.snapshot)["ready"])
        self.settings["scope"] = "artist"
        result = readiness(self.settings, self.snapshot)
        self.assertFalse(result["ready"])
        self.assertEqual(result["code"], "workers")
        self.assertIn("does not accept Deadline user artist", result["message"])
        self.assertIn("Artist-comfy", result["message"])

    def test_empty_user_allowlist_and_unverified_scheduling_modes_block_submission(self):
        worker = self.snapshot["workers"][0]
        worker.update(scheduling_mode="UserJobs", user_job_names=[])
        self.assertFalse(readiness(self.settings, self.snapshot)["ready"])
        self.assertIn("no users", readiness(self.settings, self.snapshot)["message"])
        for mode in ("ListedJobs", "Unknown"):
            worker["scheduling_mode"] = mode
            result = readiness(self.settings, self.snapshot)
            self.assertFalse(result["ready"])
            self.assertIn("could not be verified", result["message"])

    def test_machine_jobs_require_the_verified_machine_user(self):
        worker = self.snapshot["workers"][0]
        worker.update(scheduling_mode="MachineJobs", machine_user="ARTIST")
        self.assertTrue(readiness(self.settings, self.snapshot)["ready"])
        self.settings["scope"] = "artist-comfy"
        self.assertFalse(readiness(self.settings, self.snapshot)["ready"])
        worker["machine_user"] = ""
        self.assertIn("could not be verified", readiness(self.settings, self.snapshot)["message"])
