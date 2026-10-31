"""字幕发现/解析/选择测试。全部使用固定样本与假网络，不访问真实平台。"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subtitles  # noqa: E402
from platforms import VideoRef  # noqa: E402
from transcript import SOURCE_AUTOMATIC_SUBTITLE, SOURCE_HUMAN_SUBTITLE, SOURCE_UNKNOWN_SUBTITLE  # noqa: E402

SRT_SAMPLE = """1
00:00:01,000 --> 00:00:04,000
你好，这是第一句

2
00:00:04,500 --> 00:00:06,200
<i>第二句</i>带标签
"""

VTT_SAMPLE = """WEBVTT

NOTE 这是注释

cue-1
00:00:01.000 --> 00:00:03.000 align:start position:0%
<c>第一句</c>字幕

00:00:03.500 --> 00:00:05.000
第二句字幕
"""

BILI_SAMPLE = json.dumps(
    {
        "body": [
            {"from": 1.5, "to": 3.2, "content": "实体表示图中的对象"},
            {"from": 3.2, "to": 5.0, "content": "关系描述对象之间的连接"},
        ]
    },
    ensure_ascii=False,
)


class ParserTests(unittest.TestCase):
    def test_parses_srt(self):
        parsed = subtitles.parse_srt(SRT_SAMPLE)
        self.assertEqual(len(parsed.cues), 2)
        self.assertEqual(parsed.cues[0][0], 1000)
        self.assertEqual(parsed.cues[0][1], 4000)
        self.assertIn("第一句", parsed.cues[0][2])

    def test_srt_strips_inline_tags(self):
        parsed = subtitles.parse_srt(SRT_SAMPLE)
        self.assertEqual(parsed.cues[1][2], "第二句带标签")

    def test_parses_vtt_with_note_and_settings(self):
        parsed = subtitles.parse_vtt(VTT_SAMPLE)
        self.assertEqual(len(parsed.cues), 2)
        self.assertEqual(parsed.cues[0][0], 1000)
        self.assertEqual(parsed.cues[0][2], "第一句字幕")

    def test_parses_bilibili_json_in_milliseconds(self):
        parsed = subtitles.parse_bilibili_json(BILI_SAMPLE)
        self.assertEqual(len(parsed.cues), 2)
        self.assertEqual(parsed.cues[0][0], 1500)
        self.assertEqual(parsed.cues[0][1], 3200)

    def test_invalid_timecode_is_counted_not_silently_accepted(self):
        broken = "1\n00:00:0X,000 --> 00:00:04,000\n坏时间码\n"
        parsed = subtitles.parse_srt(broken)
        self.assertEqual(parsed.cues, [])
        self.assertEqual(parsed.invalid_count, 1)

    def test_empty_subtitle_raises(self):
        with self.assertRaises(subtitles.SubtitleParseError):
            subtitles.parse_srt("   \n\n")

    def test_garbage_is_rejected(self):
        with self.assertRaises(subtitles.SubtitleParseError):
            subtitles.parse_subtitle("这不是字幕，只是一段普通文字。")

    def test_unknown_format_is_not_guessed(self):
        self.assertEqual(subtitles.detect_format("random"), "unknown")

    def test_bilibili_json_requires_body(self):
        with self.assertRaises(subtitles.SubtitleParseError):
            subtitles.parse_bilibili_json('{"foo": 1}')


class TrackTests(unittest.TestCase):
    def test_ytdlp_subtitles_are_human_and_auto_are_automatic(self):
        info = {
            "subtitles": {"zh-CN": [{"ext": "vtt", "url": "u1"}]},
            "automatic_captions": {"zh-CN": [{"ext": "vtt", "url": "u2"}]},
        }
        tracks = subtitles.tracks_from_info(info)
        self.assertEqual(len(tracks), 2)
        kinds = sorted(track.source_type for track in tracks)
        self.assertEqual(kinds, [SOURCE_AUTOMATIC_SUBTITLE, SOURCE_HUMAN_SUBTITLE])
        self.assertTrue(all(track.language == "zh-CN" for track in tracks))

    def test_human_track_sorted_before_automatic(self):
        tracks = [
            subtitles.SubtitleTrack("zh-CN", SOURCE_AUTOMATIC_SUBTITLE),
            subtitles.SubtitleTrack("en", SOURCE_HUMAN_SUBTITLE),
        ]
        ordered = subtitles.order_tracks(tracks)
        self.assertEqual(ordered[0].source_type, SOURCE_HUMAN_SUBTITLE)

    def test_smart_mode_never_falls_back_to_automatic_subtitle(self):
        tracks = [subtitles.SubtitleTrack("zh-CN", SOURCE_AUTOMATIC_SUBTITLE)]
        track, reason = subtitles.select_track(tracks, mode=subtitles.MODE_SMART)
        self.assertIsNone(track)
        self.assertIn("自动字幕", reason)

    def test_smart_mode_picks_human_subtitle(self):
        tracks = [
            subtitles.SubtitleTrack("zh-CN", SOURCE_AUTOMATIC_SUBTITLE),
            subtitles.SubtitleTrack("zh-Hans", SOURCE_HUMAN_SUBTITLE),
        ]
        track, reason = subtitles.select_track(tracks, mode=subtitles.MODE_SMART)
        self.assertIsNotNone(track)
        self.assertEqual(track.source_type, SOURCE_HUMAN_SUBTITLE)
        self.assertIn("人工字幕", reason)

    def test_subtitle_only_mode_may_use_automatic_but_labels_it(self):
        tracks = [subtitles.SubtitleTrack("zh-CN", SOURCE_AUTOMATIC_SUBTITLE)]
        track, _reason = subtitles.select_track(tracks, mode=subtitles.MODE_SUBTITLE_ONLY)
        self.assertIsNotNone(track)
        self.assertEqual(track.source_type, SOURCE_AUTOMATIC_SUBTITLE)

    def test_whisper_mode_selects_nothing(self):
        tracks = [subtitles.SubtitleTrack("zh-Hans", SOURCE_HUMAN_SUBTITLE)]
        track, reason = subtitles.select_track(tracks, mode=subtitles.MODE_WHISPER_ONLY)
        self.assertIsNone(track)
        self.assertIn("强制 Whisper", reason)


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload if self._payload is not None else {}


class FakeSession:
    """模拟 B站接口：分P列表、字幕列表、字幕文件。"""

    def __init__(self, pages, subtitles, subtitle_text):
        self.pages = pages
        self.subtitles = subtitles
        self.subtitle_text = subtitle_text
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append((url, dict(params or {})))
        if "pagelist" in url:
            return FakeResponse({"code": 0, "data": self.pages})
        if "wbi/v2" in url or "player/v2" in url:
            return FakeResponse(
                {"code": 0, "data": {"subtitle": {"subtitles": self.subtitles}}}
            )
        return FakeResponse(text=self.subtitle_text)


def bilibili_ref(page=33):
    return VideoRef(
        platform="bilibili",
        video_id="BV1vwME6AEpd",
        canonical_url=f"https://www.bilibili.com/video/BV1vwME6AEpd?p={page}",
        cache_key=f"BV1vwME6AEpd_p{page}",
        page=page,
        title_hint="BV1vwME6AEpd",
    )


class BilibiliPageIsolationTests(unittest.TestCase):
    def _pages(self):
        return [
            {"cid": 111, "page": 1, "part": "第1P", "duration": 100},
            {"cid": 222, "page": 2, "part": "第2P", "duration": 100},
            {"cid": 333, "page": 33, "part": "第33P", "duration": 413},
        ]

    def test_discovers_subtitles_by_target_cid_not_first_page(self):
        session = FakeSession(
            self._pages(),
            [{"lan": "zh-CN", "type": 0, "lan_doc": "中文（中国）", "subtitle_url": "//sub/333.json"}],
            BILI_SAMPLE,
        )
        with mock.patch.object(subtitles, "_requests_session", return_value=session), \
             mock.patch.object(subtitles, "_bilibili_wbi_query", return_value="bvid=BV1vwME6AEpd&cid=333"), \
             mock.patch.object(subtitles, "bilibili_page_meta", return_value={
                 "cid": 333, "page": 33, "title": "第33P", "duration_ms": 413000, "total_pages": 3
             }):
            tracks, meta = subtitles.discover_bilibili("BV1vwME6AEpd", 33)

        self.assertEqual(meta["cid"], 333)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].source_type, SOURCE_HUMAN_SUBTITLE)
        # 查询路径必须带目标分P的 cid，且不能出现第1P的 cid
        self.assertIn("cid=333", session.calls[0][0])
        self.assertNotIn("cid=111", session.calls[0][0])

    def test_ai_subtitle_is_marked_automatic(self):
        session = FakeSession(
            self._pages(),
            [{"lan": "ai-zh", "type": 0, "lan_doc": "中文（自动生成）", "subtitle_url": "//sub/ai.json"}],
            BILI_SAMPLE,
        )
        with mock.patch.object(subtitles, "_requests_session", return_value=session), \
             mock.patch.object(subtitles, "_bilibili_wbi_query", return_value="cid=333"), \
             mock.patch.object(subtitles, "bilibili_page_meta", return_value={
                 "cid": 333, "page": 33, "title": "第33P", "duration_ms": 413000, "total_pages": 3
             }):
            tracks, _meta = subtitles.discover_bilibili("BV1vwME6AEpd", 33)
        self.assertEqual(tracks[0].source_type, SOURCE_AUTOMATIC_SUBTITLE)


    def test_normal_language_without_type_remains_unknown(self):
        session = FakeSession(self._pages(), [{
            "lan": "zh-CN", "lan_doc": "中文", "subtitle_url": "//sub/333.json"
        }], BILI_SAMPLE)
        with mock.patch.object(subtitles, "_requests_session", return_value=session), \
             mock.patch.object(subtitles, "_bilibili_wbi_query", return_value="cid=333"), \
             mock.patch.object(subtitles, "bilibili_page_meta", return_value={
                 "cid": 333, "page": 33, "title": "第33P", "duration_ms": 413000
             }):
            tracks, _meta = subtitles.discover_bilibili("BV1vwME6AEpd", 33)
        self.assertEqual(tracks[0].source_type, SOURCE_UNKNOWN_SUBTITLE)
        chosen, _reason = subtitles.select_track(tracks, mode=subtitles.MODE_SMART)
        self.assertIsNone(chosen)

    def test_non_ai_language_with_type_one_is_automatic(self):
        session = FakeSession(self._pages(), [{
            "lan": "zh-CN", "type": 1, "subtitle_url": "//sub/333.json"
        }], BILI_SAMPLE)
        with mock.patch.object(subtitles, "_requests_session", return_value=session), \
             mock.patch.object(subtitles, "_bilibili_wbi_query", return_value="cid=333"), \
             mock.patch.object(subtitles, "bilibili_page_meta", return_value={
                 "cid": 333, "page": 33, "title": "第33P", "duration_ms": 413000
             }):
            tracks, _meta = subtitles.discover_bilibili("BV1vwME6AEpd", 33)
        self.assertEqual(tracks[0].source_type, SOURCE_AUTOMATIC_SUBTITLE)

    def test_wbi_error_does_not_trust_legacy_unsigned_endpoint(self):
        class DeniedSession(FakeSession):
            def get(self, url, params=None, headers=None, timeout=None, **kwargs):
                self.calls.append((url, dict(params or {})))
                return FakeResponse({"code": -400, "message": "no access"})
        session = DeniedSession(self._pages(), [], BILI_SAMPLE)
        with mock.patch.object(subtitles, "_requests_session", return_value=session), \
             mock.patch.object(subtitles, "_bilibili_wbi_query", return_value="cid=333"), \
             mock.patch.object(subtitles, "bilibili_page_meta", return_value={
                 "cid": 333, "page": 33, "title": "第33P", "duration_ms": 413000
             }):
            with self.assertRaises(subtitles.SubtitleError):
                subtitles.discover_bilibili("BV1vwME6AEpd", 33)
        self.assertTrue(session.calls)
        self.assertTrue(all("/x/player/v2" not in url for url, _ in session.calls))


class ResolveTests(unittest.TestCase):
    def test_no_subtitle_falls_back_in_smart_mode(self):
        with mock.patch.object(subtitles, "discover_ytdlp", return_value=([], {"title": "t", "duration_ms": 1000})):
            selection = subtitles.resolve_subtitles(
                VideoRef("youtube", "abc", "https://youtu.be/abc", "yt_abc", None, "abc"),
                mode=subtitles.MODE_SMART,
            )
        self.assertIsNone(selection.document)
        self.assertIn("没有发现", selection.fallback_reason)

    def test_subtitle_only_mode_raises_instead_of_whisper(self):
        with mock.patch.object(subtitles, "discover_ytdlp", return_value=([], {"title": "t"})):
            with self.assertRaises(subtitles.SubtitleNotFoundError):
                subtitles.resolve_subtitles(
                    VideoRef("youtube", "abc", "https://youtu.be/abc", "yt_abc", None, "abc"),
                    mode=subtitles.MODE_SUBTITLE_ONLY,
                )

    def test_incomplete_subtitle_does_not_count_as_success(self):
        track = subtitles.SubtitleTrack("zh-Hans", SOURCE_HUMAN_SUBTITLE, "vtt", "u")
        parsed = subtitles.ParsedSubtitle(cues=[(0, 1000, "只有一秒")])
        with mock.patch.object(
            subtitles, "discover_ytdlp",
            return_value=([track], {"title": "t", "duration_ms": 600000}),
        ), mock.patch.object(subtitles, "fetch_ytdlp_subtitle", return_value=parsed):
            selection = subtitles.resolve_subtitles(
                VideoRef("youtube", "abc", "https://youtu.be/abc", "yt_abc", None, "abc"),
                mode=subtitles.MODE_SMART,
            )
        self.assertIsNone(selection.document)
        self.assertIn("不完整", selection.fallback_reason)

    def test_incomplete_subtitle_raises_in_subtitle_only_mode(self):
        track = subtitles.SubtitleTrack("zh-Hans", SOURCE_HUMAN_SUBTITLE, "vtt", "u")
        parsed = subtitles.ParsedSubtitle(cues=[(0, 1000, "只有一秒")])
        with mock.patch.object(
            subtitles, "discover_ytdlp",
            return_value=([track], {"title": "t", "duration_ms": 600000}),
        ), mock.patch.object(subtitles, "fetch_ytdlp_subtitle", return_value=parsed):
            with self.assertRaises(subtitles.SubtitleParseError):
                subtitles.resolve_subtitles(
                    VideoRef("youtube", "abc", "https://youtu.be/abc", "yt_abc", None, "abc"),
                    mode=subtitles.MODE_SUBTITLE_ONLY,
                )

    def test_successful_human_subtitle_becomes_document(self):
        track = subtitles.SubtitleTrack("zh-Hans", SOURCE_HUMAN_SUBTITLE, "vtt", "u", evidence="测试")
        parsed = subtitles.ParsedSubtitle(cues=[(0, 4000, "第一句"), (4000, 8000, "第二句")])
        with mock.patch.object(
            subtitles, "discover_ytdlp",
            return_value=([track], {"title": "t", "duration_ms": 8000}),
        ), mock.patch.object(subtitles, "fetch_ytdlp_subtitle", return_value=parsed):
            selection = subtitles.resolve_subtitles(
                VideoRef("youtube", "abc", "https://youtu.be/abc", "yt_abc", None, "abc"),
                mode=subtitles.MODE_SMART,
            )
        self.assertIsNotNone(selection.document)
        document = selection.document
        self.assertEqual(document.source_type, SOURCE_HUMAN_SUBTITLE)
        self.assertEqual([segment.id for segment in document.segments], ["seg_0001", "seg_0002"])
        self.assertEqual(document.segments[0].time_precision, "exact")

    def test_whisper_mode_never_touches_subtitles(self):
        with mock.patch.object(subtitles, "discover_ytdlp", side_effect=AssertionError("不应调用")):
            selection = subtitles.resolve_subtitles(
                VideoRef("youtube", "abc", "https://youtu.be/abc", "yt_abc", None, "abc"),
                mode=subtitles.MODE_WHISPER_ONLY,
            )
        self.assertIsNone(selection.document)


class LocalSubtitleOnlyTests(unittest.TestCase):
    def test_cli_local_file_subtitle_only_never_calls_whisper(self):
        from tools import v2_e2e
        with tempfile.TemporaryDirectory() as folder:
            ref = VideoRef(
                platform="local", video_id="local-demo",
                canonical_url="/tmp/local_demo.mp4", cache_key="local_demo",
                page=None, title_hint="本地样例"
            )
            argv = ["v2_e2e.py", "--url", "/tmp/local_demo.mp4", "--mode", "subtitle-only",
                    "--output-root", folder]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(v2_e2e, "resolve_video_input", return_value=ref), \
                 mock.patch.object(v2_e2e, "run_whisper", side_effect=AssertionError("Whisper 不得运行")):
                self.assertEqual(v2_e2e.main(), 2)


if __name__ == "__main__":
    unittest.main()
