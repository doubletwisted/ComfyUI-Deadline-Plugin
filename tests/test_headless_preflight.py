from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from test_reuse_endpoint import ComfyUIError, new_plugin


WORKFLOW = {
    "nodes": [
        {"id": 1, "type": "LoadImage", "title": "Farm input"},
        {"id": 2, "type": "SaveImage", "title": "Final render"},
        {"id": 5, "type": "ImpactSwitch", "title": "Lighting choice"},
    ]
}


def object_info():
    return {
        "LoadImage": {
            "input": {"required": {"image": [["a.png"], {"image_upload": True}]}, "hidden": {}},
            "output_node": False,
        },
        "SaveImage": {
            "input": {"required": {}, "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"}},
            "output_node": True,
        },
        "ImpactSwitch": {
            "input": {"required": {}, "hidden": {"extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"}},
            "output_node": False,
        },
        "FL_ImagePicker": {"input": {"required": {}, "hidden": {"unique_id": "UNIQUE_ID"}}, "output_node": False},
        "Explode": {"input": {"required": {}, "hidden": {}}, "output_node": False},
    }


class HeadlessPreflightTests(unittest.TestCase):
    def make_plugin(self, root):
        return new_plugin(
            object_info=object_info(),
            comfyui_input_dir=str(root / "input"),
            comfyui_output_dir=str(root / "output"),
            comfyui_install_path=str(root / "install"),
        )

    def test_farm_safe_payload_passes_and_preserves_workflow_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "input").mkdir()
            (root / "input" / "a.png").write_bytes(b"image")
            plugin = self.make_plugin(root)
            prompt = {
                "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
                "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
            }
            result, expected = plugin._preflight_worker_payload(prompt, WORKFLOW)
            self.assertEqual(result, prompt)
            self.assertEqual(expected, ["2"])
            self.assertEqual(WORKFLOW["nodes"][1]["title"], "Final render")

    def test_fixed_switch_is_removed_and_consumers_are_reconnected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin = self.make_plugin(root)
            prompt = {
                "3": {"class_type": "Explode", "inputs": {}},
                "4": {"class_type": "Explode", "inputs": {}},
                "5": {"class_type": "ImpactSwitch", "inputs": {"select": 2, "input1": ["3", 0], "input2": ["4", 0]}},
                "2": {"class_type": "SaveImage", "inputs": {"images": ["5", 0], "label": ["5", 1]}},
            }
            result, _ = plugin._preflight_worker_payload(prompt, WORKFLOW)
            self.assertNotIn("5", result)
            self.assertEqual(result["2"]["inputs"]["images"], ["4", 0])
            self.assertEqual(result["2"]["inputs"]["label"], "input2")

    def test_gui_dependent_node_is_rejected_with_context(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = self.make_plugin(Path(temp))
            with self.assertRaisesRegex(ComfyUIError, r"node 9 .*FL_ImagePicker.*worker M21"):
                plugin._preflight_worker_payload({
                    "9": {"class_type": "FL_ImagePicker", "inputs": {}},
                    "2": {"class_type": "SaveImage", "inputs": {"images": ["9", 0]}},
                }, WORKFLOW)

    def test_missing_node_type_is_rejected_against_object_info(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = self.make_plugin(Path(temp))
            with self.assertRaisesRegex(ComfyUIError, r"MissingOnWorker.*not installed in /object_info"):
                plugin._preflight_worker_payload({
                    "7": {"class_type": "MissingOnWorker", "inputs": {}},
                    "2": {"class_type": "SaveImage", "inputs": {"images": ["7", 0]}},
                }, WORKFLOW)

    def test_invalid_staged_path_is_rejected_before_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = self.make_plugin(Path(temp))
            with self.assertRaisesRegex(ComfyUIError, "invalid staged path"):
                plugin._preflight_worker_payload({
                    "1": {"class_type": "LoadImage", "inputs": {"image": "missing.png"}},
                    "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
                }, WORKFLOW)

    def test_execution_exception_reports_node_title_type_worker_and_original(self):
        plugin = new_plugin(
            standard_workflow={"nodes": [{"id": 7, "type": "Explode", "title": "Broken sampler"}]},
            current_prompt_payload={"7": {"class_type": "Explode", "inputs": {}}},
        )
        status = {"status_str": "error", "messages": [["execution_error", {
            "node_id": "7", "node_type": "Explode", "exception_message": "synthetic boom"
        }]]}
        with self.assertRaisesRegex(ComfyUIError, r"node 7 .*Broken sampler.*Explode.*M21.*synthetic boom"):
            plugin._handle_prompt_status(status)

    def test_missing_expected_output_file_fails_successful_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin = self.make_plugin(root)
            plugin.prompt_id = "prompt-a"
            plugin.expected_outputs_by_prompt = {"prompt-a": ["2"]}
            plugin.current_prompt_payload = {"2": {"class_type": "SaveImage", "inputs": {}}}
            with self.assertRaisesRegex(ComfyUIError, r"expected output file is missing.*node 2"):
                plugin._validate_expected_outputs({"2": {"images": [{
                    "filename": "never-written.png", "subfolder": "", "type": "output"
                }]}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
