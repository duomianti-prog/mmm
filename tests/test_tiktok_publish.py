"""Task 9: TikTok 创作者中心网页发布。

覆盖两层:
1. 平台函数 publish_tiktok 的页面流程(桩页面):上传/填 caption/可见范围/
   单次点击/成功证据/commit 拦截/登录墙/提交前失败/待确认/人工验证/取消;
2. 引擎三态裁决:done / uncertain(防重发、不重试、不记 RiskEvent)/
   pending(瞬时退避保留预约、风控、登录失效)/ failed(无登录态);
3. API:POST /api/publish 的 tiktok 白名单与登录态校验。
"""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import app.db as db
import app.main as main
from app import platforms
from app.config import Config, EngineConfig
from app.engine.monitor import MonitorEngine
from app.models import DouyinAccount, PublishTask, RiskEvent
from app.platforms import registry as pf
from app.platforms.tiktok import publish as tpub
from app.platforms.tiktok.publish import (
    build_tiktok_caption,
    publish_tiktok,
)
from sqlmodel import select


# ── 纯函数 ─────────────────────────────────────────────────────────────

class CaptionBuildTests(unittest.TestCase):
    def test_title_desc_topics_merge_into_single_caption(self):
        caption = build_tiktok_caption("周末露营", "天气不错", "露营,户外")
        self.assertEqual(caption, "周末露营\n天气不错\n#露营 #户外")

    def test_empty_parts_and_duplicate_hashes(self):
        self.assertEqual(build_tiktok_caption("", " ", "  "), "")
        self.assertEqual(
            build_tiktok_caption("t", "", "#露营, #户外"),
            "t\n#露营 #户外")

    def test_caption_is_cut_at_tiktok_limit(self):
        caption = build_tiktok_caption("x" * 3000, "", "")
        self.assertEqual(len(caption), 2200)


class CommitPayloadTests(unittest.TestCase):
    def test_aweme_id_with_handle(self):
        self.assertEqual(
            tpub._published_url({"status_code": 0, "aweme_id": "741234567890",
                                 "author": {"uniqueId": "camper"}}),
            "https://www.tiktok.com/@camper/video/741234567890")

    def test_nested_item_without_handle_falls_back_to_manage_anchor(self):
        url = tpub._published_url(
            {"status_code": 0, "itemStruct": {"itemId": "7410000000000"}})
        self.assertTrue(url.endswith("#item7410000000000"))
        self.assertIn("creator-center/content", url)

    def test_non_dict_payload_yields_empty(self):
        self.assertEqual(tpub._published_url(None), "")
        self.assertEqual(tpub._published_url({"status_code": 0}), "")


# ── 桩页面 ────────────────────────────────────────────────────────────

class _El:
    def __init__(self, text="", *, visible=True, enabled=True, role="",
                 name="", tag="div", kind="", aria_checked=None):
        self.text = text
        self.visible = visible
        self.enabled = enabled
        self.role = role
        self.name = name
        self.tag = tag
        self.kind = kind
        self.aria_checked = aria_checked
        self.checks = 0
        self.clicks = 0


