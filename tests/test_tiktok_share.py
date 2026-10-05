"""Task 4: TikTok 分享链接解析与原生下载(纯函数 + 浏览器桩 + 分派)。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# local_project 定义在该模块,模块级导入才能让 pytest 注册为 fixture
from test_project_optimizations import local_project  # noqa: F401
from app.engine.share_downloader import ShareDownloadError, extract_share_urls
from app.platforms import registry as pf
from app.platforms.tiktok import share as tshare


def run(coro):
    return asyncio.run(coro)


# ── URL 解析 ──────────────────────────────────────────────────────────

def test_parse_long_video_and_photo_urls():
    assert tshare.parse_tiktok_item_url(
        "https://www.tiktok.com/@creator/video/7300000000000000000?lang=en"
    ) == ("video", "7300000000000000000")
    assert tshare.parse_tiktok_item_url(
        "https://www.tiktok.com/@creator/photo/7311111111111111111/"
    ) == ("photo", "7311111111111111111")
    assert tshare.parse_tiktok_item_url(
        "http://m.tiktok.com/v/7322222222222222222.html"
    ) == ("video", "7322222222222222222")


def test_short_and_non_tiktok_urls_return_none():
    assert tshare.parse_tiktok_item_url(
        "https://vm.tiktok.com/ZMrAbCdEf/") is None
    assert tshare.parse_tiktok_item_url(
        "https://vt.tiktok.com/ZSabc123/") is None
    assert tshare.is_tiktok_short_url("https://vm.tiktok.com/ZMrAbCdEf/")
    assert not tshare.is_tiktok_short_url(
        "https://www.tiktok.com/@u/video/1")
    assert tshare.parse_tiktok_item_url(
        "https://www.douyin.com/video/7300000000000000000") is None
    assert tshare.parse_tiktok_item_url(
        "https://www.tiktok.com/@creator") is None
    assert tshare.parse_tiktok_item_url("not a url") is None


def test_share_text_extraction_marks_tiktok_platform():
    text = ("Check this out! https://vt.tiktok.com/ZSabc123/ "
            "and https://www.tiktok.com/@u/video/7300000000000000000")
    links = extract_share_urls(text)
    assert [item.platform for item in links] == ["tiktok", "tiktok"]
    nested = "落地页 https://xxx.example/jump?target=https%3A%2F%2Fvm.tiktok.com%2FZzz1"
    assert extract_share_urls(nested)[0].platform == "tiktok"


# ── itemStruct 归一化 ─────────────────────────────────────────────────

def video_item():
    return {
        "id": "7300000000000000000",
        "desc": "hello tiktok",
        "createTime": 1700000000,
        "author": {
            "uniqueId": "creator",
            "nickname": "Creator Name",
            "avatarLarger": "https://cdn/avatar.jpg",
        },
        "statsV2": {"diggCount": "12", "commentCount": "3",
                    "playCount": "99"},
        "video": {
            "duration": 11,
            "cover": "https://cdn/cover.jpg",
            "playAddr": "https://cdn-fallback/play.mp4",
            "bitRateInfo": [
                {"Quality": 13, "Bitrate": 800_000,
                 "PlayAddr": {"Height": 576,
                              "UrlList": ["https://cdn/low.mp4"]}},
                {"Quality": 14, "Bitrate": 2_000_000,
                 "PlayAddr": {"Height": 1024,
                              "UrlList": ["https://cdn/high.mp4"]}},
            ],
        },
    }


def test_parse_video_item_picks_quality_and_fields():
    aweme = tshare.parse_tiktok_item(video_item(), "highest")
    assert aweme is not None
    assert aweme.platform == "tiktok"
    assert aweme.media_type == "video"
    assert aweme.aweme_id == "7300000000000000000"
    assert aweme.author_name == "Creator Name"
    assert aweme.desc == "hello tiktok"
    assert aweme.duration == 11
    assert aweme.like_count == 12 and aweme.comment_count == 3
    assert aweme.cover == "https://cdn/cover.jpg"
    assert len(aweme.medias) == 1
    assert aweme.medias[0].url == "https://cdn/high.mp4"
    assert aweme.quality_label == "1024p"

    low = tshare.parse_tiktok_item(video_item(), "lowest")
    assert low.medias[0].url == "https://cdn/low.mp4"

    mid = tshare.parse_tiktok_item(video_item(), "720")
    assert mid.medias[0].url == "https://cdn/low.mp4"


def test_parse_video_falls_back_to_play_addr_string():
    item = video_item()
    item["video"].pop("bitRateInfo")
    aweme = tshare.parse_tiktok_item(item)
    assert aweme.medias[0].url == "https://cdn-fallback/play.mp4"
    assert aweme.quality_label == ""

    item["video"]["playAddr"] = ""
    item["video"]["downloadAddr"] = "https://cdn/dl.mp4"
    aweme = tshare.parse_tiktok_item(item)
    assert aweme.medias[0].url == "https://cdn/dl.mp4"


def test_parse_photo_post_item():
    item = {
        "id": "7311111111111111111",
        "desc": "gallery",
        "createTime": 1700000100,
        "author": {"uniqueId": "creator", "nickname": "Creator Name"},
        "stats": {"diggCount": 5, "commentCount": 1},
        "imagePost": {
            "cover": {"imageURL": {"urlList": ["https://cdn/pcover.webp"]}},
            "images": [
                {"imageURL": {"urlList": [
                    "https://cdn/img1.webp?a=1", "https://cdn/img1-hi.webp"]}},
                {"imageURL": {"urlList": ["https://cdn/img2.jpeg"]}},
            ],
        },
    }
    aweme = tshare.parse_tiktok_item(item)
    assert aweme.media_type == "images"
    assert aweme.cover == "https://cdn/pcover.webp"
    assert [m.ext for m in aweme.medias] == ["webp", "jpeg"]
    assert [m.index for m in aweme.medias] == [0, 1]
    assert aweme.medias[0].url == "https://cdn/img1-hi.webp"
    assert aweme.like_count == 5


def test_parse_rejects_missing_id_or_media():
    assert tshare.parse_tiktok_item({"desc": "no id"}) is None
    assert tshare.parse_tiktok_item({"id": "abc"}) is None
    no_media = {"id": "7300000000000000000",
                "video": {"duration": 3}, "author": {}}
    assert tshare.parse_tiktok_item(no_media) is None


# ── 浏览器读取 ────────────────────────────────────────────────────────

class _SharePage:
    def __init__(self, reads, urls=None, goto_error=None):
        self._reads = list(reads)
        self._urls = list(urls or [])
        self._goto_error = goto_error
        self.closed = False

    async def goto(self, url, **kwargs):
        if self._goto_error:
            raise self._goto_error

    async def evaluate(self, js):
        if self._reads:
            value = self._reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return None

    async def wait_for_timeout(self, ms):
        return None

    @property
    def url(self):
        return self._urls[0] if self._urls else \
            "https://www.tiktok.com/@creator/video/7300000000000000000"

    async def close(self):
        self.closed = True


class _ShareMgr:
    def __init__(self, page):
        self._page = page

    async def new_page(self, identity, block_media=True):
        return self._page


def test_fetch_share_item_polls_until_data():
    page = _SharePage([None, None, video_item()])
    mgr = _ShareMgr(page)
    item, final_url, error = run(tshare.fetch_tiktok_share_item(
        mgr, SimpleNamespace(), "https://vm.tiktok.com/ZMr/",
        timeout_ms=3000, poll_interval_ms=500))
    assert error == "" and item["id"] == "7300000000000000000"
    assert page.closed


def test_fetch_share_item_detects_login_and_timeout():
    login_page = _SharePage(
        [None], urls=["https://www.tiktok.com/login?lang=en"])
    _item, _url, error = run(tshare.fetch_tiktok_share_item(
        _ShareMgr(login_page), SimpleNamespace(),
        "https://www.tiktok.com/x", timeout_ms=1000, poll_interval_ms=500))
    assert error == "login_required"

    timeout_page = _SharePage([None], urls=["https://www.tiktok.com/"])
    _item, _url, error = run(tshare.fetch_tiktok_share_item(
        _ShareMgr(timeout_page), SimpleNamespace(),
        "https://www.tiktok.com/x", timeout_ms=900, poll_interval_ms=500))
    assert error == "timeout"

    class _Boom(Exception):
        pass

    goto_page = _SharePage([], goto_error=_Boom("blocked"))
    _item, _url, error = run(tshare.fetch_tiktok_share_item(
        _ShareMgr(goto_page), SimpleNamespace(),
        "https://www.tiktok.com/x", timeout_ms=1000))
    assert error.startswith("goto:_Boom")


# ── main 分派与落盘 ───────────────────────────────────────────────────

@pytest.fixture()
def tt_context(local_project, monkeypatch, tmp_path):
    from test_project_optimizations import store
    from app import main
    from app.models import DouyinAccount

    acc_id = store(DouyinAccount(
        platform="tiktok", status="active", nickname="Creator Name",
        proxy="http://proxy:8080",
        storage_state=json.dumps({"cookies": [], "origins": []})))

    identity = SimpleNamespace(proxy="http://proxy:8080", ua="UA/TT",
                               key=f"acc:{acc_id}")
    # main.browser 在测试环境未必初始化,用最小桩替换(仅本用例需要的方法)
    monkeypatch.setattr(main, "browser", SimpleNamespace(
        identity_for=lambda account: identity,
        direct_request_user_agent=lambda ident: "UA/TT",
    ))
    return SimpleNamespace(
        main=main, store=store, account_id=acc_id, tmp_path=tmp_path,
        monkeypatch=monkeypatch, identity=identity,
        video_url="https://www.tiktok.com/@creator/video/7300000000000000000",
        photo_url="https://www.tiktok.com/@creator/photo/7311111111111111111",
    )


def _patch_read(ctx, payload, outcome=None, *, final_url=None):
    ctx.monkeypatch.setattr(
        ctx.main, "_run_account_read", AsyncMock(return_value=(
            {"item": payload,
             "final_url": final_url or ctx.video_url}, outcome)))


def test_native_share_without_account_falls_back_or_guides(tt_context):
    ctx = tt_context
    # 视频链接:无账号回退 yt-dlp
    assert run(ctx.main._tiktok_native_share(
        ctx.video_url, account_id=None, output_root=ctx.tmp_path,
        quality="highest", should_download=False,
        save_metadata=True, save_thumbnail=True, proxy="")) is None
    # 图集链接:无账号直接给可执行提示
    with pytest.raises(ShareDownloadError):
        run(ctx.main._tiktok_native_share(
            ctx.photo_url, account_id=None, output_root=ctx.tmp_path,
            quality="highest", should_download=True,
            save_metadata=True, save_thumbnail=True, proxy=""))


def test_native_share_skips_non_tiktok_account(tt_context):
    from app.models import DouyinAccount
    ctx = tt_context
    other_id = ctx.store(DouyinAccount(platform="douyin", status="active"))
    assert run(ctx.main._tiktok_native_share(
        ctx.video_url, account_id=other_id, output_root=ctx.tmp_path,
        quality="highest", should_download=False,
        save_metadata=True, save_thumbnail=True, proxy="")) is None


def test_native_share_proxy_must_match_account(tt_context):
    ctx = tt_context
    with pytest.raises(ShareDownloadError):
        run(ctx.main._tiktok_native_share(
            ctx.video_url, account_id=ctx.account_id,
            output_root=ctx.tmp_path, quality="highest",
            should_download=False, save_metadata=True,
            save_thumbnail=True, proxy="http://other-proxy:9999"))


def test_native_share_inspect_returns_metadata(tt_context):
    ctx = tt_context
    _patch_read(ctx, video_item())
    result = run(ctx.main._tiktok_native_share(
        ctx.video_url, account_id=ctx.account_id,
        output_root=ctx.tmp_path, quality="highest",
        should_download=False, save_metadata=True,
        save_thumbnail=True, proxy="http://proxy:8080"))
    assert result["ok"]
    meta = result["metadata"]
    assert meta["platform"] == "tiktok"
    assert meta["id"] == "7300000000000000000"
    assert meta["media_type"] == "video" and meta["media_count"] == 1
    assert meta["uploader"] == "Creator Name"


def test_native_share_read_errors_become_guidance(tt_context):
    ctx = tt_context
    _patch_read(ctx, {}, "login_required")
    with pytest.raises(ShareDownloadError, match="登录态已失效"):
        run(ctx.main._tiktok_native_share(
            ctx.video_url, account_id=ctx.account_id,
            output_root=ctx.tmp_path, quality="highest",
            should_download=False, save_metadata=True,
            save_thumbnail=True, proxy="http://proxy:8080"))

    _patch_read(ctx, {}, "timeout")
    with pytest.raises(ShareDownloadError, match="人机校验"):
        run(ctx.main._tiktok_native_share(
            ctx.video_url, account_id=ctx.account_id,
            output_root=ctx.tmp_path, quality="highest",
            should_download=False, save_metadata=True,
            save_thumbnail=True, proxy="http://proxy:8080"))

    _patch_read(ctx, {}, {"skipped": True, "reason": "账号冷却中"})
    with pytest.raises(ShareDownloadError, match="冷却"):
        run(ctx.main._tiktok_native_share(
            ctx.video_url, account_id=ctx.account_id,
            output_root=ctx.tmp_path, quality="highest",
            should_download=False, save_metadata=True,
            save_thumbnail=True, proxy="http://proxy:8080"))


def test_native_share_download_writes_files_and_is_idempotent(tt_context):
    ctx = tt_context
    _patch_read(ctx, video_item(), final_url=ctx.video_url)

    async def fake_download(self, aweme, base_dir="", proxy=""):
        out_dir = Path(base_dir) / "Creator Name"
        out_dir.mkdir(parents=True, exist_ok=True)
        fpath = out_dir / f"{aweme.aweme_id}_hello tiktok.mp4"
        fpath.write_bytes(b"fake-mp4")
        return True, str(fpath), ""

    ctx.monkeypatch.setattr(
        "app.engine.downloader.Downloader.download_aweme", fake_download)

    result = run(ctx.main._tiktok_native_share(
        ctx.video_url, account_id=ctx.account_id,
        output_root=ctx.tmp_path, quality="highest",
        should_download=True, save_metadata=True,
        save_thumbnail=False, proxy="http://proxy:8080"))
    assert result["ok"] and result["job_id"] == "tiktok_7300000000000000000"
    media = [f for f in result["files"] if f["role"] == "media"]
    assert len(media) == 1
    info = [f for f in result["files"] if f["role"] == "metadata"]
    assert info and Path(info[0]["path"]).exists()
    payload = json.loads(Path(info[0]["path"]).read_text(encoding="utf-8"))
    assert payload["id"] == "7300000000000000000"
    assert payload["media"][0]["url"] == "https://cdn/high.mp4"

    # 重复提交:成品文件已存在,Downloader 自身按路径跳过(幂等)。
    result2 = run(ctx.main._tiktok_native_share(
        ctx.video_url, account_id=ctx.account_id,
        output_root=ctx.tmp_path, quality="highest",
        should_download=True, save_metadata=True,
        save_thumbnail=False, proxy="http://proxy:8080"))
    assert result2["ok"]


# ── 注册面 ────────────────────────────────────────────────────────────

def test_tiktok_registry_has_link_download_only_after_task4():
    assert pf.has_cap("tiktok", pf.LINK_DOWNLOAD)
    assert pf.has_cap("tiktok", pf.COOKIE_LOGIN)
    assert pf.has_cap("tiktok", pf.PUBLISH)
    # Task 11 起 DM / SOCIAL_ACTION 合法开启
    assert pf.has_cap("tiktok", pf.DM)
    assert pf.has_cap("tiktok", pf.SOCIAL_ACTION)
    assert "tiktok" in pf.keys_with(pf.LINK_DOWNLOAD)
