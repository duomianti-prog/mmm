"""Task 20:视频号本账号能力稳定化 —— 发布/评论回复/数据解析的离线单测。

真实账号的发布与评论回复走浏览器自动化,无法在 CI 里跑;这里用 fake
page/frame 验证:
  - 解析层对真实 mmfinderassistant 字段变体的兜底;
  - 发布流程的登录态判定、上传、成功跳转、失败 toast 证据化;
  - 评论读取的多路由尝试 + 登录态失效 + 增量去重;
  - 评论回复的跨 frame 定位与回复接口业务回包判定。
"""
import asyncio
import unittest
from types import SimpleNamespace

from app.browser.channels_fetcher import (
    COMMENT_LIST_URLS,
    _dig_comments,
    _mm_business_ok,
    fetch_channels_comments,
    post_channels_comment,
)
from app.platforms.channels.extract import (
    flatten_channels_comments,
    parse_channels_comment,
    parse_channels_feed,
    parse_self_user,
)
from app.platforms.channels.publish import publish_channels


# ───────────────────────── 解析层纯函数 ─────────────────────────

class ChannelsExtractTests(unittest.TestCase):
    def test_video_feed_normalizes_ms_timestamp_and_counts(self):
        item = {
            "objectId": "1111111111111111",
            "createtime": 1_700_000_000_000,          # 毫秒
            "nickName": "视频号作者",
            "likeCount": "1.2万",
            "commentCount": "300",
            "objectDesc": {
                "description": "一条视频",
                "mediaType": 1,
                "media": [{
                    "url": "https://example.test/v.mp4",
                    "coverUrl": "https://example.test/cover.jpg",
                    "videoPlayLen": 15_000,          # 毫秒
                }],
            },
        }
        aw = parse_channels_feed(item)
        self.assertIsNotNone(aw)
        self.assertEqual(aw.aweme_id, "1111111111111111")
        self.assertEqual(aw.media_type, "video")
        self.assertEqual(aw.create_time, 1_700_000_000)
        self.assertEqual(aw.like_count, 12000)
        self.assertEqual(aw.comment_count, 300)
        self.assertEqual(aw.duration, 15)
        self.assertEqual(aw.cover, "https://example.test/cover.jpg")
        self.assertEqual(aw.author_name, "视频号作者")
        self.assertEqual(aw.platform, "shipinhao")
        self.assertEqual(len(aw.medias), 1)
        self.assertEqual(aw.medias[0].kind, "video")

    def test_feed_without_downloadable_media_still_returns(self):
        # 加密 CDN 无直链是常态:元数据仍要保留
        aw = parse_channels_feed({"objectId": "o1", "desc": {"description": "无直链"}})
        self.assertIsNotNone(aw)
        self.assertEqual(aw.medias, [])
        self.assertEqual(aw.desc, "无直链")

    def test_feed_rejects_garbage_and_missing_id(self):
        self.assertIsNone(parse_channels_feed(None))
        self.assertIsNone(parse_channels_feed("x"))
        self.assertIsNone(parse_channels_feed({"description": "无 id"}))

    def test_comment_nested_user_info(self):
        raw = {
            "commentId": "c9",
            "content": 12345,                          # 非字符串也要兜底
            "createtime": 1_700_000_000,
            "likeCount": 7,
            "replyCommentId": "root1",
            "userInfo": {"nickName": "评论者", "headUrl": "https://x/h.jpg"},
        }
        c = parse_channels_comment(raw)
        self.assertEqual(c["comment_id"], "c9")
        self.assertEqual(c["text"], "12345")
        self.assertEqual(c["user_nickname"], "评论者")
        self.assertEqual(c["like_count"], 7)
        self.assertEqual(c["reply_to"], "root1")

    def test_comment_flatten_sub_comment_variants(self):
        root_a = {"commentId": "a", "subCommentList": [{"commentId": "a1"}]}
        root_b = {"commentId": "b", "replyList": [{"commentId": "b1"},
                                                   {"commentId": "b2"}]}
        flat = flatten_channels_comments([root_a, root_b, "junk"])
        self.assertEqual([c["commentId"] for c in flat],
                         ["a", "a1", "b", "b1", "b2"])

    def test_self_user_nested_finder_user(self):
        u = {"data": 0, "finderUser": {
            "nickname": "我", "username": "v2_abc@finder",
            "headImgUrl": "https://x/a.jpg", "fansCount": 88, "feedCount": 5}}
        p = parse_self_user(u)
        self.assertEqual(p["nickname"], "我")
        self.assertEqual(p["sec_uid"], "v2_abc@finder")
        self.assertEqual(p["avatar"], "https://x/a.jpg")
        self.assertEqual(p["follower_count"], 88)
        self.assertEqual(p["aweme_count"], 5)

    def test_self_user_empty_shape(self):
        p = parse_self_user(None)
        self.assertEqual(p["nickname"], "")
        self.assertEqual(p["follower_count"], 0)

    def test_dig_comments_candidate_keys(self):
        self.assertEqual(_dig_comments({"data": {"rootComments": [1]}}), [1])
        self.assertEqual(_dig_comments({"data": {"objectComments": [2]}}), [2])
        self.assertEqual(_dig_comments({"data": {"x": 1}}), [])

    def test_mm_business_ok_variants(self):
        self.assertEqual(_mm_business_ok({"errCode": 0}), (True, ""))
        ok, _ = _mm_business_ok({"baseResponse": {"retCode": "0"}})
        self.assertTrue(ok)
        ok, msg = _mm_business_ok({"errCode": -201, "errMsg": "freq"})
        self.assertFalse(ok)
        self.assertIn("freq", msg)
        ok, _ = _mm_business_ok({"baseResponse": {"retCode": -1, "retMsg": "deny"}})
        self.assertFalse(ok)
        # 无业务码不阻断(交调用方按未确认处理)
        self.assertEqual(_mm_business_ok({"hello": 1}), (True, ""))


