"""Task 10: TikTok 自动评论/回复浏览器证据化与流水线测试。

覆盖三层:
1. comment_tiktok_browser 本体页面流程(桩页面):空参数/登录墙/
   回包成功/回包拒绝/回包不可解析/DOM佐证成功与失败/定位失败/发送按钮不可用;
2. _resolve_rule_target 对 tiktok 的 creator/work/keyword 解析与拒绝;
3. 引擎 _discover_targets / _execute_comment_task_locked tiktok 分支:
   发现排序截断/自评过滤/一级评论过滤/manual不过滤/RISK直通/三态裁决。
"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app.db as db
import app.main as main
from app.config import Config, EngineConfig
from app.engine.monitor import MonitorEngine
from app.models import CommentRule, CommentTask, DouyinAccount
from app.platforms.tiktok import auto_comment as ttac
from app.platforms.tiktok.auto_comment import (
    comment_tiktok_browser,
    tiktok_video_url,
)
from sqlmodel import select


# ── 纯函数 ─────────────────────────────────────────────────────────────

class UrlTests(unittest.TestCase):
    def test_with_handle(self):
        self.assertEqual(
            tiktok_video_url("741234567890", "camper"),
            "https://www.tiktok.com/@camper/video/741234567890")

    def test_without_handle(self):
        self.assertEqual(
            tiktok_video_url("741234567890"),
            "https://www.tiktok.com/video/741234567890")

    def test_handle_strips_at(self):
        self.assertEqual(
            tiktok_video_url("741234567890", "@camper"),
            "https://www.tiktok.com/@camper/video/741234567890")


# ── 桩页面(评论专用)─────────────────────────────────────────────────────

class _FakeTime:
    """假时钟:wait_for_timeout 推进它,8s 回包等待不耗真实时间。"""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def advance(self, ms):
        self.now += ms / 1000.0


class _Resp:
    def __init__(self, url, payload, status=200):
        self.url = url
        self.status = status
        self._payload = payload

    async def text(self):
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)

    async def json(self):
        return self._payload


class _Kb:
    def __init__(self, page):
        self._page = page

    async def press(self, key):
        self._page.events.append(("key", key))

    async def type(self, text, delay=0):
        self._page.events.append(("type", text))


class _Loc:
    def __init__(self, page, label):
        self._page = page
        self._label = label

    @property
    def first(self):
        return self

    async def count(self):
        sc = self._page.scenario
        if "comment-input" in self._label:
            return 1 if sc.editor_ready else 0
        if "data-mmm-editor" in self._label:
            return 1 if (sc.editor_ready or sc.reply_editor_ready) else 0
        return 1

    async def click(self, timeout=None, force=False):
        self._page.events.append(("click", self._label))
        if "data-mmm-send" in self._label:
            await self._page._emit_responses()

    async def fill(self, text, timeout=None):
        self._page.events.append(("fill", text))

    async def press(self, key):
        self._page.events.append(("key", key))

    def locator(self, sub, **_kw):
        return _Loc(self._page, f"{self._label}>{sub}")

    def get_by_text(self, text, exact=False):
        return _Loc(self._page, f"text={text}")


class _Page:
    def __init__(self, scenario):
        self.scenario = scenario
        self.handlers = {}
        self.events = []
        self.url = scenario.url
        self.keyboard = _Kb(self)
        self.closed = False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def goto(self, url, wait_until=None, timeout=None):
        self.events.append(("goto", url))
        self.url = self.scenario.url

    async def wait_for_load_state(self, *a, **kw):
        return None

    async def wait_for_timeout(self, ms):
        self.scenario.clock.advance(ms)
        await asyncio.sleep(0)

    def locator(self, sel):
        return _Loc(self, sel)

    def get_by_text(self, text, exact=False):
        return _Loc(self, f"text={text}")

    async def inner_text(self, sel="body"):
        return ""

    async def evaluate(self, js, arg=None):
        sc = self.scenario
        if js is ttac._TT_FIND_VISIBLE_EDITOR:
            return {"editor": sc.editor_ready, "items": sc.items}
        if js is ttac._TT_FIND_COMMENT_ITEM:
            if sc.find_result:
                return list(sc.find_result)
            return [False, sc.items]
        if js is ttac._TT_SCROLL_COMMENTS:
            return True
        if js is ttac._TT_FIND_REPLY_BUTTON:
            return {"found": True, "how": "e2e"}
        if js is ttac._TT_FIND_REPLY_EDITOR:
            return {"found": sc.reply_editor_ready, "how": "inline-target"}
        if js is ttac._TT_FIND_SEND_BUTTON:
            return {"found": True, "enabled": sc.send_enabled}
        if js is ttac._TT_VERIFY_COMMENT_POST:
            return sc.verify_result or {"posted": False, "settled": False,
                                        "alive": False, "empty": True}
        if js is ttac._TT_DIAG_INPUTS:
            return ""
        if js is ttac._TT_FIND_EXPAND_REPLIES:
            return {"found": False}
        return None     # scrollIntoView / clear-mark 等

    async def close(self):
        self.closed = True

    async def _emit_responses(self):
        handler = self.handlers.get("response")
        if not handler:
            return
        for url, payload in self.scenario.responses:
            await handler(_Resp(url, payload))


class _Ctx:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page

    async def close(self):
        self.page.closed = True


class _Mgr:
    def __init__(self, page):
        self.page = page

    async def open_headed(self, identity):
        return _Ctx(self.page)


class _Scenario:
    """脚本化评论页:状态集 + evaluate/click 时按剧本返回。"""

    def __init__(self, *, url="https://www.tiktok.com/video/123456",
                 editor_ready=True, items=0, find_result=None,
                 reply_editor_ready=False, send_enabled=True,
                 verify_result=None, responses=None):
        self.url = url
        self.clock = _FakeTime()
        self.editor_ready = editor_ready
        self.items = items
        self.find_result = find_result   # (hit, count, why)
        self.reply_editor_ready = reply_editor_ready
        self.send_enabled = send_enabled
        self.verify_result = verify_result
        self.responses = responses or []


def run_comment(scenario, *, content="hello", aweme_id="123456",
                reply_to_text="", target_nick="", target_cid="",
                require_reply=False, on_submit=None):
    page = _Page(scenario)
    mgr = _Mgr(page)

    async def main():
        with patch.object(ttac, "time", scenario.clock):
            return await comment_tiktok_browser(
                mgr, None, aweme_id, content,
                reply_to_text=reply_to_text,
                target_nick=target_nick,
                target_cid=target_cid,
                require_reply=require_reply,
                headed=True, settle_ms=1,
                verify_wait_seconds=2,
                on_submit=on_submit)

    return asyncio.run(main()), page


# ── comment_tiktok_browser 本体桩测试 ───────────────────────────────────

class CommentBrowserFlowTests(unittest.TestCase):
    def test_empty_content(self):
        ok, err = asyncio.run(comment_tiktok_browser(None, None, "123", ""))
        self.assertFalse(ok)
        self.assertEqual(err, "空文案")

    def test_missing_aweme_id(self):
        ok, err = asyncio.run(comment_tiktok_browser(None, None, "", "hi"))
        self.assertFalse(ok)
        self.assertEqual(err, "missing_aweme_id")

    def test_require_reply_without_text(self):
        ok, err = asyncio.run(
            comment_tiktok_browser(None, None, "123", "hi",
                                   require_reply=True))
        self.assertFalse(ok)
        self.assertIn("缺少目标评论原文", err)

    def test_logged_out_redirect(self):
        sc = _Scenario(url="https://www.tiktok.com/login?lang=en")
        (ok, err), page = run_comment(sc)
        self.assertFalse(ok)
        self.assertIn("logged_out", err)

    def test_success_with_publish_response(self):
        sc = _Scenario(
            responses=[("https://www.tiktok.com/api/comment/publish?x=1",
                        {"statusCode": 0, "comment": {"cid": "999"}})])
        markers = []
        (ok, err), page = run_comment(sc, on_submit=lambda: markers.append(1))
        self.assertTrue(ok, err)
        self.assertEqual(err, "")
        self.assertEqual(markers, [1])
        clicks = [e for e in page.events if e[0] == "click"]
        self.assertTrue(any("data-mmm-send" in c[1] for c in clicks))

    def test_rejection_with_publish_response(self):
        sc = _Scenario(
            responses=[("https://www.tiktok.com/api/comment/publish",
                        {"statusCode": 3000101, "statusMsg": "Too frequent"})])
        (ok, err), _ = run_comment(sc)
        self.assertFalse(ok)
        self.assertIn("平台拒绝", err)
        self.assertIn("3000101", err)
        self.assertNotIn("write_uncertain", err)

    def test_uncertain_with_unparseable_response(self):
        sc = _Scenario(
            responses=[("https://www.tiktok.com/api/comment/publish",
                        "not-json")])
        (ok, err), _ = run_comment(sc)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("write_uncertain:"), err)
        self.assertIn("无法解析", err)

    def test_dom_verification_success_without_response(self):
        sc = _Scenario(
            responses=[],
            verify_result={"posted": True, "settled": True,
                           "alive": False, "empty": True, "items": 5,
                           "nRoots": 1})
        (ok, err), _ = run_comment(sc)
        self.assertTrue(ok, err)

    def test_dom_verification_fails_uncertain(self):
        sc = _Scenario(
            responses=[],
            verify_result={"posted": False, "settled": False,
                           "alive": False, "empty": True, "items": 5,
                           "nRoots": 0})
        (ok, err), _ = run_comment(sc)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("write_uncertain:"), err)
        self.assertIn("DOM 佐证亦未确认", err)

    def test_reply_target_not_found(self):
        sc = _Scenario(items=12, find_result=(False, 12, ""))
        (ok, err), _ = run_comment(
            sc, reply_to_text="不存在的目标", target_nick="nick",
            require_reply=True)
        self.assertFalse(ok)
        self.assertIn("未找到目标评论回复区", err)
        self.assertIn("12", err)

    def test_send_button_disabled(self):
        sc = _Scenario(send_enabled=False)
        (ok, err), _ = run_comment(sc)
        self.assertFalse(ok)
        self.assertIn("发送按钮不可用", err)

    def test_editor_not_found(self):
        sc = _Scenario(editor_ready=False)
        (ok, err), _ = run_comment(sc)
        self.assertFalse(ok)
        self.assertIn("未找到评论输入框", err)


# ── _resolve_rule_target 测试 ──────────────────────────────────────────

class ResolveRuleTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_tiktok_creator(self):
        kind, sec, aw, kw, xt = await main._resolve_rule_target(
            "tiktok", "auto_comment", "creator", "@testuser")
        self.assertEqual(kind, "creator")
        self.assertEqual(sec, "testuser")
        self.assertEqual(aw, "")

    async def test_tiktok_keyword_rejected(self):
        with self.assertRaises(main.HTTPException) as ctx:
            await main._resolve_rule_target(
                "tiktok", "auto_comment", "keyword", "camping")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("TikTok", str(ctx.exception.detail))
        self.assertIn("关键词", str(ctx.exception.detail))

    async def test_tiktok_work_url(self):
        kind, sec, aw, kw, xt = await main._resolve_rule_target(
            "tiktok", "auto_reply", "work",
            "https://www.tiktok.com/@user/video/741234567890")
        self.assertEqual(kind, "work")
        self.assertEqual(aw, "741234567890")

    async def test_tiktok_work_pure_id(self):
        kind, sec, aw, kw, xt = await main._resolve_rule_target(
            "tiktok", "auto_reply", "work", "741234567890")
        self.assertEqual(kind, "work")
        self.assertEqual(aw, "741234567890")

    async def test_tiktok_work_invalid(self):
        with self.assertRaises(main.HTTPException) as ctx:
            await main._resolve_rule_target(
                "tiktok", "auto_reply", "work", "not-an-id")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("无法识别", str(ctx.exception.detail))


# ── 引擎 discover + execute 测试 ────────────────────────────────────────

class _BrowserStub:
    def __init__(self):
        self._locks = {}

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def identity_for(self, _account):
        return None

    def anon_identity(self):
        return None


def _work(item_id, create_time, desc="work"):
    return {"id": item_id, "desc": desc, "createTime": create_time,
            "statsV2": {}}


class EnginePipelineTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "gates.db"))
        self.cfg = Config(engine=EngineConfig(
            media_dir=str(Path(self.tmp.name) / "media"),
            profiles_dir=str(Path(self.tmp.name) / "profiles"),
            quiet_hours_enabled=False,
            comment_daily_cap_per_account=10,
            comment_hourly_cap_per_account=5,
            comment_min_gap_seconds=60,
            comment_recent_works=2,
            comment_recent_days=7,
            comment_max_scrolls=2,
            tiktok_captcha_wait_seconds=5,
        ))

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _account(self, handle="tester"):
        with db.get_session() as session:
            account = DouyinAccount(
                platform="tiktok", nickname="fixture", status="active",
                sec_uid="sec_123", douyin_id=handle,
                storage_state='{"cookies": []}',
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            return account.id

    def _rule(self, account_id, mode="auto_comment", kind="creator",
              sec_uid="", aweme_id=""):
        with db.get_session() as session:
            rule = CommentRule(
                platform="tiktok", mode=mode, target_kind=kind,
                account_id=account_id, sec_uid=sec_uid, aweme_id=aweme_id,
                templates='["nice post"]',
                max_per_run=2, daily_cap=10, min_gap_seconds=60,
            )
            session.add(rule)
            session.commit()
            session.refresh(rule)
            return rule.id

    def _task(self, account_id, aweme_id, target_cid="",
              target_text=""):
        with db.get_session() as session:
            task = CommentTask(
                platform="tiktok", account_id=account_id,
                aweme_id=aweme_id, target_comment_id=target_cid,
                target_text=target_text, content="thanks",
                status="pending",
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            return task.id

    def _tasks_of(self, rule_id):
        with db.get_session() as session:
            return list(session.exec(select(CommentTask).where(
                CommentTask.rule_id == rule_id)).all())

    # -- discover --

    def test_discover_auto_comment_creator_sorts_and_limits(self):
        now = int(time.time())
        account_id = self._account()
        rule_id = self._rule(account_id, sec_uid="creator1")
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def fake_works(*a, **kw):
            return [
                _work("1", now - 300, "old"),
                _work("2", now - 100, "new"),
                _work("3", now - 200, "mid"),
            ], ""

        with patch("app.engine.monitor.fetch_tiktok_works", fake_works):
            result = asyncio.run(engine.run_comment_rule(rule_id))

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["created"], 2)   # comment_recent_works=2
        ids = [t.aweme_id for t in self._tasks_of(rule_id)]
        self.assertEqual(sorted(ids), ["2", "3"])   # 倒序取前 2

    def test_discover_auto_comment_manual_no_time_cutoff(self):
        account_id = self._account()
        rule_id = self._rule(account_id, sec_uid="creator1")
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def fake_works(*a, **kw):
            return [_work("1", 1_000_000_000, "very old")], ""

        with patch("app.engine.monitor.fetch_tiktok_works", fake_works):
            result = asyncio.run(engine.run_comment_rule(rule_id, manual=True))

        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["created"], 1)

    def test_discover_auto_comment_keyword_rejected(self):
        account_id = self._account()
        with db.get_session() as session:
            rule = CommentRule(
                platform="tiktok", mode="auto_comment", target_kind="keyword",
                account_id=account_id, keyword="camping",
                templates='["cool"]',
            )
            session.add(rule)
            session.commit()
            session.refresh(rule)
            rule_id = rule.id

        engine = MonitorEngine(self.cfg, _BrowserStub())
        result = asyncio.run(engine.run_comment_rule(rule_id))
        # 业务拒绝不属于 RISK/AUTH/NETWORK,规则轮次正常结束但不产生任务
        self.assertEqual(result["created"], 0)
        self.assertIn("关键词", result["error"])

    def test_discover_auto_reply_self_filters_own_and_nested(self):
        now = int(time.time())
        account_id = self._account(handle="tester")
        rule_id = self._rule(account_id, mode="auto_reply", kind="self")
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def fake_works(*a, **kw):
            return [_work("1001", now - 60)], ""

        async def fake_comments(*a, **kw):
            return [
                {"cid": "c1", "text": "hello", "createTime": now - 10,
                 "user": {"nickname": "visitor", "secUid": "v1"}},
                {"cid": "c2", "text": "nested", "createTime": now - 10,
                 "reply_id": "c1",
                 "user": {"nickname": "other", "secUid": "v2"}},
                {"cid": "c3", "text": "self", "createTime": now - 10,
                 "user": {"nickname": "fixture", "secUid": "sec_123"}},
            ], ""

        with patch("app.engine.monitor.fetch_tiktok_works", fake_works), \
             patch("app.engine.monitor.fetch_tiktok_comments", fake_comments):
            result = asyncio.run(engine.run_comment_rule(rule_id))

        self.assertTrue(result["ok"], result.get("error"))
        cids = [t.target_comment_id for t in self._tasks_of(rule_id)]
        self.assertEqual(cids, ["c1"])

    def test_discover_auto_reply_work_bypasses_works_list(self):
        now = int(time.time())
        account_id = self._account()
        rule_id = self._rule(account_id, mode="auto_reply", kind="work",
                             aweme_id="99")
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def fail_works(*a, **kw):
            raise AssertionError("work 模式不应再拉取作品列表")

        async def fake_comments(*a, **kw):
            return [
                {"cid": "c1", "text": "hi", "createTime": now - 10,
                 "user": {"nickname": "u1", "secUid": "v1"}},
            ], ""

        with patch("app.engine.monitor.fetch_tiktok_works", fail_works), \
             patch("app.engine.monitor.fetch_tiktok_comments", fake_comments):
            result = asyncio.run(engine.run_comment_rule(rule_id))

        self.assertTrue(result["ok"], result.get("error"))
        tasks = self._tasks_of(rule_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].aweme_id, "99")

    def test_discover_auto_reply_skips_old_comments_when_not_manual(self):
        now = int(time.time())
        account_id = self._account()
        rule_id = self._rule(account_id, mode="auto_reply", kind="work",
                             aweme_id="99")
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def fake_comments(*a, **kw):
            return [
                {"cid": "cold", "text": "old", "createTime": now - 30 * 86400,
                 "user": {"nickname": "u1", "secUid": "v1"}},
                {"cid": "cms", "text": "ms ts",
                 "createTime": (now - 60) * 1000,      # 毫秒时间戳
                 "user": {"nickname": "u2", "secUid": "v2"}},
            ], ""

        with patch("app.engine.monitor.fetch_tiktok_comments", fake_comments):
            result = asyncio.run(engine.run_comment_rule(rule_id))

        self.assertTrue(result["ok"], result.get("error"))
        cids = [t.target_comment_id for t in self._tasks_of(rule_id)]
        self.assertEqual(cids, ["cms"])   # 30 天前被过滤,毫秒戳正确换算

    def test_discover_risk_error_returns_empty(self):
        account_id = self._account()
        rule_id = self._rule(account_id, sec_uid="creator1")
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def fake_works(*a, **kw):
            return [], "风控:操作频繁"

        with patch("app.engine.monitor.fetch_tiktok_works", fake_works):
            result = asyncio.run(engine.run_comment_rule(rule_id))

        self.assertFalse(result["ok"])
        self.assertIn("频繁", result["error"])
        self.assertEqual(self._tasks_of(rule_id), [])

    # -- execute --

    def test_execute_tiktok_success(self):
        account_id = self._account()
        task_id = self._task(account_id, "w1")
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def fake_comment(*a, **kw):
            return True, ""

        with patch("app.engine.monitor.comment_tiktok_browser", fake_comment):
            result = asyncio.run(engine.execute_comment_task(task_id))

        self.assertTrue(result["ok"])
        with db.get_session() as session:
            task = session.get(CommentTask, task_id)
            self.assertEqual(task.status, "done")
            self.assertEqual(task.method, "browser")

    def test_execute_tiktok_uncertain_not_retried(self):
        account_id = self._account()
        task_id = self._task(account_id, "w1", target_cid="c1",
                             target_text="hello")
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def fake_comment(*a, **kw):
            kw["on_submit"]()
            with db.get_session() as session:
                self.assertEqual(
                    session.get(CommentTask, task_id).error,
                    "write_submitted:browser")
            return False, "write_uncertain:已提交但未捕获回包"

        with patch("app.engine.monitor.comment_tiktok_browser", fake_comment):
            result = asyncio.run(engine.execute_comment_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(CommentTask, task_id)
            self.assertEqual(task.status, "uncertain")
            self.assertEqual(task.method, "browser")
            self.assertIsNone(task.scheduled_at)
            self.assertIsNone(task.done_at)

    def test_execute_tiktok_hard_failure_not_retried(self):
        account_id = self._account()
        task_id = self._task(account_id, "w1")
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def fake_comment(*a, **kw):
            return False, "未找到评论输入框。页面输入元素:无"

        with patch("app.engine.monitor.comment_tiktok_browser", fake_comment):
            result = asyncio.run(engine.execute_comment_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(CommentTask, task_id)
            self.assertEqual(task.status, "failed")

    def test_execute_tiktok_risk_defers(self):
        account_id = self._account()
        task_id = self._task(account_id, "w1")
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def fake_comment(*a, **kw):
            return False, "风控:操作频繁,稍后再试"

        with patch("app.engine.monitor.comment_tiktok_browser", fake_comment):
            result = asyncio.run(engine.execute_comment_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(CommentTask, task_id)
            self.assertEqual(task.status, "pending")
            self.assertIn("频繁", task.error)
            self.assertIsNotNone(task.next_allowed_at)

    def test_execute_tiktok_logged_out_defers(self):
        account_id = self._account()
        task_id = self._task(account_id, "w1")
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def fake_comment(*a, **kw):
            return False, "logged_out:账号未登录,无法发评论"

        with patch("app.engine.monitor.comment_tiktok_browser", fake_comment):
            result = asyncio.run(engine.execute_comment_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(CommentTask, task_id)
            self.assertEqual(task.status, "pending")
            self.assertIsNotNone(task.next_allowed_at)


if __name__ == "__main__":
    unittest.main()
