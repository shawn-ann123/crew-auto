from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from tools.add_solutions import AddSolutionsTool, SolutionItem
from tools.append_file import AppendFileTool
from tools.safe_file_read import SafeFileReadTool
from tools.save_evidence import SaveEvidenceTool
from tools.state_io import StateFileError, load_json_strict
from tools.nightly_control import (
    in_window,
    is_deepseek_peak,
    mark_failed,
    parse_clock,
    prepare,
)
from tools.literature_rerank import LiteratureRerankTool, rerank


class WorkingDirectoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_cwd = Path.cwd()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chdir(self.root)
        (self.root / "baseline").mkdir()
        (self.root / "baseline" / "模块基线.md").write_text(
            "# 基线\n\n## M1 测试模块\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        self.temp.cleanup()

    def save_paper(self, verification: str = "abstract_only") -> str:
        return SaveEvidenceTool()._run(
            kind="paper",
            item_id="2401.12345v2",
            title="Test paper",
            modules="M1",
            summary="summary",
            verification=verification,
            content="# Test paper\n\n## 方法卡\n测试内容",
        )

    def test_evidence_is_immutable_and_cross_category_deduplicated(self) -> None:
        self.assertIn("已保存", self.save_paper())
        original = (self.root / "library" / "papers" / "2401.12345.md").read_text()
        repeated = self.save_paper()
        self.assertIn("未覆盖", repeated)
        cross = SaveEvidenceTool()._run(
            kind="cross",
            item_id="2401.12345",
            title="Same paper",
            modules="M1",
            summary="same",
            content="# Same",
        )
        self.assertIn("未按新分类重复保存", cross)
        self.assertEqual(
            original,
            (self.root / "library" / "papers" / "2401.12345.md").read_text(),
        )

    def test_broken_state_fails_closed(self) -> None:
        path = self.root / "library" / "processed.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(StateFileError):
            self.save_paper()
        self.assertEqual("{broken", path.read_text(encoding="utf-8"))
        with self.assertRaises(StateFileError):
            load_json_strict(path, {})

    def test_evidence_grade_is_capped_by_saved_verification(self) -> None:
        self.save_paper("abstract_only")
        result = AddSolutionsTool()._run(
            [
                SolutionItem(
                    module="M1",
                    name="Method",
                    core="Core idea",
                    source_type="paper",
                    source_ref="2401.12345",
                    evidence="A",
                )
            ]
        )
        self.assertIn("M1-S1", result)
        data = json.loads((self.root / "library" / "solutions.json").read_text())
        self.assertEqual("C", data["solutions"][0]["evidence"])

    def test_unknown_source_and_module_are_rejected(self) -> None:
        self.save_paper("full_text")
        result = AddSolutionsTool()._run(
            [
                SolutionItem(
                    module="M99",
                    name="Bad module",
                    core="x",
                    source_type="paper",
                    source_ref="2401.12345",
                ),
                SolutionItem(
                    module="M1",
                    name="Bad source",
                    core="x",
                    source_type="paper",
                    source_ref="missing",
                ),
            ]
        )
        self.assertIn("不在已冻结的模块基线中", result)
        self.assertIn("未在证据账本登记", result)

    def test_file_tools_cannot_read_or_write_model_config(self) -> None:
        (self.root / ".env").write_text("SECRET=value", encoding="utf-8")
        self.assertIn("不在研究资料白名单", SafeFileReadTool()._run(".env"))
        self.assertIn(
            "只允许追加",
            AppendFileTool()._run(path=".env", content="overwrite"),
        )
        self.assertEqual("SECRET=value", (self.root / ".env").read_text())


class NightlyPolicyTest(unittest.TestCase):
    def test_deepseek_peak_hours_are_blocked(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        monday = datetime(2026, 10, 19, 10, 0, tzinfo=tz)
        lunch = datetime(2026, 10, 19, 12, 30, tzinfo=tz)
        saturday = datetime(2026, 10, 24, 10, 0, tzinfo=tz)
        self.assertTrue(is_deepseek_peak(monday))
        self.assertFalse(is_deepseek_peak(lunch))
        self.assertFalse(is_deepseek_peak(saturday))

    def test_automatic_window_supports_crossing_midnight(self) -> None:
        self.assertTrue(in_window(parse_clock("23:30"), parse_clock("23:00"), parse_clock("01:00")))
        self.assertTrue(in_window(parse_clock("00:30"), parse_clock("23:00"), parse_clock("01:00")))
        self.assertFalse(in_window(parse_clock("02:00"), parse_clock("23:00"), parse_clock("01:00")))

    def test_failed_nightly_state_is_explicit_and_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "logs").mkdir()
            (root / "library").mkdir()
            mark_failed(root, "2026-10-21", "digest-b", 2, "local failure")
            failed = json.loads(
                (root / "logs" / "nightly-2026-10-21.state.json").read_text()
            )
            self.assertEqual("FAILED", failed["status"])
            self.assertEqual("digest-b", failed["failure"]["stage"])

            prepare(root, "2026-10-21")
            resumed = json.loads(
                (root / "logs" / "nightly-2026-10-21.state.json").read_text()
            )
            self.assertEqual("RUNNING", resumed["status"])
            self.assertIn("resumed_at", resumed)
            self.assertNotIn("failure", resumed)


class LiteratureRerankTest(unittest.TestCase):
    def test_core_paper_ranks_before_excluded_homonym(self) -> None:
        result = rerank(
            {
                "query": "spinning target motion prediction robot",
                "must_have": [
                    ["spinning target", "rotating target"],
                    ["motion prediction", "trajectory prediction"],
                ],
                "should_have": [["robot", "robotic"]],
                "exclude": [["medical", "tumor"]],
                "candidates": [
                    {
                        "title": "Medical Motion Prediction for a Rotating Tumor Target",
                        "abstract": "A medical treatment study.",
                    },
                    {
                        "title": "Spinning Target Motion Prediction for Robotic Vision",
                        "abstract": "Online prediction for an interception robot.",
                    },
                ],
            }
        )
        self.assertEqual("core", result["items"][0]["classification"])
        self.assertIn("Robotic Vision", result["items"][0]["title"])
        self.assertEqual("exclude", result["items"][1]["classification"])

    def test_invalid_payload_returns_readable_error(self) -> None:
        result = LiteratureRerankTool()._run("{broken")
        self.assertIn("候选重排失败", result)


if __name__ == "__main__":
    unittest.main()
