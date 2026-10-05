# -*- coding: utf-8 -*-
"""客服服务器入口（部署在云服务器，对公网提供访客页与坐席接口）。

用法（在仓库根目录）：

    python -m app.cs_server --host 0.0.0.0 --port 8080

特性：
- 复用 app.main 的完整应用（同一数据库），但不启动浏览器/
  采集引擎（MMM_CS_ONLY=1），可在无桌面环境的 Linux 云主机运行。
- 客服服务器节点不接入授权（授权绑定桌面工作台机器），入口自动
  设置 CREATORHUB_SKIP_LICENSE=1；如需强制校验可显式设为 0。
- 建议前置 nginx/caddy 终止 HTTPS，访客链接使用 https://域名/chat/{token}。
- 平台私信/评论的回投由持有平台登录态的 Windows 工作台节点执行：
  坐席回复在纯服务器节点进入 CsRelayJob 队列，工作台凭节点令牌
  （MMM_CS_NODE_TOKEN 或坐席管理界面配置）认领、执行并回报结果。
"""
from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="mmm 客服服务器")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    args = parser.parse_args()

    os.environ["MMM_CS_ONLY"] = "1"
    # 客服服务器不接入授权（授权绑定桌面工作台机器）；显式设 0 可恢复校验
    os.environ.setdefault("CREATORHUB_SKIP_LICENSE", "1")
    import uvicorn
    print(f"[cs-server] mmm 客服服务器启动于 http://{args.host}:{args.port} "
          "（客服模式，不启动采集引擎）")
    # SSE 坐席事件流(/api/cs/agent/stream)是常驻连接，默认优雅关闭会一直
    # 卡在 "Waiting for connections to close"，Ctrl+C 退不掉；给 5 秒
    # 宽限后强制断开连接。再按一次 Ctrl+C 也可立即强制退出。
    uvicorn.run("app.main:app", host=args.host, port=args.port,
                log_level="info", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