class _Loc:
    def __init__(self, page, els, label):
        self._page = page
        self._els = els
        self._label = label

    @property
    def first(self):
        return _Loc(self._page, self._els[:1], self._label + ":first")

    async def count(self):
        return len(self._els)

    def nth(self, i):
        return _Loc(self._page,
                    [self._els[i]] if 0 <= i < len(self._els) else [],
                    f"{self._label}:nth({i})")

    async def is_visible(self):
        return bool(self._els and self._els[0].visible)

    async def is_enabled(self):
        return bool(self._els and self._els[0].enabled)

    async def inner_text(self):
        return self._els[0].text if self._els else ""

    async def get_attribute(self, name):
        if not self._els:
            return None
        return getattr(self._els[0], "aria_" + name.replace("-", "_"), None)

    async def wait_for(self, state=None, timeout=None):
        if not self._els:
            raise TimeoutError(f"{self._label} not present")

    async def scroll_into_view_if_needed(self, timeout=None):
        if not self._els:
            raise RuntimeError(self._label)

    async def click(self, timeout=None):
        if not self._els:
            raise RuntimeError("not found: " + self._label)
        el = self._els[0]
        el.clicks += 1
        self._page.events.append(("click", self._label, el.text or el.name))
        await self._page.scenario.on_element_click(el)

    async def check(self, timeout=None):
        if not self._els:
            raise RuntimeError("not found: " + self._label)
        el = self._els[0]
        el.checks += 1
        el.aria_checked = "true"
        self._page.events.append(("check", self._label, el.name or el.text))
        await self._page.scenario.on_element_click(el)

    async def set_input_files(self, files, timeout=None):
        if not self._els:
            raise RuntimeError("no file input")
        files = list(files)
        self._els[0].files = files
        self._page.scenario.set_files = list(files)
        self._page.events.append(("set_files", str(len(files)), ""))

    def locator(self, sub, **_kw):
        # 仅用于 xpath=.. 取父节点,桩里把开关容器视为同元素
        return _Loc(self._page, self._els, f"{self._label}>parent")


class _Kb:
    def __init__(self, page):
        self._page = page

    async def press(self, key):
        self._page.events.append(("key", key, ""))

    async def type(self, text, delay=0):
        self._page.events.append(("type", text, ""))


class _Resp:
    def __init__(self, url, payload, method="POST"):
        self.url = url
        self.request = SimpleNamespace(method=method)
        self._payload = payload

    async def json(self):
        return self._payload


class _Page:
    def __init__(self, scenario):
        self.scenario = scenario
        self.url = scenario.initial_url
        self.handlers = {}
        self.events = []
        self.keyboard = _Kb(self)
        self.closed = False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def goto(self, url, wait_until=None, timeout=None):
        self.events.append(("goto", url, ""))
        self.url = self.scenario.after_goto_url or url

    async def wait_for_load_state(self, *_a, **_kw):
        return None

    async def wait_for_timeout(self, ms):
        await self.scenario.tick(self, ms)

    def locator(self, sel):
        return _Loc(self, self.scenario.resolve_locator(sel), sel)

    def get_by_role(self, role, name=None, exact=False):
        return _Loc(self, self.scenario.resolve_role(role, name),
                    f"role={role}:{name}")

    def get_by_text(self, text, exact=False):
        return _Loc(self, self.scenario.resolve_text(text, exact),
                    f"text={text}:exact={exact}")

    async def click(self, selector, timeout=None):
        el = self.scenario.resolve_click_selector(selector)
        if el is None:
            raise RuntimeError("no element for " + selector)
        el.clicks += 1
        self.events.append(("click-sel", selector, el.text))
        await self.scenario.on_element_click(el)

    async def bring_to_front(self):
        self.events.append(("front", "", ""))

    async def screenshot(self, path, full_page=False):
        Path(path).write_bytes(b"\x89PNG stub")

    async def inner_text(self, selector="body"):
        return self.scenario.body_text if selector == "body" else ""

    async def close(self):
        self.closed = True


class _Ctx:
    def __init__(self, page):
        self.page = page
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True
        self.page.closed = True


class _Mgr:
    def __init__(self, page):
        self.page = page
        self.opened = 0

    async def open_headed(self, identity):
        self.opened += 1
        return _Ctx(self.page)


