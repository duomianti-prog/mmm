# -*- coding: utf-8 -*-
"""客服子系统数据表。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Index, text
from sqlmodel import Field, SQLModel

# 会话来源：guest=访客链接；{platform}_dm / {platform}_comment=平台私信/评论转人工
SOURCE_GUEST = "guest"
STATUS_QUEUED = "queued"
STATUS_ACTIVE = "active"
STATUS_CLOSED = "closed"

SENDER_CUSTOMER = "customer"
SENDER_AGENT = "agent"
SENDER_SYSTEM = "system"

# 平台入站类型
KIND_DM = "dm"
KIND_COMMENT = "comment"
# 统一收件箱已支持/预留的平台（快手具备私信能力时自动纳入）
CS_PLATFORMS = ("douyin", "xhs", "tiktok", "kuaishou")


def make_source(platform: str, kind: str) -> str:
    """组合会话来源标识：douyin_dm / tiktok_comment ..."""
    platform = (platform or "").strip().lower()
    kind = (kind or "").strip().lower()
    if not platform or not kind:
        return ""
    return f"{platform}_{kind}"


def parse_source(source: str) -> tuple[str, str]:
    """拆分来源标识为 (platform, kind)。

    guest 及无法识别的格式返回 ("", "")。按最后一个下划线拆分，
    kind 仅认可 dm / comment。
    """
    source = (source or "").strip().lower()
    if not source or source == SOURCE_GUEST or "_" not in source:
        return "", ""
    platform, _, kind = source.rpartition("_")
    if kind not in (KIND_DM, KIND_COMMENT):
        return "", ""
    return platform, kind


class CsAgent(SQLModel, table=True):
    """客服坐席账号。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str = ""
    salt: str = ""
    display_name: str = ""
    role: str = "agent"                 # admin | agent
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CsSession(SQLModel, table=True):
    """坐席登录会话（只存 token 的哈希）。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True)
    agent_id: int = Field(index=True)
    user_agent: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    revoked: bool = False


class CsConversation(SQLModel, table=True):
    """客服会话。一条会话 = 一位客户与客服团队的对话。

    访客链接会话 source=guest；平台转人工 source 形如 douyin_dm / xhs_comment，
    并通过 account_id + thread_key 回链平台对象（私信 conv_id 或评论 work_id:cid）。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    token: str = Field(index=True, unique=True)   # 访客公开令牌
    source: str = Field(default=SOURCE_GUEST, index=True)
    account_id: int = Field(default=0, index=True)
    # 平台账号稳定标识（抖音 sec_uid / 小红书 user_id）：跨节点匹配持有该账号
    # 登录态的工作台；account_id 只是发起转人工那台工作台的本地库 id，多节点不可靠。
    account_key: str = Field(default="", index=True)
    thread_key: str = Field(default="", index=True)  # 平台会话/评论定位键
    customer_name: str = ""
    customer_avatar: str = ""
    status: str = Field(default=STATUS_QUEUED, index=True)  # queued | active | closed
    owner_agent_id: Optional[int] = Field(default=None, index=True)
    last_text: str = ""
    last_time: int = 0                           # unix 秒
    unread_agent: int = 0                        # 客服侧未读
    created_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None


