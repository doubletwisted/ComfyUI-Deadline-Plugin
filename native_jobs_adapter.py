"""Plugin-owned Jobs API middleware; never inserts farm work in PromptQueue."""

import json
import uuid

from aiohttp import web


STATUSES = {"pending", "in_progress", "completed", "failed", "cancelled"}
TERMINAL = {"completed", "failed", "cancelled"}


def validate_job_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("job id must be a UUID in canonical lowercase hyphenated form")
    return value


def select_jobs(jobs, query):
    statuses = None
    if query.get("status"):
        statuses = {s.strip().lower() for s in query["status"].split(",") if s.strip()}
        if statuses - STATUSES:
            raise ValueError("Invalid status value(s): " + ", ".join(sorted(statuses - STATUSES)))
    sort_by = query.get("sort_by", "created_at").lower()
    order = query.get("sort_order", "desc").lower()
    if sort_by not in {"created_at", "execution_duration"}:
        raise ValueError("sort_by must be 'created_at' or 'execution_duration'")
    if order not in {"asc", "desc"}:
        raise ValueError("sort_order must be 'asc' or 'desc'")
    try:
        limit = int(query["limit"]) if "limit" in query else None
        offset = max(0, int(query.get("offset", 0)))
    except (ValueError, TypeError):
        raise ValueError("limit and offset must be integers") from None
    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive integer")

    filtered = [job for job in jobs if (statuses is None or job["status"] in statuses)
                and (not query.get("workflow_id") or job.get("workflow_id") == query["workflow_id"])]
    if sort_by == "execution_duration":
        def sort_key(job):
            start, end = job.get("execution_start_time"), job.get("execution_end_time")
            return end - start if start and end else 0
    else:
        def sort_key(job):
            return job.get("create_time") or 0
    filtered.sort(key=sort_key, reverse=order == "desc")
    total = len(filtered)
    page = filtered[offset:] if limit is None else filtered[offset:offset + limit]
    return {"jobs": page, "pagination": {"offset": offset, "limit": limit,
            "total": total, "has_more": offset + len(page) < total}}


def response_data(response):
    return json.loads(response.body)


