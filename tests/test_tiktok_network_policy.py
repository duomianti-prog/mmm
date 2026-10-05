"""Task 2: TikTok 海外网络闸门与出口驱动的浏览器画像。

全部离线:网络探测用桩,不发起真实请求。
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.config import Config
from app.platforms import registry as pf
from app.platforms.tiktok.network_policy import (
    TiktokNetworkPolicy,
    apply_ready_to_account,
    country_list,
    network_policy,
)
import app.platforms.tiktok  # noqa: F401  导入即自注册
from app.browser.identity import Identity
from test_project_optimizations import local_project, store  # noqa: F401


US_GEO = {"ip": "1.2.3.4", "country": "US", "city": "New York",
          "lat": 40.7128, "lon": -74.0060}
PROXY = "http://user:pass@hk.example:8000"


def test_country_list_parsing():
    assert country_list("US, CA, jp") == {"US", "CA", "JP"}
    assert country_list("US；香港、CN") == {"US", "HK", "CN"}
    assert country_list("") == set()


def test_config_defaults_are_safe():
    cfg = Config()
    assert cfg.engine.tiktok_require_proxy is True
    assert cfg.engine.tiktok_blocked_exit_countries.upper().replace(" ", "") == "CN,HK"
    assert cfg.engine.tiktok_locale == "en-US"
    assert cfg.engine.tiktok_exit_probe_timeout_seconds > 0


def test_decide_blocks_all_unsafe_cases():
    cfg = Config()
    p = TiktokNetworkPolicy()

    ok, reason = p.decide(proxy="", geo=None, cfg=cfg)
    assert ok is False and "代理" in reason

    ok, _ = p.decide(proxy=PROXY, geo=None, cfg=cfg)
    assert ok is False  # 探测失败:不裸连放行

    ok, _ = p.decide(proxy=PROXY, geo={"country": "", "lat": 0}, cfg=cfg)
    assert ok is False  # 无归属地

    ok, reason = p.decide(proxy=PROXY, geo={"country": "CN"}, cfg=cfg)
    assert ok is False and "CN" in reason
    assert not p.decide(proxy=PROXY, geo={"country": "HK"}, cfg=cfg)[0]

    # 账号级要求地区优先
    ok, reason = p.decide(proxy=PROXY, geo={"country": "JP"}, cfg=cfg,
                          required_country="US")
    assert ok is False and "US" in reason and "JP" in reason

    # 全局允许列表
    cfg.engine.tiktok_allowed_exit_countries = "US,CA"
    assert not p.decide(proxy=PROXY, geo={"country": "JP"}, cfg=cfg)[0]
    assert p.decide(proxy=PROXY, geo={"country": "CA"}, cfg=cfg)[0]


def test_decide_allows_supported_exit():
    cfg = Config()
    p = TiktokNetworkPolicy()
    for code in ("US", "JP", "GB", "SG", "DE"):
        assert p.decide(proxy=PROXY, geo={"country": code}, cfg=cfg)[0]
    ok, _ = p.decide(proxy=PROXY, geo=US_GEO, cfg=cfg, required_country="US")
    assert ok


def run(coro):
    return asyncio.run(coro)


def test_ensure_ready_success_builds_aligned_profile():
    cfg = Config()
    calls = []

    async def prober(proxy, timeout):
        calls.append((proxy, timeout))
        return US_GEO

    p = TiktokNetworkPolicy()
    ready = run(p.ensure_ready(
        cfg=cfg, proxy=PROXY, account_id=7, prober=prober))
    assert ready["ok"] is True
    assert ready["country"] == "US"
    assert ready["timezone_id"] == "America/New_York"
    assert ready["locale"] == "en-US"
    assert ready["lat"] == 40.7128 and ready["lon"] == -74.0060
    assert calls == [(PROXY, cfg.engine.tiktok_exit_probe_timeout_seconds)]


def test_ensure_ready_caches_until_proxy_changes_or_forced():
    cfg = Config()
    cfg.engine.tiktok_network_cache_ttl_seconds = 300
    calls = []

    async def prober(proxy, timeout):
        calls.append(proxy)
        return US_GEO

    p = TiktokNetworkPolicy()
    kw = dict(cfg=cfg, proxy=PROXY, account_id=9, prober=prober)
    run(p.ensure_ready(**kw))
    run(p.ensure_ready(**kw))
    assert len(calls) == 1  # TTL 内复用
    run(p.ensure_ready(**kw, force_refresh=True))
    assert len(calls) == 2
    run(p.ensure_ready(**{**kw, "proxy": "http://other:8001"}))
    assert len(calls) == 3  # 换代理重新探测
    run(p.ensure_ready(**{**kw, "proxy": ""}))
    assert len(calls) == 3  # 无代理直接阻断,不探测
    run(p.ensure_ready(**kw, now=0))  # 时钟推进后过期
    assert len(calls) == 4


def test_ensure_ready_probe_exception_blocks_without_leaking():
    cfg = Config()

    async def boom(proxy, timeout):
        raise RuntimeError("proxy unreachable")

    p = TiktokNetworkPolicy()
    ready = run(p.ensure_ready(
        cfg=cfg, proxy=PROXY, account_id=11, prober=boom))
    assert ready["ok"] is False and ready["reason"]


def test_registry_self_registration_and_gate_dispatch():
    spec = pf.get("tiktok")
    assert spec is not None
    # Task 3 起平台开放(登录/体检),网络闸门仍独立于能力挂载
    assert spec.enabled is True
    assert spec.pin_context_locale is True
    assert pf.has_cap("tiktok", pf.COOKIE_LOGIN)
    assert "tiktok" in [s.key for s in pf.all_specs()]
    assert pf.get_network_policy("tiktok") is not None
    # 未挂策略的平台无闸门
    assert run(pf.run_network_gate("douyin", cfg=Config(), proxy="x")) is None
    # 经注册表分派的 TikTok 闸门:无代理阻断
    ready = run(pf.run_network_gate(
        "tiktok", cfg=Config(), proxy="", account_id=3, force_refresh=True))
    assert ready["ok"] is False and "代理" in ready["reason"]


def test_apply_ready_to_account_persists_profile():
    acc = SimpleNamespace(
        locale="zh-CN", timezone_id="Asia/Shanghai", fp_country="",
        exit_country="", exit_ip="", geo_lat=0.0, geo_lon=0.0,
        proxy_status="unknown", exit_checked_at=None)
    ready = {"ok": True, "locale": "en-US", "timezone_id": "America/New_York",
             "country": "US", "ip": "1.2.3.4", "lat": 40.7, "lon": -74.0}
    apply_ready_to_account(acc, ready)
    assert acc.locale == "en-US"
    assert acc.timezone_id == "America/New_York"
    assert acc.exit_country == acc.fp_country == "US"
    assert acc.exit_ip == "1.2.3.4"
    assert acc.geo_lat == 40.7 and acc.geo_lon == -74.0
    assert acc.proxy_status == "ok" and acc.exit_checked_at is not None
    # 失败结果不改账号
    before = acc.locale
    apply_ready_to_account(acc, {"ok": False, "reason": "x"})
    assert acc.locale == before


def _account_ns(platform, locale, tz):
    base = dict(
        id=5, profile_dir="", storage_state="", creator_storage_state="",
        platform=platform, identity_mode="native", browser_backend="default",
        browser_runtime_id="", proxy=PROXY, ua="UA", viewport_w=1280,
        viewport_h=800, timezone_id=tz, locale=locale, fp_seed="seed",
        geo_lat=0.0, geo_lon=0.0,
        fp_platform="", fp_platform_version="", fp_brand="", fp_brand_version="",
        fp_hardware_concurrency=0, fp_gpu_vendor="", fp_gpu_renderer="",
        fp_accept_languages="", fp_disable_spoofing="",
        fp_language_mode="auto", fp_timezone_mode="auto",
        fp_viewport_mode="auto", fp_location_mode="auto",
        fp_geolocation_permission="allow", fp_webrtc_mode="conceal",
        fp_extra_args="")
    return SimpleNamespace(**base)


def test_identity_pins_locale_only_for_tiktok_platform():
    tt = Identity.from_account(
        _account_ns("tiktok", "en-US", "America/New_York"), ".", "UA")
    assert tt.pin_context_locale is True
    assert tt.locale == "en-US"
    assert tt.timezone_id == "America/New_York"
    assert tt.proxy == PROXY

    dy = Identity.from_account(
        _account_ns("douyin", "zh-CN", "Asia/Shanghai"), ".", "UA")
    assert dy.pin_context_locale is False

    unknown = Identity.from_account(
        _account_ns("future-platform", "en-US", "UTC"), ".", "UA")
    assert unknown.pin_context_locale is False


def test_monitor_gate_blocks_and_persists_on_success(
        local_project, monkeypatch):
    """周期体检路径:无策略平台无影响;TikTok 无代理阻断;通过后画像落库。"""
    import app.db as db
    import app.netfp as netfp
    from app.engine.monitor import MonitorEngine
    from app.models import DouyinAccount

    eng = SimpleNamespace(cfg=Config())
    gate = MonitorEngine._platform_network_gate

    assert asyncio.run(gate(eng, 999001, "douyin", "")) is None

    acc_id = store(DouyinAccount(platform="tiktok", status="active"))
    blocked = asyncio.run(gate(eng, acc_id, "tiktok", ""))
    assert blocked["ok"] is False and "代理" in blocked["reason"]

    async def fake_prober(proxy, timeout):
        return {"ip": "5.6.7.8", "country": "US", "city": "NYC",
                "lat": 40.7, "lon": -74.0}

    monkeypatch.setattr(netfp, "probe_ip_region", fake_prober)
    network_policy.invalidate(acc_id)
    ready = asyncio.run(gate(eng, acc_id, "tiktok", "http://proxy:8080"))
    assert ready["ok"] is True and ready["country"] == "US"

    with db.get_session() as s:
        row = s.get(DouyinAccount, acc_id)
        assert row.exit_country == row.fp_country == "US"
        assert row.exit_ip == "5.6.7.8"
        assert row.timezone_id == "America/New_York"
        assert row.locale == "en-US"
        assert row.proxy_status == "ok" and row.exit_checked_at
