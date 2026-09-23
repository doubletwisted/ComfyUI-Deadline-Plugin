from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from deadline_submit import DeadlineSubmitNode, InputAssetStager


class InputStagingTests(unittest.TestCase):
    def test_videohelpersuite_upload_nodes_are_staged_and_rewritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "comfy-input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "clip.mp4").write_bytes(b"video")

            stager = InputAssetStager(str(output_dir), "test", "submission")
            stager._get_local_input_directory = lambda: str(input_dir)
            prompt = {
                "1": {"class_type": "VHS_LoadVideo", "inputs": {"video": "clip.mp4"}},
                "2": {"class_type": "VHS_LoadAudioUpload", "inputs": {"audio": "clip.mp4"}},
            }

            _, _, assets = stager.stage_referenced_assets(prompt)

            self.assertEqual(len(assets), 1)
            self.assertTrue(Path(assets[0]["destination"]).is_file())

            rewritten = copy.deepcopy(prompt)
            DeadlineSubmitNode()._rewrite_prompt_asset_references(rewritten, assets)
            self.assertEqual(rewritten["1"]["inputs"]["video"], "clip.mp4")
            self.assertEqual(rewritten["2"]["inputs"]["audio"], "clip.mp4")

    def test_videohelpersuite_path_file_is_copied_and_rewritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "comfy-input"
            output_dir = root / "output"
            source_dir = root / "desktop"
            input_dir.mkdir()
            source_dir.mkdir()
            source = source_dir / "beauty.mp4"
            source.write_bytes(b"video")

            stager = InputAssetStager(str(output_dir), "test", "submission")
            stager._get_local_input_directory = lambda: str(input_dir)
            prompt = {
                "1": {
                    "class_type": "VHS_LoadVideoPath",
                    "inputs": {"video": str(source)},
                },
            }

            _, _, assets = stager.stage_referenced_assets(prompt)

            self.assertEqual(len(assets), 1)
            staged_path = Path(assets[0]["destination"])
            self.assertTrue(staged_path.is_file())
            self.assertEqual(staged_path.read_bytes(), b"video")

            rewritten = copy.deepcopy(prompt)
            DeadlineSubmitNode()._rewrite_prompt_asset_references(rewritten, assets)
            self.assertEqual(rewritten["1"]["inputs"]["video"], str(staged_path))

    def test_directory_loader_is_not_treated_as_a_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "comfy-input"
            output_dir = root / "output"
            source_dir = root / "frames"
            input_dir.mkdir()
            source_dir.mkdir()

            stager = InputAssetStager(str(output_dir), "test", "submission")
            stager._get_local_input_directory = lambda: str(input_dir)
            prompt = {
                "1": {
                    "class_type": "VHS_LoadImagesPath",
                    "inputs": {"directory": str(source_dir)},
                },
            }

            _, _, assets = stager.stage_referenced_assets(prompt)

            self.assertEqual(assets, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