class NativeJobsAdapter:
    """Provider methods use cached records and must not block the aiohttp loop."""

    def __init__(self, provider):
        self.provider = provider

    @web.middleware
    async def middleware(self, request, handler):
        path = request.path
        if path.startswith("/api/"):
            path = path[4:]

        if request.method == "GET" and path == "/view" and request.query.get("subfolder", "").startswith("deadline-native/"):
            if hasattr(self.provider, "view"):
                return await self.provider.view(request)
            return web.Response(status=404)

        if request.method == "POST" and path == "/prompt":
            body = await request.json()
            workflow = body.get("extra_data", {}).get("extra_pnginfo", {}).get("workflow", {}) if isinstance(body, dict) else {}
            if isinstance(workflow, dict) and "__deadline_native_queue" in workflow:
                return web.json_response({"error": {"type": "deadline_routing_error",
                    "message": "A Deadline snapshot reached the local prompt endpoint. Check extension compatibility.",
                    "details": "The prompt was not queued locally."}, "node_errors": {}}, status=409)

        if request.method == "GET" and path == "/jobs":
            try:
                select_jobs([], request.query)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            # Fetch the complete local set. Pagination applies once, after merging.
            local = await handler(request.clone(rel_url=request.rel_url.with_query({})))
            if local.status != 200:
                return local
            farm = await self.provider.list_jobs()
            jobs = {job["id"]: job for job in response_data(local)["jobs"]}
            for job in farm:
                if job["id"] in jobs:
                    return web.json_response({"error": "Farm/local run ID collision"}, status=409)
                jobs[job["id"]] = job
            return web.json_response(select_jobs(list(jobs.values()), request.query))

        parts = path.strip("/").split("/")
        if len(parts) in {2, 3} and parts[0] == "jobs" and parts[1] != "cancel":
            job = await self.provider.get_job(parts[1])
            if job is not None:
                if request.method == "GET" and len(parts) == 2:
                    return web.json_response(job)
                if request.method == "GET" and parts[2:] == ["assets"]:
                    try:
                        limit = int(request.query.get("limit", 500))
                        offset = max(0, int(request.query.get("offset", 0)))
                        if not 0 < limit <= 500:
                            raise ValueError("assets limit must be between 1 and 500")
                    except ValueError as exc:
                        return web.json_response({"error": str(exc)}, status=400)
                    assets = await self.provider.get_assets(parts[1])
                    page = assets[offset:offset + limit]
                    return web.json_response({"job_id": parts[1], "assets": page,
                        "pagination": {"offset": offset, "limit": limit,
                            "total": len(assets), "has_more": offset + len(page) < len(assets)}})
                if request.method == "POST" and parts[2:] == ["cancel"]:
                    return web.json_response({"cancelled": await self.provider.cancel(parts[1])})

        if request.method == "POST" and path == "/jobs/cancel":
            try:
                body = await request.json()
                ids = body.get("job_ids") if isinstance(body, dict) else None
                if not isinstance(ids, list):
                    raise ValueError("job_ids must be a list")
                for job_id in ids:
                    validate_job_id(job_id)
            except (ValueError, AttributeError) as exc:
                return web.json_response({"error": str(exc)}, status=400)
            # The stock handler treats unknown farm UUIDs as no-ops; it retains
            # responsibility for local IDs and its atomic targeted interruption.
            local = await handler(request)
            if local.status != 200:
                return local
            cancelled = response_data(local)["cancelled"]
            for job_id in dict.fromkeys(ids):
                if await self.provider.get_job(job_id) is not None:
                    cancelled = await self.provider.cancel(job_id) or cancelled
            return web.json_response({"cancelled": cancelled})

        if request.method == "POST" and path == "/interrupt":
            try:
                body = await request.json() if request.can_read_body else {}
            except ValueError:
                return await handler(request)
            job_id = body.get("prompt_id") if isinstance(body, dict) else None
            if isinstance(job_id, str) and await self.provider.get_job(job_id) is not None:
                await self.provider.cancel(job_id)
                return web.Response(status=200)

        if path == "/queue":
            if request.method == "GET":
                local = await handler(request)
                if request.query.get("deadline_local") == "1":
                    return local  # Session drain observes actual execution only.
                if local.status != 200:
                    return local
                data = response_data(local)
                for job in await self.provider.list_jobs():
                    if job["status"] in {"pending", "in_progress"}:
                        history = await self.provider.get_history(job["id"])
                        key = "queue_running" if job["status"] == "in_progress" else "queue_pending"
                        data[key].append(history["prompt"])
                return web.json_response(data)
            if request.method == "POST":
                body = await request.json()
                local = await handler(request)
                if local.status != 200:
                    return local
                ids = body.get("delete", [])
                if body.get("clear"):
                    ids = [j["id"] for j in await self.provider.list_jobs() if j["status"] == "pending"]
                for job_id in ids:
                    if await self.provider.get_job(job_id) is not None:
                        await self.provider.cancel(job_id)
                return local

        if request.method == "GET" and parts[0] == "history":
            if len(parts) == 2:
                job = await self.provider.get_job(parts[1])
                if job is not None and job["status"] in TERMINAL:
                    return web.json_response({parts[1]: await self.provider.get_history(parts[1])})
            elif len(parts) == 1:
                local = await handler(request.clone(rel_url=request.rel_url.with_query({})))
                if local.status != 200:
                    return local
                records = response_data(local)
                for job in await self.provider.list_jobs():
                    if job["status"] in TERMINAL:
                        records[job["id"]] = await self.provider.get_history(job["id"])
                try:
                    limit = int(request.query["max_items"]) if "max_items" in request.query else None
                    offset = int(request.query.get("offset", -1))
                    if limit is not None and limit < 0:
                        raise ValueError("max_items must be non-negative")
                except ValueError as exc:
                    return web.json_response({"error": str(exc)}, status=400)
                items = sorted(records.items(), key=lambda item: item[1]["prompt"][3].get("create_time", 0))
                if offset < 0:
                    offset = max(0, len(items) - limit) if limit is not None else 0
                items = items[offset:] if limit is None else items[offset:offset + limit]
                return web.json_response(dict(items))

        if request.method == "POST" and path == "/history":
            body = await request.json()
            local = await handler(request)
            if local.status != 200:
                return local
            ids = body.get("delete", [])
            if body.get("clear"):
                ids = [j["id"] for j in await self.provider.list_jobs() if j["status"] in TERMINAL]
            for job_id in ids:
                job = await self.provider.get_job(job_id)
                if job is not None and job["status"] in TERMINAL:
                    await self.provider.hide(job_id)
            return local

        return await handler(request)
