# -*- coding: utf-8 -*-
"""客服系统命令行管理工具。

用法（在仓库根目录，使用项目虚拟环境的 Python）：

    python -m app.cs_admin create-agent <用户名> <密码> [--name 显示名] [--admin]
    python -m app.cs_admin list
    python -m app.cs_admin reset-password <用户名> <新密码>
    python -m app.cs_admin enable <用户名>
    python -m app.cs_admin disable <用户名>
    python -m app.cs_admin link [--name 客户名] [--base https://域名]

注意：坐席写进「当前目录的 data/mmmim.db」（或 --db 指定的库）。
登录时报"用户名或密码错误"最常见的原因是 cs_admin 与客服服务不在同一
个工作目录执行、坐席建进了另一个数据库——先看输出里的"数据库："路径
是否就是客服服务实际使用的库。
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .config import load_config
from .cs import service
from .cs.models import CsAgent
from .db import get_session, init_db
from sqlmodel import select


def _find_agent(key: str) -> CsAgent | None:
    """按用户名或 id 找坐席。"""
    with get_session() as s:
        agent = s.exec(select(CsAgent).where(CsAgent.username == key.lower())).first()
        if not agent and key.isdigit():
            agent = s.get(CsAgent, int(key))
        if agent:
            s.expunge(agent)
        return agent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cs_admin", description="mmm 客服系统管理")
    parser.add_argument(
        "--db", default="",
        help="指定数据库文件路径（默认随当前工作目录：data/mmmim.db）。"
             "管理桌面版内嵌客服时可指向其用户数据目录下的 "
             "mmm/user-data/data/mmmim.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create-agent", help="创建客服坐席")
    p_create.add_argument("username")
    p_create.add_argument("password", nargs="?", default="",
                          help="不填则交互输入（隐藏显示）")
    p_create.add_argument("--name", default="", help="显示名")
    p_create.add_argument("--admin", action="store_true", help="设为管理员")

    sub.add_parser("list", help="列出全部坐席")

    p_reset = sub.add_parser("reset-password", help="重置密码")
    p_reset.add_argument("username")
    p_reset.add_argument("password", nargs="?", default="")

    p_enable = sub.add_parser("enable", help="启用坐席")
    p_enable.add_argument("username")
    p_disable = sub.add_parser("disable", help="停用坐席")
    p_disable.add_argument("username")

    p_link = sub.add_parser("link", help="生成一条访客会话链接")
    p_link.add_argument("--name", default="", help="客户备注名")
    p_link.add_argument("--base", default="",
                        help="对外访问基址，如 https://kf.example.com")

    args = parser.parse_args(argv)
    cfg = load_config()
    if args.db:
        cfg.db_path = str(Path(args.db).expanduser())
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    print(f"数据库：{Path(cfg.db_path).resolve()}")
    init_db(cfg.db_path)

    try:
        if args.cmd == "create-agent":
            password = args.password or getpass.getpass("密码（至少 6 位）：")
            agent = service.create_agent(
                args.username, password, args.name,
                role="admin" if args.admin else "agent")
            print(f"已创建坐席：id={agent.id} 用户名={agent.username} "
                  f"显示名={agent.display_name} 角色={agent.role}")
        elif args.cmd == "list":
            rows = service.list_agents(include_disabled=True)
            if not rows:
                print("（暂无坐席，使用 create-agent 创建第一个管理员）")
            for a in rows:
                flag = "启用" if a.enabled else "停用"
                print(f"  #{a.id:<3} {a.username:<16} {a.display_name:<12} "
                      f"{a.role:<6} {flag}")
        elif args.cmd == "reset-password":
            agent = _find_agent(args.username)
            if not agent:
                print(f"坐席不存在：{args.username}", file=sys.stderr)
                return 2
            password = args.password or getpass.getpass("新密码（至少 6 位）：")
            service.set_agent_password(agent.id, password)
            print(f"已重置 {agent.username} 的密码")
        elif args.cmd in ("enable", "disable"):
            agent = _find_agent(args.username)
            if not agent:
                print(f"坐席不存在：{args.username}", file=sys.stderr)
                return 2
            service.set_agent_enabled(agent.id, args.cmd == "enable")
            print(f"已{('启用' if args.cmd == 'enable' else '停用')} {agent.username}")
        elif args.cmd == "link":
            conv = service.create_link(args.name)
            path = f"/chat/{conv.token}"
            print(f"访客链接（会话 id={conv.id}）：")
            print(f"  路径：{path}")
            if args.base:
                print(f"  完整链接：{args.base.rstrip('/')}{path}")
            else:
                print("  加 --base https://你的域名 可输出完整链接")
    except service.CsError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
