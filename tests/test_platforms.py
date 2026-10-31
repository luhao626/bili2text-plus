"""platforms.py 的单元测试：链接解析、平台判定、缓存键与文件名安全。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from platforms import (  # noqa: E402
    UnsupportedLinkError,
    extract_url,
    is_short_link,
    resolve_video_input,
    safe_filename,
)


class BilibiliParsingTests(unittest.TestCase):
    def test_multi_page_link_keeps_page_param(self):
        ref = resolve_video_input("https://www.bilibili.com/video/BV1vwME6AEpd?p=33")
        self.assertEqual(ref.platform, "bilibili")
        self.assertEqual(ref.video_id, "BV1vwME6AEpd")
        self.assertEqual(ref.page, 33)
        self.assertEqual(ref.cache_key, "BV1vwME6AEpd_p33")
        self.assertIn("p=33", ref.canonical_url)

    def test_single_page_link_uses_plain_cache_key(self):
        ref = resolve_video_input("https://www.bilibili.com/video/BV1vwME6AEpd")
        self.assertIsNone(ref.page)
        self.assertEqual(ref.cache_key, "BV1vwME6AEpd")

    def test_page_one_and_default_share_cache(self):
        first = resolve_video_input("https://www.bilibili.com/video/BV1vwME6AEpd?p=1")
        default = resolve_video_input("https://www.bilibili.com/video/BV1vwME6AEpd")
        self.assertEqual(first.cache_key, default.cache_key)

    def test_bare_bv_number(self):
        ref = resolve_video_input("BV1vwME6AEpd")
        self.assertEqual(ref.platform, "bilibili")
        self.assertEqual(ref.video_id, "BV1vwME6AEpd")

    def test_av_number(self):
        ref = resolve_video_input("https://www.bilibili.com/video/av170001")
        self.assertEqual(ref.video_id, "av170001")

    def test_b23_short_link_marked_for_expansion(self):
        ref = resolve_video_input("https://b23.tv/abc123")
        self.assertEqual(ref.platform, "bilibili")
        self.assertTrue(is_short_link(ref.canonical_url))

    def test_domain_spoof_is_not_bilibili(self):
        ref = resolve_video_input("https://evilbilibili.com/video/BV1vwME6AEpd")
        self.assertNotEqual(ref.platform, "bilibili")


class DouyinParsingTests(unittest.TestCase):
    def test_share_text_extracts_short_link(self):
        text = "7.92 复制打开抖音，看看【作者】的作品！ https://v.douyin.com/iRNBho6u/ 复制此链接"
        ref = resolve_video_input(text)
        self.assertEqual(ref.platform, "douyin")
        self.assertTrue(is_short_link(ref.canonical_url))
        self.assertTrue(ref.cache_key.startswith("dy_"))

    def test_full_video_url(self):
        ref = resolve_video_input("https://www.douyin.com/video/7296829101289123123")
        self.assertEqual(ref.platform, "douyin")
        self.assertEqual(ref.video_id, "7296829101289123123")
        self.assertEqual(ref.cache_key, "dy_7296829101289123123")

    def test_iesdouyin_share_url(self):
        ref = resolve_video_input("https://www.iesdouyin.com/share/video/7296829101289123123/")
        self.assertEqual(ref.platform, "douyin")
        self.assertEqual(ref.video_id, "7296829101289123123")

    def test_different_videos_get_different_cache(self):
        a = resolve_video_input("https://www.douyin.com/video/1111111111111111111")
        b = resolve_video_input("https://www.douyin.com/video/2222222222222222222")
        self.assertNotEqual(a.cache_key, b.cache_key)


class YoutubeAndGenericTests(unittest.TestCase):
    def test_youtu_be(self):
        ref = resolve_video_input("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(ref.platform, "youtube")
        self.assertEqual(ref.cache_key, "yt_dQw4w9WgXcQ")

    def test_watch_url(self):
        ref = resolve_video_input("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(ref.platform, "youtube")
        self.assertEqual(ref.video_id, "dQw4w9WgXcQ")

    def test_unknown_site_is_generic(self):
        ref = resolve_video_input("https://example.com/v/1")
        self.assertEqual(ref.platform, "generic")

    def test_invalid_input_raises_friendly_error(self):
        with self.assertRaises(UnsupportedLinkError):
            resolve_video_input("这不是一个链接")
        with self.assertRaises(UnsupportedLinkError):
            resolve_video_input("")


class DouyinInvalidPageTests(unittest.TestCase):
    """抖音链接缺少单条视频 ID 时必须报错，而不是把首页当视频下载。"""

    def test_homepage_rejected(self):
        with self.assertRaises(UnsupportedLinkError):
            resolve_video_input("https://www.douyin.com")

    def test_homepage_with_slash_rejected(self):
        with self.assertRaises(UnsupportedLinkError):
            resolve_video_input("https://www.douyin.com/")

    def test_user_page_rejected(self):
        with self.assertRaises(UnsupportedLinkError):
            resolve_video_input("https://www.douyin.com/user/MS4wLjABAAAA")

    def test_error_message_is_actionable(self):
        with self.assertRaises(UnsupportedLinkError) as context:
            resolve_video_input("https://www.douyin.com/discover")
        self.assertIn("video/", str(context.exception))

    def test_short_link_still_accepted(self):
        ref = resolve_video_input("https://v.douyin.com/iRNBho6u/")
        self.assertEqual(ref.platform, "douyin")
        self.assertTrue(ref.video_id.startswith("short_"))

    def test_detail_url_still_accepted(self):
        ref = resolve_video_input("https://www.douyin.com/video/7296829101289123123")
        self.assertEqual(ref.video_id, "7296829101289123123")
        self.assertEqual(ref.cache_key, "dy_7296829101289123123")


class LocalFileTests(unittest.TestCase):
    """平台封掉命令行直连时，本地文件是唯一的兜底入口。"""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video = os.path.join(self.tmp.name, "抖音 测试视频.mp4")
        with open(self.video, "wb") as handle:
            handle.write(b"\x00" * 32)
        self.audio = os.path.join(self.tmp.name, "audio-clip.m4a")
        with open(self.audio, "wb") as handle:
            handle.write(b"\x00" * 16)

    def test_absolute_video_path(self):
        ref = resolve_video_input(self.video)
        self.assertEqual(ref.platform, "local")
        self.assertEqual(ref.video_id, "抖音 测试视频.mp4")
        self.assertEqual(os.path.abspath(self.video), ref.canonical_url)
        self.assertTrue(ref.cache_key.startswith("local_"))

    def test_audio_path_supported(self):
        ref = resolve_video_input(self.audio)
        self.assertEqual(ref.platform, "local")

    def test_quoted_path_from_explorer(self):
        """资源管理器「复制为路径」会带双引号。"""
        ref = resolve_video_input(f'"{self.video}"')
        self.assertEqual(ref.platform, "local")
        self.assertEqual(os.path.abspath(self.video), ref.canonical_url)

    def test_same_name_different_path_does_not_share_cache(self):
        other_dir = os.path.join(self.tmp.name, "sub")
        os.makedirs(other_dir)
        other = os.path.join(other_dir, "抖音 测试视频.mp4")
        with open(other, "wb") as handle:
            handle.write(b"\x00" * 8)
        first = resolve_video_input(self.video)
        second = resolve_video_input(other)
        self.assertNotEqual(first.cache_key, second.cache_key)

    def test_missing_file_raises_friendly_error(self):
        with self.assertRaises(UnsupportedLinkError) as ctx:
            resolve_video_input(os.path.join(self.tmp.name, "not-there.mp4"))
        self.assertIn("本地文件不存在", str(ctx.exception))

    def test_directory_is_rejected(self):
        folder = os.path.join(self.tmp.name, "folder.mp4")
        os.makedirs(folder)
        with self.assertRaises(UnsupportedLinkError) as ctx:
            resolve_video_input(folder)
        self.assertIn("文件夹", str(ctx.exception))

    def test_bare_filename_without_file_is_not_treated_as_path(self):
        """仅输入 video.mp4 且文件不存在时，应提示找不到链接而不是报本地文件。"""
        with self.assertRaises(UnsupportedLinkError) as ctx:
            resolve_video_input("video.mp4")
        self.assertIn("没有识别到可用的视频链接", str(ctx.exception))

    def test_display_uses_local_label(self):
        ref = resolve_video_input(self.video)
        self.assertIn("本地文件", ref.display)


class HelperTests(unittest.TestCase):
    def test_extract_url_from_chinese_share_text(self):
        text = "看看这个 https://v.douyin.com/iRNBho6u/，超级好用！"
        self.assertEqual(extract_url(text), "https://v.douyin.com/iRNBho6u/")

    def test_safe_filename_strips_illegal_characters(self):
        self.assertEqual(safe_filename('a/b\\c:d*e?f"g<h>i|'), "a_b_c_d_e_f_g_h_i")

    def test_safe_filename_reserved_names(self):
        self.assertEqual(safe_filename("CON"), "_CON")
        self.assertEqual(safe_filename("NUL.txt"), "_NUL.txt")

    def test_safe_filename_empty_fallback(self):
        self.assertEqual(safe_filename("   "), "video")

    def test_safe_filename_truncates_long_names(self):
        self.assertLessEqual(len(safe_filename("长" * 200, max_length=80)), 80)


if __name__ == "__main__":
    unittest.main()