class _Scenario:
    """脚本化发布页:元素集 + 点击/轮询时的状态推进。"""
    UPLOAD_URL = "https://www.tiktok.com/creator-center/upload?lang=en"

    def __init__(self, *, media_type="video", visibility="public",
                 initial_url=UPLOAD_URL, after_goto_url=None,
                 file_input=True, editor=True, radios=False,
                 photo_tab=False, body_text=""):
        self.media_type = media_type
        self.visibility = visibility
        self.initial_url = initial_url
        self.after_goto_url = after_goto_url
        self.file_el = _El(tag="input", kind="file") if file_input else None
        self.editor_el = _El(kind="caption", visible=editor)
        self.post_el = _El("Post", enabled=True, tag="button", kind="post")
        self.confirm_el = _El("Post now", visible=False, tag="button",
                              kind="confirm")
        self.photo_el = _El("Switch to photo mode", tag="button",
                            kind="photo", visible=photo_tab)
        self.btns_extra = []
        self.radio_els = []
        if radios:
            self.radio_els = [
                _El("Public", role="radio", name="Public", kind="radio",
                    aria_checked="true"),
                _El("Friends", role="radio", name="Friends", kind="radio"),
                _El("Only you", role="radio", name="Only you", kind="radio"),
            ]
        self.body_text = body_text
        self.visible_keywords = set()
        self.post_url = None
        self.commit_payload = None
        self.commit_url = ("https://www.tiktok.com/web/aweme/v1/commit/item/"
                           "?aid=1988")
        # 成功信号:在第 N 次 post-click 轮询时按 kind 投递
        self.success_kind = None        # commit | url | keyword
        self.success_on_poll = None
        self.confirm_until_poll = None
        self.verify_from_poll = None
        self.goto_raises = None
        self.tick_raises_after_post = None
        self.polls = 0
        self.set_files = []

    # ── 解析 ──
    def resolve_locator(self, sel):
        if sel == 'input[type="file"]':
            return [self.file_el] if self.file_el else []
        if "contenteditable" in sel or "caption" in sel or "describe" in sel:
            return [self.editor_el] if self.editor_el.visible else []
        if sel == "button, [role=\"button\"]":
            return [b for b in ([self.post_el, self.confirm_el]
                                + self.btns_extra) if b.visible]
        if "post-button" in sel or sel == '[data-e2e="post"]':
            return [self.post_el] if self.post_el.visible else []
        if "Post now" in sel or "Continue to post" in sel:
            return [self.confirm_el] if self.confirm_el.visible else []
        if "checkbox" in sel or "switch" in sel:
            return []
        if sel.startswith("label") or "radio" in sel or "option" in sel:
            return [r for r in self.radio_els if r.visible]
        return []

    def resolve_role(self, role, name):
        if role != "radio":
            return []
        return [r for r in self.radio_els if r.name == name]

    def resolve_text(self, text, exact):
        if exact:
            return [r for r in self.radio_els if r.visible and r.name == text]
        if text in self.visible_keywords:
            return [_El(text, visible=True)]
        return []

    def resolve_click_selector(self, sel):
        if "photo" in sel.lower() and self.photo_el.visible:
            return self.photo_el
        if self.confirm_el.visible and (
                "Post now" in sel or "Continue" in sel):
            return self.confirm_el
        return None

    # ── 推进 ──
    async def goto_or_raise(self):
        if self.goto_raises:
            raise self.goto_raises

    async def on_element_click(self, el):
        if el is self.file_el:
            self.set_files = list(getattr(el, "files", []))
        if el.kind == "post":
            self.post_url = None
        if el.kind == "confirm":
            el.visible = False
            if self.commit_payload is not None:
                await self._dispatch()

    async def _dispatch(self):
        page = self._page
        handler = page.handlers.get("response")
        if handler and self.commit_payload is not None:
            await handler(_Resp(self.commit_url, self.commit_payload))

    async def tick(self, page, ms):
        self._page = page
        if self.post_el.clicks == 0:
            return
        if self.tick_raises_after_post is not None:
            raise self.tick_raises_after_post
        self.polls += 1
        if self.verify_from_poll is not None and \
                self.polls >= self.verify_from_poll:
            self.visible_keywords |= {"Verification"}
        if self.confirm_until_poll is not None:
            self.confirm_el.visible = self.polls <= self.confirm_until_poll
        if self.success_on_poll is not None and \
                self.polls == self.success_on_poll:
            if self.success_kind == "commit":
                await self._dispatch()
            elif self.success_kind == "url":
                page.url = ("https://www.tiktok.com/creator-center/content"
                            "?lang=en")
            elif self.success_kind == "keyword":
                self.visible_keywords.add(
                    "Your video is being uploaded to TikTok")


