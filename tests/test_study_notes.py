"""智能笔记测试：结构校验、证据回填、分块合并与失败保护。"""

import json
import os
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_config  # noqa: E402
import pipeline  # noqa: E402
import study_notes  # noqa: E402
from transcript import (  # noqa: E402
    SOURCE_HUMAN_SUBTITLE,
    SOURCE_LEGACY_MARKDOWN,
    SOURCE_WHISPER,
    Segment,
    TranscriptDocument,
)


class _Completions:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.responder(kwargs["messages"][-1]["content"])
        choices = [SimpleNamespace(message=SimpleNamespace(content=content))] if content is not None else []
        return SimpleNamespace(choices=choices, model=kwargs.get("model"))


class FakeClient:
    def __init__(self, responder):
        self.completions = _Completions(responder)
        self.chat = SimpleNamespace(completions=self.completions)


def ids_in(prompt):
    return re.findall(r"\[(seg_\d{4})\]\s*(.+)", prompt)


def echo_responder(prompt):
    """按提示里的 segment 生成合法 JSON：章节用首段，知识点用前两段。"""
    pairs = ids_in(prompt)
    ids = [pair[0] for pair in pairs]
    if not ids:
        return json.dumps({"chapters": [], "key_points": []}, ensure_ascii=False)
    quote = pairs[0][1][:20]
    return json.dumps(
        {
            "chapters": [{"title": "第一章", "segment_ids": [ids[0]]}],
            "key_points": [
                {
                    "title": "知识点一",
                    "explanation": "解释",
                    "segment_ids": ids[:2] if len(ids) > 1 else ids[:1],
                    "quote": quote,
                }
            ],
            "overview": "不应该出现的概述",
        },
        ensure_ascii=False,
    )


def settings():
    return llm_config.LLMSettings(
        provider="zhipu",
        api_key="test-key",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4.7-flash",
    )


def document(source_type=SOURCE_HUMAN_SUBTITLE, precision="exact", count=3, duration=30000):
    segments = []
    for index in range(1, count + 1):
        start = (index - 1) * 5000
        segments.append(
            Segment(f"seg_{index:04d}", f"第 {index} 段的内容文本", start, start + 4000, precision)
        )
    if precision == "unknown":
        for segment in segments:
            segment.start_ms = None
            segment.end_ms = None
    return TranscriptDocument(
        platform="bilibili",
        video_id="BV1",
        title="示例视频",
        url="https://example.com",
        page=1,
        duration_ms=duration,
        source_type=source_type,
        language="zh-Hans",
        segments=segments,
    )


class ExtractJsonTests(unittest.TestCase):
    def test_strips_code_fence(self):
        payload = study_notes._extract_json('```json\n{"chapters": []}\n```')
        self.assertEqual(payload, {"chapters": []})

    def test_recovers_wrapped_json(self):
        payload = study_notes._extract_json('好的，结果如下：{"chapters": []} 以上。')
        self.assertEqual(payload, {"chapters": []})

    def test_rejects_non_json(self):
        with self.assertRaises(study_notes.StudyNotesError):
            study_notes._extract_json("完全没有 JSON")


