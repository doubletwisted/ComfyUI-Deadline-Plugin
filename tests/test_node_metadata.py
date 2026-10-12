from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
import urllib.parse
from unittest.mock import MagicMock, patch

from test_reuse_endpoint import ComfyUIError, new_plugin, plugin_module


class NodeMetadataTests(unittest.TestCase):
    def test_api_requests_only_used_types_and_encodes_custom_node_names(self):
        requests = []
        definitions = {
            "ImageResize+": {"input": {"required": {}}, "output_node": False},
            "easy cleanGpuUsed": {"input": {"hidden": {"prompt": "PROMPT"}}, "output_node": False},
            "SaveVideo": {"input": {"required": {}}, "output_node": True},
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path == "/prompt":
                    data = {"exec_info": {"queue_remaining": 0}}
                elif self.path.startswith("/object_info/"):
                    node_type = urllib.parse.unquote(self.path[len("/object_info/"):])
                    data = {node_type: definitions[node_type]} if node_type in definitions else {}
                else:
                    self.send_error(500, "Global catalog must not be requested")
                    return
                body = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        plugin = new_plugin(comfyui_api_url=f"http://127.0.0.1:{server.server_port}")
        prompt = {
            "1": {"class_type": "ImageResize+", "inputs": {}},
            "2": {"class_type": "ImageResize+", "inputs": {}},
            "3": {"class_type": "easy cleanGpuUsed", "inputs": {}},
            "4": {"class_type": "SaveVideo", "inputs": {}},
        }
        try:
            with patch.object(plugin_module.time, "sleep"):
                self.assertTrue(plugin.initialize_api_connection(prompt))
            self.assertEqual(requests, [
                "/prompt", "/object_info/ImageResize%2B", "/object_info/SaveVideo",
                "/object_info/easy%20cleanGpuUsed",
            ])
            self.assertEqual(plugin.object_info, definitions)
            self.assertEqual(plugin._preflight_worker_payload(prompt)[1], ["4"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_metadata_timeout_identifies_worker_and_requested_node(self):
        request = MagicMock(side_effect=[{"status_code": 200}, TimeoutError("timed out")])
        plugin = new_plugin(comfyui_api_url="http://127.0.0.1:8188", http_request=request)
        prompt = {"1": {"class_type": "SlowNode", "inputs": {}}}
        with patch.object(plugin_module.time, "sleep"):
            with self.assertRaisesRegex(ComfyUIError, "M21.*'SlowNode'.*timed out"):
                plugin.initialize_api_connection(prompt)
        self.assertEqual(request.call_count, 2)

    def test_missing_node_still_fails_preflight_with_node_context(self):
        request = MagicMock(side_effect=[
            {"status_code": 200},
            {"status_code": 200, "json": lambda: {}},
            {"status_code": 200, "json": lambda: {"SaveVideo": {"output_node": True}}},
        ])
        plugin = new_plugin(comfyui_api_url="http://127.0.0.1:8188", http_request=request)
        prompt = {
            "1": {"class_type": "MissingOnWorker", "inputs": {}},
            "2": {"class_type": "SaveVideo", "inputs": {}},
        }
        with patch.object(plugin_module.time, "sleep"):
            self.assertTrue(plugin.initialize_api_connection(prompt))
        with self.assertRaisesRegex(ComfyUIError, "node 1.*MissingOnWorker.*M21.*not installed"):
            plugin._preflight_worker_payload(prompt)

    def test_failed_or_invalid_metadata_is_rejected_with_node_name(self):
        for response in (
            {"status_code": 500},
            {"status_code": 200, "json": lambda: []},
            {"status_code": 200, "json": lambda: {"SaveVideo": "invalid"}},
        ):
            with self.subTest(response=response):
                request = MagicMock(side_effect=[{"status_code": 200}, response])
                plugin = new_plugin(comfyui_api_url="http://127.0.0.1:8188", http_request=request)
                with patch.object(plugin_module.time, "sleep"):
                    with self.assertRaisesRegex(ComfyUIError, "SaveVideo"):
                        plugin.initialize_api_connection({"1": {"class_type": "SaveVideo", "inputs": {}}})


if __name__ == "__main__":
    unittest.main()
