"""TranscriptDocument 数据契约测试。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transcript  # noqa: E402
from transcript import (  # noqa: E402
    Segment,
    TranscriptDocument,
    assess_coverage,
    atomic_write_text,
    merge_adjacent,
)


def make_document(**overrides):
    segments = overrides.pop(
        "segments",
        [
            Segment("seg_0001", "第一段文字", 0, 4000, "exact"),
            Segment("seg_0002", "第二段文字", 4000, 9000, "exact"),
        ],
    )
    payload = dict(
        platform="bilibili",
        video_id="BV1vwME6AEpd",
        title="示例",
        url="https://www.bilibili.com/video/BV1vwME6AEpd?p=33",
        page=33,
        duration_ms=10000,
        source_type="human_subtitle",
        language="zh-Hans",
        segments=segments,
    )
    payload.update(overrides)
    return TranscriptDocument(**payload)


class SerializationTests(unittest.TestCase):
    def test_roundtrip_keeps_semantics(self):
        document = make_document()
        payload = document.to_dict()
        self.assertEqual(payload["schema_version"], "2.0")
        self.assertEqual(payload["video"]["page"], 33)
        restored = TranscriptDocument.from_dict(json.loads(json.dumps(payload)))
        self.assertEqual(restored.segments[0].start_ms, 0)
        self.assertEqual(restored.source_type, "human_subtitle")
        self.assertEqual(restored.validate(), [])

    def test_rejects_wrong_schema_version(self):
        with self.assertRaises(transcript.TranscriptError):
            TranscriptDocument.from_dict({"schema_version": "1.0", "video": {}, "transcript": {}})

    def test_atomic_write_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sub", "transcript.md")
            atomic_write_text(path, "内容")
            self.assertTrue(os.path.exists(path))
            leftovers = [name for name in os.listdir(os.path.dirname(path)) if name.startswith(".tmp-")]
            self.assertEqual(leftovers, [])


class ValidationTests(unittest.TestCase):
    def test_duplicate_ids_are_reported(self):
        document = make_document(
            segments=[
                Segment("seg_0001", "A", 0, 1000, "exact"),
                Segment("seg_0001", "B", 1000, 2000, "exact"),
            ]
        )
        problems = document.validate()
        self.assertTrue(any("ID 重复" in item for item in problems))

    def test_unknown_precision_must_not_carry_times(self):
        document = make_document(
            segments=[Segment("seg_0001", "A", 0, 1000, "unknown")]
        )
        problems = document.validate()
        self.assertTrue(any("unknown 但带有时间码" in item for item in problems))

    def test_exact_precision_requires_times(self):
        document = make_document(
            segments=[Segment("seg_0001", "A", None, None, "exact")]
        )
        problems = document.validate()
        self.assertTrue(any("缺少时间码" in item for item in problems))

    def test_empty_text_is_reported(self):
        document = make_document(
            segments=[Segment("seg_0001", "   ", 0, 1000, "exact")]
        )
        self.assertTrue(any("文本为空" in item for item in document.validate()))

    def test_ensure_valid_raises(self):
        document = make_document(segments=[])
        with self.assertRaises(transcript.TranscriptError):
            document.ensure_valid()


class TimeLabelTests(unittest.TestCase):
    def test_exact_label_has_no_marker(self):
        segment = Segment("s", "文字", 15000, 15500, "exact")
        self.assertEqual(segment.time_label(), "[00:15]")

    def test_approximate_label_is_marked(self):
        segment = Segment("s", "文字", 15000, 19500, "approximate")
        self.assertTrue(segment.time_label().startswith("≈["))

    def test_unknown_has_no_label(self):
        self.assertIsNone(Segment("s", "文字", None, None, "unknown").time_label())


class CoverageTests(unittest.TestCase):
    def test_full_coverage_is_usable(self):
        report = assess_coverage(make_document())
        self.assertEqual(report.verdict, "ok")
        self.assertAlmostEqual(report.coverage_ratio, 0.9)

    def test_short_subtitle_in_long_video_is_incomplete(self):
        document = make_document(duration_ms=600000)
        report = assess_coverage(document)
        self.assertEqual(report.verdict, "incomplete")
        self.assertTrue(any("时间覆盖" in reason for reason in report.reasons))

    def test_invalid_timecodes_block_success(self):
        document = make_document(
            segments=[Segment("seg_0001", "A", 5000, 1000, "exact")]
        )
        report = assess_coverage(document)
        self.assertEqual(report.verdict, "incomplete")
        self.assertGreater(report.invalid_count, 0)

    def test_empty_document_is_incomplete(self):
        report = assess_coverage(make_document(segments=[]))
        self.assertEqual(report.verdict, "incomplete")


class MergeTests(unittest.TestCase):
    def test_duplicate_rolling_cues_are_dropped(self):
        segments = [
            Segment("s1", "同一句话", 0, 1000, "exact"),
            Segment("s2", "同一句话", 1000, 2000, "exact"),
        ]
        merged = merge_adjacent(segments)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].end_ms, 2000)

    def test_sentences_are_joined_without_losing_text(self):
        segments = [
            Segment("s1", "第一句", 0, 1000, "exact"),
            Segment("s2", "第二句", 1100, 2000, "exact"),
        ]
        merged = merge_adjacent(segments)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].text, "第一句第二句")


if __name__ == "__main__":
    unittest.main()
