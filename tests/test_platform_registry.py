"""Task 1: 平台能力注册表与目录接口。

守卫点:
- 内置四平台显示名/能力面与历史行为一致;
- 登录兜底/监控/评论/自动评论白名单由能力推导,集合不漂移;
- 新平台仅需 register() 即进入目录与相关白名单(注册即生效);
- /api/platforms 路由存在且返回目录。
"""
import asyncio

import pytest

from app.platforms import registry as pf
import app.main as main


def test_builtin_labels_and_order():
    # tiktok 在 Task 3 已开放(登录/体检);四平台原顺序保持不变,tiktok 列末尾
    assert [s.key for s in pf.all_specs()] == [
        "douyin", "xhs", "kuaishou", "shipinhao", "tiktok"]
    spec = pf.get("tiktok")
    assert spec.enabled is True
    assert pf.has_cap("tiktok", pf.COOKIE_LOGIN)
    # Task 9 起 tiktok 开放创作者中心网页发布
    assert pf.has_cap("tiktok", pf.PUBLISH)
    assert pf.label_of("douyin") == "抖音"
    assert pf.label_of("xhs") == "小红书"
    assert pf.label_of("kuaishou") == "快手"
    assert pf.label_of("shipinhao") == "视频号"
    assert pf.label_of("nope", "兜底") == "兜底"
    assert pf.account_id_label("xhs") == "小红书号"
    assert pf.sec_uid_label("shipinhao") == "finder_id"


def test_builtin_capability_surface_matches_legacy_behavior():
    # 抖音:全能力
    assert pf.ALL_CAPABILITIES <= set(pf.get("douyin").capabilities)
    # 小红书:无弹幕,其余现有能力保留(含关键词采集与私信)
    xhs_caps = set(pf.get("xhs").capabilities)
    assert pf.DANMAKU_MONITOR not in xhs_caps
    assert {pf.KEYWORD_COLLECTION, pf.DM, pf.PUBLISH, pf.LINK_DOWNLOAD} <= xhs_caps
    # 快手:无弹幕/关键词/私信
    ks_caps = set(pf.get("kuaishou").capabilities)
    assert not ({pf.DANMAKU_MONITOR, pf.KEYWORD_COLLECTION, pf.DM} & ks_caps)
    assert {pf.WORK_MONITOR, pf.COMMENT_MONITOR, pf.PUBLISH,
            pf.AUTO_COMMENT, pf.LINK_DOWNLOAD} <= ks_caps
    # 视频号:仅本账号作品与发布
    assert set(pf.get("shipinhao").capabilities) == {
        pf.OWN_WORKS, pf.PUBLISH, pf.CREATOR_LOGIN}


def test_capability_derived_allowlists_unchanged():
    # Task 3: tiktok 开放 Cookie 兜底登录,其余能力白名单保持不变
    assert pf.cookie_login_platforms() == {
        "douyin", "xhs", "kuaishou", "tiktok"}
    assert pf.keys_with(pf.WORK_MONITOR) == {
        "douyin", "xhs", "kuaishou", "tiktok"}
    assert pf.keys_with(pf.COMMENT_MONITOR) == {
        "douyin", "xhs", "kuaishou", "tiktok"}
    assert pf.keys_with(pf.AUTO_COMMENT) == {"douyin", "xhs", "kuaishou", "tiktok"}
    assert pf.keys_with(pf.PUBLISH) == {
        "douyin", "xhs", "kuaishou", "shipinhao", "tiktok"}
    assert pf.keys_with(pf.DM) == {"douyin", "xhs", "tiktok"}
    assert pf.keys_with(pf.SOCIAL_ACTION) == {
        "douyin", "xhs", "kuaishou", "tiktok"}
    # Task 8: tiktok 开放关键词采集
    assert pf.keys_with(pf.KEYWORD_COLLECTION) == {
        "douyin", "xhs", "tiktok"}


@pytest.fixture
def temp_platform():
    created = []

    def _make(key, caps, enabled=True):
        spec = pf.PlatformSpec(
            key=key, label=key.upper(), capabilities=frozenset(caps),
            enabled=enabled)
        pf.register(spec)
        created.append(key)
        return spec

    yield _make
    for key in created:
        pf._REGISTRY.pop(key, None)
        if key in pf._ORDER:
            pf._ORDER.remove(key)


def test_registered_platform_is_effective_everywhere(temp_platform):
    # 新平台注册+给能力即:出现在目录、通过支持校验、进入对应白名单
    assert not pf.is_supported("acme")
    temp_platform("acme", {pf.COOKIE_LOGIN, pf.WORK_MONITOR, pf.PUBLISH})
    assert pf.is_supported("acme")
    assert "acme" in pf.cookie_login_platforms()
    assert "acme" in pf.keys_with(pf.PUBLISH)
    keys = [row["key"] for row in pf.catalog()]
    assert "acme" in keys
    row = next(r for r in pf.catalog() if r["key"] == "acme")
    assert row["label"] == "ACME"
    assert row["enabled"] is True
    assert "publish" in row["capabilities"]


def test_disabled_platform_hidden_but_listed(temp_platform):
    temp_platform("future", {pf.PUBLISH}, enabled=False)
    assert not pf.is_supported("future")
    assert "future" not in [s.key for s in pf.all_specs()]
    assert "future" not in pf.keys_with(pf.PUBLISH)
    # 目录仍带 enabled=false,前端据此隐藏入口
    row = next(r for r in pf.catalog() if r["key"] == "future")
    assert row["enabled"] is False


def test_catalog_endpoint_and_route():
    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/api/platforms" in paths
    rows = asyncio.run(main.platforms_catalog())
    # tiktok 已开放但仅具备登录能力,其余能力门在后续切片逐项打开
    assert {r["key"] for r in rows} == {
        "douyin", "xhs", "kuaishou", "shipinhao", "tiktok"}
    tt = next(r for r in rows if r["key"] == "tiktok")
    assert tt["enabled"] is True and "cookie_login" in tt["capabilities"]
    assert "publish" in tt["capabilities"]
    sample = next(r for r in rows if r["key"] == "kuaishou")
    assert sample["label"] == "快手"
    assert "dm" not in sample["capabilities"]
    assert "comment_monitor" in sample["capabilities"]
