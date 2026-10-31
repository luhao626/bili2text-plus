"""speech2text 纯逻辑单元测试：分块、重复清理、无 Key 行为、空内容防护。

导入本模块会加载 whisper/torch（较慢），但不下载模型、不联网。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import speech2text as s  # noqa: E402


class ChunkingTests(unittest.TestCase):
    def test_estimate_tokens_chinese(self):
        self.assertGreater(s._estimate_tokens("这是一段中文文本" * 100), 100)

    def test_model_max_tokens_for_glm(self):
        # glm 不在已知列表里，走默认长上下文分支
        self.assertGreaterEqual(s._get_model_max_tokens("glm-4.7-flash"), 128000)

    def test_split_keeps_short_text_single_chunk(self):
        text = "第一段内容。\n第二段内容。"
        chunks = s._split_text_into_chunks(text, max_tokens_per_chunk=100000)
        self.assertEqual(len(chunks), 1)


class RepetitionTailTests(unittest.TestCase):
    def test_collapses_repeated_sentence_tail(self):
        body = "这是正常的正文内容。第二句也很重要。"
        loop = "这就是普通话的评论。" * 300
        cleaned = s._strip_repetition_tail(body + loop)
        self.assertNotIn("这就是普通话的评论。" * 4, cleaned)
        self.assertTrue(cleaned.startswith("这是正常的正文内容"))

    def test_normal_text_untouched(self):
        text = "第一句。第二句。第一句。第三句。"
        self.assertEqual(s._strip_repetition_tail(text), text.strip())


class NoKeyBehaviourTests(unittest.TestCase):
    def test_refine_without_key_raises_readable_error(self):
        with mock.patch.object(s, "MINIMAX_API_KEY", ""), mock.patch.object(s, "minimax_client", None):
            with self.assertRaises(RuntimeError) as context:
                s.refine_text("whatever")
        self.assertIn("未配置", str(context.exception))


class EmptyContentGuardTests(unittest.TestCase):
    def test_empty_chunk_result_raises(self):
        """模型返回空内容时必须报错，而不是把空字符串当作润色结果。"""

        class FakeMessage:
            content = ""

        class FakeChoice:
            message = FakeMessage()

        class FakeResponse:
            choices = [FakeChoice()]

        class FakeCompletions:
            def create(self, **_kwargs):
                return FakeResponse()

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        with mock.patch.object(s, "minimax_client", FakeClient()):
            with self.assertRaises(RuntimeError) as context:
                s._refine_single_chunk("测试文本", "以下是普通话的句子。")
        self.assertIn("空", str(context.exception))


class OutputDirTests(unittest.TestCase):
    def test_set_output_dir_creates_and_returns_absolute(self):
        with tempfile.TemporaryDirectory() as base:
            target = os.path.join(base, "custom_out")
            result = s.set_output_dir(target)
            self.assertTrue(os.path.isdir(target))
            self.assertEqual(Path(result), Path(target).resolve())
        s.set_output_dir("outputs")  # 还原默认值，避免影响其他测试


if __name__ == "__main__":
    unittest.main()
