import copy
import json
import unittest
import uuid

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from native_jobs_adapter import NativeJobsAdapter, select_jobs
from scripts.native_queue_spike import FixtureProvider, snapshot


def job(status="pending", created=1):
    return {"id": str(uuid.uuid4()), "status": status, "create_time": created,
            "outputs_count": 0, "previewable_outputs_count": 0, "workflow_id": "local"}


class NativeJobsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = FixtureProvider()
        self.farm_id = self.provider.submit({**snapshot(), "submission_key": str(uuid.uuid4())})["prompt_id"]
        self.local = [job(created=10), job("completed", 20)]
        self.local_requests = []
        self.local_cancel_ids = []
        app = web.Application(middlewares=[NativeJobsAdapter(self.provider).middleware])

        async def local(request):
            self.local_requests.append((request.method, request.path, dict(request.query)))
            if request.method == "GET" and request.path.endswith("/jobs"):
                return web.json_response(select_jobs(self.local, request.query))
            if request.path.endswith("/jobs/cancel"):
                ids = (await request.json())["job_ids"]
                self.local_cancel_ids += [j["id"] for j in self.local if j["id"] in ids and j["status"] == "pending"]
                return web.json_response({"cancelled": bool(self.local_cancel_ids)})
            if request.path.endswith("/queue") and request.method == "GET":
                return web.json_response({"queue_running": [], "queue_pending": []})
            if request.method == "GET" and request.path.endswith("/history"):
                return web.json_response({})
            if request.method == "GET":
                return web.json_response({"local": True})
            return web.Response(status=200)

        app.router.add_route("*", "/{path:.*}", local)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_combined_pagination_and_filters_before_slicing(self):
        response = await self.client.get("/api/jobs?status=pending&limit=1&offset=1&sort_order=asc")
        data = await response.json()
        self.assertEqual(data["pagination"], {"offset": 1, "limit": 1, "total": 2, "has_more": False})
        self.assertEqual(data["jobs"][0]["id"], self.farm_id)
        self.assertEqual(self.local_requests[0][2], {})
        response = await self.client.get("/api/jobs?workflow_id=local")
        self.assertEqual((await response.json())["pagination"]["total"], 2)

    async def test_over_200_active_runs_have_correct_total_and_next_page(self):
        for _ in range(205):
            self.provider.submit({**snapshot(), "submission_key": str(uuid.uuid4())})
        first = await (await self.client.get("/api/jobs?status=pending,in_progress&limit=200&offset=0")).json()
        second = await (await self.client.get("/api/jobs?status=pending,in_progress&limit=200&offset=200")).json()
        self.assertEqual(first["pagination"]["total"], 207)
        self.assertTrue(first["pagination"]["has_more"])
        self.assertEqual(len(second["jobs"]), 7)
        self.assertFalse(set(j["id"] for j in first["jobs"]) & set(j["id"] for j in second["jobs"]))

    async def test_local_detail_and_unrelated_routes_pass_through(self):
        for path in ("/api/jobs/" + self.local[0]["id"], "/system_stats", "/object_info"):
            self.assertEqual(await (await self.client.get(path)).json(), {"local": True})

    async def test_owner_idle_query_excludes_remote_execution_records(self):
        visible = await (await self.client.get("/api/queue")).json()
        self.assertEqual(len(visible["queue_pending"]), 1)
        actual = await (await self.client.get("/api/queue?deadline_local=1")).json()
        self.assertEqual(actual, {"queue_pending": [], "queue_running": []})

    async def test_farm_workflow_detail_assets_and_legacy_history(self):
        self.provider.transition(self.farm_id, "completed")
        data = await (await self.client.get("/api/jobs/" + self.farm_id)).json()
        self.assertEqual(data["workflow"]["prompt"]["1"]["class_type"], "EmptyImage")
        self.assertEqual(data["outputs_count"], 1)
        self.assertIn("workflow", data["workflow"]["extra_data"]["extra_pnginfo"])
        assets = await (await self.client.get(f"/api/jobs/{self.farm_id}/assets?limit=1")).json()
        self.assertEqual(assets["assets"][0]["node_id"], "2")
        self.assertEqual(assets["pagination"]["total"], 1)
        history = await (await self.client.get("/api/history/" + self.farm_id)).json()
        self.assertEqual(history[self.farm_id]["prompt"][1], self.farm_id)

    async def test_mixed_cancel_keeps_local_handler_and_is_idempotent(self):
        ids = [self.local[0]["id"], self.farm_id]
        data = await (await self.client.post("/api/jobs/cancel", json={"job_ids": ids})).json()
        self.assertTrue(data["cancelled"])
        self.assertEqual(self.local_cancel_ids, [self.local[0]["id"]])
        self.assertEqual(self.provider.records[self.farm_id]["status"], "cancelled")
        data = await (await self.client.post(f"/api/jobs/{self.farm_id}/cancel")).json()
        self.assertFalse(data["cancelled"])

    async def test_invalid_mixed_cancel_has_no_side_effects(self):
        for invalid in ({}, "uppercase", self.farm_id.upper(), None):
            response = await self.client.post("/api/jobs/cancel", json={"job_ids": [self.farm_id, invalid]})
            self.assertEqual(response.status, 400)
        self.assertEqual(self.provider.records[self.farm_id]["status"], "pending")
        self.assertEqual(self.local_cancel_ids, [])

    async def test_targeted_interrupt_and_queue_clear_only_address_farm_jobs(self):
        self.provider.transition(self.farm_id, "in_progress", 50)
        self.assertEqual((await self.client.post("/api/interrupt", json={"prompt_id": self.farm_id})).status, 200)
        self.assertEqual(self.provider.records[self.farm_id]["status"], "cancelled")
        pending = self.provider.submit({**snapshot(), "submission_key": str(uuid.uuid4())})["prompt_id"]
        self.assertEqual((await self.client.post("/api/queue", json={"clear": True})).status, 200)
        self.assertEqual(self.provider.records[pending]["status"], "cancelled")

    async def test_history_delete_hides_record_without_destroying_snapshot(self):
        self.provider.transition(self.farm_id, "failed")
        await self.client.post("/api/history", json={"delete": [self.farm_id]})
        self.assertIn(self.farm_id, self.provider.records)
        self.assertIsNone(await self.provider.get_job(self.farm_id))
        self.assertEqual((await (await self.client.get("/api/jobs")).json())["pagination"]["total"], 2)

    async def test_invalid_query_never_runs_local_handler(self):
        for query in ("status=bad", "limit=0", "offset=NaN", "sort_by=bogus", "sort_order=nope"):
            self.assertEqual((await self.client.get("/api/jobs?" + query)).status, 400)
        self.assertEqual(self.local_requests, [])

    async def test_lost_frontend_wrapper_cannot_execute_farm_snapshot_locally(self):
        payload = snapshot()
        payload["extra_data"]["extra_pnginfo"]["workflow"]["__deadline_native_queue"] = str(uuid.uuid4())
        response = await self.client.post("/api/prompt", json=payload)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.local_requests, [])


class FixtureSnapshotTests(unittest.TestCase):
    def test_transport_retries_and_different_payload_conflict(self):
        provider = FixtureProvider()
        body = {**snapshot(), "submission_key": str(uuid.uuid4())}
        accepted = provider.submit(body)
        self.assertEqual(provider.submit(copy.deepcopy(body)), accepted)
        changed = copy.deepcopy(body)
        changed["prompt"]["1"]["inputs"]["color"] = 999
        with self.assertRaisesRegex(ValueError, "different snapshot"):
            provider.submit(changed)
        self.assertEqual(len(provider.records), 1)

    def test_browser_credentials_are_not_retained(self):
        provider = FixtureProvider()
        body = {**snapshot(), "submission_key": str(uuid.uuid4())}
        body["extra_data"].update(auth_token_comfy_org="private_token", api_key_comfy_org="private_key")
        run_id = provider.submit(body)["prompt_id"]
        self.assertNotIn("private_", json.dumps(provider.records[run_id]))
