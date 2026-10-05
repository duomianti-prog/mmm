# -*- coding: utf-8 -*-
"""工作台回投节点：认领云客服服务器上的待回投任务并在本机执行。

纯客服服务器（app.cs_server，MMM_CS_ONLY=1）没有平台登录态，坐席回复产生的
CsRelayJob 只能由持有平台账号浏览器 Profile 的 Windows 工作台执行。本 worker
定时轮询节点接口：

1. 上报本机处于有效登录态的平台账号（platform + sec_uid 稳定标识 + 本地 id）；
2. 拉取服务端按账号匹配给本节点的任务；
3. 复用本机引擎的回投链路（relay_message → 私信/评论写操作）；
4. 把平台结果回报给服务端，由服务端写回 relayed 标记并通知坐席。

健壮性（Task 15）：
- claim 幂等：服务端对同一条坐席消息全程只发一个任务（幂等键 idem_key）；
  本节点把「已执行结果」按幂等键持久化，认领超时被服务端回收重发时
  不再执行平台写操作，只补报缓存结果——平台侧绝不重复发布。
- 回报不丢：结果回报失败时落本地待报队列，下一轮先补报再拉新任务；
  超过 24h 的待报放弃（服务端认领超时回收路径兜底）。
- 轮询退避：连续异常按 30s→60s→120s→240s 指数退避（封顶 300s），
  任一轮成功即复位；有任务时 3s 快速连拉。
- 心跳/断线重连：每轮认领即服务端心跳（含账号路由表），异常退避后自动重连。

配置全部存在 AppSetting（界面「客服接待 → 回投节点」配置），热生效，无需重启。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx
from sqlmodel import select

from ..db import get_session
from ..models import DouyinAccount
from ..settings import get_setting, set_setting
from .relay import relay_message

log = logging.getLogger("cs-node")

SET_ENABLED = "cs_node_enabled"
SET_URL = "cs_node_url"
SET_TOKEN = "cs_node_token"
SET_NODE_ID = "cs_node_id"
SET_STATUS = "cs_node_status"
SET_EXECUTED = "cs_node_executed"            # 已执行结果缓存 idem_key -> 结果
SET_PENDING_REPORTS = "cs_node_pending_reports"  # 回报失败待补报队列
SET_LAN_ENABLED = "cs_lan_enabled"      # 本机作为局域网客服服务器(监听 0.0.0.0)
SET_LAN_PORT = "cs_lan_port"            # 局域网服务端口,默认 8080,重启生效

DEFAULT_LAN_PORT = 8080

POLL_INTERVAL_SECONDS = 15        # 空闲轮询节奏
ERROR_BACKOFF_SECONDS = 30        # 网络/鉴权失败退避基数
MAX_BACKOFF_SECONDS = 300         # 连续异常退避封顶
CLAIM_MAX_JOBS = 3                # 每轮最多认领（写操作顺序执行，避免并发风控）
HTTP_TIMEOUT = httpx.Timeout(connect=10, read=30, write=30, pool=10)
# 已执行结果缓存与待报队列的容量上限（AppSetting 单值，超出裁最旧）
RESULT_CACHE_MAX = 200
PENDING_MAX = 100
# 待补报最长保留时间：超时放弃，交给服务端认领超时回收路径兜底
REPORT_MAX_AGE_SECONDS = 24 * 3600
# 本机登录态有效的账号才参与认领；平台以私信/评论回投实际支持的为准
SUPPORTED_PLATFORMS = ("douyin", "xhs", "kuaishou", "shipinhao")


def node_error_backoff(consecutive: int) -> int:
    """连续第 consecutive 次轮询异常后的退避秒数：30s 指数递增，封顶 300s。"""
    return min(ERROR_BACKOFF_SECONDS * (2 ** max(0, consecutive - 1)),
               MAX_BACKOFF_SECONDS)


def job_idem_key(job: dict) -> str:
    """任务的平台写操作幂等键：优先服务端下发，兼容旧任务按 message_id 推导。"""
    key = str(job.get("idem_key") or "").strip()
    if key:
        return key
    mid = int(job.get("message_id") or 0)
    if mid:
        return f"relay:m{mid}"
    return f"job:{job.get('id')}"


def _load_json_setting(key: str, default):
    raw = get_setting(key, "")
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except Exception:
        return default
    return value if isinstance(value, type(default)) else default


def _save_json_setting(key: str, value) -> None:
    set_setting(key, json.dumps(value, ensure_ascii=False)[:6000])


def _remember_result(idem_key: str, ok: bool, error: str,
                     retryable) -> None:
    """按幂等键持久化执行结果：回报丢失/任务重发时据此拒绝重复执行。"""
    if not idem_key:
        return
    cache = _load_json_setting(SET_EXECUTED, {})
    cache[idem_key] = {"ok": bool(ok), "error": (error or "")[:300],
                       "retryable": retryable,
                       "at": time.time()}
    if len(cache) > RESULT_CACHE_MAX:
        newest = sorted(cache.items(), key=lambda kv: kv[1].get("at", 0))
        cache = dict(newest[-RESULT_CACHE_MAX:])
    _save_json_setting(SET_EXECUTED, cache)


def _recall_result(idem_key: str):
    if not idem_key:
        return None
    return _load_json_setting(SET_EXECUTED, {}).get(idem_key)


def _queue_pending_report(job: dict, ok: bool, error: str,
                          retryable) -> None:
    """结果回报失败：进本地待报队列（同任务去重），下轮先补报。"""
    pending = [p for p in _load_json_setting(SET_PENDING_REPORTS, [])
               if p.get("job_id") != int(job.get("id") or 0)]
    pending.append({"job_id": int(job.get("id") or 0), "ok": bool(ok),
                    "error": (error or "")[:300], "retryable": retryable,
                    "at": time.time()})
    _save_json_setting(SET_PENDING_REPORTS, pending[-PENDING_MAX:])


def _pending_count() -> int:
    return len(_load_json_setting(SET_PENDING_REPORTS, []))


def _is_direct_host(url: str) -> bool:
    """目标是否必须绕过系统代理直连（回环/局域网/链路本地/内网单标签名）。

    httpx 默认 trust_env=True，会读取 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY。
    工作机常开 Clash/V2Ray/企业代理，发往 192.168.* 的请求被送进代理后
    通常直接回 502 Bad Gateway，根本到不了客服服务器。公网云服务器地址
    仍走系统代理，兼容必须通过代理出网的企业网络。
    """
    try:
        host = (urlsplit(url).hostname or "").strip("[]").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or "." not in host:
        return True                       # localhost / 内网单标签机器名
    if host.endswith((".local", ".internal", ".home.arpa")):
        return True                       # mDNS / 内网域名
    try:
        ip = ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    check = mapped or ip
    return bool(check.is_loopback or check.is_private
                or check.is_link_local)


def _http_client(*, url: str, headers: dict | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers,
                             trust_env=not _is_direct_host(url))


def _node_id() -> str:
    node_id = (get_setting(SET_NODE_ID, "") or "").strip()
    if not node_id:
        node_id = uuid.uuid4().hex
        set_setting(SET_NODE_ID, node_id)
    return node_id


def lan_server_config() -> dict:
    """局域网客服服务器配置与运行状态(监听 socket 由桌面启动器创建)。"""
    try:
        port = int((get_setting(SET_LAN_PORT, "") or "").strip()
                   or DEFAULT_LAN_PORT)
    except ValueError:
        port = DEFAULT_LAN_PORT
    return {
        "lan_enabled": get_setting(SET_LAN_ENABLED, "") == "1",
        "lan_port": port,
        "lan_ip": _lan_ip(),
    }


def _lan_ip() -> str:
    """本机局域网 IP( UDP 连接式探测,不真正发包);失败返回空串。"""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 80))
            return s.getsockname()[0]
    except Exception:
        return ""


def normalize_server_url(url: str) -> str:
    """统一服务器地址形态：补 http:// 前缀、去首尾空白与末尾斜杠。

    用户常只填 `192.168.0.10:8080` 不带协议；不补全则 urlsplit 解析不出
    主机名，内网直连判定失效（错误地走系统代理），httpx 也无法发请求。
    """
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def node_config() -> dict:
    return {
        "enabled": get_setting(SET_ENABLED, "") == "1",
        "url": normalize_server_url(get_setting(SET_URL, "")),
        "has_token": bool((get_setting(SET_TOKEN, "") or "").strip()),
        "node_id": _node_id(),
        "status": _load_status(),
        **lan_server_config(),
    }


def save_node_config(*, enabled: bool | None = None,
                     url: str | None = None,
                     token: str | None = None,
                     lan_enabled: bool | None = None,
                     lan_port: int | None = None) -> None:
    if enabled is not None:
        set_setting(SET_ENABLED, "1" if enabled else "")
    if url is not None:
        set_setting(SET_URL, normalize_server_url(url))
    if token is not None and token.strip():
        set_setting(SET_TOKEN, token.strip())
    if lan_enabled is not None:
        set_setting(SET_LAN_ENABLED, "1" if lan_enabled else "")
    if lan_port is not None and 1 <= int(lan_port) <= 65535:
        set_setting(SET_LAN_PORT, str(int(lan_port)))
    # 配置变更后清掉旧的连通状态：此前代理拦截等残留错误(如 502)不应再展示
    _save_status(connected=False, error="")


def _load_status() -> dict:
    raw = get_setting(SET_STATUS, "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_status(**fields) -> None:
    status = _load_status()
    status.update(fields)
    status["checked_at"] = datetime.utcnow().isoformat() + "Z"
    set_setting(SET_STATUS, json.dumps(status, ensure_ascii=False)[:2000])


class RelayNodeWorker:
    """挂载在工作台主事件循环上的轮询任务。"""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False
        self._consec_errors = 0        # 连续轮询异常次数（退避用）

    def start(self) -> None:
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._loop())
            log.info("客服回投节点已启动（未启用时保持空闲轮询）")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            delay = POLL_INTERVAL_SECONDS
            try:
                if await self._tick():
                    delay = 3          # 有任务时尽快拉取下一批
                self._consec_errors = 0
            except Exception as e:
                self._consec_errors += 1
                delay = node_error_backoff(self._consec_errors)
                log.warning("回投节点轮询异常（连续第 %d 次，退避 %ds）: %r",
                            self._consec_errors, delay, e)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    # ── 单轮：补报欠账 → 拉任务 → 本机执行 → 回报 ──

    async def _tick(self) -> bool:
        cfg = node_config()
        if not cfg["enabled"] or not cfg["url"] or not cfg["has_token"]:
            return False
        headers = {"X-CS-Node-Token": (get_setting(SET_TOKEN, "") or "").strip()}
        accounts = _local_accounts()
        async with _http_client(url=cfg["url"], headers=headers) as client:
            # 1) 先补报上一轮没送到的结果（回报不丢；服务端幂等，重复报无害）
            pending_left = await self._flush_reports(client, cfg["url"],
                                                     cfg["node_id"])
            resp = await client.post(
                cfg["url"] + "/api/cs/node/claim",
                json={"node_id": cfg["node_id"], "accounts": accounts,
                      "max_jobs": CLAIM_MAX_JOBS})
            if resp.status_code != 200:
                _save_status(connected=False,
                             error=f"认领被拒 HTTP {resp.status_code}: "
                                   f"{resp.text[:150]}",
                             pending=_pending_count())
                return False
            jobs = (resp.json() or {}).get("jobs") or []
            if not jobs:
                _save_status(connected=True, error="",
                             pending=_pending_count())
                return False
            executed = 0
            for job in jobs:
                idem_key = job_idem_key(job)
                cached = _recall_result(idem_key)
                if cached is not None:
                    # 该平台写操作在本机已执行过（上轮回报丢失、服务端超时
                    # 回收重发）——绝不再执行，直接补报缓存结果。
                    log.info("回投任务 %s 已执行过（idem=%s），补报缓存结果",
                             job.get("id"), idem_key)
                    ok = bool(cached.get("ok"))
                    error = str(cached.get("error") or "")
                    retryable = cached.get("retryable")
                else:
                    ok, error, retryable = await self._execute(job)
                    # 先落缓存再回报：回报途中断电/断网，重发也不会重复执行
                    _remember_result(idem_key, ok, error, retryable)
                executed += 1
                await self._report(client, cfg["node_id"], job, ok, error,
                                   retryable)
            _save_status(connected=True, error="",
                         pending=_pending_count(),
                         last_executed=datetime.utcnow().isoformat() + "Z",
                         executed_count=_load_status().get("executed_count", 0)
                         + executed)
            return len(jobs) >= CLAIM_MAX_JOBS

    async def _flush_reports(self, client: httpx.AsyncClient, url: str,
                             node_id: str) -> int:
        """补报本地待报队列；成功或被服务端明确拒绝（400）即移除。"""
        pending = _load_json_setting(SET_PENDING_REPORTS, [])
        if not pending:
            return 0
        now = time.time()
        kept = []
        for item in pending:
            if now - float(item.get("at") or 0) > REPORT_MAX_AGE_SECONDS:
                continue                     # 太久放弃：服务端超时回收兜底
            try:
                resp = await client.post(
                    f"{url}/api/cs/node/jobs/{int(item['job_id'])}/result",
                    json=self._result_payload(node_id, bool(item.get("ok")),
                                              str(item.get("error") or ""),
                                              item.get("retryable")))
                if resp.status_code == 200:
                    continue
                if resp.status_code == 400:
                    continue                 # 服务端明确拒绝（任务不存在等）
            except Exception as e:
                log.warning("补报结果失败 job=%s: %r", item.get("job_id"), e)
            kept.append(item)
        _save_json_setting(SET_PENDING_REPORTS, kept)
        return len(kept)

    @staticmethod
    def _result_payload(node_id: str, ok: bool, error: str,
                        retryable) -> dict:
        payload = {"node_id": node_id, "ok": ok, "error": error[:500]}
        if retryable is not None:
            payload["retryable"] = bool(retryable)
        return payload

    async def _execute(self, job: dict) -> tuple[bool, str, object]:
        """本机引擎执行回投（与坐席在本机直接回复走同一条写链路）。

        返回 ``(ok, error, retryable)``；retryable 为 None 表示交由服务端
        按错误文案分类（Task 14 的 classify_relay_failure）。
        """
        snapshot = {
            "source": job.get("source") or "",
            "account_id": int(job.get("account_id") or 0),
            "account_key": job.get("account_key") or "",
            "thread_key": job.get("thread_key") or "",
            "conv_id": int(job.get("conv_id") or 0),
            "message_id": int(job.get("message_id") or 0),
        }
        try:
            result = await relay_message(snapshot, job.get("text") or "")
        except Exception as e:
            return False, f"工作台回投异常：{e!r}", None
        # 本机引擎理应在线；万一引擎未启动也要给出明确结果
        if result.get("state") == "queued":
            return False, "工作台引擎未运行（平台登录态不可用）", False
        return bool(result.get("ok")), str(result.get("error") or ""), None

    async def _report(self, client: httpx.AsyncClient, node_id: str,
                      job: dict, ok: bool, error: str,
                      retryable=None) -> None:
        url = normalize_server_url(get_setting(SET_URL, ""))
        try:
            resp = await client.post(
                f"{url}/api/cs/node/jobs/{int(job['id'])}/result",
                json=self._result_payload(node_id, ok, error or "", retryable))
            if resp.status_code == 200 or resp.status_code == 400:
                return
            _queue_pending_report(job, ok, error, retryable)
        except Exception as e:
            # 回报失败：进本地待报队列下轮补报；即使本机数据丢失，
            # 服务端也会在认领超时后回收任务（幂等键保证不会重复执行）
            log.warning("回投结果回报失败 job=%s: %r", job.get("id"), e)
            _queue_pending_report(job, ok, error, retryable)


def _local_accounts() -> list[dict]:
    """本机可用于回投的平台账号（有效登录态 + 有稳定标识）。"""
    with get_session() as s:
        rows = s.exec(select(DouyinAccount).where(
            DouyinAccount.platform.in_(SUPPORTED_PLATFORMS))).all()
        result = []
        for a in rows:
            if a.status == "invalid" or not (a.sec_uid or "").strip():
                continue
            result.append({"platform": a.platform,
                           "account_key": a.sec_uid.strip(),
                           "account_id": a.id})
        return result


async def probe_node_connection(url: str, token: str) -> dict:
    """界面保存配置时做一次连通性自检。"""
    url = normalize_server_url(url)
    token = (token or "").strip()
    if not url or not token:
        return {"ok": False, "error": "服务器地址和令牌都不能为空"}
    try:
        async with _http_client(url=url) as client:
            resp = await client.get(
                url + "/api/cs/node/status",
                headers={"X-CS-Node-Token": token})
    except httpx.ConnectError as e:
        return {"ok": False, "error":
                f"连不上 {url}（TCP 连接失败）。请确认：①服务器进程正在运行"
                "（控制台有 Uvicorn running 一行）；②地址和端口填写正确；"
                "③服务器防火墙已放行该端口"}
    except httpx.TimeoutException:
        return {"ok": False, "error":
                f"连接 {url} 超时。请检查网络连通性（可在本机 PowerShell 执行 "
                "Test-NetConnection 服务器IP -Port 端口 验证）"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"HTTP 请求失败：{e!r}"}
    except Exception as e:
        return {"ok": False, "error": f"无法连接服务器：{e!r}"}
    if resp.status_code == 200:
        return {"ok": True, "queue": (resp.json() or {}).get("queue") or {}}
    if resp.status_code == 502:
        return {"ok": False, "error":
                "HTTP 502：请求被本机代理/网关拦截转发后失败，未到达客服服务器。"
                "若地址是内网 IP 请确认已升级到最新版本（内网地址会自动绕过代理）；"
                "若走公网请检查代理软件对该地址的处理规则"}
    if resp.status_code == 503 and "令牌" in resp.text:
        return {"ok": False, "error":
                "HTTP 503：服务器未设置回投节点令牌。请先在服务器「坐席管理 → "
                "回投节点令牌」中设置，或配置 MMM_CS_NODE_TOKEN 环境变量"}
    return {"ok": False,
            "error": f"HTTP {resp.status_code}：{resp.text[:200]}"}
