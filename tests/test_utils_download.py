"""下载器单元测试：全部 mock yt-dlp 与网络，不访问真实视频平台。"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils  # noqa: E402
from platforms import resolve_video_input  # noqa: E402


class ExplainFailureTests(unittest.TestCase):
    def test_private_video(self):
        message = utils._explain_download_failure("ERROR: [douyin] Private video")
        self.assertIn("私密", message)

    def test_not_found(self):
        message = utils._explain_download_failure("HTTP Error 404: Not Found")
        self.assertIn("不存在", message)

    def test_forbidden(self):
        message = utils._explain_download_failure("HTTP Error 403: Forbidden")
        self.assertIn("拒绝", message)

    def test_timeout(self):
        message = utils._explain_download_failure("Connection timed out")
        self.assertIn("超时", message)

    def test_unknown_keeps_generic_hint(self):
        message = utils._explain_download_failure("something else")
        self.assertIn("下载失败", message)


class CacheIsolationTests(unittest.TestCase):
    """验证不同链接得到互不冲突的缓存目录（不真正下载）。"""

    def test_bilibili_page33_is_isolated_from_page1(self):
        p1 = resolve_video_input("https://www.bilibili.com/video/BV1vwME6AEpd")
        p33 = resolve_video_input("https://www.bilibili.com/video/BV1vwME6AEpd?p=33")
        self.assertNotEqual(p1.cache_key, p33.cache_key)
        self.assertEqual(p1.cache_key, "BV1vwME6AEpd")
        self.assertEqual(p33.cache_key, "BV1vwME6AEpd_p33")

    def test_pages_eight_and_thirty_three_differ(self):
        p8 = resolve_video_input("https://www.bilibili.com/video/BV1vwME6AEpd?p=8")
        p33 = resolve_video_input("https://www.bilibili.com/video/BV1vwME6AEpd?p=33")
        self.assertNotEqual(p8.cache_key, p33.cache_key)

    def test_invalid_input_returns_none_without_network(self):
        self.assertIsNone(utils.download_video("这不是链接 !!!"))


class WbiPageSelectionTests(unittest.TestCase):
    def test_missing_page_gives_clear_error(self):
        """分P越界时，应给出包含总P数的中文错误，而不是悄悄下第一P。"""
        calls = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                if calls.get("stage") == "pagelist":
                    return {
                        "code": 0,
                        "data": [{"page": i, "cid": 1000 + i, "part": f"P{i}"} for i in range(1, 51)],
                    }
                return {"data": {"wbi_img": {"img_url": "https://x/a.png", "sub_url": "https://x/b.png"}}}

        def fake_get(_self, url, **_kwargs):
            calls["stage"] = "pagelist" if "pagelist" in url else "nav"
            return FakeResponse()

        with mock.patch.object(utils.requests.Session, "get", new=fake_get):
            with self.assertRaises(RuntimeError) as context:
                utils._download_bilibili_via_wbi(
                    "BV1vwME6AEpd", "bilibili_video/BV1vwME6AEpd_p99", page=99
                )
        self.assertIn("50", str(context.exception))
        self.assertIn("99", str(context.exception))


class CookieArgumentTests(unittest.TestCase):
    """Cookie 参数默认不启用，只有显式配置时才传给 yt-dlp。"""

    def test_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YTDLP_COOKIES_FROM_BROWSER", None)
            os.environ.pop("YTDLP_COOKIE_FILE", None)
            self.assertEqual(utils._ytdlp_cookie_arguments(), [])

    def test_browser_session(self):
        with mock.patch.dict(os.environ, {"YTDLP_COOKIES_FROM_BROWSER": "edge"}):
            self.assertEqual(
                utils._ytdlp_cookie_arguments(),
                ["--cookies-from-browser", "edge"],
            )

    def test_cookie_file_missing_is_ignored(self):
        with mock.patch.dict(os.environ, {"YTDLP_COOKIE_FILE": "Z:/definitely/missing.txt"}):
            self.assertEqual(utils._ytdlp_cookie_arguments(), [])


class ShortLinkPlaceholderTests(unittest.TestCase):
    """短链展开失败时不能把首页当成视频去下载。"""

    def test_unresolved_short_link_returns_none(self):
        ref = resolve_video_input("https://v.douyin.com/iRNBho6u/")
        self.assertTrue(ref.video_id.startswith("short_"))
        with mock.patch.object(utils, "_expand_short_link", return_value="https://www.douyin.com/"):
            with mock.patch.object(utils, "_download_with_ytdlp") as downloader:
                with mock.patch.object(utils, "ensure_folders_exist"):
                    result = utils.download_video("https://v.douyin.com/iRNBho6u/")
        self.assertIsNone(result)
        downloader.assert_not_called()


class SafeCommandTests(unittest.TestCase):
    def test_ytdlp_command_resolves(self):
        command = utils._ytdlp_command()
        self.assertTrue(command)
        self.assertTrue(command[0].endswith(("yt-dlp", "python.exe")) or "yt_dlp" in command)


if __name__ == "__main__":
    unittest.main()
