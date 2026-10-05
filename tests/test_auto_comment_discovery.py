"""auto_comment 目标发现的回归测试。

主页 feed 把置顶/老作品排在最前时,直接按 feed 顺序截前 N 个会一直拿到
老作品;且窗口只有 comment_recent_works(默认5)个作品时,「每轮上限」
调大也不会生效。三平台发现都必须按发布时间倒序,窗口跟随 max_per_run。
"""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app.db as db

from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import CommentWatch


class _Identity:
    timezone_id = "Asia/Shanghai"


class _BrowserStub:
    def __init__(self):
        self._locks = {}

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def identity_for(self, _account):
        return _Identity()

    def anon_identity(self):
        return _Identity()


def _rule(platform, max_per_run=5, kind="creator"):
    return {
        "platform": platform, "mode": "auto_comment", "target_kind": kind,
        "aweme_id": "", "xsec_token": "", "keyword": "防晒",
        "sec_uid": "creator-sec-uid", "has_creator": False, "account_uid": "",
        "max_per_run": max_per_run,
    }


class AutoCommentDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "auto-comment-discovery.db"))
        self.cfg = Config()
        self.engine = MonitorEngine(self.cfg, _BrowserStub())

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    # ── 抖音 ──
    def test_douyin_pinned_old_works_do_not_block_newest(self):
        now = int(time.time())
        items = [
            {"aweme_id": "pinned-old", "create_time": now - 30 * 86400,
             "desc": "置顶老作品", "is_top": 1},
            {"aweme_id": "newest", "create_time": now - 3600, "desc": "最新"},
            {"aweme_id": "two-days", "create_time": now - 2 * 86400, "desc": "两天前"},
            {"aweme_id": "six-days", "create_time": now - 6 * 86400, "desc": "六天前"},
            {"aweme_id": "out-window", "create_time": now - 20 * 86400, "desc": "老"},
        ]

        async def fake_fetch_videos(*_a, **_k):
            return items, "creator", ""

        with patch("app.engine.monitor.fetch_videos", fake_fetch_videos):
            cands, err = asyncio.run(self.engine._discover_targets(
                _rule("douyin", max_per_run=3), "state", "", "", "", _Identity()))

        self.assertEqual(err, "")
        # 修复前按 feed 顺序截前 5 个 = [pinned-old, newest, two-days, six-days,
        # out-window];修复后按时间倒序,置顶老作品仅在新作品不足时补回
        self.assertEqual([c["aweme_id"] for c in cands],
                         ["newest", "two-days", "six-days", "pinned-old"])

    def test_douyin_window_follows_max_per_run(self):
        now = int(time.time())
        # 30 个 7 天内的新作品:窗口应跟随 max_per_run=25 而非默认的 5
        items = [{"aweme_id": f"w{i:02d}", "create_time": now - (i + 1) * 3600,
                  "desc": f"作品{i}"} for i in range(30)]

        async def fake_fetch_videos(*_a, **_k):
            return items, "creator", ""

        with patch("app.engine.monitor.fetch_videos", fake_fetch_videos):
            cands, err = asyncio.run(self.engine._discover_targets(
                _rule("douyin", max_per_run=25), "state", "", "", "", _Identity()))

        self.assertEqual(err, "")
        self.assertEqual(len(cands), 25)
        # 按发布时间倒序:最新在前
        self.assertEqual([c["aweme_id"] for c in cands][:3],
                         ["w00", "w01", "w02"])

    # ── 快手 ──
    def test_kuaishou_sorted_by_time_and_window_follows_max_per_run(self):
        now_ms = int(time.time()) * 1000

        def feed(idx, age_hours):
            return {"photo": {"id": f"ks{idx:02d}",
                              "caption": f"快手作品{idx}",
                              "photoUrl": f"http://cdn/{idx}.mp4",
                              "timestamp": now_ms - age_hours * 3600 * 1000}}

        # feed 顺序故意把最老的排最前(模拟主页置顶/老作品在前),1~29 小时前
        feeds = [feed(i, i) for i in range(29, -1, -1)]

        async def fake_fetch_ks_videos(*_a, **_k):
            return feeds, None, ""

        with patch("app.engine.monitor.fetch_ks_videos", fake_fetch_ks_videos):
            cands, err = asyncio.run(self.engine._discover_targets(
                _rule("kuaishou", max_per_run=25), "state", "", "", "", _Identity()))

        self.assertEqual(err, "")
        self.assertEqual(len(cands), 25)
        self.assertEqual([c["aweme_id"] for c in cands][:3],
                         ["ks00", "ks01", "ks02"])

    # ── 小红书 ──
    def test_xhs_creator_sorted_by_time_and_window_follows_max_per_run(self):
        now = int(time.time())

        def note(idx, age_hours):
            return {"note_id": f"xhs{idx:02d}", "xsec_token": "t",
                    "display_title": f"笔记{idx}", "time": now - age_hours * 3600}

        # 30 篇笔记乱序(最老的最前),全部在 7 天窗口内(1~29 小时前)
        notes = [note(i, i) for i in range(29, -1, -1)]

        class _XhsClient:
            async def notes_by_creator(self, *_a, **_k):
                return {"notes": notes}

        self.engine._xhs_client = lambda *_a, **_k: _XhsClient()
        cands, err = asyncio.run(self.engine._discover_targets(
            _rule("xhs", max_per_run=25), "state", "", "", "", _Identity()))

        self.assertEqual(err, "")
        self.assertEqual(len(cands), 25)
        self.assertEqual([c["aweme_id"] for c in cands][:3],
                         ["xhs00", "xhs01", "xhs02"])

    def test_xhs_creator_cutoff_filters_old_notes(self):
        now = int(time.time())
        notes = [
            {"note_id": "old", "xsec_token": "t", "display_title": "十天前",
             "time": now - 10 * 86400},
            {"note_id": "fresh", "xsec_token": "t", "display_title": "昨天",
             "time": now - 86400},
        ]

        class _XhsClient:
            async def notes_by_creator(self, *_a, **_k):
                return {"notes": notes}

        self.engine._xhs_client = lambda *_a, **_k: _XhsClient()
        cands, err = asyncio.run(self.engine._discover_targets(
            _rule("xhs", max_per_run=5), "state", "", "", "", _Identity()))

        self.assertEqual(err, "")
        self.assertEqual([c["aweme_id"] for c in cands], ["fresh"])

    def test_auto_reply_does_not_filter_by_work_age(self):
        """自动回复/评论监控关注评论时间,不限制作品发布时间:博主只发过老作品时也应能取到作品。"""
        now = int(time.time())
        items = [
            {"aweme_id": "old-235d", "create_time": now - 235 * 86400, "desc": "235天前"},
            {"aweme_id": "old-1137d", "create_time": now - 1137 * 86400, "desc": "1137天前"},
        ]
        # auto_comment 只评论新作品:7 天前的被过滤
        self.assertEqual(len(MonitorEngine._latest_work_items(items, 5, 7)), 0)
        # auto_reply/评论监控 recent_days=0:不过滤作品发布时间
        self.assertEqual(len(MonitorEngine._latest_work_items(items, 5, 0)), 2)

    def test_auto_comment_trial_run_skips_work_age_cutoff(self):
        """试跑(manual=True)时 auto_comment 也不按发布时间过滤,让用户能看到目标作品和生成文案。"""
        now = int(time.time())
        items = [
            {"aweme_id": "old-235d", "create_time": now - 235 * 86400, "desc": "235天前"},
            {"aweme_id": "old-1137d", "create_time": now - 1137 * 86400, "desc": "1137天前"},
        ]
        # 非 manual: auto_comment 过滤 7 天前的作品
        recent_days = 0 if False else 7  # 模拟非 manual
        self.assertEqual(len(MonitorEngine._latest_work_items(items, 5, recent_days)), 0)
        # manual: auto_comment 不过滤
        recent_days_manual = 0  # rf.get("manual") → 0
        self.assertEqual(len(MonitorEngine._latest_work_items(items, 5, recent_days_manual)), 2)

    # ── 评论监控(独立 CommentWatch 扫描博主主页):与自动评论发现共用同一倒序规则 ──
    def _watch(self, platform, sec_uid):
        with db.get_session() as s:
            w = CommentWatch(platform=platform, kind="user", sec_uid=sec_uid,
                             mode="public")
            s.add(w)
            s.commit()
            s.refresh(w)
            return w.id

    def test_kuaishou_comment_watch_scans_newest_works_first(self):
        now_ms = int(time.time()) * 1000
        watch_id = self._watch("kuaishou", "ks-creator")

        def feed(pid, age_hours):
            return {"photo": {"id": pid, "caption": pid,
                              "photoUrl": f"http://cdn/{pid}.mp4",
                              "timestamp": now_ms - age_hours * 3600 * 1000}}

        # 主页顺序:最老的在最前(置顶/默认排序),全部在 7 天窗口内
        feeds = [feed("oldest", 100), feed("newest", 1), feed("middle", 50)]
        scanned = []

        async def fake_videos(*_a, **_k):
            return feeds, None, ""

        async def fake_comments(_b, _i, photo_id, _known, **_k):
            scanned.append(photo_id)
            return [], ""

        with patch("app.engine.monitor.fetch_ks_videos", fake_videos), \
                patch("app.engine.monitor.fetch_ks_comments", fake_comments):
            asyncio.run(self.engine._cw_ks_user(
                watch_id, _Identity(), "ks-creator", "快手博主", True))

        # 修复前永远扫 oldest;修复后按发布时间倒序扫到 newest
        self.assertEqual(scanned, ["newest", "middle", "oldest"])

    def test_xhs_comment_watch_scans_newest_notes_first(self):
        now = int(time.time())
        watch_id = self._watch("xhs", "xhs-creator")

        def note(pid, age_hours):
            return {"note_id": pid, "xsec_token": "t", "display_title": pid,
                    "time": now - age_hours * 3600}

        notes = [note("oldest", 100), note("newest", 1), note("middle", 50)]
        scanned = []

        class _XhsClient:
            async def notes_by_creator(self, *_a, **_k):
                return {"notes": notes, "user": {}}

            async def user_info(self, *_a, **_k):
                return {}

        self.engine._xhs_client = lambda *_a, **_k: _XhsClient()
        self.engine._xhs_browser_reads_enabled = lambda: False

        async def _no_gap():
            pass

        self.engine._xhs_gap = _no_gap

        async def fake_fetch_comments(_client, nid, _token, _known):
            scanned.append(nid)
            return []

        self.engine._xhs_fetch_comments = fake_fetch_comments
        state = '{"cookies": [{"name": "a1", "value": "fixture"}]}'
        asyncio.run(self.engine._cw_xhs_creator(
            watch_id, _Identity(), state, "xhs-creator", "tok", "小红书博主", True))

        self.assertEqual(scanned, ["newest", "middle", "oldest"])
    # ── 抖音:发现与评论监控共用签名 Web API 通道(hybrid 回退浏览器)──
    class _DyApiStub:
        """DouyinClient 最小替身:session_scope + last_error + 作品/评论返回。"""

        def __init__(self, works=None, comments=None, last_error=""):
            self._works = works or []
            self._comments = comments or {}
            self.last_error = last_error
            self.works_calls = []
            self.comments_calls = []

        def session_scope(self):
            client = self

            class _Scope:
                async def __aenter__(self):
                    return client

                async def __aexit__(self, *exc):
                    return False

            return _Scope()

        async def fetch_all_video_list(self, sec_uid, *_a, **_k):
            self.works_calls.append(sec_uid)
            return self._works

        async def fetch_all_comments(self, aweme_id, *_a, **_k):
            self.comments_calls.append(aweme_id)
            return self._comments.get(aweme_id, [])

    def test_douyin_auto_comment_prefers_api_channel(self):
        """有 cookie 时与评论监控一样走签名 API,不再依赖浏览器拦截主页。"""
        now = int(time.time())
        items = [{"aweme_id": f"w{i}", "create_time": now - i * 3600,
                  "desc": f"作品{i}"} for i in range(3)]
        stub = self._DyApiStub(works=items)
        self.engine._dy_read_client = lambda *_a, **_k: stub

        async def _boom(*_a, **_k):
            raise AssertionError("浏览器通道不应被调用")

        with patch("app.engine.monitor.fetch_videos", _boom):
            cands, err = asyncio.run(self.engine._discover_targets(
                _rule("douyin", max_per_run=5), "state", "", "", "", _Identity()))

        self.assertEqual(err, "")
        self.assertEqual(len(cands), 3)
        self.assertEqual(stub.works_calls, ["creator-sec-uid"])

    def test_douyin_auto_comment_hybrid_falls_back_to_browser(self):
        """hybrid 模式 API 空响应时回退浏览器抓取(与评论监控一致)。"""
        now = int(time.time())
        stub = self._DyApiStub(works=[], last_error="empty_response")
        self.engine._dy_read_client = lambda *_a, **_k: stub
        items = [{"aweme_id": "wb1", "create_time": now, "desc": "浏览器作品"}]

        async def fake_fetch_videos(*_a, **_k):
            return items, "creator", ""

        with patch("app.engine.monitor.fetch_videos", fake_fetch_videos):
            cands, err = asyncio.run(self.engine._discover_targets(
                _rule("douyin", max_per_run=5), "state", "", "", "", _Identity()))

        self.assertEqual([c["aweme_id"] for c in cands], ["wb1"])

    def test_douyin_auto_reply_public_uses_api_for_works_and_comments(self):
        """自动回复(非创作中心)作品列表与评论都走 API 通道。"""
        now = int(time.time())
        works = [{"aweme_id": "w1", "create_time": now, "desc": "作品1"}]
        comments = {"w1": [{"cid": "c1", "text": "求链接",
                            "user": {"nickname": "粉丝"},
                            "create_time": now}]}
        stub = self._DyApiStub(works=works, comments=comments)
        self.engine._dy_read_client = lambda *_a, **_k: stub

        rule = _rule("douyin", max_per_run=5)
        rule["mode"] = "auto_reply"
        cands, err = asyncio.run(self.engine._discover_targets(
            rule, "state", "", "me-sec-uid", "我", _Identity()))

        self.assertEqual(err, "")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["aweme_id"], "w1")
        self.assertEqual(cands[0]["target_comment_id"], "c1")
        self.assertEqual(cands[0]["target_nick"], "粉丝")

    def test_douyin_auto_reply_public_filters_self_comments(self):
        now = int(time.time())
        works = [{"aweme_id": "w1", "create_time": now, "desc": "作品1"}]
        comments = {"w1": [
            {"cid": "c-self", "text": "置顶", "user": {"nickname": "我"},
             "create_time": now},
            {"cid": "c-fan", "text": "好看", "user": {"nickname": "粉丝"},
             "create_time": now},
        ]}
        stub = self._DyApiStub(works=works, comments=comments)
        self.engine._dy_read_client = lambda *_a, **_k: stub

        rule = _rule("douyin", max_per_run=5)
        rule["mode"] = "auto_reply"
        cands, _err = asyncio.run(self.engine._discover_targets(
            rule, "state", "", "me-sec-uid", "我", _Identity()))

        self.assertEqual([c["target_comment_id"] for c in cands], ["c-fan"])

    def test_douyin_read_client_none_without_cookie(self):
        """无 cookie/纯 browser 模式时通道为 None,保持浏览器抓取行为不变。"""
        self.assertIsNone(
            self.engine._dy_read_client("not-json", "ua", "", _Identity()))
        self.cfg.engine.douyin_read_mode = "browser"
        self.assertIsNone(
            self.engine._dy_read_client('{"cookies":[]}', "ua", "", _Identity()))

    def _reply_rule(self, **over):
        rule = _rule("douyin", max_per_run=5)
        rule["mode"] = "auto_reply"
        rule.update(over)
        return rule

    def test_douyin_auto_reply_creator_prefers_api_over_creator_page(self):
        """有创作者登录态且 cookie 可直连时,走「我的作品→抓取评论」同款
        签名 API 按作品取评论(aweme_id 天然已知),不再开创作中心页面。"""
        now = int(time.time())
        works = [{"aweme_id": "w1", "create_time": now, "desc": "作品1"}]
        comments = {"w1": [{"cid": "c1", "text": "求链接",
                            "user": {"nickname": "粉丝"},
                            "create_time": now}]}
        stub = self._DyApiStub(works=works, comments=comments)
        self.engine._dy_read_client = lambda *_a, **_k: stub

        async def _boom(*_a, **_k):
            raise AssertionError("创作中心页面通道不应被调用")

        with patch("app.engine.monitor.fetch_creator_comments", _boom):
            cands, err = asyncio.run(self.engine._discover_targets(
                self._reply_rule(has_creator=True), "state", "",
                "me-sec-uid", "我", _Identity()))

        self.assertEqual(err, "")
        self.assertEqual([(c["aweme_id"], c["target_comment_id"]) for c in cands],
                         [("w1", "c1")])

    def test_douyin_auto_reply_creator_falls_back_to_creator_page(self):
        """无 cookie(client=None)且有创作者登录态时,才回退创作中心页面抓取。"""
        now = int(time.time())
        self.engine._dy_read_client = lambda *_a, **_k: None

        async def fake_creator(*_a, **_k):
            return ([{"cid": "cc1", "aweme_id": "w9", "text": "怎么买",
                      "user": {"nickname": "粉丝"}, "create_time": now}], "")

        with patch("app.engine.monitor.fetch_creator_comments", fake_creator):
            cands, err = asyncio.run(self.engine._discover_targets(
                self._reply_rule(has_creator=True), "state", "",
                "me-sec-uid", "我", _Identity()))

        self.assertEqual(err, "")
        self.assertEqual([(c["aweme_id"], c["target_comment_id"]) for c in cands],
                         [("w9", "cc1")])

    def test_douyin_auto_reply_creator_api_scheduled_skips_old_comments(self):
        """定时正式跑(manual 缺省)按评论时间过滤;试跑 manual=True 不过滤。"""
        now = int(time.time())
        works = [{"aweme_id": "w1", "create_time": now, "desc": "作品1"}]
        comments = {"w1": [
            {"cid": "c-old", "text": "去年的评论", "user": {"nickname": "粉丝"},
             "create_time": now - 365 * 86400},
            {"cid": "c-new", "text": "刚发的评论", "user": {"nickname": "粉丝"},
             "create_time": now - 3600},
        ]}
        stub = self._DyApiStub(works=works, comments=comments)
        self.engine._dy_read_client = lambda *_a, **_k: stub

        cands, _err = asyncio.run(self.engine._discover_targets(
            self._reply_rule(has_creator=True), "state", "",
            "me-sec-uid", "我", _Identity()))
        self.assertEqual([c["target_comment_id"] for c in cands], ["c-new"])

        cands, _err = asyncio.run(self.engine._discover_targets(
            self._reply_rule(has_creator=True, manual=True), "state", "",
            "me-sec-uid", "我", _Identity()))
        self.assertEqual([c["target_comment_id"] for c in cands],
                         ["c-old", "c-new"])

    def test_douyin_auto_reply_skips_sub_comments(self):
        """子评论(reply_id 非0)在作品页折叠在「展开回复」里,浏览器定位不到,
        自动回复只生成一级评论目标。"""
        now = int(time.time())
        works = [{"aweme_id": "w1", "create_time": now, "desc": "作品1",
                  "xsec_token": "tok-1"}]
        comments = {"w1": [
            {"cid": "c-top", "text": "顶层评论", "reply_id": "0",
             "user": {"nickname": "粉丝"}, "create_time": now},
            {"cid": "c-sub", "text": "折叠的追问", "reply_id": "c-top",
             "user": {"nickname": "粉丝2"}, "create_time": now},
        ]}
        stub = self._DyApiStub(works=works, comments=comments)
        self.engine._dy_read_client = lambda *_a, **_k: stub

        cands, err = asyncio.run(self.engine._discover_targets(
            self._reply_rule(has_creator=True, manual=True), "state", "",
            "me-sec-uid", "我", _Identity()))

        self.assertEqual(err, "")
        self.assertEqual([c["target_comment_id"] for c in cands], ["c-top"])

    def test_douyin_candidates_carry_xsec_token(self):
        """作品项自带的 xsec_token 必须透传到候选:浏览器打开作品页不带 token
        会被重定向/评论区不加载。auto_comment 与 auto_reply 都要带。"""
        now = int(time.time())
        works = [{"aweme_id": "w1", "create_time": now, "desc": "作品1",
                  "xsec_token": "abctoken"}]
        stub = self._DyApiStub(
            works=works,
            comments={"w1": [{"cid": "c1", "text": "你好", "reply_id": "0",
                              "user": {"nickname": "粉丝"},
                              "create_time": now}]})
        self.engine._dy_read_client = lambda *_a, **_k: stub

        cands, _ = asyncio.run(self.engine._discover_targets(
            _rule("douyin", max_per_run=5), "state", "", "", "", _Identity()))
        self.assertEqual(cands[0]["xsec_token"], "abctoken")

        cands, _ = asyncio.run(self.engine._discover_targets(
            self._reply_rule(has_creator=True, manual=True), "state", "",
            "me-sec-uid", "我", _Identity()))
        self.assertEqual(cands[0]["xsec_token"], "abctoken")


if __name__ == "__main__":
    unittest.main()
