"""llm_config.py 的单元测试：配置优先级、密钥脱敏、错误分类。全部使用 mock，不联网。"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_config  # noqa: E402


class LoadSettingsTests(unittest.TestCase):
    def tearDown(self):
        for name in (
            "LLM_API_KEY",
            "LLM_BASE_URL",
            "LLM_MODEL",
            "LLM_PROVIDER",
            "MINIMAX_APIKEY",
            "MINIMAX_API_BASE",
            "MINIMAX_MODEL",
        ):
            os.environ.pop(name, None)

    def test_legacy_minimax_env_still_works(self):
        with mock.patch.dict(
            os.environ,
            {
                "MINIMAX_APIKEY": "test-key",
                "MINIMAX_API_BASE": "https://open.bigmodel.cn/api/paas/v4",
                "MINIMAX_MODEL": "glm-4.7-flash",
            },
        ):
            settings = llm_config.load_settings()
        self.assertTrue(settings.configured)
        self.assertEqual(settings.provider, "zhipu")
        self.assertEqual(settings.source, "legacy_env")

    def test_llm_env_overrides_legacy(self):
        with mock.patch.dict(
            os.environ,
            {
                "MINIMAX_APIKEY": "old-key",
                "LLM_API_KEY": "new-key",
                "LLM_BASE_URL": "https://api.minimax.cn/v1",
                "LLM_MODEL": "MiniMax-M3",
            },
        ):
            settings = llm_config.load_settings()
        self.assertEqual(settings.api_key, "new-key")
        self.assertEqual(settings.provider, "minimax")
        self.assertEqual(settings.source, "llm_env")

    def test_explicit_override_wins(self):
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "env-key"}):
            settings = llm_config.load_settings({"api_key": "ui-key", "model": "glm-4.7-flash"})
        self.assertEqual(settings.api_key, "ui-key")
        self.assertEqual(settings.source, "ui")

    def test_provider_preset_fills_missing_fields(self):
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_PROVIDER": "zhipu"}):
            settings = llm_config.load_settings()
        self.assertEqual(settings.base_url, "https://open.bigmodel.cn/api/paas/v4")
        self.assertEqual(settings.model, "glm-4.7-flash")

    def test_unconfigured_when_no_key(self):
        settings = llm_config.load_settings()
        self.assertFalse(settings.configured)
        self.assertEqual(settings.source, "none")


class MaskingTests(unittest.TestCase):
    def test_mask_hides_middle(self):
        masked = llm_config.mask_key("abcdefghijklmnop")
        self.assertTrue(masked.startswith("abc"))
        self.assertTrue(masked.endswith("mnop"))
        self.assertNotIn("defghijk", masked)

    def test_mask_short_key(self):
        self.assertEqual(llm_config.mask_key("abc"), "***")

    def test_mask_empty(self):
        self.assertEqual(llm_config.mask_key(""), "(未配置)")


class ClassifyErrorTests(unittest.TestCase):
    def _error(self, status, text):
        error = Exception(text)
        error.status_code = status
        return error

    def test_auth_error(self):
        category, _ = llm_config.classify_error(self._error(401, "invalid api key"))
        self.assertEqual(category, "auth")

    def test_model_not_found(self):
        category, _ = llm_config.classify_error(self._error(404, "模型不存在"))
        self.assertEqual(category, "model")

    def test_platform_overload(self):
        category, _ = llm_config.classify_error(self._error(429, "1305 模型访问量过大"))
        self.assertEqual(category, "overload")

    def test_quota(self):
        category, _ = llm_config.classify_error(self._error(429, "1113 您的账户已欠费"))
        self.assertEqual(category, "quota")

    def test_rate_limit(self):
        category, _ = llm_config.classify_error(self._error(429, "并发过高"))
        self.assertEqual(category, "rate_limit")

    def test_network(self):
        category, _ = llm_config.classify_error(TimeoutError("timed out"))
        self.assertEqual(category, "network")


class ThinkingSwitchTests(unittest.TestCase):
    def test_glm_models_disable_thinking(self):
        settings = llm_config.LLMSettings(model="glm-4.7-flash")
        self.assertTrue(settings.supports_thinking_switch)
        kwargs = llm_config.request_kwargs(settings)
        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "disabled"}})

    def test_minimax_keeps_default(self):
        settings = llm_config.LLMSettings(model="MiniMax-M3")
        self.assertFalse(settings.supports_thinking_switch)
        self.assertEqual(llm_config.request_kwargs(settings), {})


class BuildClientTests(unittest.TestCase):
    def test_no_key_returns_none(self):
        self.assertIsNone(llm_config.build_client(llm_config.LLMSettings()))

    def test_with_key_builds_client(self):
        client = llm_config.build_client(
            llm_config.LLMSettings(api_key="k", base_url="https://example.com/v1")
        )
        self.assertIsNotNone(client)


if __name__ == "__main__":
    unittest.main()
