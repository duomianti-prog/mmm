"""可替换品牌资源（Logo 与产品名）。

约定：运行目录下的 ``data/branding/`` 中如存在以下文件，即视为自定义品牌：

- ``logo.png`` / ``logo.jpg`` / ``logo.jpeg`` / ``logo.ico`` —— 自定义 Logo（位图）
- ``logo.svg`` —— 自定义 Logo（矢量，Web 端优先使用）
- ``name.txt`` —— 自定义产品名（取第一行，去空白）

不存在时回退到内置默认（mmm 字母标记 + "mmm"）。
这样无需改源码、无需重新构建，直接丢文件即可替换品牌。
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Optional

DEFAULT_NAME = "mmm"
BRANDING_DIR_NAME = "branding"
_LOGO_CANDIDATES = ("logo.svg", "logo.png", "logo.jpg", "logo.jpeg", "logo.ico")
_NAME_FILE = "name.txt"


def branding_dir(base: Optional[str] = None) -> Path:
    """返回品牌资源目录（不保证存在）。默认相对当前工作目录 ``data/branding``。"""
    root = Path(base) if base else Path.cwd()
    return root / "data" / BRANDING_DIR_NAME


def find_logo(directory: Optional[Path] = None) -> Optional[Path]:
    """按优先级返回第一个存在的 Logo 文件；不存在返回 None。"""
    directory = directory or branding_dir()
    if not directory.is_dir():
        return None
    for name in _LOGO_CANDIDATES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def read_name(directory: Optional[Path] = None) -> str:
    """读取自定义产品名；不存在或为空时返回默认名。"""
    directory = directory or branding_dir()
    name_file = directory / _NAME_FILE
    if not name_file.is_file():
        return DEFAULT_NAME
    try:
        text = name_file.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_NAME
    first_line = next(
        (line.strip() for line in text.splitlines() if line.strip()),
        "",
    )
    return first_line or DEFAULT_NAME


def logo_data_uri(directory: Optional[Path] = None) -> Optional[str]:
    """把 Logo 编码成 data URI，供 Web 端直接嵌入。SVG 走 UTF-8，位图走 base64。"""
    logo = find_logo(directory)
    if logo is None:
        return None
    suffix = logo.suffix.lower()
    if suffix == ".svg":
        try:
            raw = logo.read_text(encoding="utf-8")
        except OSError:
            return None
        encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        return f"data:image/svg+xml;base64,{encoded}"
    mime = mimetypes.guess_type(logo.name)[0] or "application/octet-stream"
    try:
        raw = logo.read_bytes()
    except OSError:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def branding_payload(directory: Optional[Path] = None) -> dict:
    """供 /api/branding 返回的统一结构。"""
    return {
        "name": read_name(directory),
        "logo": logo_data_uri(directory),
        "has_custom_logo": find_logo(directory) is not None,
    }
