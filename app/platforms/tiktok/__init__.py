"""TikTok(tiktok.com 国际版)平台适配包(v1.7.0)。

浏览器优先:不做私有 API 逆向、不伪造签名。包被导入即向平台注册表
登记自身;能力随 Task 4-11 切片逐项加入。
"""
from .network_policy import (
    TiktokNetworkPolicy,
    apply_ready_to_account,
    country_list,
    network_policy,
)
from .auto_comment import comment_tiktok_browser, tiktok_video_url
from .comments import (
    fetch_tiktok_comments,
    parse_tiktok_comment,
)
from .hub import (
    fetch_tiktok_works,
    norm_tiktok_work,
    parse_tiktok_handle,
)
from .dm import fetch_tiktok_dm_conversations, send_tiktok_dm
from .login import interactive_tiktok_login
from .profile import fetch_tiktok_self_profile
from .publish import publish_tiktok
from .social_action import follow_tiktok_browser
from .search import (
    extract_tiktok_search_items,
    fetch_tiktok_search,
    sort_tiktok_search_items,
    tiktok_search_item_matches,
)
from .share import (
    fetch_tiktok_share_item,
    is_tiktok_host,
    is_tiktok_short_url,
    parse_tiktok_item,
    parse_tiktok_item_url,
    select_tiktok_video_url,
)
from .webstate import (
    TT_LOGIN_COOKIES,
    extract_self_user,
    parse_tiktok_self_user,
    tt_login_ready,
)
from ..registry import (
    AUTO_COMMENT,
    COMMENT_MONITOR,
    COOKIE_LOGIN,
    DM,
    FOLLOW_SYNC,
    KEYWORD_COLLECTION,
    LINK_DOWNLOAD,
    OWN_WORKS,
    PUBLISH,
    SOCIAL_ACTION,
    PlatformSpec,
    WORK_MONITOR,
    register,
    register_network_policy,
)

# Task 3:账号登录(交互网页登录 + Cookie 兜底)与体检;
# Task 4:链接下载(长链/短链/视频/图集,浏览器读取作品数据 + 通用 Downloader);
# Task 5:我的内容(本账号作品分页同步 + 关注/粉丝列表同步);
# Task 6:创作者主页作品监控(增量水位线 + 自动下载)。
# Task 7:评论读取与评论监控;Task 8:关键词/标签批量采集;
# Task 9:创作者中心网页发布(三态裁决 + submitted 防重发 + 瞬时重试保留预约)。
# Task 10:自动评论/回复(浏览器证据化写 + 风控闸门);
# Task 11:关注动作与网页私信收件箱(网页不支持的能力在 UI 降级标注)。
register(PlatformSpec(
    key="tiktok",
    label="TikTok",
    capabilities=frozenset({COOKIE_LOGIN, LINK_DOWNLOAD,
                            OWN_WORKS, FOLLOW_SYNC, WORK_MONITOR,
                            COMMENT_MONITOR, KEYWORD_COLLECTION, PUBLISH,
                            AUTO_COMMENT, DM, SOCIAL_ACTION}),
    account_id_label="TikTok ID",
    sec_uid_label="sec_uid",
    enabled=True,
    color="#010101",
    pin_context_locale=True,
))
register_network_policy("tiktok", network_policy)

__all__ = [
    "TiktokNetworkPolicy", "apply_ready_to_account", "country_list",
    "network_policy", "interactive_tiktok_login",
    "fetch_tiktok_self_profile", "parse_tiktok_self_user",
    "tt_login_ready", "extract_self_user", "TT_LOGIN_COOKIES",
    "fetch_tiktok_share_item", "is_tiktok_host", "is_tiktok_short_url",
    "parse_tiktok_item", "parse_tiktok_item_url", "select_tiktok_video_url",
    "fetch_tiktok_works", "norm_tiktok_work", "parse_tiktok_handle",
    "fetch_tiktok_comments", "parse_tiktok_comment",
    "fetch_tiktok_search", "extract_tiktok_search_items",
    "sort_tiktok_search_items", "tiktok_search_item_matches",
    "publish_tiktok",
    "comment_tiktok_browser", "tiktok_video_url",
    "follow_tiktok_browser",
    "fetch_tiktok_dm_conversations", "send_tiktok_dm",
]