def run_publish(scenario, *, identity=None, on_submit=None,
                timeout_seconds=20):
    page = _Page(scenario)
    scenario._page = page
    mgr = _Mgr(page)

    async def goto_patch(self, url, **kw):
        page.events.append(("goto", url, ""))
        if scenario.goto_raises:
            raise scenario.goto_raises
        page.url = scenario.after_goto_url or url

    async def main():
        with patch.object(_Page, "goto", goto_patch):
            return await publish_tiktok(
                mgr, identity, "{}", scenario.media_type,
                "Weekend camping", "Nice weather",
                _FILES[0:1] if scenario.media_type == "video" else _FILES,
                topics="camping,outdoor",
                visibility=scenario.visibility,
                headed=True, timeout_seconds=timeout_seconds,
                on_submit=on_submit)

    return asyncio.run(main()), page, mgr


_FILES = []


class TiktokPublishFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        global _FILES
        _FILES = []
        for i in range(3):
            p = Path(cls.tmp.name) / f"m{i}.mp4" if i == 0 else \
                Path(cls.tmp.name) / f"m{i}.jpg"
            p.write_bytes(b"x")
            _FILES.append(str(p))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self._dbg = tempfile.TemporaryDirectory()
        self.patchdbg = patch.object(tpub, "_DEBUG_DIR",
                                     Path(self._dbg.name) / "debug")
        self.patchdbg.start()

    def tearDown(self):
        self.patchdbg.stop()
        self._dbg.cleanup()

    def test_success_via_commit_response_clicks_post_once(self):
        scenario = _Scenario()
        scenario.commit_payload = {"status_code": 0,
                                   "aweme_id": "741234567890",
                                   "author": {"uniqueId": "camper"}}
        scenario.success_kind, scenario.success_on_poll = "commit", 1
        markers = []

        def on_submit():
            markers.append(("submit", scenario.post_el.clicks))

        (ok, url, err), page, mgr = run_publish(scenario, on_submit=on_submit)

        self.assertTrue(ok, err)
        self.assertEqual(url,
                         "https://www.tiktok.com/@camper/video/741234567890")
        self.assertEqual(err, "")
        # 浏览器开了一次、Post 点了一次、提交回调发生在点击之前
        self.assertEqual(mgr.opened, 1)
        self.assertEqual(scenario.post_el.clicks, 1)
        self.assertEqual(markers, [("submit", 0)])
        typed = [e[1] for e in page.events if e[0] == "type"]
        self.assertEqual(len(typed), 1)
        self.assertIn("Weekend camping", typed[0])
        self.assertIn("#camping #outdoor", typed[0])
        # public 为默认,不点任何 radio
        self.assertEqual([e for e in page.events if e[0] == "check"], [])
        self.assertTrue(page.closed)

    def test_success_via_manage_page_navigation(self):
        scenario = _Scenario()
        # 无 commit 回包,第一次 post-click 轮询后跳转管理页
        scenario.success_kind, scenario.success_on_poll = "url", 1

        (ok, url, err), _page, _mgr = run_publish(scenario)

        self.assertTrue(ok, err)
        self.assertIn("creator-center/content", url)

    def test_success_via_uploaded_keyword(self):
        scenario = _Scenario()
        scenario.success_kind, scenario.success_on_poll = "keyword", 1

        (ok, url, err), _p, _m = run_publish(scenario)

        self.assertTrue(ok, err)

    def test_login_wall_returns_auth_error_without_submit(self):
        scenario = _Scenario(
            after_goto_url="https://www.tiktok.com/login?lang=en")
        markers = []
        (ok, url, err), page, mgr = run_publish(
            scenario, on_submit=lambda: markers.append(1))
        self.assertFalse(ok)
        self.assertIn("TikTok 登录态已失效", err)
        self.assertEqual(markers, [])
        self.assertEqual(scenario.post_el.clicks, 0)
        self.assertNotIn(("set_files",),
                         {tuple(e[:1]) for e in page.events})

    def test_missing_file_input_is_pre_submit_failure(self):
        scenario = _Scenario(file_input=False)
        (ok, _url, err), _p, _m = run_publish(scenario)
        self.assertFalse(ok)
        self.assertIn("上传文件失败", err)
        self.assertEqual(scenario.post_el.clicks, 0)

    def test_editor_not_ready_is_pre_submit_failure(self):
        scenario = _Scenario(editor=False, media_type="images")
        (ok, _url, err), _p, _m = run_publish(scenario, timeout_seconds=1)
        self.assertFalse(ok)
        self.assertIn("未进入编辑页", err)
        self.assertEqual(scenario.post_el.clicks, 0)

    def test_unconfirmed_after_click_is_uncertain_and_single_submit(self):
        scenario = _Scenario()
        # 不投递 commit、不跳转、无成功文案
        markers = []
        (ok, _url, err), page, _m = run_publish(
            scenario, timeout_seconds=1,
            on_submit=lambda: markers.append(scenario.post_el.clicks))
        self.assertFalse(ok)
        self.assertTrue(err.startswith("write_uncertain:"), err)
        self.assertIn("不会自动重发", err)
        self.assertEqual(markers, [0])
        self.assertEqual(scenario.post_el.clicks, 1)

    def test_verification_after_post_waits_passively_then_uncertain(self):
        scenario = _Scenario()
        scenario.verify_from_poll = 1
        (ok, _url, err), page, _m = run_publish(scenario, timeout_seconds=1)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("write_uncertain:"))
        self.assertIn("人工验证", err)
        # 被动等待:把窗口置前,且没有对验证按钮做额外提交点击
        self.assertIn(("front", "", ""), page.events)
        self.assertEqual(scenario.post_el.clicks, 1)

    def test_continue_to_post_confirmation_is_same_submission(self):
        scenario = _Scenario()
        scenario.confirm_until_poll = 1
        scenario.commit_payload = {"status_code": 0, "aweme_id": "7001"}
        # 首次轮询出现 Post now,点击确认即投递 commit(不走定时信号)

        async def on_confirm(self, el):
            if el.kind == "confirm" and self.commit_payload is not None:
                await self._dispatch()

        with patch.object(_Scenario, "on_element_click",
                          lambda self, el: on_confirm(self, el)):
            (ok, url, err), page, _m = run_publish(scenario)
        self.assertTrue(ok, err)
        self.assertTrue(url.endswith("7001"))
        self.assertEqual(scenario.post_el.clicks, 1)
        self.assertGreaterEqual(scenario.confirm_el.clicks, 1)

    def test_commit_without_code_but_with_item_id_is_success(self):
        scenario = _Scenario()
        scenario.commit_payload = {"aweme_id": "7009",
                                   "author": {"uniqueId": "h"}}
        scenario.success_kind, scenario.success_on_poll = "commit", 1
        (ok, url, err), _p, _m = run_publish(scenario)
        self.assertTrue(ok, err)
        self.assertEqual(url, "https://www.tiktok.com/@h/video/7009")

    def test_business_rejection_after_submit_is_uncertain(self):
        scenario = _Scenario()
        scenario.commit_payload = {"status_code": 1009,
                                   "status_msg": "missing permission"}
        scenario.success_kind, scenario.success_on_poll = "commit", 1
        (ok, _url, err), _p, _m = run_publish(scenario)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("write_uncertain:"))
        self.assertIn("missing permission", err)
        self.assertEqual(scenario.post_el.clicks, 1)

    def test_private_visibility_selects_only_you_radio(self):
        scenario = _Scenario(radios=True, visibility="private")
        scenario.commit_payload = {"status_code": 0, "aweme_id": "7002"}
        scenario.success_kind, scenario.success_on_poll = "commit", 1
        (ok, _url, err), page, _m = run_publish(scenario)
        self.assertTrue(ok, err)
        only_you = next(r for r in scenario.radio_els
                        if r.name == "Only you")
        self.assertEqual(only_you.checks, 1)
        # 绝不能点到 Friends
        friends = next(r for r in scenario.radio_els if r.name == "Friends")
        self.assertEqual(friends.checks, 0)

    def test_images_switch_photo_tab_and_uploads_all_files(self):
        scenario = _Scenario(media_type="images", photo_tab=True, editor=True)
        scenario.commit_payload = {"status_code": 0, "aweme_id": "7003"}
        scenario.success_kind, scenario.success_on_poll = "commit", 1
        (ok, _url, err), _p, _m = run_publish(scenario)
        self.assertTrue(ok, err)
        self.assertGreaterEqual(scenario.photo_el.clicks, 1)
        self.assertEqual(len(scenario.set_files), 3)

    def test_pre_submit_exception_is_plain_error(self):
        scenario = _Scenario()
        scenario.goto_raises = RuntimeError("navigation boom")
        (ok, _url, err), _p, _m = run_publish(scenario)
        self.assertFalse(ok)
        self.assertIn("发布异常", err)
        self.assertNotIn("write_uncertain", err)
        self.assertEqual(scenario.post_el.clicks, 0)

    def test_cancel_after_submit_is_uncertain_and_re_raised(self):
        scenario = _Scenario()
        scenario.tick_raises_after_post = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            run_publish(scenario)
        self.assertEqual(scenario.post_el.clicks, 1)

    def test_no_media_files_returns_hard_error_before_browser(self):
        mgr = _Mgr(_Page(_Scenario()))

        async def main():
            return await publish_tiktok(
                mgr, None, "{}", "video", "t", "d",
                [str(Path(self.tmp.name) / "missing.mp4")])

        ok, _url, err = asyncio.run(main())
        self.assertFalse(ok)
        self.assertIn("没有可用的本地媒体文件", err)
        self.assertEqual(mgr.opened, 0)


