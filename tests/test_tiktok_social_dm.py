"""Task 11: TikTok 关注动作与私信收件箱测试。

覆盖三层:
1. social_action.follow_tiktok_browser 桩页面流程(关注/取关/幂等/失败);
2. dm.fetch_tiktok_dm_conversations / dm.send_tiktok_dm 桩页面流程;
3. 引擎 _execute_action_task_locked tiktok 分支分派;
4. main.sync_dm tiktok 分支(桩浏览器)。
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import app.db as db
import app.main as main
from app.config import Config, EngineConfig
from app.engine.monitor import MonitorEngine
from app.models import AccountActionTask, DouyinAccount
from app.platforms.tiktok import social_action as ttsa
from app.platforms.tiktok import dm as ttdm
from app.platforms.tiktok.dm import _norm_conversation, _walk_conversations
from sqlmodel import select


# ── 桩页面(关注/私信共用)─────────────────────────────────────────────────

class _FakeTime:
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
        self.request = type("req", (), {"resource_type": "xhr"})()

    async def json(self):
        return self._payload

    async def text(self):
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)


class _Loc:
    def __init__(self, page, label):
        self._page = page
        self._label = label

    @property
    def first(self):
        return self

    def nth(self, i):
        # 用于 _first_visible 的遍历;count 为 0 时本不该被调用
        return self

    async def count(self):
        sc = self._page.scenario
        if "follow-button" in self._label or "follow-btn" in self._label:
            return 1 if sc.follow_button_visible else 0
        if "message-button" in self._label or "/messages" in self._label:
            return 1 if sc.dm_entry_visible else 0
        if "contenteditable" in self._label or "textarea" in self._label:
            return 1 if sc.dm_editor_visible else 0
        if "send-message-button" in self._label:
            return 1 if sc.dm_send_visible else 0
        return 0

    async def is_visible(self, timeout=None):
        return await self.count() > 0

    async def click(self, timeout=None):
        self._page.events.append(("click", self._label))
        if "follow" in self._label:
            # 点击后切换关注态
            self._page.scenario.following = not self._page.scenario.following
        if "send-message" in self._label:
            self._page.scenario.dm_sent = True
            self._page.scenario.dm_editor_text = ""

    async def fill(self, text, timeout=None):
        self._page.events.append(("fill", text))
        self._page.scenario.dm_editor_text = text

    async def inner_text(self):
        sc = self._page.scenario
        if "follow" in self._label:
            return "Following" if sc.following else "Follow"
        if "contenteditable" in self._label or "textarea" in self._label:
            return sc.dm_editor_text
        return ""

    async def input_value(self):
        sc = self._page.scenario
        return sc.dm_editor_text


class _StrictLoc(_Loc):
    """严格版 locator:不可见时 is_visible/count 都返回 0,click/fill 抛异常。"""

    async def is_visible(self, timeout=None):
        return await self.count() > 0

    async def click(self, timeout=None):
        if await self.count() == 0:
            raise AssertionError(f"click on invisible element: {self._label}")
        await super().click(timeout=timeout)

    async def fill(self, text, timeout=None):
        if await self.count() == 0:
            raise AssertionError(f"fill on invisible element: {self._label}")
        await super().fill(text, timeout=timeout)

    async def input_value(self):
        if await self.count() == 0:
            raise AssertionError(f"input_value on invisible element: {self._label}")
        return await super().input_value()

    async def inner_text(self):
        if await self.count() == 0:
            raise AssertionError(f"inner_text on invisible element: {self._label}")
        return await super().inner_text()


class _Kb:
    def __init__(self, page):
        self._page = page

    async def press(self, key):
        self._page.events.append(("key", key))
        if key == "Enter":
            self._page.scenario.dm_sent = True
            self._page.scenario.dm_editor_text = ""


class _Page:
    def __init__(self, scenario):
        self.scenario = scenario
        self.events = []
        self.url = scenario.url
        self.keyboard = _Kb(self)
        self.handlers = {}
        self.closed = False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def goto(self, url, wait_until=None, timeout=None):
        self.events.append(("goto", url))
        self.url = self.scenario.url

    async def wait_for_timeout(self, ms):
        self.scenario.clock.advance(ms)
        await asyncio.sleep(0)

    def locator(self, sel):
        return _StrictLoc(self, sel)

    async def evaluate(self, js, arg=None):
        if js == "() => window.scrollBy(0, document.body.scrollHeight)":
            return None
        return None

    async def close(self):
        self.closed = True


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

    async def new_page(self, identity, block_media=False):
        return self.page


class _Scenario:
    def __init__(self, *, url="https://www.tiktok.com/@testuser",
                 follow_button_visible=True, following=False,
                 dm_entry_visible=True, dm_editor_visible=True,
                 dm_send_visible=True):
        self.url = url
        self.clock = _FakeTime()
        self.follow_button_visible = follow_button_visible
        self.following = following
        self.dm_entry_visible = dm_entry_visible
        self.dm_editor_visible = dm_editor_visible
        self.dm_send_visible = dm_send_visible
        self.dm_editor_text = ""
        self.dm_sent = False


def run_follow(scenario, *, target_uid="testuser", unfollow=False):
    page = _Page(scenario)
    mgr = _Mgr(page)

    async def main():
        return await ttsa.follow_tiktok_browser(
            mgr, None, target_uid, "", unfollow=unfollow)

    return asyncio.run(main()), page


def run_dm_send(scenario, *, target_uid="testuser", text="hello"):
    page = _Page(scenario)
    mgr = _Mgr(page)

    async def main():
        return await ttdm.send_tiktok_dm(
            mgr, None, target_uid, "", text)

    return asyncio.run(main()), page


# ── 纯函数测试 ───────────────────────────────────────────────────────────

class NormConversationTests(unittest.TestCase):
    def test_basic(self):
        raw = {
            "conversation_id": "c123",
            "conversation_short_id": "s123",
            "ticket": "t123",
            "peer_user": {"uid": "u1", "sec_uid": "sec1", "nickname": "nick"},
            "last_message": {"text": "hi", "create_time": 1234567890},
            "unread_count": 3,
        }
        conv = _norm_conversation(raw)
        self.assertIsNotNone(conv)
        self.assertEqual(conv["conv_id"], "c123")
        self.assertEqual(conv["peer_uid"], "u1")
        self.assertEqual(conv["peer_nickname"], "nick")
        self.assertEqual(conv["last_text"], "hi")
        self.assertEqual(conv["unread_count"], 3)

    def test_camel_case(self):
        raw = {
            "conversationId": "c456",
            "peerUser": {"id": "u2", "uniqueId": "handle2"},
            "lastMessage": {"content": "hello", "createTime": 1234567891},
        }
        conv = _norm_conversation(raw)
        self.assertIsNotNone(conv)
        self.assertEqual(conv["conv_id"], "c456")
        self.assertEqual(conv["peer_uid"], "u2")
        self.assertEqual(conv["last_text"], "hello")

    def test_not_conversation(self):
        self.assertIsNone(_norm_conversation({"foo": "bar"}))
        self.assertIsNone(_norm_conversation({"id": ""}))
        self.assertIsNone(_norm_conversation("not-a-dict"))

    def test_walk_conversations(self):
        data = {
            "conversations": [
                {"conversation_id": "c1", "peer_user": {"uid": "u1"}},
                {"conversation_id": "c2", "peer_user": {"uid": "u2"}},
            ],
            "nested": {
                "list": [
                    {"conversation_id": "c3", "peer_user": {"uid": "u3"}},
                ]
            }
        }
        out = {}
        _walk_conversations(data, out)
        self.assertEqual(len(out), 3)
        self.assertIn("c1", out)
        self.assertIn("c3", out)


# ── follow_tiktok_browser 桩测试 ─────────────────────────────────────────

class FollowBrowserFlowTests(unittest.TestCase):
    def test_follow_success(self):
        sc = _Scenario(following=False)
        (ok, err), page = run_follow(sc)
        self.assertTrue(ok, err)
        self.assertEqual(err, "")
        self.assertTrue(sc.following)

    def test_unfollow_success(self):
        sc = _Scenario(following=True)
        (ok, err), page = run_follow(sc, unfollow=True)
        self.assertTrue(ok, err)
        self.assertFalse(sc.following)

    def test_follow_idempotent(self):
        sc = _Scenario(following=True)
        (ok, err), page = run_follow(sc)
        self.assertTrue(ok, err)
        self.assertEqual(err, "")
        self.assertTrue(sc.following)

    def test_unfollow_idempotent(self):
        sc = _Scenario(following=False)
        (ok, err), page = run_follow(sc, unfollow=True)
        self.assertTrue(ok, err)
        self.assertFalse(sc.following)

    def test_missing_target(self):
        ok, err = asyncio.run(ttsa.follow_tiktok_browser(None, None, "", ""))
        self.assertFalse(ok)
        self.assertIn("missing_target", err)

    def test_logged_out(self):
        sc = _Scenario(url="https://www.tiktok.com/login?lang=en")
        (ok, err), page = run_follow(sc)
        self.assertFalse(ok)
        self.assertIn("logged_out", err)

    def test_button_not_found(self):
        sc = _Scenario(follow_button_visible=False)
        (ok, err), page = run_follow(sc)
        self.assertFalse(ok)
        self.assertIn("未找到关注按钮", err)


# ── send_tiktok_dm 桩测试 ────────────────────────────────────────────────

class DmSendFlowTests(unittest.TestCase):
    def test_send_success(self):
        sc = _Scenario()
        (ok, err), page = run_dm_send(sc)
        self.assertTrue(ok, err)
        self.assertTrue(sc.dm_sent)
        self.assertEqual(sc.dm_editor_text, "")

    def test_empty_content(self):
        ok, err = asyncio.run(ttdm.send_tiktok_dm(None, None, "u1", "", ""))
        self.assertFalse(ok)
        self.assertEqual(err, "空内容")

    def test_missing_target(self):
        ok, err = asyncio.run(ttdm.send_tiktok_dm(None, None, "", "", "hi"))
        self.assertFalse(ok)
        self.assertIn("missing_target", err)

    def test_logged_out(self):
        sc = _Scenario(url="https://www.tiktok.com/login?lang=en")
        (ok, err), page = run_dm_send(sc)
        self.assertFalse(ok)
        self.assertIn("logged_out", err)

    def test_dm_entry_not_found(self):
        sc = _Scenario(dm_entry_visible=False, dm_editor_visible=False)
        (ok, err), page = run_dm_send(sc)
        self.assertFalse(ok)
        self.assertIn("未找到私信入口", err)

    def test_dm_editor_not_found(self):
        sc = _Scenario(dm_entry_visible=True, dm_editor_visible=False)
        (ok, err), page = run_dm_send(sc)
        self.assertFalse(ok)
        self.assertIn("未找到私信输入框", err)


# ── fetch_tiktok_dm_conversations 桩测试 ─────────────────────────────────

class FetchDmConversationsTests(unittest.TestCase):
    def test_fetch_success(self):
        page = _Page(_Scenario(url="https://www.tiktok.com/messages"))
        mgr = _Mgr(page)

        # 预置响应处理器,在 goto 后触发
        original_goto = page.goto

        async def goto_with_response(url, **kw):
            await original_goto(url, **kw)
            handler = page.handlers.get("response")
            if handler:
                resp = _Resp(
                    "https://www.tiktok.com/api/conversations",
                    {"conversations": [
                        {"conversation_id": "c1",
                         "peer_user": {"uid": "u1", "nickname": "n1"},
                         "last_message": {"text": "hi", "create_time": 123}}
                    ]})
                await handler(resp)

        page.goto = goto_with_response

        async def main():
            return await ttdm.fetch_tiktok_dm_conversations(mgr, None)

        convs, err = asyncio.run(main())
        self.assertEqual(err, "")
        self.assertEqual(len(convs), 1)
        self.assertEqual(convs[0]["conv_id"], "c1")

    def test_logged_out(self):
        page = _Page(_Scenario(url="https://www.tiktok.com/login"))
        mgr = _Mgr(page)

        async def main():
            return await ttdm.fetch_tiktok_dm_conversations(mgr, None)

        convs, err = asyncio.run(main())
        self.assertEqual(convs, [])
        self.assertIn("logged_out", err)


# ── 引擎分派测试 ─────────────────────────────────────────────────────────

class _BrowserStub:
    def __init__(self):
        self._locks = {}

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def identity_for(self, _account):
        return None

    def anon_identity(self):
        return None


class EngineDispatchTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "gates.db"))
        self.cfg = Config(engine=EngineConfig(
            media_dir=str(Path(self.tmp.name) / "media"),
            profiles_dir=str(Path(self.tmp.name) / "profiles"),
            quiet_hours_enabled=False,
        ))

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _account(self):
        with db.get_session() as session:
            account = DouyinAccount(
                platform="tiktok", nickname="fixture", status="active",
                sec_uid="sec_123", douyin_id="tester",
                storage_state='{"cookies": []}',
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            return account.id

    def _task(self, account_id, action="follow", target_uid="target1"):
        with db.get_session() as session:
            task = AccountActionTask(
                platform="tiktok", account_id=account_id, action=action,
                target_uid=target_uid, target_sec_uid="",
                target_nick="", content="", status="pending",
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            return task.id

    @patch("app.engine.monitor.follow_tiktok_browser")
    def test_dispatch_follow(self, mock_follow):
        mock_follow.return_value = (True, "")
        engine = MonitorEngine(self.cfg, _BrowserStub())
        account_id = self._account()
        task_id = self._task(account_id, "follow")

        async def run():
            return await engine.execute_action_task(task_id, manual=True)

        result = asyncio.run(run())
        self.assertTrue(result["ok"], result.get("error"))
        mock_follow.assert_called_once()

    @patch("app.engine.monitor.follow_tiktok_browser")
    def test_dispatch_unfollow(self, mock_follow):
        mock_follow.return_value = (True, "")
        engine = MonitorEngine(self.cfg, _BrowserStub())
        account_id = self._account()
        task_id = self._task(account_id, "unfollow")

        async def run():
            return await engine.execute_action_task(task_id, manual=True)

        result = asyncio.run(run())
        self.assertTrue(result["ok"], result.get("error"))
        # 确认 unfollow=True 被传入
        call_kwargs = mock_follow.call_args
        self.assertTrue(call_kwargs[1].get("unfollow", False))

    @patch("app.engine.monitor.send_tiktok_dm")
    def test_dispatch_send_dm(self, mock_dm):
        mock_dm.return_value = (True, "")
        engine = MonitorEngine(self.cfg, _BrowserStub())
        account_id = self._account()
        task_id = self._task(account_id, "send_dm")

        async def run():
            return await engine.execute_action_task(task_id, manual=True)

        result = asyncio.run(run())
        self.assertTrue(result["ok"], result.get("error"))
        mock_dm.assert_called_once()

    @patch("app.engine.monitor.follow_tiktok_browser")
    def test_dispatch_failure(self, mock_follow):
        mock_follow.return_value = (False, "按钮未找到")
        engine = MonitorEngine(self.cfg, _BrowserStub())
        account_id = self._account()
        task_id = self._task(account_id, "follow")

        async def run():
            return await engine.execute_action_task(task_id, manual=True)

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertIn("按钮未找到", result.get("error", ""))


# ── main.sync_dm tiktok 分支测试 ─────────────────────────────────────────

class SyncDmTiktokTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "dm.db"))

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _account(self):
        with db.get_session() as session:
            account = DouyinAccount(
                platform="tiktok", nickname="fixture", status="active",
                sec_uid="sec_123", douyin_id="tester",
                storage_state='{"cookies": []}',
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            return account.id

    @patch("app.main.fetch_tiktok_dm_conversations")
    def test_sync_dm_success(self, mock_fetch):
        mock_fetch.return_value = (
            [{"conv_id": "c1", "peer_uid": "u1", "peer_nickname": "n1",
              "last_text": "hi", "last_time": 123, "raw_json": "{}"}],
            "",
        )
        account_id = self._account()

        # sync_dm 需要 browser/engine 全局对象;用真实 BrowserManager 但不开窗
        from app.browser import BrowserManager
        cfg = Config(engine=EngineConfig(
            media_dir=str(Path(self.tmp.name) / "media"),
            profiles_dir=str(Path(self.tmp.name) / "profiles"),
        ))
        mgr = BrowserManager(cfg)
        engine = MonitorEngine(cfg, mgr)

        with patch.object(main, "browser", mgr), \
             patch.object(main, "engine", engine):
            async def run():
                return await main.sync_dm(account_id)

            result = asyncio.run(run())
        self.assertTrue(result["ok"])
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["source"], "browser")

    @patch("app.main.fetch_tiktok_dm_conversations")
    def test_sync_dm_logged_out(self, mock_fetch):
        mock_fetch.return_value = ([], "logged_out:登录态失效")
        account_id = self._account()

        from app.browser import BrowserManager
        cfg = Config(engine=EngineConfig(
            media_dir=str(Path(self.tmp.name) / "media"),
            profiles_dir=str(Path(self.tmp.name) / "profiles"),
        ))
        mgr = BrowserManager(cfg)
        engine = MonitorEngine(cfg, mgr)

        with patch.object(main, "browser", mgr), \
             patch.object(main, "engine", engine):
            async def run():
                return await main.sync_dm(account_id)

            with self.assertRaises(main.HTTPException) as ctx:
                asyncio.run(run())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("登录态已失效", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