# ───────────────────────── fake 浏览器骨架 ─────────────────────────

class FakeLocator:
    def __init__(self, count=0, enabled=True, on_click=None, files_box=None):
        self._count = count
        self._enabled = enabled
        self._on_click = on_click
        self._files_box = files_box
        self.filled = None
        self.files_set = None

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_enabled(self):
        return self._enabled

    async def click(self, timeout=None):
        if self._on_click:
            self._on_click()

    async def fill(self, text, timeout=None):
        self.filled = text

    async def set_input_files(self, files, timeout=None):
        self.files_set = files
        if self._files_box is not None:
            self._files_box.extend(files)


class FakeKeyboard:
    def __init__(self):
        self.typed = []
        self.pressed = []

    async def type(self, text, delay=0):
        self.typed.append(text)

    async def press(self, key):
        self.pressed.append(key)


class FakeFrame:
    def __init__(self, locators=None, texts=None, diag="f0:host ta=1 ce=0 btn=[发送]"):
        self._locators = locators or {}
        self._texts = set(texts or [])
        self._diag = diag

    def locator(self, sel):
        return self._locators.get(sel, FakeLocator(0))

    def get_by_text(self, text, exact=False):
        return FakeLocator(1 if text in self._texts else 0)

    async def evaluate(self, script):
        return self._diag


class FakeResp:
    def __init__(self, url, data, status=200):
        self.url = url
        self.status = status
        self._data = data

    async def json(self):
        return self._data


class FakePage:
    """最小可用 page:frames[0] 即主 frame;goto 时把预置响应发给监听器。"""

    def __init__(self, frames=None, start_url="", goto_url=None, emit=None):
        self.frames = frames or [FakeFrame()]
        self.url = start_url
        self._goto_url = goto_url
        self._emit_per_goto = list(emit or [])
        self._handlers = []
        self.keyboard = FakeKeyboard()
        self.closed = False
        self.mouse = SimpleNamespace(wheel=lambda *a, **k: None)

    def on(self, event, handler):
        self._handlers.append(handler)

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = self._goto_url(url) if callable(self._goto_url) else (
            self._goto_url or url)
        for resp in self._emit_per_goto:
            for h in self._handlers:
                asyncio.create_task(h(resp))

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)

    async def close(self):
        self.closed = True


