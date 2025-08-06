"""
Deadline API endpoints for ComfyUI
Provides REST and WebSocket endpoints for the Deadline panel
"""

import json
import asyncio
from typing import Dict, List, Optional, Set
from aiohttp import web
import logging

logger = logging.getLogger(__name__)

class DeadlineAPIHandler:
    """Handles API requests for Deadline integration"""
    
    def __init__(self):
        self.workers: Dict[str, Dict] = {}
        self.active_jobs: Dict[str, Dict] = {}
        self.websocket_clients: Set[web.WebSocketResponse] = set()
        self.update_lock = asyncio.Lock()
    
    async def get_workers(self, request: web.Request) -> web.Response:
        """GET /deadline/workers - Return list of active workers"""
        try:
            response_data = {
                "workers": list(self.workers.values()),
                "activeWorkers": len([w for w in self.workers.values() if w.get("status") == "active"]),
                "totalJobs": len(self.active_jobs)
            }
            return web.json_response(response_data)
        except Exception as e:
            logger.error(f"Error getting workers: {e}")
            return web.json_response({"error": str(e)}, status=500)
    
    async def submit_job(self, request: web.Request) -> web.Response:
        """POST /deadline/submit - Submit workflow to Deadline"""
        try:
            data = await request.json()
            workflow = data.get("workflow")
            is_distributed = data.get("isDistributed", False)
            master_ws = data.get("masterWs", "localhost:8188")
            
            # Here you would integrate with the actual Deadline submission
            # For now, return a mock response
            job_id = f"job_{len(self.active_jobs) + 1:04d}"
            
            self.active_jobs[job_id] = {
                "id": job_id,
                "status": "submitted",
                "isDistributed": is_distributed,
                "masterWs": master_ws
            }
            
            # Notify WebSocket clients
            await self._broadcast({
                "type": "job_submitted",
                "jobId": job_id
            })
            
            return web.json_response({"jobId": job_id, "status": "submitted"})
        except Exception as e:
            logger.error(f"Error submitting job: {e}")
            return web.json_response({"error": str(e)}, status=500)
    
    async def stop_worker(self, request: web.Request) -> web.Response:
        """POST /deadline/workers/{workerId}/stop - Stop specific worker"""
        try:
            worker_id = request.match_info.get("workerId")
            
            if worker_id in self.workers:
                self.workers[worker_id]["status"] = "stopping"
                
                # Here you would send actual stop command to Deadline
                
                # Remove worker after a delay
                asyncio.create_task(self._remove_worker_delayed(worker_id))
                
                await self._broadcast({
                    "type": "worker_stopping",
                    "workerId": worker_id
                })
                
                return web.json_response({"status": "stopping"})
            else:
                return web.json_response({"error": "Worker not found"}, status=404)
        except Exception as e:
            logger.error(f"Error stopping worker: {e}")
            return web.json_response({"error": str(e)}, status=500)
    
    async def stop_all_workers(self, request: web.Request) -> web.Response:
        """POST /deadline/workers/stop-all - Stop all workers"""
        try:
            for worker_id in list(self.workers.keys()):
                self.workers[worker_id]["status"] = "stopping"
            
            # Here you would send actual stop commands to Deadline
            
            await self._broadcast({
                "type": "all_workers_stopping"
            })
            
            # Clear workers after a delay
            asyncio.create_task(self._clear_workers_delayed())
            
            return web.json_response({"status": "stopping all"})
        except Exception as e:
            logger.error(f"Error stopping all workers: {e}")
            return web.json_response({"error": str(e)}, status=500)
    
    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint for real-time updates"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        # Add to clients set
        self.websocket_clients.add(ws)
        
        try:
            # Send initial state
            await ws.send_json({
                "type": "initial_state",
                "workers": list(self.workers.values()),
                "activeWorkers": len([w for w in self.workers.values() if w.get("status") == "active"]),
                "totalJobs": len(self.active_jobs)
            })
            
            # Keep connection alive
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        # Handle incoming messages if needed
                        if data.get("type") == "ping":
                            await ws.send_json({"type": "pong"})
                    except json.JSONDecodeError:
                        pass
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
        finally:
            # Remove from clients set
            self.websocket_clients.discard(ws)
            
        return ws
    
    async def register_worker(self, worker_info: Dict) -> None:
        """Register a new worker"""
        async with self.update_lock:
            worker_id = worker_info.get("id")
            self.workers[worker_id] = worker_info
            
        await self._broadcast({
            "type": "worker_registered",
            "worker": worker_info
        })
    
    async def update_worker_status(self, worker_id: str, status: Dict) -> None:
        """Update worker status"""
        async with self.update_lock:
            if worker_id in self.workers:
                self.workers[worker_id].update(status)
                
        await self._broadcast({
            "type": "worker_update",
            "workers": list(self.workers.values())
        })
    
    async def unregister_worker(self, worker_id: str) -> None:
        """Unregister a worker"""
        async with self.update_lock:
            if worker_id in self.workers:
                del self.workers[worker_id]
                
        await self._broadcast({
            "type": "worker_unregistered",
            "workerId": worker_id
        })
    
    async def _broadcast(self, message: Dict) -> None:
        """Broadcast message to all WebSocket clients"""
        if not self.websocket_clients:
            return
            
        # Create tasks for sending to all clients
        tasks = []
        for ws in list(self.websocket_clients):
            if not ws.closed:
                tasks.append(ws.send_json(message))
                
        # Send to all clients concurrently
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _remove_worker_delayed(self, worker_id: str, delay: float = 2.0) -> None:
        """Remove worker after a delay"""
        await asyncio.sleep(delay)
        await self.unregister_worker(worker_id)
    
    async def _clear_workers_delayed(self, delay: float = 2.0) -> None:
        """Clear all workers after a delay"""
        await asyncio.sleep(delay)
        async with self.update_lock:
            self.workers.clear()
        await self._broadcast({
            "type": "workers_cleared"
        })


# Global handler instance
deadline_api = DeadlineAPIHandler()


def setup_routes(app: web.Application) -> None:
    """Setup routes for Deadline API"""
    app.router.add_get("/deadline/workers", deadline_api.get_workers)
    app.router.add_post("/deadline/submit", deadline_api.submit_job)
    app.router.add_post("/deadline/workers/{workerId}/stop", deadline_api.stop_worker)
    app.router.add_post("/deadline/workers/stop-all", deadline_api.stop_all_workers)
    app.router.add_get("/deadline", deadline_api.websocket_handler)


# Integration with ComfyUI
def integrate_with_comfyui(server):
    """Integrate Deadline API with ComfyUI server"""
    if hasattr(server, "app"):
        setup_routes(server.app)
        logger.info("Deadline API routes registered")
    else:
        logger.warning("Could not register Deadline API routes - server.app not found")