class CsMessage(SQLModel, table=True):
    """客服会话中的单条消息。"""
    __table_args__ = (
        # 平台入站消息全局幂等键（仅非空生效）：同一平台消息重复投递只入库一次。
        # 访客/坐席消息 idem_key 为空，多条空值互不冲突。
        Index("ux_csmessage_idemkey", "idem_key", unique=True,
              sqlite_where=text("idem_key != ''")),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    conv_id: int = Field(index=True)
    sender_kind: str = SENDER_CUSTOMER           # customer | agent | system
    agent_id: Optional[int] = None
    sender_name: str = ""
    msg_type: str = "text"
    text: str = ""
    relayed: bool = False                        # 平台来源会话是否已成功回投
    platform_msg_id: str = ""
    # 平台消息幂等键：{source}|{account_key 或本地 id 作用域}|{platform_msg_id}
    idem_key: str = Field(default="", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CsParticipant(SQLModel, table=True):
    """被邀请加入会话的坐席（含归属人；多对多，支持协作接待）。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    conv_id: int = Field(index=True)
    agent_id: int = Field(index=True)
    joined_at: datetime = Field(default_factory=datetime.utcnow)


class CsInboundCursor(SQLModel, table=True):
    """各平台入站拉取游标（私信/评论按账号+类型一行）。

    坐席不在线/节点离线期间，入站总线依据游标补拉，保证不丢不重。
    """
    __table_args__ = (
        Index("ux_csinboundcursor_scope",
              "platform", "account_key", "kind", unique=True),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="", index=True)
    account_key: str = Field(default="", index=True)
    kind: str = Field(default=KIND_DM, index=True)   # dm | comment
    cursor: str = ""                                 # 平台私有的不透明游标/时间戳
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CsAccountReception(SQLModel, table=True):
    """账号级接待开关：关闭后该平台账号的入站消息不自动建档/镜像到统一收件箱。

    无记录视为开启（历史部署平滑兼容）。
    """
    __table_args__ = (
        Index("ux_csaccountreception_account",
              "platform", "account_key", unique=True),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="", index=True)
    account_key: str = Field(default="", index=True)
    enabled: bool = True
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CsNodeHeartbeat(SQLModel, table=True):
    """回投工作台节点心跳与路由信息（持久化，服务重启不丢）。

    accounts_json 为节点上报的可回投账号清单
    [{platform, account_key, account_id}]，是「该由哪个节点执行」的路由依据。
    """
    node_id: str = Field(primary_key=True)
    platforms: str = ""                        # 逗号分隔，冗余便于展示
    account_count: int = 0
    accounts_json: str = ""
    last_seen: int = Field(default=0, index=True)   # unix 秒
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# 回投任务生命周期
RELAY_PENDING = "pending"    # 等待工作台认领（含瞬时故障退避等待期）
RELAY_CLAIMED = "claimed"    # 已被某工作台认领，执行中
RELAY_DONE = "done"          # 已成功回投平台（对坐席展示为 sent）
RELAY_FAILED = "failed"      # 工作台明确回报失败（永久故障或重试已耗尽）
RELAY_TIMEOUT = "timeout"    # 认领后多次超时无回报，结果未知，需人工核查/重发

# 失败类别
FAIL_TRANSIENT = "transient"   # 瞬时故障（网络/繁忙/超时）：可自动退避重试
FAIL_PERMANENT = "permanent"   # 永久故障（登录态/风控/参数）：不自动重试
FAIL_TIMEOUT = "timeout"       # 认领超时无回报


class CsRelayJob(SQLModel, table=True):
    """跨节点平台回投任务。

    纯客服服务器节点（无平台登录态）收到坐席回复后入队；持有该平台账号
    登录态的 Windows 工作台通过节点接口认领、在本地执行写操作，再回报结果。

    幂等：idem_key 全局唯一（``relay:m<坐席消息id>``），同一条坐席回复无论
    入队/自动重试/手动重发多少轮都只有一行、一个幂等键，工作台据此保证
    平台侧写操作不重复。
    """
    __table_args__ = (
        Index("ux_csrelayjob_idemkey", "idem_key", unique=True,
              sqlite_where=text("idem_key != ''")),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    conv_id: int = Field(index=True)
    message_id: int = Field(index=True)
    source: str = ""                                  # douyin_comment / xhs_dm ...
    platform: str = Field(default="", index=True)
    kind: str = ""                                    # dm | comment
    account_id: int = 0                               # 发起节点的本地账号 id（单节点兜底）
    account_key: str = Field(default="", index=True)  # 平台稳定标识 sec_uid/user_id
    thread_key: str = ""
    text: str = ""
    status: str = Field(default=RELAY_PENDING, index=True)
    attempts: int = 0                                 # 认领/执行次数
    claimed_by: str = Field(default="", index=True)   # 节点 id
    claimed_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None            # 瞬时故障退避：到此时间才可再认领
    fail_kind: str = ""                               # "" | transient | permanent | timeout
    result_error: str = ""
    idem_key: str = Field(default="", index=True)     # 平台写操作全局幂等键
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
