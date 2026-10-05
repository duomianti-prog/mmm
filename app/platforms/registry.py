"""平台目录:集中管理平台标识、显示名与能力标志。

新增平台的唯一入口是在实现 ``app/platforms/<key>/`` 适配后调用
:func:`register` 注册一个 :class:`PlatformSpec`;账号白名单、平台标签、
登录兜底方式、监控/发布分派与前端平台 tab 一律从本注册表取信息,
不再在业务代码里散落 ``if platform == "<key>"`` 与内联名称字典。

本模块只依赖标准库,可被任意层(含浏览器层、前端目录接口)安全导入。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── 能力标志 ───────────────────────────────────────────────────────────
LINK_DOWNLOAD = "link_download"            # 分享链接解析与媒体下载
OWN_WORKS = "own_works"                    # 我的内容:本账号作品同步
FOLLOW_SYNC = "follow_sync"                # 关注/粉丝列表同步
WORK_MONITOR = "work_monitor"              # 创作者主页作品监控
COMMENT_MONITOR = "comment_monitor"        # 作品/主页评论监控
DANMAKU_MONITOR = "danmaku_monitor"        # 短视频弹幕监控
KEYWORD_COLLECTION = "keyword_collection"  # 关键词/标签批量采集
PUBLISH = "publish"                        # 作品发布(含跨平台转发)
AUTO_COMMENT = "auto_comment"              # 自动评论/回复
DM = "dm"                                  # 平台私信
SOCIAL_ACTION = "social_action"            # 关注/取关等互动动作
COOKIE_LOGIN = "cookie_login"              # 支持 Cookie 粘贴兜底登录
CREATOR_LOGIN = "creator_login"            # 存在独立的创作者登录态

ALL_CAPABILITIES = frozenset({
    LINK_DOWNLOAD, OWN_WORKS, FOLLOW_SYNC, WORK_MONITOR, COMMENT_MONITOR,
    DANMAKU_MONITOR, KEYWORD_COLLECTION, PUBLISH, AUTO_COMMENT, DM,
    SOCIAL_ACTION, COOKIE_LOGIN, CREATOR_LOGIN,
})


@dataclass(frozen=True)
class PlatformSpec:
    """一个平台的静态目录信息(运行期状态不放在这里)。"""
    key: str
    label: str
    capabilities: frozenset[str] = frozenset()
    # DouyinAccount.douyin_id / sec_uid 在该平台的展示名
    account_id_label: str = "账号 ID"
    sec_uid_label: str = "ID"
    # False = 已登记但暂不对用户开放(适配未完成),不出现在前端与白名单
    enabled: bool = True
    # 前端主题色(十六进制);缺省由前端使用中性色
    color: str = ""
    # True = 即使 native 画像也强制把账号 locale/时区传给浏览器上下文
    # (TikTok 界面语言必须固定 en-US、时区对齐出口国,不能跟随宿主系统)
    pin_context_locale: bool = False

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def to_catalog(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "enabled": self.enabled,
            "color": self.color,
            "pin_context_locale": self.pin_context_locale,
            "account_id_label": self.account_id_label,
            "sec_uid_label": self.sec_uid_label,
            "capabilities": sorted(self.capabilities),
        }


_REGISTRY: dict[str, PlatformSpec] = {}
_ORDER: list[str] = []


def register(spec: PlatformSpec) -> PlatformSpec:
    """登记平台;重复 key 以最后一次注册为准(测试可借此替换桩)。"""
    if spec.key not in _REGISTRY:
        _ORDER.append(spec.key)
    _REGISTRY[spec.key] = spec
    return spec


def get(key: str | None) -> PlatformSpec | None:
    return _REGISTRY.get(key or "")


def is_supported(key: str | None) -> bool:
    """已登记、已启用的平台才允许进入账号/登录等业务流程。"""
    spec = get(key)
    return bool(spec and spec.enabled)


def all_specs(*, include_disabled: bool = False) -> list[PlatformSpec]:
    specs = [_REGISTRY[k] for k in _ORDER if k in _REGISTRY]
    if not include_disabled:
        specs = [s for s in specs if s.enabled]
    return specs


def catalog() -> list[dict]:
    """供前端平台目录接口使用(含未启用项,前端据此隐藏)。"""
    return [s.to_catalog() for s in all_specs(include_disabled=True)]


def label_of(key: str | None, default: str | None = None) -> str:
    spec = get(key)
    if spec:
        return spec.label
    return default if default is not None else (key or "平台")


def account_id_label(key: str | None) -> str:
    spec = get(key)
    return spec.account_id_label if spec else "账号 ID"


def sec_uid_label(key: str | None) -> str:
    spec = get(key)
    return spec.sec_uid_label if spec else "账号 ID"


def has_cap(key: str | None, capability: str) -> bool:
    spec = get(key)
    return bool(spec and spec.enabled and spec.has(capability))


def keys_with(capability: str) -> set[str]:
    return {s.key for s in all_specs() if s.has(capability)}


# ── 已知平台「不支持某能力」的面向用户原因 ─────────────────────────────
# 仅登记 UI 曾暴露、需要在接口层同步拒绝的能力,原因必须与
# .trae/documents/sph_channels_feasibility.md 的 Spike 结论一致。
# 未登记的平台/能力组合仍走各端点既有的默认/校验逻辑(历史兼容)。
BLOCKED_CAPABILITY_REASONS: dict[str, dict[str, str]] = {
    "shipinhao": {
        WORK_MONITOR: "视频号助手未提供他人作品监控入口,仅支持管理本账号作品",
        COMMENT_MONITOR: "视频号助手仅支持查看与回复本账号作品下的评论,无法监控他人作品评论",
        KEYWORD_COLLECTION: "视频号助手未提供关键词搜索入口,无法按关键词采集他人内容",
        AUTO_COMMENT: "视频号助手仅支持回复本账号作品的评论,无法在他人作品下自动评论",
        DM: "v1.7.0 暂未接入视频号私信:助手仅有粉丝私信收件箱,且无主动私信入口",
        FOLLOW_SYNC: "视频号助手未提供关注/粉丝列表的读取入口",
    },
}


def blocked_capability_reason(key: str | None, capability: str) -> str | None:
    """已知平台缺少该能力时,返回面向用户的明确拒绝原因;平台未知(保持各
    端点默认平台兼容)或能力具备时返回 None。"""
    spec = get(key)
    if spec is None or spec.has(capability):
        return None
    return BLOCKED_CAPABILITY_REASONS.get(spec.key, {}).get(capability)


# ── 平台网络闸门(适配侧车)──────────────────────────────────────────────
# 平台包导入时用 register_network_policy 挂上自己的出口策略对象
# (需实现 async ensure_ready(**kwargs) -> dict);核心引擎/登录流程统一经
# run_network_gate 调用,不感知平台细节。返回 None 表示该平台无闸门。
_NETWORK_POLICIES: dict[str, object] = {}


def register_network_policy(key: str, policy: object) -> None:
    _NETWORK_POLICIES[key] = policy


def get_network_policy(key: str | None) -> object | None:
    return _NETWORK_POLICIES.get(key or "")


async def run_network_gate(key: str | None, **kwargs) -> dict | None:
    policy = _NETWORK_POLICIES.get(key or "")
    if policy is None:
        return None
    return await policy.ensure_ready(**kwargs)


def apply_network_ready(key: str | None, account, ready: dict | None) -> None:
    """闸门通过后把出口画像(locale/时区/坐标/出口记录)持久化到账号。"""
    if not ready or not ready.get("ok"):
        return
    policy = _NETWORK_POLICIES.get(key or "")
    if policy is not None:
        policy.apply_to_account(account, ready)


def cookie_login_platforms() -> set[str]:
    return keys_with(COOKIE_LOGIN)


# ── 内置平台(能力以当前代码真实支持面为准,改造不得改变现有可见行为) ─────
register(PlatformSpec(
    key="douyin", label="抖音",
    capabilities=ALL_CAPABILITIES,
    account_id_label="抖音号", sec_uid_label="sec_uid",
    color="#fe2c55",
))
register(PlatformSpec(
    key="xhs", label="小红书",
    capabilities=frozenset({
        LINK_DOWNLOAD, OWN_WORKS, FOLLOW_SYNC, WORK_MONITOR, COMMENT_MONITOR,
        KEYWORD_COLLECTION, PUBLISH, AUTO_COMMENT, DM, SOCIAL_ACTION,
        COOKIE_LOGIN, CREATOR_LOGIN,
    }),
    account_id_label="小红书号", sec_uid_label="user_id",
    color="#ff2442",
))
register(PlatformSpec(
    key="kuaishou", label="快手",
    capabilities=frozenset({
        LINK_DOWNLOAD, OWN_WORKS, FOLLOW_SYNC, WORK_MONITOR, COMMENT_MONITOR,
        PUBLISH, AUTO_COMMENT, SOCIAL_ACTION, COOKIE_LOGIN, CREATOR_LOGIN,
    }),
    account_id_label="快手号", sec_uid_label="user_id",
    color="#ff7902",
))
register(PlatformSpec(
    key="shipinhao", label="视频号",
    # 视频号助手网页端仅提供本账号能力:作品/数据/发布/本账号评论回复。
    # 他人作品监控、他人评论、关键词采集、主动私信均无入口(详见
    # .trae/documents/sph_channels_feasibility.md)。
    # 粉丝私信收件箱虽有入口,但 v1.7.0 范围内不实现 dm 能力。
    capabilities=frozenset({
        OWN_WORKS, PUBLISH, CREATOR_LOGIN,
    }),
    account_id_label="视频号", sec_uid_label="finder_id",
    color="#07c160",
))
