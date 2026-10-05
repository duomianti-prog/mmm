"""授权拦截中间件与授权失效提示页。"""
from __future__ import annotations

import html
import json

from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import license_core

STATUS_PATH = "/api/license/status"
ALLOWED_PREFIXES = ()          # 失效后仅放行状态接口
_ALLOWED_PATHS = {STATUS_PATH}

# 访客客服路径前缀（授权失效时给简短的"客服暂不可用"，不暴露授权详情页）
_GUEST_API_PREFIX = "/api/cs/guest/"


def guest_unavailable_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>客服暂不可用 · mmm</title>
<style>
body{margin:0;font-family:-apple-system,"Segoe UI","Microsoft YaHei UI",sans-serif;
background:#f7f7fa;color:#1d1d1f;min-height:100vh;display:flex;
align-items:center;justify-content:center;padding:24px;text-align:center}
.card{max-width:360px}.logo{font-weight:800;font-size:30px;letter-spacing:1px;margin-bottom:18px}
h1{font-size:18px;margin-bottom:10px}p{color:#666;font-size:14px;line-height:1.7}
</style></head><body><div class="card">
<div class="logo">mmm</div>
<h1>在线客服暂不可用</h1>
<p>客服服务暂时无法连接，请稍后再试，<br>或通过其他方式联系我们。</p>
</div></body></html>"""


def blocked_html(info: dict) -> str:
    status = info.get("status", "")
    status_text = html.escape(info.get("status_text", "授权未通过"))
    customer = html.escape(info.get("customer") or "—")
    fingerprint = html.escape(info.get("fingerprint", ""))
    expire_at = info.get("expire_at") or 0
    days_remaining = info.get("days_remaining")
    expire_text = "永久" if info.get("perpetual") else (
        __import__("datetime").datetime.fromtimestamp(expire_at)
        .strftime("%Y-%m-%d %H:%M") if expire_at else "—")

    paths = info.get("searched_paths") or []
    path_items = "".join(
        f"<li><code>{html.escape(p)}</code></li>" for p in paths[:4])

    reason_map = {
        license_core.MISSING: "本机未找到授权文件。请将服务人员分发的 "
                              "<b>license.dat</b> 放到下列任一位置。",
        license_core.EXPIRED: "本机授权已到期。请联系服务人员续期，"
                              "并将新的 <b>license.dat</b> 放到下列任一位置。",
        license_core.SIGNATURE_INVALID: "授权文件签名无效，可能已被篡改或损坏。"
                                        "请重新向服务人员索取授权文件。",
        license_core.MACHINE_MISMATCH: "授权文件绑定的是其他机器，无法在本机使用。"
                                       "请把下方机器指纹发给服务人员重新签发。",
        license_core.MALFORMED: "授权文件格式损坏，请重新向服务人员索取。",
    }
    reason = reason_map.get(status, "授权校验未通过，请联系服务人员。")

    renew_hint = ""
    if status == license_core.VALID and days_remaining is not None \
            and days_remaining <= license_core.WARN_DAYS:
        renew_hint = (f'<div class="hint">授权将在 {days_remaining} 天内到期，'
                      '请提前联系服务人员续期。</div>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>授权验证 · mmm</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: "Microsoft YaHei UI", -apple-system, "Segoe UI", sans-serif;
       background: linear-gradient(160deg,#f7f7fa,#eef0f5); color:#1d1d1f;
       min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px; }}
.card {{ background:#fff; border-radius:16px; box-shadow:0 12px 40px rgba(20,30,60,.12);
        max-width:640px; width:100%; padding:36px 40px; }}
.badge {{ display:inline-block; background:#fdecee; color:#c71f46; font-weight:700;
         font-size:13px; padding:5px 12px; border-radius:999px; margin-bottom:16px; }}
h1 {{ font-size:24px; margin-bottom:10px; }}
.reason {{ color:#444; line-height:1.7; margin-bottom:20px; font-size:15px; }}
.meta {{ background:#f6f6f9; border-radius:10px; padding:16px 18px; margin-bottom:18px;
        font-size:14px; line-height:2; }}
.meta b {{ color:#1d1d1f; }} .meta span {{ color:#555; }}
.fp-box {{ display:flex; gap:8px; align-items:center; margin:4px 0; }}
code {{ background:#eceef3; padding:2px 7px; border-radius:5px; font-size:13px;
       word-break:break-all; font-family:Consolas,monospace; }}
.fp-box code {{ flex:1; }}
button {{ border:0; border-radius:9px; padding:10px 20px; font-size:14px;
         cursor:pointer; font-weight:600; }}
.primary {{ background:#c71f46; color:#fff; }}
.primary:hover {{ background:#b3183e; }}
.ghost {{ background:#eef0f4; color:#333; }}
.paths {{ margin:6px 0 4px; padding-left:20px; color:#666; font-size:13px; line-height:1.9; }}
.hint {{ background:#fff7e8; border:1px solid #ffe1a8; color:#8a5a00;
        padding:10px 14px; border-radius:9px; font-size:13px; margin-bottom:16px; }}
.status-ok {{ color:#07733f; font-weight:600; margin-left:10px; }}
.footer {{ margin-top:22px; color:#9aa; font-size:12px; text-align:center; }}
</style></head>
<body><div class="card">
<span class="badge">授权验证</span>
<h1>{status_text}<span id="ok" class="status-ok" hidden>已恢复，正在进入…</span></h1>
<p class="reason">{reason}</p>
{renew_hint}
<div class="meta">
  <div><b>客户名称：</b><span>{customer}</span></div>
  <div><b>到期时间：</b><span>{expire_text}</span></div>
  <div><b>机器指纹：</b></div>
  <div class="fp-box"><code id="fp">{fingerprint}</code>
    <button class="ghost" type="button" onclick="copyFp()">复制指纹</button></div>
</div>
<div><b>授权文件位置（任选其一）：</b>
<ul class="paths">{path_items}</ul></div>
<div style="margin-top:18px; display:flex; gap:10px;">
  <button class="primary" type="button" onclick="check()">我已放入新授权，重新检查</button>
</div>
<div class="footer">放入新文件后无需重启，本页每 10 秒自动检查一次。</div>
</div>
<script>
function copyFp() {{
  var t = document.getElementById('fp').textContent.trim();
  navigator.clipboard.writeText(t).then(function() {{}});
}}
async function check() {{
  try {{
    var r = await fetch('{STATUS_PATH}', {{cache:'no-store'}});
    var d = await r.json();
    if (d.status === 'valid' || d.status === 'skipped') {{
      document.getElementById('ok').hidden = false;
      setTimeout(function() {{ location.href = '/'; }}, 600);
      return true;
    }}
  }} catch (e) {{}}
  return false;
}}
setInterval(check, 10000);
</script></body></html>"""


class LicenseGateMiddleware:
    """授权失效时拦截全部请求，仅放行状态接口并返回提示页。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in _ALLOWED_PATHS:
            await self.app(scope, receive, send)
            return
        info = license_core.evaluate()
        if license_core.gate_passed(info):
            await self.app(scope, receive, send)
            return
        # 访客客服路径：授权失效时返回简短提示，不暴露授权/指纹等内部信息
        if path.startswith(_GUEST_API_PREFIX):
            response = JSONResponse(
                status_code=503,
                content={"error": "service_unavailable",
                         "detail": "客服暂不可用，请稍后再试"})
        elif path.startswith("/chat/"):
            response = HTMLResponse(
                status_code=503, content=guest_unavailable_html())
        elif path.startswith("/api/"):
            response = JSONResponse(status_code=403, content={
                "error": "license_required",
                **{k: info.get(k) for k in
                   ("status", "status_text", "customer", "expire_at",
                    "days_remaining", "perpetual", "fingerprint")},
            })
        else:
            response = HTMLResponse(status_code=403, content=blocked_html(info))
        await response(scope, receive, send)
