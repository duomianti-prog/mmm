#!/usr/bin/env python3
"""授权签发工具（仅服务人员 / 管理员使用，不要随客户端分发）。

用法：

  # 首次使用：自动生成 RSA-2048 密钥对（私钥留在 tools/keys/，公钥复制到 app/）
  python tools/generate_license.py init-keys

  # 查看当前机器指纹（客户也可在授权失效窗口直接复制）
  python tools/generate_license.py fingerprint

  # 签发 90 天、绑定机器的授权
  python tools/generate_license.py issue --customer "张三公司" \
      --fingerprint a1b2c3... --days 90 --output license.dat

  # 签发永久授权（不绑定机器）
  python tools/generate_license.py issue --customer "演示账号" --perpetual --no-bind

  # 查看 / 验签一个授权文件
  python tools/generate_license.py info --file license.dat

license.dat 交给客户后，放到 mmm.exe 同目录（或用户数据目录）即可，
程序会实时读取，更新授权无需重启。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if getattr(sys, "frozen", False):
    # 冻结版（onefile）：__file__ 指向临时解包目录，密钥/公钥一律落在 exe 旁边。
    # license_pubkey.pem 供客户端构建时替换 app/license_pubkey.pem 使用。
    _BASE = Path(sys.executable).resolve().parent
    KEYS_DIR = _BASE / "keys"
    PUBLIC_KEY_IN_APP = _BASE / "license_pubkey.pem"
else:
    KEYS_DIR = Path(__file__).resolve().parent / "keys"
    PUBLIC_KEY_IN_APP = ROOT / "app" / "license_pubkey.pem"
PRIVATE_KEY = KEYS_DIR / "private_key.pem"
PUBLIC_KEY_IN_KEYS = KEYS_DIR / "public_key.pem"

PAYLOAD_VERSION = 1


def _add_root_to_path() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def cmd_init_keys(args) -> int:
    from Crypto.PublicKey import RSA
    if PRIVATE_KEY.exists() and not args.force:
        print(f"密钥已存在：{PRIVATE_KEY}")
        print("如需重新生成请加 --force（注意：换密钥后所有旧授权全部失效）。")
        return 1
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    key = RSA.generate(2048)
    PRIVATE_KEY.write_bytes(key.export_key("PEM"))
    PUBLIC_KEY_IN_KEYS.write_bytes(key.publickey().export_key("PEM"))
    PUBLIC_KEY_IN_APP.write_bytes(key.publickey().export_key("PEM"))
    print("密钥对已生成：")
    print(f"  私钥（妥善保管，禁止随客户端分发）：{PRIVATE_KEY}")
    print(f"  公钥（已复制到客户端）：{PUBLIC_KEY_IN_APP}")
    return 0


def cmd_fingerprint(args) -> int:
    _add_root_to_path()
    from app.license_core import machine_fingerprint
    print(machine_fingerprint())
    return 0


def cmd_issue(args) -> int:
    from Crypto.PublicKey import RSA
    from Crypto.Signature import pkcs1_15
    from Crypto.Hash import SHA256

    if not PRIVATE_KEY.is_file():
        print(f"找不到私钥：{PRIVATE_KEY}，请先运行 init-keys。")
        return 1

    # 到期时间
    if args.perpetual:
        expire_at = 0
    elif args.expire:
        expire_at = int(datetime.strptime(args.expire, "%Y-%m-%d")
                        .replace(hour=23, minute=59, second=59,
                                 tzinfo=timezone.utc).timestamp())
    else:
        days = args.days if args.days is not None else 90
        if days <= 0:
            print("--days 必须为正整数（永久授权请用 --perpetual）。")
            return 1
        expire_at = int(time.time() + days * 86400)

    fingerprint = ""
    if not args.no_bind:
        fingerprint = (args.fingerprint or "").strip().lower()
        if not fingerprint:
            print("需要 --fingerprint（客户机器指纹）；如确需不绑定机器请加 --no-bind。")
            return 1

    payload = {
        "version": PAYLOAD_VERSION,
        "customer": args.customer.strip(),
        "fingerprint": fingerprint,
        "issued_at": int(time.time()),
        "expire_at": expire_at,
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8")
    key = RSA.import_key(PRIVATE_KEY.read_bytes())
    signature = pkcs1_15.new(key).sign(SHA256.new(payload_bytes))

    content = (base64.b64encode(payload_bytes).decode("ascii")
               + "\n" + base64.b64encode(signature).decode("ascii") + "\n")
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")

    print("授权文件已生成：", output)
    print(f"  客户名称：{payload['customer']}")
    if fingerprint:
        print(f"  绑定指纹：{fingerprint}")
    else:
        print("  机器绑定：不绑定（任意机器可用）")
    if expire_at:
        print("  到期时间："
              + datetime.fromtimestamp(expire_at).strftime("%Y-%m-%d %H:%M:%S")
              + f"（{args.days or round((expire_at - time.time()) / 86400)} 天）")
    else:
        print("  到期时间：永久")
    print("请将该文件发送给客户，放到 mmm.exe 同目录即可生效。")
    return 0


def cmd_info(args) -> int:
    _add_root_to_path()
    from app.license_core import verify_license_file, STATUS_TEXT, machine_fingerprint
    path = Path(args.file).resolve()
    if not path.is_file():
        print(f"文件不存在：{path}")
        return 1
    payload, status = verify_license_file(path)
    print("校验状态：", STATUS_TEXT.get(status, status))
    print("本机指纹：", machine_fingerprint())
    if payload:
        print("客户名称：", payload.get("customer"))
        bound = payload.get("fingerprint") or ""
        print("绑定指纹：", bound or "不绑定")
        issued = payload.get("issued_at")
        expire = payload.get("expire_at")
        if issued:
            print("签发时间：",
                  datetime.fromtimestamp(issued).strftime("%Y-%m-%d %H:%M:%S"))
        if expire:
            print("到期时间：",
                  datetime.fromtimestamp(expire).strftime("%Y-%m-%d %H:%M:%S"))
            remaining = (expire - time.time()) / 86400
            print(f"剩余天数：{max(0, round(remaining, 1))}")
        else:
            print("到期时间：永久")
    return 0 if status in {"valid", "expired", "machine_mismatch"} else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="mmm 授权签发工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-keys", help="生成 RSA 密钥对")
    p_init.add_argument("--force", action="store_true", help="覆盖已有密钥")
    p_init.set_defaults(func=cmd_init_keys)

    p_fp = sub.add_parser("fingerprint", help="查看本机机器指纹")
    p_fp.set_defaults(func=cmd_fingerprint)

    p_issue = sub.add_parser("issue", help="签发授权文件")
    p_issue.add_argument("--customer", required=True, help="客户名称")
    p_issue.add_argument("--fingerprint", default="", help="客户机器指纹")
    p_issue.add_argument("--days", type=int, default=None,
                         help="授权天数（默认 90）")
    p_issue.add_argument("--expire", default="",
                         help="到期日期 YYYY-MM-DD（与 --days 二选一）")
    p_issue.add_argument("--perpetual", action="store_true", help="永久授权")
    p_issue.add_argument("--no-bind", action="store_true",
                         help="不绑定机器（任意机器可用，请谨慎）")
    p_issue.add_argument("--output", default="license.dat", help="输出文件路径")
    p_issue.set_defaults(func=cmd_issue)

    p_info = sub.add_parser("info", help="查看并验签授权文件")
    p_info.add_argument("--file", required=True, help="license.dat 路径")
    p_info.set_defaults(func=cmd_info)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