class ValidationTests(unittest.TestCase):
    def test_invalid_ids_are_dropped(self):
        doc = document()
        payload = {
            "chapters": [{"title": "章节", "segment_ids": ["seg_9999", "seg_0001"]}],
            "key_points": [{"title": "点", "segment_ids": ["seg_8888"], "quote": "x"}],
        }
        chapters, points, warnings, dropped = study_notes.validate_payload(payload, doc.id_map())
        self.assertEqual([chapter.segment_ids for chapter in chapters], [["seg_0001"]])
        self.assertEqual(points, [])
        self.assertGreaterEqual(dropped, 2)

    def test_point_without_any_valid_id_is_dropped(self):
        doc = document()
        payload = {"chapters": [], "key_points": [{"title": "凭空结论", "segment_ids": []}]}
        _chapters, points, _warnings, _dropped = study_notes.validate_payload(payload, doc.id_map())
        self.assertEqual(points, [])

    def test_quote_not_matching_source_is_marked_for_review(self):
        doc = document()
        payload = {
            "chapters": [],
            "key_points": [
                {
                    "title": "无法支持的结论",
                    "explanation": "原文没有说过",
                    "segment_ids": ["seg_0001"],
                    "quote": "这是 AI 编造的一句原文",
                }
            ],
        }
        _chapters, points, _warnings, _dropped = study_notes.validate_payload(payload, doc.id_map())
        self.assertEqual(len(points), 1)
        self.assertTrue(points[0].needs_review)
        # 证据文字必须来自源数据，而不是 AI 的引文
        self.assertIn("第 1 段的内容文本", points[0].evidence[0][1])
        self.assertNotIn("AI 编造", points[0].evidence[0][1])

    def test_extra_fields_are_ignored(self):
        doc = document()
        payload = {
            "chapters": [{"title": "章节", "segment_ids": ["seg_0001"]}],
            "key_points": [],
            "overview": "课程概述",
            "glossary": ["术语"],
        }
        _chapters, _points, warnings, _dropped = study_notes.validate_payload(payload, doc.id_map())
        self.assertTrue(any("额外栏目" in warning for warning in warnings))


class ChunkTests(unittest.TestCase):
    def test_chunking_keeps_order_and_ids(self):
        doc = document(count=30)
        chunks = study_notes.chunk_segments(doc.segments, max_tokens=60)
        self.assertGreater(len(chunks), 1)
        flattened = [segment.id for chunk in chunks for segment in chunk]
        self.assertEqual(flattened, [segment.id for segment in doc.segments])

    def test_merge_deduplicates_titles(self):
        chapter = study_notes.Chapter("第一章", ["seg_0001"])
        point = study_notes.KeyPoint("同一个点", "解释", ["seg_0001"])
        merged_chapters, merged_points = study_notes.merge_results(
            [([chapter], [point]), ([study_notes.Chapter("第一章", ["seg_0002"])], [study_notes.KeyPoint("同一个点", "解释", ["seg_0002"])])]
        )
        self.assertEqual(len(merged_chapters), 1)
        self.assertEqual(merged_chapters[0].segment_ids, ["seg_0001", "seg_0002"])
        self.assertEqual(len(merged_points), 1)


class RenderTests(unittest.TestCase):
    def test_only_two_sections_are_rendered(self):
        doc = document()
        notes = study_notes.StudyNotes(
            title="示例视频",
            chapters=[study_notes.Chapter("第一章", ["seg_0001"])],
            key_points=[study_notes.KeyPoint("知识点", "解释", ["seg_0002"], [("[00:05–00:09]", "第 2 段的内容文本")])],
        )
        markdown = study_notes.render_markdown(notes, doc)
        headings = [line for line in markdown.splitlines() if line.startswith("## ")]
        self.assertEqual(headings, ["## 02 · 章节结构", "## 03 · 核心知识点"])
        for forbidden in ("课程概述", "术语", "复习", "思维导图", "知识图谱"):
            self.assertNotIn(forbidden, markdown)

    def test_chapter_label_shows_start_time_only(self):
        doc = document()
        notes = study_notes.StudyNotes(
            chapters=[study_notes.Chapter("第一章", ["seg_0002"])],
            key_points=[],
        )
        markdown = study_notes.render_markdown(notes, doc)
        # 章节标起始时间，不把「首段的 4 秒区间」误当成章节长度
        self.assertIn("- [00:05] 第一章", markdown)

    def test_approximate_time_is_marked(self):
        doc = document(source_type=SOURCE_WHISPER, precision="approximate")
        notes = study_notes.StudyNotes(
            chapters=[study_notes.Chapter("第一章", ["seg_0001"])],
            key_points=[],
        )
        markdown = study_notes.render_markdown(notes, doc)
        self.assertIn("≈[", markdown)

    def test_unknown_time_uses_paragraph_reference(self):
        doc = document(source_type=SOURCE_LEGACY_MARKDOWN, precision="unknown")
        notes = study_notes.StudyNotes(
            chapters=[study_notes.Chapter("第一章", ["seg_0002"])],
            key_points=[],
        )
        markdown = study_notes.render_markdown(notes, doc)
        self.assertIn("原文第 2 段", markdown)
        self.assertIsNone(re.search(r"\[\d{2}:\d{2}", markdown))

    def test_needs_review_is_visible(self):
        doc = document()
        notes = study_notes.StudyNotes(
            chapters=[],
            key_points=[study_notes.KeyPoint("可疑结论", "解释", ["seg_0001"], [("[00:00–00:04]", "文本")], True, "引文不一致")],
        )
        markdown = study_notes.render_markdown(notes, doc)
        self.assertIn("（待核实）", markdown)


