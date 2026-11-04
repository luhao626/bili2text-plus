"""抖音独立下载后端单元测试：全部 Mock，不访问抖音或任何网络。"""

import inspect
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import douyin_backend  # noqa: E402
import utils  # noqa: E402
from platforms import resolve_video_input  # noqa: E402

DOUYIN_URL = "https://www.douyin.com/video/6961737553342991651"
DOUYIN_ID = "6961737553342991651"


class _FakeProcess:
    """模拟 subprocess.Popen 返回的进程对象。"""

    def __init__(self, returncode=0, output_lines=None, on_start=None):
        self._returncode = returncode
        self._lines = output_lines or []
        self._on_start = on_start

    def __enter__(self):
        if self._on_start:
            self._on_start()
        return self

    def __exit__(self, *exc_info):
        return False

    def readline(self):
        return self._lines.pop(0) if self._lines else ""

    stdout = property(lambda self: self)

    def close(self):
        pass

    def wait(self):
        return self._returncode


class ValidateUrlTests(unittest.TestCase):
    def test_standard_video_url_accepted(self):
        ref = resolve_video_input(DOUYIN_URL)
        self.assertEqual(ref.platform, "douyin")
        self.assertEqual(ref.video_id, DOUYIN_ID)
        # 复核函数不改变合法链接
        self.assertEqual(douyin_backend.validate_douyin_url(ref.canonical_url), DOUYIN_URL)

    def test_short_link_expanded_then_backend_called(self):
        """合法短链经原解析模块展开后应进入抖音后端。"""
        expanded = DOUYIN_URL
        with mock.patch.object(utils, "_expand_short_link", return_value=expanded), \
             mock.patch.object(douyin_backend, "download_douyin_video") as backend, \
             mock.patch.object(utils, "_has_complete_video", return_value=False):
            backend.return_value = os.path.join("bilibili_video", "dy_x", f"{DOUYIN_ID}.mp4")
            result = utils.download_video("https://v.douyin.com/abc123/")
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith("dy_"))
        ref_arg = backend.call_args[0][0]
        self.assertEqual(ref_arg.canonical_url, expanded)
        self.assertEqual(ref_arg.video_id, DOUYIN_ID)

    def test_invalid_host_rejected(self):
        ref = resolve_video_input("https://evil.example.com/video/123456")
        with self.assertRaises(douyin_backend.DouyinDownloadError) as ctx:
            douyin_backend.validate_douyin_url(ref.canonical_url)
        self.assertIn("白名单", str(ctx.exception))

    def test_http_scheme_rejected(self):
        with self.assertRaises(douyin_backend.DouyinDownloadError):
            douyin_backend.validate_douyin_url("http://www.douyin.com/video/123456")

    def test_missing_video_id_rejected(self):
        ref = mock.Mock(platform="douyin", video_id="", canonical_url=DOUYIN_URL)
        with self.assertRaises(douyin_backend.DouyinDownloadError) as ctx:
            douyin_backend.download_douyin_video(ref, tempfile.gettempdir())
        self.assertIn("视频 ID", str(ctx.exception))

    def test_non_douyin_ref_rejected(self):
        ref = mock.Mock(platform="bilibili", video_id="BV1xx", canonical_url="https://www.bilibili.com/video/BV1xx")
        with self.assertRaises(douyin_backend.DouyinDownloadError):
            douyin_backend.download_douyin_video(ref, tempfile.gettempdir())


class EnvironmentTests(unittest.TestCase):
    def test_fix_python_missing_gives_install_hint(self):
        with mock.patch.dict(os.environ, {douyin_backend.FIX_PYTHON_ENV_VAR: ""}, clear=False), \
             mock.patch.object(douyin_backend, "default_fix_python_path",
                               return_value=os.path.join(tempfile.gettempdir(), "no_such", "python.exe")):
            self.assertIsNone(douyin_backend.resolve_fix_python())
        hint = douyin_backend.install_hint()
        self.assertIn("yt-dlp", hint)
        self.assertIn(douyin_backend.FIX_COMMIT, hint)
        ref = resolve_video_input(DOUYIN_URL)
        with mock.patch.object(douyin_backend, "resolve_fix_python", return_value=None):
            with self.assertRaises(douyin_backend.DouyinDownloadError) as ctx:
                douyin_backend.download_douyin_video(ref, tempfile.mkdtemp())
        self.assertIn("修复环境", str(ctx.exception))

    def test_fix_python_from_env_var(self):
        fake = os.path.join(tempfile.gettempdir(), "fake_env_python.exe")
        with open(fake, "w", encoding="utf-8") as handle:
            handle.write("")
        try:
            with mock.patch.dict(os.environ, {douyin_backend.FIX_PYTHON_ENV_VAR: fake}):
                self.assertEqual(douyin_backend.resolve_fix_python(), fake)
        finally:
            os.remove(fake)


class DownloadOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dy_backend_test_")
        self.ref = resolve_video_input(DOUYIN_URL)
        self.python = os.path.join(tempfile.gettempdir(), "fake_fix_python.exe")
        with open(self.python, "w", encoding="utf-8") as handle:
            handle.write("")

    def tearDown(self):
        mock.patch.stopall()

    def _run(self, popen_side_effect):
        with mock.patch.object(douyin_backend, "resolve_fix_python", return_value=self.python), \
             mock.patch("subprocess.Popen", side_effect=popen_side_effect):
            return douyin_backend.download_douyin_video(self.ref, self.tmpdir)

    def test_success_returns_file_path(self):
        target = os.path.join(self.tmpdir, f"{DOUYIN_ID}.mp4")

        def on_start():
            with open(target, "wb") as handle:
                handle.write(b"video-bytes")

        popen = mock.Mock(return_value=_FakeProcess(returncode=0, on_start=on_start))
        path = self._run(popen)
        self.assertEqual(os.path.abspath(path), os.path.abspath(target))
        self.assertGreater(os.path.getsize(path), 0)
        # 必须用参数数组调用独立解释器，且显式 shell=False
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], self.python)
        self.assertIn("-m", argv[:3])
        self.assertEqual(argv[-1], DOUYIN_URL)
        self.assertIs(popen.call_args.kwargs.get("shell"), False)

    def test_nonzero_exit_code_raises(self):
        with self.assertRaises(douyin_backend.DouyinDownloadError) as ctx:
            self._run(lambda *_a, **_k: _FakeProcess(returncode=1, output_lines=["ERROR: 403"]))
        self.assertIn("返回码 1", str(ctx.exception))

    def test_missing_output_file_raises(self):
        with self.assertRaises(douyin_backend.DouyinDownloadError) as ctx:
            self._run(lambda *_a, **_k: _FakeProcess(returncode=0))
        self.assertIn("没有视频文件", str(ctx.exception))

    def test_empty_output_file_raises(self):
        target = os.path.join(self.tmpdir, f"{DOUYIN_ID}.mp4")

        def on_start():
            open(target, "wb").close()

        # 下载流程把空文件视为「没有视频文件」
        with self.assertRaises(douyin_backend.DouyinDownloadError) as ctx:
            self._run(lambda *_a, **_k: _FakeProcess(returncode=0, on_start=on_start))
        self.assertIn("没有视频文件", str(ctx.exception))
        # 产物校验函数对空文件给出「空文件」错误
        with self.assertRaises(douyin_backend.DouyinDownloadError) as ctx:
            douyin_backend._validate_output_path(target, self.tmpdir)
        self.assertIn("空文件", str(ctx.exception))

    def test_existing_cache_file_reused_without_download(self):
        target = os.path.join(self.tmpdir, f"{DOUYIN_ID}.mp4")
        with open(target, "wb") as handle:
            handle.write(b"cached")
        with mock.patch.object(douyin_backend, "resolve_fix_python", return_value=self.python), \
             mock.patch("subprocess.Popen") as popen:
            path = douyin_backend.download_douyin_video(self.ref, self.tmpdir)
        self.assertEqual(os.path.abspath(path), os.path.abspath(target))
        popen.assert_not_called()

    def test_output_path_escaping_cache_dir_rejected(self):
        outside = os.path.join(tempfile.gettempdir(), "outside_evil.mp4")
        with self.assertRaises(douyin_backend.DouyinDownloadError) as ctx:
            douyin_backend._validate_output_path(outside, self.tmpdir)
        self.assertIn("逃逸", str(ctx.exception))

    def test_no_shell_true_in_source(self):
        source = inspect.getsource(douyin_backend)
        self.assertNotIn("shell=True", source)


class OtherPlatformsUnaffectedTests(unittest.TestCase):
    def test_bilibili_youtube_local_not_routed_to_backend(self):
        local_file = os.path.join(tempfile.gettempdir(), "dy_test_local.mp4")
        with open(local_file, "wb") as handle:
            handle.write(b"local")
        try:
            with mock.patch.object(douyin_backend, "download_douyin_video") as backend, \
                 mock.patch.object(utils, "_download_with_ytdlp", return_value=True) as ytdlp, \
                 mock.patch.object(utils, "_prepare_local_media", return_value=True) as local_media, \
                 mock.patch.object(utils, "_has_complete_video", return_value=False):
                # B站
                result_bili = utils.download_video("BV1vwME6AEpd")
                self.assertEqual(result_bili, "BV1vwME6AEpd")
                # YouTube
                result_yt = utils.download_video("https://www.youtube.com/watch?v=aircAruvnKk")
                self.assertEqual(result_yt, "yt_aircAruvnKk")
                # 本地文件
                result_local = utils.download_video(local_file)
                self.assertTrue(result_local.startswith("local_"))
            backend.assert_not_called()
            self.assertEqual(ytdlp.call_count, 2)
            local_media.assert_called_once()
        finally:
            os.remove(local_file)


if __name__ == "__main__":
    unittest.main()