class FakeCtx:
    def __init__(self, page):
        self.page = page
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class FakeMgr:
    def __init__(self, page):
        self.page = page
        self.ctx = None

    async def new_page(self, identity=None, block_media=True):
        return self.page

    async def open_headed(self, identity=None):
        self.ctx = FakeCtx(self.page)
        return self.ctx


# ───────────────────────── 发布流程 ─────────────────────────

class ChannelsPublishFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_files_returns_evidence_error(self):
        ok, url, err = await publish_channels(
            FakeMgr(FakePage()), object(), "{}", "video", "t", "d",
            ["D:/no/such/file.mp4"], headed=True)
        self.assertFalse(ok)
        self.assertEqual(url, "")
        self.assertIn("没有可用的本地媒体文件", err)

    async def test_logged_out_after_open(self):
        page = FakePage(goto_url=lambda u: "https://channels.weixin.qq.com/login.html")
        mgr = FakeMgr(page)
        ok, _, err = await publish_channels(
            mgr, object(), "{}", "video", "", "",
            [__file__], headed=True)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("logged_out"))
        self.assertTrue(mgr.ctx.closed)

    async def test_video_publish_success_jump_to_list(self):
        files_box = []
        file_loc = FakeLocator(1, files_box=files_box)
        desc_loc = FakeLocator(1)

        def on_publish():
            page.url = ("https://channels.weixin.qq.com/platform/"
                        "finderNewLifePostList?index=0")

        pub_loc = FakeLocator(1, on_click=on_publish)
        frame = FakeFrame(locators={
            "input[type=\"file\"]": file_loc,
            "div[contenteditable=\"true\"]": desc_loc,
            "button:has-text(\"发表\")": pub_loc,
        })
        page = FakePage(frames=[frame],
                        start_url="https://channels.weixin.qq.com/platform/post/create",
                        goto_url=lambda u: u)
        ok, result_url, err = await publish_channels(
            FakeMgr(page), object(), "{}", "video", "", "测试描述",
            [__file__], headed=True, timeout_seconds=4)
        self.assertTrue(ok, msg=err)
        self.assertIn("postlist", result_url.lower())
        self.assertEqual(err, "")
        self.assertEqual(files_box, [__file__])
        self.assertIn("测试描述", page.keyboard.typed)

    async def test_failure_toast_returns_evidence(self):
        file_loc = FakeLocator(1)
        desc_loc = FakeLocator(1)
        pub_loc = FakeLocator(1)            # 点击不改 URL
        frame = FakeFrame(locators={
            "input[type=\"file\"]": file_loc,
            "div[contenteditable=\"true\"]": desc_loc,
            "button:has-text(\"发表\")": pub_loc,
        }, texts={"发表失败"})
        page = FakePage(frames=[frame],
                        start_url="https://channels.weixin.qq.com/platform/post/create",
                        goto_url=lambda u: u)
        ok, _, err = await publish_channels(
            FakeMgr(page), object(), "{}", "video", "", "x",
            [__file__], headed=True, timeout_seconds=4)
        self.assertFalse(ok)
        self.assertIn("发表失败", err)
        self.assertIn("DOM诊断", err)


# ───────────────────────── 评论读取 ─────────────────────────

REPLY_URL = ("https://channels.weixin.qq.com/cgi-bin/"
             "mmfinderassistant-bin/comment/replyComment")
COMMENT_LIST_URL = ("https://channels.weixin.qq.com/cgi-bin/"
                    "mmfinderassistant-bin/comment/list")


class ChannelsCommentFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_comments_tries_candidate_routes_and_dedups(self):
        resp = FakeResp(COMMENT_LIST_URL,
                        {"errCode": 0,
                         "data": {"rootComments": [
                             {"commentId": "c1", "content": "一"},
                             {"commentId": "c2", "content": "二"},
                         ]}})
        page = FakePage(goto_url=lambda u: u, emit=[resp])
        new, err = await fetch_channels_comments(
            FakeMgr(page), object(), "obj1", known_cids={"c1"},
            max_scrolls=2, settle_ms=0)
        self.assertEqual(err, "")
        self.assertEqual([c["commentId"] for c in new], ["c2"])
        self.assertTrue(page.closed)

    async def test_fetch_comments_logged_out(self):
        page = FakePage(goto_url=lambda u: "https://channels.weixin.qq.com/login")
        new, err = await fetch_channels_comments(
            FakeMgr(page), object(), "obj1", set(),
            max_scrolls=1, settle_ms=0)
        self.assertEqual(new, [])
        self.assertTrue(err.startswith("logged_out"))

    async def test_fetch_comments_no_api_lists_tried_routes(self):
        page = FakePage(goto_url=lambda u: u)
        new, err = await fetch_channels_comments(
            FakeMgr(page), object(), "obj1", set(),
            max_scrolls=1, settle_ms=0)
        self.assertEqual(new, [])
        self.assertIn("未拦截到评论", err)
        # 两个候选路由 + 一个旧兜底都应尝试
        for u in COMMENT_LIST_URLS:
            self.assertIn(u, err)


# ───────────────────────── 评论回复 ─────────────────────────

class ChannelsCommentReplyTests(unittest.IsolatedAsyncioTestCase):
    def _page(self, emit, texts=None, goto_url=None, locators=None):
        editor = FakeLocator(1)
        submit = FakeLocator(1)
        frame = FakeFrame(locators=locators or {
            'textarea[placeholder*="回复"]': editor,
            'button:has-text("发送")': submit,
        }, texts=texts or set())
        return FakePage(frames=[frame], emit=emit,
                        goto_url=goto_url or (lambda u: u)), editor, submit

    async def test_empty_content_rejected(self):
        ok, err = await post_channels_comment(
            FakeMgr(FakePage()), object(), "o1", "   ", headed=False)
        self.assertFalse(ok)
        self.assertIn("空文案", err)

    async def test_logged_out(self):
        page = FakePage(goto_url=lambda u: "https://channels.weixin.qq.com/login")
        ok, err = await post_channels_comment(
            FakeMgr(page), object(), "o1", "好的", headed=False, settle_ms=0)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("logged_out"))

    async def test_reply_success_confirmed_by_api(self):
        page, editor, submit = self._page(
            [FakeResp(REPLY_URL, {"errCode": 0, "data": {}})])
        ok, err = await post_channels_comment(
            FakeMgr(page), object(), "o1", "谢谢支持", headed=False, settle_ms=0)
        self.assertTrue(ok, msg=err)
        self.assertEqual(err, "")
        self.assertIn("谢谢支持", page.keyboard.typed)

    async def test_reply_rejected_by_business_code(self):
        page, _, _ = self._page(
            [FakeResp(REPLY_URL, {"errCode": -201, "errMsg": "freq limit"})])
        ok, err = await post_channels_comment(
            FakeMgr(page), object(), "o1", "x", headed=False, settle_ms=0)
        self.assertFalse(ok)
        self.assertIn("拒绝回复", err)
        self.assertIn("freq limit", err)

    async def test_reply_unconfirmed_without_api_response(self):
        page, _, _ = self._page(emit=[])
        ok, err = await post_channels_comment(
            FakeMgr(page), object(), "o1", "x", headed=False, settle_ms=0)
        self.assertFalse(ok)
        self.assertIn("无法确认", err)
        self.assertIn("DOM诊断", err)

    async def test_reply_editor_missing_returns_diag(self):
        # 所有候选页都没有回复框
        page = FakePage(frames=[FakeFrame(locators={})],
                        goto_url=lambda u: u)
        ok, err = await post_channels_comment(
            FakeMgr(page), object(), "o1", "x", headed=False, settle_ms=0)
        self.assertFalse(ok)
        self.assertIn("未找到评论回复框", err)


if __name__ == "__main__":
    unittest.main()
