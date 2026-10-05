"""授权文件（license.dat）校验核心。

授权文件是一个经过 RSA-2048 私钥签名的文本文件，内容为两行：

    <base64 编码的 JSON 载荷>
    <base64 编码的 RSA 签名>

载荷字段：
    version     授权格式版本（当前 1）
    customer    客户名称
    fingerprint 绑定的机器指纹；空字符串表示不绑定机器
    issued_at   签发时间（Unix 秒）
    expire_at   到期时间（Unix 秒）；0 表示永久授权

公钥 ``license_pubkey.pem`` 随程序分发；私钥仅服务人员持有（tools/keys/，不打包）。
客户拿到服务人员分发的 license.dat 后，放到程序 exe 同目录或用户数据目录即可；
更新授权后无需重启，刷新页面即会重新校验。

开发/测试时设置环境变量 ``CREATORHUB_SKIP_LICENSE=1`` 可跳过校验。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

LICENSE_FILENAME = "license.dat"
PUBKEY_FILENAME = "license_pubkey.pem"
PAYLOAD_VERSION = 1
WARN_DAYS = 7                      # 到期前 7 天开始提醒
SKIP_ENV = "CREATORHUB_SKIP_LICENSE"

# 校验状态
VALID = "valid"
MISSING = "missing"
MALFORMED = "malformed"
SIGNATURE_INVALID = "signature_invalid"
MACHINE_MISMATCH = "machine_mismatch"
EXPIRED = "expired"
SKIPPED = "skipped"

STATUS_TEXT = {
    VALID: "授权有效",
    MISSING: "未找到授权文件",
    MALFORMED: "授权文件已损坏",
    SIGNATURE_INVALID: "授权文件签名无效",
    MACHINE_MISMATCH: "授权与本机不匹配",
    EXPIRED: "授权已到期",
    SKIPPED: "开发模式（已跳过授权）",
}


def skip_enabled() -> bool:
    return str(os.environ.get(SKIP_ENV, "")).strip() in {"1", "true", "TRUE", "yes"}


def _resource_root() -> Path:
    """打包后返回 _MEIPASS，源码运行返回仓库根目录。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def public_key_pem() -> Optional[bytes]:
    """读取内置公钥；找不到返回 None（此时只能按开发模式放行或报错）。"""
    candidates = [
        _resource_root() / PUBKEY_FILENAME,
        Path(__file__).resolve().parent / PUBKEY_FILENAME,
    ]
    for path in candidates:
        try:
            if path.is_file():
                return path.read_bytes()
        except OSError:
            continue
    return None


def machine_fingerprint() -> str:
    """计算本机机器指纹：稳定的系统标识 + 主网卡 MAC 的 SHA256。"""
    system_id = ""
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Cryptography") as key:
                system_id, _ = winreg.QueryValueEx(key, "MachineGuid")
        except OSError:
            system_id = ""
    else:
        for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                text = Path(candidate).read_text(encoding="utf-8").strip()
                if text:
                    system_id = text
                    break
            except OSError:
                continue
    raw = f"{system_id}|{uuid.getnode():012x}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def candidate_paths() -> list[Path]:
    """license.dat 的查找位置（按优先级）。"""
    paths: list[Path] = []
    # 1) exe 同目录（绿色版最直观的位置）
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).resolve().parent / LICENSE_FILENAME)
    # 2) 当前工作目录（桌面版 serve 子进程的 cwd 即用户数据目录）
    paths.append(Path.cwd() / LICENSE_FILENAME)
    # 3) 源码运行：data/license.dat
    paths.append(Path.cwd() / "data" / LICENSE_FILENAME)
    # 4) 桌面用户数据目录（mmm 新目录优先，CreatorHub 旧目录兼容）
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        paths.append(Path(local_appdata) / "mmm" / "user-data" / LICENSE_FILENAME)
        paths.append(Path(local_appdata) / "CreatorHub" / "user-data" / LICENSE_FILENAME)
    # 去重保序
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_license_file() -> Optional[Path]:
    for path in candidate_paths():
        if path.is_file():
            return path
    return None


def _verify_signature(payload: bytes, signature: bytes) -> bool:
    pem = public_key_pem()
    if pem is None:
        return False
    try:
        from Crypto.PublicKey import RSA
        from Crypto.Signature import pkcs1_15
        from Crypto.Hash import SHA256
        key = RSA.import_key(pem)
        pkcs1_15.new(key).verify(SHA256.new(payload), signature)
        return True
    except Exception:
        return False


def verify_license_file(path: Path) -> tuple[Optional[dict], str]:
    """校验单个授权文件，返回 (payload, status)。payload 无效时为 None。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None, MALFORMED
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 2:
        return None, MALFORMED
    try:
        payload_bytes = base64.b64decode(lines[0], validate=True)
        signature = base64.b64decode(lines[1], validate=True)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None, MALFORMED
    if not isinstance(payload, dict) or payload.get("version") != PAYLOAD_VERSION:
        return None, MALFORMED
    required = ("customer", "fingerprint", "issued_at", "expire_at")
    if not all(key in payload for key in required):
        return None, MALFORMED
    if not _verify_signature(payload_bytes, signature):
        return None, SIGNATURE_INVALID
    bound = str(payload.get("fingerprint") or "").strip().lower()
    if bound and bound != machine_fingerprint().lower():
        return payload, MACHINE_MISMATCH
    try:
        expire_at = float(payload.get("expire_at") or 0)
    except (TypeError, ValueError):
        return None, MALFORMED
    if expire_at and time.time() > expire_at:
        return payload, EXPIRED
    return payload, VALID


def evaluate(now: Optional[float] = None) -> dict:
    """综合评估当前授权状态（每次调用都重新读文件，便于换新授权后即时生效）。"""
    fingerprint = machine_fingerprint()
    searched = [str(p) for p in candidate_paths()]
    base = {
        "fingerprint": fingerprint,
        "searched_paths": searched,
        "warn_days": WARN_DAYS,
        "license_file": None,
        "payload": None,
        "customer": "",
        "issued_at": 0,
        "expire_at": 0,
        "days_remaining": None,
        "perpetual": False,
    }
    if skip_enabled():
        return {**base, "status": SKIPPED, "status_text": STATUS_TEXT[SKIPPED]}

    path = find_license_file()
    if path is None:
        return {**base, "status": MISSING, "status_text": STATUS_TEXT[MISSING]}

    payload, status = verify_license_file(path)
    result = {
        **base,
        "status": status,
        "status_text": STATUS_TEXT[status],
        "license_file": str(path),
        "payload": payload,
    }
    if payload:
        result["customer"] = str(payload.get("customer") or "")
        result["issued_at"] = int(payload.get("issued_at") or 0)
        result["expire_at"] = int(payload.get("expire_at") or 0)
        expire_at = float(payload.get("expire_at") or 0)
        if expire_at:
            current = now if now is not None else time.time()
            result["days_remaining"] = max(
                0, round((expire_at - current) / 86400, 1))
        else:
            result["perpetual"] = True
            result["days_remaining"] = None
    return result


def gate_passed(result: Optional[dict] = None) -> bool:
    """是否放行：只有有效或开发模式放行。"""
    result = result or evaluate()
    return result["status"] in {VALID, SKIPPED}
