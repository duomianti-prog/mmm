"""TikTok 海外网络闸门。

硬规则(区别于既有 verify_proxy_region 的"只告警"):
1. 账号必须绑定代理,禁止本机裸连;
2. 必须能经代理探测到出口 IP 与归属地,探测失败即阻断(不给出"也许能用"的放行);
3. 出口地区必须满足:账号级 required_exit_country 优先;否则落在全局允许列表
   (若配置)且不在封禁列表内。

通过后返回的画像(en-US 界面语言 + 出口国时区 + 出口坐标)由
:func:`apply_ready_to_account` 写回账号,浏览器上下文据此组装,保证
代理出口、IP 归属地、时区、界面语言四者一致。
"""
from __future__ import annotations

import time
from datetime import datetime

from ...browser.ip_fingerprint import normalize_country, timezone_for_geo
from ...config import Config

REASON_NO_PROXY = (
    "TikTok 要求先为账号绑定海外代理后再操作，已按安全策略禁止本机直连；"
    "请在账号设置中选择代理后重试")
REASON_PROBE_FAILED = (
    "代理出口探测失败，无法确认当前是允许地区，已阻止连接；"
    "请检查代理是否可用后重试（不会在代理不可用时裸连）")
REASON_NO_COUNTRY = (
    "代理出口未返回有效归属地，已按安全策略阻止连接；请更换更稳定的海外代理")


def country_list(raw: str) -> set[str]:
    """解析逗号/分号/顿号分隔的 ISO2 地区配置(容忍空格/小写/中文别名)。"""
    text = str(raw or "")
    for sep in (";", "；", "，", "、", "|", "/"):
        text = text.replace(sep, ",")
    return {code for code in (
        normalize_country(part) for part in text.split(",")) if code}


class TiktokNetworkPolicy:
    """无状态判定 + 按账号短期缓存出口探测结果。"""

    def __init__(self) -> None:
        # account_key -> (proxy, required_country, expires_at, result_dict)
        self._cache: dict[object, tuple] = {}

    @staticmethod
    def decide(*, proxy: str, geo: dict | None, cfg: Config,
               required_country: str = "") -> tuple[bool, str]:
        """纯判定,不做网络 IO,便于单测穷举。"""
        if cfg.engine.tiktok_require_proxy and not str(proxy or "").strip():
            return False, REASON_NO_PROXY
        if not geo:
            return False, REASON_PROBE_FAILED
        country = normalize_country(geo.get("country") or "")
        if not country:
            return False, REASON_NO_COUNTRY
        wanted = normalize_country(required_country)
        if wanted:
            if country != wanted:
                return False, (
                    f"该账号要求 {wanted} 地区出口，当前代理出口为 {country}；"
                    "请更换与账号要求地区一致的长效代理")
        allowed = country_list(cfg.engine.tiktok_allowed_exit_countries)
        if allowed and country not in allowed:
            return False, (
                f"当前代理出口 {country} 不在允许地区列表内；请更换指定地区的代理")
        blocked = country_list(cfg.engine.tiktok_blocked_exit_countries)
        if country in blocked:
            return False, (
                f"当前代理出口 {country} 为 TikTok 不可用地区；请更换海外代理")
        return True, ""

    async def ensure_ready(self, *, cfg: Config, proxy: str,
                           required_country: str = "", account_id=None,
                           prober=None, force_refresh: bool = False,
                           now: float | None = None) -> dict:
        """执行闸门。prober 可注入(签名同 netfp.probe_ip_region)。"""
        proxy = str(proxy or "").strip()
        required = normalize_country(required_country)
        moment = time.time() if now is None else now
        key = account_id if account_id is not None else f"anon:{proxy or 'direct'}"

        if not force_refresh:
            hit = self._cache.get(key)
            if hit and hit[0] == proxy and hit[1] == required and hit[2] > moment:
                return dict(hit[3])

        geo = None
        # 即使未来关闭"必须代理",也绝不允许在空代理下把直连当海外出口:
        # 探测结果必须带有效归属地,decide 仍会拦截 CN/HK。
        if proxy or not cfg.engine.tiktok_require_proxy:
            if prober is None:
                from ...netfp import probe_ip_region
                prober = probe_ip_region
            try:
                geo = await prober(
                    proxy, float(cfg.engine.tiktok_exit_probe_timeout_seconds))
            except Exception:
                geo = None

        ok, reason = self.decide(
            proxy=proxy, geo=geo, cfg=cfg, required_country=required)
        result: dict = {"ok": ok, "reason": reason}
        if geo:
            country = normalize_country(geo.get("country") or "")
            result.update({
                "ip": str(geo.get("ip") or ""),
                "country": country,
                "city": str(geo.get("city") or ""),
                "lat": float(geo.get("lat") or 0.0),
                "lon": float(geo.get("lon") or 0.0),
            })
            if ok:
                result["timezone_id"] = timezone_for_geo(
                    country, fallback="America/New_York")
                result["locale"] = str(cfg.engine.tiktok_locale or "en-US")
        ttl = max(0, int(cfg.engine.tiktok_network_cache_ttl_seconds))
        self._cache[key] = (proxy, required, moment + ttl, dict(result))
        return result

    def invalidate(self, account_id=None) -> None:
        if account_id is None:
            self._cache.clear()
        else:
            self._cache.pop(account_id, None)

    @staticmethod
    def apply_to_account(account, ready: dict) -> None:
        apply_ready_to_account(account, ready)


def apply_ready_to_account(account, ready: dict) -> None:
    """把通过闸门的出口画像写回账号(浏览器上下文与后续任务据此组装)。"""
    if not ready or not ready.get("ok"):
        return
    account.locale = str(ready.get("locale") or "en-US")
    if ready.get("timezone_id"):
        account.timezone_id = ready["timezone_id"]
    if ready.get("country"):
        account.fp_country = ready["country"]
        account.exit_country = ready["country"]
    if ready.get("ip"):
        account.exit_ip = ready["ip"]
    lat = float(ready.get("lat") or 0.0)
    lon = float(ready.get("lon") or 0.0)
    if lat and lon:
        account.geo_lat, account.geo_lon = lat, lon
    account.proxy_status = "ok"
    account.exit_checked_at = datetime.utcnow()


network_policy = TiktokNetworkPolicy()
