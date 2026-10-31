"""旧 Markdown 兼容测试：只读、无假时间、段落引用稳定。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import legacy_markdown  # noqa: E402
import pipeline  # noqa: E402
import study_notes  # noqa: E402
from transcript import SOURCE_LEGACY_MARKDOWN  # noqa: E402

LEGACY_TEXT = """# 转写结果（原始）

来这回进入到我们的底吧模式，先看实体数据是怎么加载的，这一段有足够长度作为正文段落。

节点与关系需要分别建模，否则后续遍历会重复计算同一个对象，这里也补充到足够长度。

最后一段用于验证段落顺序不会被改变。
"""


class SplitTests(unittest.TestCase):
    def test_headings_are_not_treated_as_content(self):
        paragraphs = legacy_markdown.split_paragraphs(LEGACY_TEXT)
        self.assertTrue(all(not item.startswith("#") for item in paragraphs))
        self.assertEqual(len(paragraphs), 3)

    def test_short_paragraphs_are_kept_as_separate_units(self):
        text = "短句。\n\n另一句。"
        paragraphs = legacy_markdown.split_paragraphs(text)
        self.assertEqual(len(paragraphs), 2)

    def test_overlong_block_is_split_by_sentence(self):
        sentence = "这是用来测试超长段落切分的一句话，长度足够触发按句切分逻辑。"
        text = sentence * 20  # 单段远超 400 字，且没有空行
        paragraphs = legacy_markdown.split_paragraphs(text)
        self.assertGreater(len(paragraphs), 1)
        self.assertTrue(all(len(item) <= 500 for item in paragraphs))
        # 顺序与内容不得被改写
        self.assertEqual("".join(paragraphs).replace(" ", ""), text)


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "20261031155014.md")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(LEGACY_TEXT)
        with open(self.path, "rb") as handle:
            self.before = handle.read()

    def tearDown(self):
        self.tmp.cleanup()

    def test_builds_document_without_times(self):
        document = legacy_markdown.load_legacy_markdown(self.path)
        self.assertEqual(document.source_type, SOURCE_LEGACY_MARKDOWN)
        self.assertEqual(len(document.segments), 3)
        self.assertEqual(document.segments[0].id, "seg_0001")
        self.assertTrue(all(segment.start_ms is None for segment in document.segments))
        self.assertTrue(all(segment.time_precision == "unknown" for segment in document.segments))
        self.assertEqual(document.validate(), [])

    def test_original_file_is_never_modified(self):
        legacy_markdown.load_legacy_markdown(self.path)
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), self.before)

    def test_empty_file_is_rejected(self):
        empty = os.path.join(self.tmp.name, "empty.md")
        with open(empty, "w", encoding="utf-8") as handle:
            handle.write("# 只有标题\n")
        with self.assertRaises(legacy_markdown.LegacyMarkdownError):
            legacy_markdown.load_legacy_markdown(empty)

    def test_notes_use_paragraph_references_only(self):
        document = legacy_markdown.load_legacy_markdown(self.path)
        notes = study_notes.StudyNotes(
            chapters=[study_notes.Chapter("章节", ["seg_0001"])],
            key_points=[
                study_notes.KeyPoint(
                    "实体与关系分别建模",
                    "解释",
                    ["seg_0002"],
                    [("原文第 2 段", document.segments[1].text)],
                )
            ],
        )
        markdown = study_notes.render_markdown(notes, document)
        self.assertIn("原文第 2 段", markdown)
        self.assertNotIn("[00:", markdown)
        self.assertIn("## 02 · 章节结构", markdown)
        self.assertIn("## 03 · 核心知识点", markdown)

    def test_legacy_task_writes_json_without_copying_source(self):
        document = legacy_markdown.load_legacy_markdown(self.path)
        paths = pipeline.TaskPaths(os.path.join(self.tmp.name, "legacy_task")).ensure()
        manifest = pipeline.manifest_from_document(document, task_id="legacy_t", mode="legacy_markdown")
        document.write_json(paths.transcript_json)
        manifest.write(paths)
        self.assertTrue(os.path.exists(paths.transcript_json))
        self.assertFalse(os.path.exists(paths.transcript_md))
        self.assertFalse(os.path.exists(os.path.join(paths.root, os.path.basename(self.path))))
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), self.before)


if __name__ == "__main__":
    unittest.main()