# ── 引擎三态 ───────────────────────────────────────────────────────────

class _BrowserStub:
    def __init__(self):
        self._locks = {}

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def identity_for(self, _account):
        return None

    def anon_identity(self):
        return None


class TiktokPublishEngineTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "engine.db"))
        self.cfg = Config(engine=EngineConfig(
            media_dir=str(Path(self.tmp.name) / "media"),
            profiles_dir=str(Path(self.tmp.name) / "profiles"),
            quiet_hours_enabled=False,
        ))
        self.media = Path(self.tmp.name) / "v.mp4"
        self.media.write_bytes(b"x")

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _account(self, *, storage='{"cookies": []}'):
        with db.get_session() as s:
            acc = DouyinAccount(platform="tiktok", nickname="tt-fixture",
                                status="active", storage_state=storage)
            s.add(acc)
            s.commit()
            s.refresh(acc)
            return acc.id

    def _task(self, account_id, *, scheduled_at=None):
        with db.get_session() as s:
            t = PublishTask(
                platform="tiktok", account_id=account_id,
                media_type="video", title="fixture",
                media_json=json.dumps([str(self.media)]),
                status="pending",
                scheduled_at=scheduled_at,
                scheduled_at_is_utc=bool(scheduled_at),
            )
            s.add(t)
            s.commit()
            s.refresh(t)
            return t.id

    def _row(self, task_id):
        with db.get_session() as s:
            t = s.get(PublishTask, task_id)
            s.expunge(t)
            return t

    def test_engine_success_marks_done(self):
        acc = self._account()
        tid = self._task(acc)
        engine = MonitorEngine(self.cfg, _BrowserStub())
        done = AsyncMock(return_value=(
            True, "https://www.tiktok.com/@x/video/1", ""))
        with patch("app.engine.monitor.publish_tiktok", done):
            result = asyncio.run(engine.publish_task(tid))
        self.assertTrue(result["ok"], result)
        row = self._row(tid)
        self.assertEqual(row.status, "done")
        self.assertEqual(row.result_url,
                         "https://www.tiktok.com/@x/video/1")
        self.assertIsNotNone(row.done_at)

    def test_engine_uncertain_blocks_replay_and_keeps_no_risk_event(self):
        acc = self._account()
        tid = self._task(acc)
        engine = MonitorEngine(self.cfg, _BrowserStub())
        calls = []

        async def ambiguous(*_a, **kwargs):
            calls.append(kwargs)
            kwargs["on_submit"]()
            return False, "", "write_uncertain:发布后连接中断"

        with patch("app.engine.monitor.publish_tiktok", ambiguous):
            first = asyncio.run(engine.publish_task(tid))
            replay = asyncio.run(engine.publish_task(tid))

        self.assertFalse(first["ok"])
        self.assertFalse(replay["ok"])
        self.assertEqual(len(calls), 1)  # 适配器绝不重放
        row = self._row(tid)
        self.assertEqual(row.status, "uncertain")
        self.assertIsNone(row.scheduled_at)
        self.assertIsNone(row.done_at)
        self.assertIsNone(row.next_allowed_at)
        with db.get_session() as s:
            self.assertEqual(s.exec(select(RiskEvent)).all(), [])

    def test_engine_submit_marker_survives_adapter_timeout(self):
        acc = self._account()
        tid = self._task(acc)
        engine = MonitorEngine(self.cfg, _BrowserStub())
        calls = []

        async def interrupted(*_a, **kwargs):
            calls.append(kwargs)
            kwargs["on_submit"]()
            raise TimeoutError("connection timeout")

        with patch("app.engine.monitor.publish_tiktok", interrupted):
            result = asyncio.run(engine.publish_task(tid))
            replay = asyncio.run(engine.publish_task(tid))

        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith("write_uncertain:"))
        self.assertFalse(replay["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._row(tid).status, "uncertain")

    def test_engine_pre_submit_timeout_retries_and_keeps_appointment(self):
        acc = self._account()
        appointment = datetime.utcnow() + timedelta(hours=3)
        tid = self._task(acc, scheduled_at=appointment)
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def glitch(*_a, **_kw):
            # 浏览器崩溃类:瞬时但不落入风控 NETWORK 控制,走退避重试
            raise RuntimeError("Target page, context or browser has been closed")

        with patch("app.engine.monitor.publish_tiktok", glitch):
            result = asyncio.run(engine.publish_task(tid))

        self.assertFalse(result["ok"])
        row = self._row(tid)
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.retry_count, 1)
        self.assertIsNotNone(row.next_allowed_at)
        self.assertEqual(row.scheduled_at, appointment)  # 预约保留

    def test_engine_risk_response_defers_without_clearing_appointment(self):
        acc = self._account()
        appointment = datetime.utcnow() + timedelta(hours=2)
        tid = self._task(acc, scheduled_at=appointment)
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def rejected(*_a, **_kw):
            return False, "", "HTTP 429 rate limit"

        with patch("app.engine.monitor.publish_tiktok", rejected):
            result = asyncio.run(engine.publish_task(tid))

        self.assertFalse(result["ok"])
        row = self._row(tid)
        self.assertEqual(row.status, "pending")
        self.assertIsNotNone(row.next_allowed_at)
        self.assertEqual(row.scheduled_at, appointment)

    def test_engine_logged_out_defers_and_invalidates_account(self):
        acc = self._account()
        tid = self._task(acc)
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def logged_out(*_a, **_kw):
            return False, "", "TikTok 登录态已失效，请重新登录后再发布"

        with patch("app.engine.monitor.publish_tiktok", logged_out):
            result = asyncio.run(engine.publish_task(tid))

        self.assertFalse(result["ok"])
        self.assertEqual(self._row(tid).status, "pending")
        with db.get_session() as s:
            self.assertEqual(s.get(DouyinAccount, acc).status, "invalid")

    def test_engine_without_state_fails_in_chinese_without_browser(self):
        acc = self._account(storage="")
        tid = self._task(acc)
        browser = _BrowserStub()
        engine = MonitorEngine(self.cfg, browser)
        adapter = AsyncMock(side_effect=AssertionError("must not open browser"))
        with patch("app.engine.monitor.publish_tiktok", adapter):
            result = asyncio.run(engine.publish_task(tid))
        self.assertFalse(result["ok"])
        self.assertIn("TikTok", result["error"])
        self.assertEqual(self._row(tid).status, "failed")
        adapter.assert_not_awaited()


# ── API ───────────────────────────────────────────────────────────────

class TiktokPublishApiTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.previous_main_engine = main.engine
        self.previous_browser = main.browser
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "api.db"))
        self.cfg = Config()
        self.browser = _BrowserStub()
        main.browser = self.browser
        main.engine = MonitorEngine(self.cfg, self.browser)
        self.media = Path(self.tmp.name) / "v.mp4"
        self.media.write_bytes(b"x")

    def tearDown(self):
        main.engine = self.previous_main_engine
        main.browser = self.previous_browser
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _account(self, platform="tiktok", storage='{"cookies": []}'):
        with db.get_session() as s:
            acc = DouyinAccount(platform=platform, nickname="api-tt",
                                status="active", storage_state=storage)
            s.add(acc)
            s.commit()
            s.refresh(acc)
            return acc.id

    def _request(self, method, path, payload=None):
        async def run():
            transport = httpx.ASGITransport(
                app=main.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://127.0.0.1") as client:
                if method == "GET":
                    return await client.get(path)
                return await client.request(
                    method, path,
                    content=json.dumps(payload),
                    headers={"Content-Type": "application/json"})
        return asyncio.run(run())

    def test_create_tiktok_publish_task(self):
        acc = self._account()
        resp = self._request("POST", "/api/publish", {
            "account_id": acc, "media_type": "video",
            "title": "hello tiktok", "media_paths": [str(self.media)],
            "visibility": "private",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["platform"], "tiktok")
        self.assertEqual(data["visibility"], "private")

    def test_unsupported_platform_account_is_rejected(self):
        # 白名单外平台(模拟未来/已停用平台)的账号不能建发布任务
        acc = self._account(platform="bilibili")
        resp = self._request("POST", "/api/publish", {
            "account_id": acc, "media_type": "video",
            "media_paths": [str(self.media)],
        })
        self.assertEqual(resp.status_code, 400)

    def test_tiktok_without_login_state_is_rejected(self):
        acc = self._account(storage="")
        resp = self._request("POST", "/api/publish", {
            "account_id": acc, "media_type": "video",
            "media_paths": [str(self.media)],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("TikTok", resp.json()["detail"])

    def test_list_filters_tiktok_and_update_visibility(self):
        acc = self._account()
        created = self._request("POST", "/api/publish", {
            "account_id": acc, "media_type": "video",
            "media_paths": [str(self.media)], "visibility": "public",
        }).json()
        tid = created["id"]

        rows = self._request("GET", "/api/publish?platform=tiktok").json()
        self.assertEqual([r["id"] for r in rows], [tid])

        resp = self._request("PUT", f"/api/publish/{tid}",
                             {"visibility": "friends"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["visibility"], "friends")


class TiktokPublishRegistryTests(unittest.TestCase):
    def test_tiktok_has_publish_capability(self):
        self.assertTrue(pf.has_cap("tiktok", pf.PUBLISH))
        self.assertIn("tiktok", pf.keys_with(pf.PUBLISH))


if __name__ == "__main__":
    unittest.main()
