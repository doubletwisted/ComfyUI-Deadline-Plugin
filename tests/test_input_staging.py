from __future__ import annotations

import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from deadline_submit import DeadlineCommandHelper, DeadlineSubmitNode, InputAssetStager


class InputStagingTests(unittest.TestCase):
    def test_submissions_isolate_inputs_and_preserve_nested_loader_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "comfy-input"
            (input_dir / "nested").mkdir(parents=True)
            source = input_dir / "nested" / "source.webp"
            (input_dir / "unused.png").write_bytes(b"unused")
            archive = root / "input"
            archive.mkdir()
            (archive / "old.png").write_bytes(b"old job")
            prompt = {"1": {"class_type": "LoadImage", "inputs": {"image": "nested/source.webp"}}}
            staged_roots = []

            for submission_id, data in (("first", b"first image"), ("second", b"second image")):
                source.write_bytes(data)
                stager = InputAssetStager(str(root / "output"), "test", submission_id)
                stager._get_local_input_directory = lambda: str(input_dir)
                staged_dir, manifest_file, assets = stager.stage_referenced_assets(prompt)
                staged_root = Path(staged_dir)
                staged_roots.append(staged_root)
                self.assertEqual(staged_root, root / "output" / ".deadline-inputs" / submission_id)
                self.assertEqual((staged_root / "nested" / "source.webp").read_bytes(), data)
                self.assertFalse((staged_root / "old.png").exists())
                self.assertFalse((staged_root / "unused.png").exists())
                self.assertEqual(len(assets), 1)
                manifest = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
                self.assertEqual(Path(manifest["input_directory"]), staged_root)
                self.assertEqual(manifest["assets"], assets)
                rewritten = copy.deepcopy(prompt)
                DeadlineSubmitNode()._rewrite_prompt_asset_references(rewritten, assets)
                self.assertEqual(rewritten["1"]["inputs"]["image"], "nested/source.webp")

            self.assertEqual((staged_roots[0] / "nested" / "source.webp").read_bytes(), b"first image")
            self.assertEqual((archive / "old.png").read_bytes(), b"old job")

    def test_submission_without_assets_gets_its_own_input_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stager = InputAssetStager(str(root / "output"), "test", "empty")
            stager._get_local_input_directory = lambda: str(root)
            staged_dir, manifest_file, assets = stager.stage_referenced_assets({})
            self.assertEqual(Path(staged_dir), root / "output" / ".deadline-inputs" / "empty")
            self.assertTrue(Path(staged_dir).is_dir())
            self.assertTrue(Path(manifest_file).is_file())
            self.assertEqual(assets, [])

    def test_submission_id_cannot_escape_its_input_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            for submission_id in ("", "..", "../outside", "nested/job", "nested\\job", temp):
                with self.subTest(submission_id=submission_id):
                    stager = InputAssetStager(str(Path(temp) / "output"), "test", submission_id)
                    stager._get_local_input_directory = lambda: temp
                    with self.assertRaisesRegex(ValueError, "Submission ID"):
                        stager.stage_referenced_assets({})

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

            with patch.object(DeadlineCommandHelper, "get_farm_settings", return_value={"input_roots": [str(source_dir)]}):
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
