import hashlib
import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from farm_protocol import RunStore, atomic_json, read_json, safe_path, _read_unc


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(self.temp.name)
        self.run_id, self.attempt = str(uuid.uuid4()), str(uuid.uuid4())
        directory = self.store.run(self.run_id)
        atomic_json(directory / "receipt.json", {"job_id": "job"})
        atomic_json(directory / "control.json", {"generation": 0, "status": "pending", "cancel_requested": False, "result": None})

    def begin(self, attempt=None, start=1):
        return self.store.begin_attempt(self.run_id, attempt or self.attempt, "job", "Worker", {"worker": "Worker", "start": start})

    @unittest.skipUnless(os.name == "nt", "Windows record reads")
    def test_unc_reader_handles_bom_exact_buffer_boundary_and_large_records(self):
        path = Path(self.temp.name) / "record.json"
        for size in (83, 65536, 131073):
            value = {"data":"a" * (size - 15)}
            data = b"\xef\xbb\xbf" + json.dumps(value).encode("utf-8")
            self.assertEqual(len(data), size)
            path.write_bytes(data)
            self.assertEqual(json.loads(_read_unc(path)), value)
        path.unlink()
        with self.assertRaises(FileNotFoundError): _read_unc(path)

    def test_execution_mode_is_part_of_the_atomic_attempt_claim(self):
        state = self.store.begin_attempt(self.run_id, self.attempt, "job", "Worker",
            {"worker": "Worker", "start": 1}, execution_mode="task-owned")
        self.assertEqual(self.store.control(self.run_id)["execution_mode"], "task-owned")
        self.assertEqual(state["generation"], 1)
        replacement = self.store.begin_attempt(self.run_id, str(uuid.uuid4()), "job", "Worker",
            {"worker": "Worker", "start": 2}, execution_mode="persistent")
        self.assertEqual(replacement["execution_mode"], "persistent")
        self.assertEqual(self.store.control(self.run_id)["generation"], 2)

    def test_late_startup_failure_cannot_overwrite_confirmed_cancellation(self):
        self.begin()
        self.store.cancel(self.run_id)
        self.store.update_attempt(self.run_id, self.attempt, 1, status="cancelled")
        self.assertFalse(self.store.update_attempt(self.run_id, self.attempt, 1, status="failed"))
        self.assertEqual(self.store.control(self.run_id)["status"], "cancelled")

    def manifest(self, attempt=None, generation=1):
        attempt = attempt or self.attempt
        relative = "attempts/" + attempt + "/outputs/image.png"
        path = safe_path(self.store.run(self.run_id), relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"validated output")
        return {"schema": 1, "run_id": self.run_id, "attempt_id": attempt, "generation": generation,
            "files": [{"path": relative, "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}

    def test_new_assignment_fences_old_writer_and_stale_assignment(self):
        self.begin()
        old = self.manifest()
        new = str(uuid.uuid4())
        state = self.begin(new, 2)
        self.assertEqual(state["generation"], 2)
        self.assertFalse(self.store.commit(self.run_id, self.attempt, 1, old))
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.begin(str(uuid.uuid4()), 1)
        self.assertTrue(self.store.commit(self.run_id, new, 2, self.manifest(new, 2)))

    def test_duplicate_assignment_and_callback_cannot_claim_again(self):
        self.begin()
        with self.assertRaisesRegex(ValueError, "already has"):
            self.begin(str(uuid.uuid4()))
        result = self.manifest()
        self.assertTrue(self.store.commit(self.run_id, self.attempt, 1, result))
        self.assertTrue(self.store.commit(self.run_id, self.attempt, 1, result))
        self.assertIsNone(self.begin(str(uuid.uuid4()), 2))
        self.assertEqual(self.store.control(self.run_id)["generation"], 1)

    def test_cancel_and_commit_have_one_deterministic_winner(self):
        self.begin()
        manifest = self.manifest()
        barrier = threading.Barrier(3)
        results = {}
        def cancel():
            barrier.wait()
            results["cancel"] = self.store.cancel(self.run_id)
        def commit():
            barrier.wait()
            results["commit"] = self.store.commit(self.run_id, self.attempt, 1, manifest)
        threads = [threading.Thread(target=cancel), threading.Thread(target=commit)]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join()
        self.assertNotEqual(results["cancel"], results["commit"])
        state = self.store.control(self.run_id)
        self.assertEqual(bool(state["result"]), results["commit"])
        self.assertEqual(state["cancel_requested"], results["cancel"])

    def test_commit_acknowledgement_loss_survives_store_restart(self):
        self.begin()
        manifest = self.manifest()
        self.store.commit(self.run_id, self.attempt, 1, manifest)
        replacement = RunStore(self.temp.name)
        self.assertEqual(replacement.result(self.run_id), manifest)
        self.assertIsNone(replacement.begin_attempt(self.run_id, str(uuid.uuid4()), "job", "Worker", {"start": 2, "worker": "Worker"}))

    def test_invalid_missing_tampered_or_escaping_outputs_cannot_commit(self):
        self.begin()
        manifest = self.manifest()
        manifest["files"][0]["path"] = "../outside.png"
        with self.assertRaises(ValueError): self.store.commit(self.run_id, self.attempt, 1, manifest)
        manifest = self.manifest()
        safe_path(self.store.run(self.run_id), manifest["files"][0]["path"]).write_bytes(b"corrupt")
        with self.assertRaises(ValueError): self.store.commit(self.run_id, self.attempt, 1, manifest)
        self.assertIsNone(self.store.control(self.run_id)["result"])

    def test_wrong_deadline_mapping_and_path_traversal_rejected(self):
        with self.assertRaises(ValueError): self.store.begin_attempt(self.run_id, self.attempt, "other", "Worker", {"start": 1})
        for value in ("../outside", "C:/outside", "/outside", "\\\\host\\share\\file", "image.png:stream"):
            with self.assertRaises(ValueError): safe_path(self.temp.name, value)

    def test_reassigned_deadline_task_cannot_publish_before_new_generation_claim(self):
        self.begin()
        result = self.manifest()
        self.assertFalse(self.store.commit(self.run_id, self.attempt, 1, result, lambda: False))
        self.assertIsNone(self.store.control(self.run_id)["result"])


if __name__ == "__main__": unittest.main()