class GenerateTests(unittest.TestCase):
    def test_generates_chapters_and_points(self):
        doc = document()
        client = FakeClient(echo_responder)
        notes = study_notes.generate_study_notes(doc, client, settings())
        self.assertTrue(notes.complete)
        self.assertEqual(len(notes.chapters), 1)
        self.assertEqual(len(notes.key_points), 1)
        self.assertEqual(notes.semantic_verification, "not_performed")
        self.assertEqual(notes.dropped_refs, 0)

    def test_all_references_exist_after_chunked_generation(self):
        doc = document(count=40, duration=200000)
        client = FakeClient(echo_responder)
        notes = study_notes.generate_study_notes(doc, client, settings(), max_chunk_tokens=80)
        self.assertGreater(len(client.completions.calls), 1)
        valid_ids = set(doc.id_map())
        for chapter in notes.chapters:
            self.assertTrue(set(chapter.segment_ids) <= valid_ids)
        for point in notes.key_points:
            self.assertTrue(set(point.segment_ids) <= valid_ids)
            for _label, text in point.evidence:
                self.assertTrue(any(text[:10] in segment.text for segment in doc.segments))

    def test_chunk_failure_marks_partial_instead_of_success(self):
        doc = document(count=20)
        calls = {"n": 0}

        def responder(prompt):
            calls["n"] += 1
            # 第 1 块（含一次纠错重试）始终返回非 JSON，模拟该块彻底失败
            if calls["n"] <= 2:
                return "不是 JSON"
            return echo_responder(prompt)

        client = FakeClient(responder)
        notes = study_notes.generate_study_notes(doc, client, settings(), max_chunk_tokens=60)
        self.assertFalse(notes.complete)
        self.assertTrue(notes.warnings)
        markdown = study_notes.render_markdown(notes, doc)
        self.assertIn("只覆盖了部分原文", markdown)

    def test_all_chunks_failing_raises(self):
        doc = document()
        client = FakeClient(lambda _prompt: "完全不是 JSON")
        with self.assertRaises(study_notes.StudyNotesError):
            study_notes.generate_study_notes(doc, client, settings())

    def test_missing_key_raises(self):
        with self.assertRaises(study_notes.StudyNotesError):
            study_notes.generate_study_notes(document(), None, settings())


class FailureKeepsTranscriptTests(unittest.TestCase):
    def test_api_failure_keeps_written_transcript(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = pipeline.TaskPaths(os.path.join(folder, "task")).ensure()
            doc = document()
            manifest = pipeline.manifest_from_document(doc, task_id="t1")
            pipeline.write_transcript(doc, paths, manifest)

            def boom(_prompt):
                raise RuntimeError("network down")

            client = FakeClient(boom)
            with self.assertRaises(study_notes.StudyNotesError):
                study_notes.generate_study_notes(doc, client, settings())

            self.assertTrue(os.path.exists(paths.transcript_md))
            self.assertTrue(os.path.exists(paths.transcript_json))
            self.assertFalse(os.path.exists(paths.notes_md))


if __name__ == "__main__":
    unittest.main()
