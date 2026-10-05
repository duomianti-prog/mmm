"""Windows desktop entry point. Source mode uses the same isolated user data.

The frozen executable also hosts --serve and --smoke-test child modes; it never
attempts to run `sys.executable -m ...` as if a frozen bootloader were Python.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
import webbrowser
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def resources() -> Path:
    return Path(getattr(sys, "_MEIPASS", ROOT))


def user_directory() -> Path:
    override = (os.environ.get("MMM_DESKTOP_HOME")
                or os.environ.get("CREATORHUB_DESKTOP_HOME"))
    if override:
        return Path(override).expanduser().resolve()
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    new_home = base / "mmm" / "user-data"
    legacy_home = base / "CreatorHub" / "user-data"
    # 品牌改名后的一次性数据迁移：新目录不存在且旧目录存在时搬过去；
    # 搬不动（旧程序仍在运行/权限不足）就继续使用旧目录，不阻塞启动。
    if not new_home.exists() and legacy_home.exists():
        try:
            new_home.parent.mkdir(parents=True, exist_ok=True)
            legacy_home.rename(new_home)
            print(f"[mmm] 已迁移用户数据目录：{legacy_home} -> {new_home}", flush=True)
        except OSError:
            try:
                shutil.copytree(legacy_home, new_home)
                print(f"[mmm] 已复制用户数据目录（旧目录保留）：{legacy_home} -> {new_home}",
                      flush=True)
            except OSError:
                print("[mmm] 用户数据目录迁移失败，继续使用旧目录", flush=True)
                return legacy_home
    return new_home


def version() -> str:
    path = resources() / "desktop-version.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else "source"


def prepare_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    for name in ("logs", "backups", "runtime"):
        (home / name).mkdir(exist_ok=True)
    config = home / "config.yaml"
    if not config.exists():
        shutil.copy2(resources() / "config.example.yaml", config)


def snapshot(home: Path) -> Path:
    """Offline pre-upgrade config/database snapshot; media/Profile stay in place."""
    import yaml
    config = home / "config.yaml"
    raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    db = Path((raw.get("storage") or {}).get("db_path", "data/mmmim.db"))
    if not db.is_absolute():
        db = home / db
    filename = home / "backups" / f"settings-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.zip"
    with zipfile.ZipFile(filename, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(config, "config.yaml")
        if db.is_file():
            temp = home / "runtime" / f"snapshot-{uuid.uuid4().hex}.db"
            try:
                with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(temp)) as destination:
                    source.backup(destination)
                archive.write(temp, "database.db")
            finally:
                temp.unlink(missing_ok=True)
        archive.writestr("README.txt", "配置与数据库快照，不包含媒体和账号 Profile。\n恢复时停止服务，将 database.db 放回 config.yaml 中 storage.db_path 对应位置。\n完整迁移请另行备份整个 user-data 及自定义目录。\n")
    return filename


class InstanceLock:
    def __init__(self, home: Path, name="desktop.lock"):
        self.file = (home / "runtime" / name).open("a+b")
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.file.close()
                raise RuntimeError("本地服务仍在使用此数据目录，请等待旧服务退出后重试。" if name == "service.lock" else "启动管理器已经打开，请检查任务栏或系统托盘。")
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def close(self):
        self.file.close()


def bind_local_port(preferred: int = 8000) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind(("127.0.0.1", preferred))
    except OSError:
        sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock


def child_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, str(Path(__file__).resolve()), *args]


def serve(home: Path, session: str, install_browser: bool, parent_pid=0) -> int:
    from desktop.lifecycle import watch_parent
    service_lock = InstanceLock(home, "service.lock")
    close_watcher = watch_parent(parent_pid, home / "runtime" / f"{session}.stop")
    try:
        return _serve(home, session, install_browser)
    finally:
        close_watcher()
        service_lock.close()


def _serve(home: Path, session: str, install_browser: bool) -> int:
    os.chdir(home)
    os.environ["CREATORHUB_CONFIG_PATH"] = str(home / "config.yaml")
    os.environ.pop("DY_CONFIG_PATH", None)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(home / "browsers")
    docs = resources() / "desktop-guide"
    if docs.is_dir():
        os.environ["CREATORHUB_DESKTOP"] = "1"
    else:
        os.environ.pop("CREATORHUB_DESKTOP", None)
    # 授权门控：未授权时跳过浏览器组件下载，uvicorn 仍启动以提供授权提示页。
    from app.license_core import evaluate as evaluate_license, gate_passed
    _lic = evaluate_license()
    license_ok = gate_passed(_lic)
    if not license_ok:
        print(f"[license] {_lic['status_text']}；指纹：{_lic['fingerprint']}", flush=True)
    if install_browser and license_ok:
        from patchright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            available = Path(playwright.chromium.executable_path).is_file()
        if not available:
            print("首次启动：下载浏览器组件，请保持联网。完成后会自动打开面板。", flush=True)
            from patchright._impl._driver import compute_driver_executable, get_driver_env
            node, cli = compute_driver_executable()
            subprocess.run([node, cli, "install", "chromium"], env=get_driver_env(), check=True)
    from fastapi.staticfiles import StaticFiles
    from app.main import app
    import uvicorn

    docs = resources() / "desktop-guide"
    if docs.exists():
        app.mount("/guide", StaticFiles(directory=str(docs), html=True), name="desktop-guide")

    @app.get("/_desktop/ready", include_in_schema=False)
    async def desktop_ready():
        return {"session": session}

    sock = bind_local_port()
    port = sock.getsockname()[1]
    state = home / "runtime" / f"{session}.json"
    temp = state.with_suffix(".tmp")
    temp.write_text(json.dumps({"port": port}), encoding="utf-8")
    temp.replace(state)
    sockets = [sock]
    lan_sock = None
    # 局域网客服服务器模式(客服设置页开启):额外监听 0.0.0.0,
    # 安全边界由 LocalAccessMiddleware 保证——非本机来源只允许 /api/cs/* 与
    # /chat/* 客服路径,其余一律 403。
    try:
        from app.settings import get_setting as _get_setting
        if (_get_setting("cs_lan_enabled", "") or "") == "1":
            try:
                lan_port = int((_get_setting("cs_lan_port", "") or "").strip()
                               or "8080")
            except ValueError:
                lan_port = 8080
            lan_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                lan_sock.bind(("0.0.0.0", lan_port))
                lan_sock.listen(128)
                sockets.append(lan_sock)
                print(f"[cs-lan] 局域网客服服务已监听 0.0.0.0:{lan_port}",
                      flush=True)
            except OSError as e:
                print(f"[cs-lan] 局域网端口 {lan_port} 绑定失败({e}),"
                      "仅本机模式运行", flush=True)
                lan_sock.close()
                lan_sock = None
    except Exception as e:
        print(f"[cs-lan] 读取局域网配置失败({e!r}),仅本机模式运行", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, workers=1, log_config=None))
    stop = home / "runtime" / f"{session}.stop"
    def watch_stop():
        while not server.should_exit:
            if stop.exists():
                server.should_exit = True
                return
            time.sleep(.25)
    threading.Thread(target=watch_stop, daemon=True).start()
    try:
        server.run(sockets=sockets)
    finally:
        server.should_exit = True
        sock.close()
        if lan_sock is not None:
            lan_sock.close()
        state.unlink(missing_ok=True)
        stop.unlink(missing_ok=True)
    return 0


def smoke_test() -> int:
    # Import all application dependencies and resolve bundled assets in an
    # isolated directory, without starting jobs, signing in or making requests.
    import tempfile
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as temp:
        try:
            os.chdir(temp)
            os.environ["CREATORHUB_CONFIG_PATH"] = str(Path(temp) / "absent.yaml")
            from app.main import app, WEB_DIR
            from patchright._impl._driver import compute_driver_executable
            from imageio_ffmpeg import get_ffmpeg_exe
            from patchright.sync_api import sync_playwright
            import tkinter as tk
            import pystray
            assert app and (WEB_DIR / "workbench.js").is_file()
            assert (resources() / "config.example.yaml").is_file()
            assert (resources() / "desktop-guide" / "xhs" / "index.html").is_file()
            if getattr(sys, "frozen", False):
                updater = resources() / "desktop" / "mmmUpdater.exe"
                assert updater.is_file()
                from desktop.update_helper import clean_environment
                subprocess.run([str(updater), "--self-test"], env=clean_environment(),
                               check=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            assert all(Path(path).is_file() for path in compute_driver_executable())
            assert Path(get_ffmpeg_exe()).is_file()
            with sync_playwright() as playwright:
                assert playwright.chromium.executable_path
            ui = tk.Tk()
            ui.withdraw()
            from types import SimpleNamespace
            from desktop.ui import LauncherView
            owner = SimpleNamespace(root=ui, **{name: lambda: None for name in (
                "start", "open_panel", "open_guide", "open_data", "diagnostics", "hide", "close")})
            view = LauncherView(owner, version())
            view.phase("ready", "http://127.0.0.1:8000")
            ui.update_idletasks()
            ui.destroy()
            assert pystray.Icon
        finally:
            os.chdir(previous)
    return 0


def update_health_check() -> int:
    """Import/resource readiness only; never start services or migrate user data."""
    import tempfile
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="creatorhub-health-") as temp:
        try:
            os.chdir(temp)
            os.environ["CREATORHUB_CONFIG_PATH"] = str(Path(temp) / "absent.yaml")
            from app.main import app, WEB_DIR
            from desktop.controller import Controller
            from desktop.web_shell import ShellServer
            from patchright._impl._driver import compute_driver_executable
            from imageio_ffmpeg import get_ffmpeg_exe
            import webview
            assert app and Controller and ShellServer and webview
            assert (WEB_DIR / "workbench.js").is_file()
            for name in ("desktop/web/app.js", "desktop/web/index.html", "config.example.yaml"):
                assert (resources() / name).is_file(), name
            assert all(Path(path).is_file() for path in compute_driver_executable())
            assert Path(get_ffmpeg_exe()).is_file()
            return 0
        finally:
            os.chdir(previous)


def recover_interrupted_update(home):
    from desktop.update_delta import read_journal
    journal = read_journal(home)
    if not journal or journal.get("phase") in {"committed", "rolled_back"}:
        return False
    from desktop.update_helper import clean_environment
    helper = home / "runtime/updates" / journal["attempt"] / "mmmUpdater.exe"
    ready = home / "runtime/update-recovery-ready.json"
    ready.unlink(missing_ok=True)
    (home / "runtime/update-recovery-cancel").unlink(missing_ok=True)
    child = subprocess.Popen([str(helper), "--recover-home", str(home), "--parent-pid", str(os.getpid())],
                             cwd=home, env=clean_environment(), creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if child.poll() is not None:
            break
        if ready.is_file() and ready.stat().st_size < 1024:
            if json.loads(ready.read_text(encoding="utf-8")).get("pid") == os.getpid():
                return True
        time.sleep(.1)
    (home / "runtime/update-recovery-cancel").touch()
    raise RuntimeError("上次更新中断，请使用完整安装包修复。用户数据与更新备份保留。")


class Launcher:
    def __init__(self, home: Path):
        import tkinter as tk
        self.home, self.events = home, queue.Queue()
        self.process = None
        self.session = None
        self.url = None
        self.busy = False
        self.closing = False
        self.tray = None
        self.launch_done = threading.Event()
        self.launch_done.set()
        self.lock = InstanceLock(home)
        self.root = tk.Tk()
        from desktop.ui import LauncherView
        self.view = LauncherView(self, version())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll)

    def license_dialog(self):
        """授权未通过时弹出的 Tk 对话框，展示指纹并支持重新检查。"""
        from app.license_core import evaluate, gate_passed
        import tkinter as tk
        info = evaluate()
        dialog = tk.Toplevel(self.root)
        dialog.title("mmm 授权验证")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        frame = tk.Frame(dialog, padx=22, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=info["status_text"], font=("Microsoft YaHei UI", 13, "bold"),
                 fg="#b42318").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        tk.Label(frame, text="请将服务人员分发的 license.dat 放到数据目录，\n或程序所在目录后，点击“重新检查”。",
                 justify="left", font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))
        tk.Label(frame, text="机器指纹（发给服务人员）：", font=("Microsoft YaHei UI", 9, "bold")
                 ).grid(row=2, column=0, columnspan=2, sticky="w")
        fp_var = tk.StringVar(value=info["fingerprint"])
        entry = tk.Entry(frame, textvariable=fp_var, width=52, state="readonly",
                         readonlybackground="#f5f5f7", font=("Consolas", 9))
        entry.grid(row=3, column=0, columnspan=2, sticky="we", pady=(2, 8))
        paths = info.get("searched_paths") or []
        if paths:
            tk.Label(frame, text=f"授权文件位置：{paths[0]}", foreground="#6b7280",
                     font=("Microsoft YaHei UI", 8), wraplength=420, justify="left"
                     ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 10))
        message = tk.StringVar()
        tk.Label(frame, textvariable=message, foreground="#b42318", font=("Microsoft YaHei UI", 9)
                 ).grid(row=5, column=0, columnspan=2, sticky="w")

        def copy_fp():
            self.root.clipboard_clear()
            self.root.clipboard_append(info["fingerprint"])
            message.set("指纹已复制")

        def open_home():
            os.startfile(str(self.home))  # type: ignore[attr-defined]

        def recheck():
            if gate_passed():
                dialog.destroy()
                self.start()
            else:
                latest = evaluate()
                message.set(latest["status_text"] + "，请确认授权文件后重试。")

        buttons = tk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(10, 0))
        tk.Button(buttons, text="复制指纹", width=10, command=copy_fp).pack(side="left", padx=4)
        tk.Button(buttons, text="打开数据目录", width=12, command=open_home).pack(side="left", padx=4)
        tk.Button(buttons, text="重新检查", width=10, command=recheck, default="active").pack(side="left", padx=4)
        dialog.grab_set()
        x = self.root.winfo_rootx() + 80
        y = self.root.winfo_rooty() + 80
        dialog.geometry(f"+{x}+{y}")

    def start(self):
        if self.closing or self.busy or (self.process and self.process.poll() is None):
            return
        from app.license_core import gate_passed
        if not gate_passed():
            self.license_dialog()
            return
        self.busy = True
        self.launch_done.clear()
        self.start_button.configure(state="disabled")
        self.view.phase("starting")
        self.progress.start()
        self.status.set("正在准备运行环境…")
        self.events.put(("detail", "首次下载可能需要数分钟；失败后可点“启动 / 重试”。"))
        threading.Thread(target=self.launch_worker, daemon=True).start()

    def launch_worker(self):
        try:
            marker = self.home / "runtime" / "last-version.txt"
            current = version()
            if marker.exists() and marker.read_text(encoding="utf-8") != current:
                self.events.put(("status", "升级前备份配置与数据库…"))
                snapshot(self.home)
            self.session = uuid.uuid4().hex
            if self.closing:
                return
            log = self.home / "logs" / f"desktop-{time.strftime('%Y%m%d-%H%M%S')}.log"
            self.events.put(("status", "正在启动；如缺少浏览器组件，将自动下载…"))
            with log.open("w", encoding="utf-8") as output:
                env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
                self.process = subprocess.Popen(child_command("--serve", "--session", self.session, "--parent-pid", str(os.getpid())),
                    cwd=self.home, env=env, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            started = time.monotonic()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while self.process.poll() is None:
                if self.closing:
                    return
                state = self.home / "runtime" / f"{self.session}.json"
                if state.exists():
                    port = json.loads(state.read_text(encoding="utf-8"))["port"]
                    url = f"http://127.0.0.1:{port}"
                    try:
                        with opener.open(url + "/_desktop/ready", timeout=1) as response:
                            ready = json.load(response).get("session") == self.session
                        if ready:
                            marker.write_text(current, encoding="utf-8")
                            self.events.put(("ready", url))
                            return
                    except (OSError, ValueError):
                        pass
                if time.monotonic() - started > 120:
                    self.events.put(("detail", "仍在准备。下载受网络影响，可查看数据目录下 logs；停止并退出后可重试。"))
                time.sleep(.5)
            self.events.put(("error", f"启动未完成（退出码 {self.process.returncode}）。请检查网络，并查看 logs 中本次 desktop / process 日志；日志可能含私人数据，不要直接公开。"))
        except Exception as exc:
            self.events.put(("error", f"启动失败：{exc}"))
        finally:
            self.launch_done.set()

    def poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "status":
                    self.status.set(value)
                elif event == "detail":
                    self.detail.set(value)
                elif event == "ready":
                    if self.closing:
                        continue
                    self.url, self.busy = value, False
                    self.progress.stop()
                    self.view.phase("ready", value)
                    self.status.set("工作台已就绪")
                    self.detail.set("关闭网页不会停止任务。退出时点击“停止并退出”。")
                    self.open_button.configure(state="normal")
                    self.open_panel()
                elif event == "error":
                    self.busy = False
                    self.progress.stop()
                    self.view.phase("error")
                    self.status.set("启动遇到了问题")
                    self.detail.set(value)
                    self.start_button.configure(state="normal")
                elif event == "show":
                    self.root.deiconify()
                    self.root.lift()
                elif event == "exit":
                    self.close()
                elif event == "stopped":
                    if self.tray:
                        self.tray.stop()
                    self.lock.close()
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        if self.url and self.process and self.process.poll() is not None and not self.closing:
            self.url = None
            self.open_button.configure(state="disabled")
            self.events.put(("error", "本地服务已停止。查看 logs 中的错误后，可点击启动 / 重试。"))
        self.root.after(150, self.poll)

    def open_data(self):
        from tkinter import messagebox
        try:
            os.startfile(str(self.home))
        except OSError as exc:
            messagebox.showerror("打开目录失败", str(exc))

    def open_panel(self):
        if self.url:
            webbrowser.open(self.url)

    def open_guide(self):
        if self.url and (resources() / "desktop-guide").exists():
            webbrowser.open(self.url + "/guide/")
        else:
            webbrowser.open("https://3441293738.github.io/creatorhub/guide/")

    def hide(self):
        from tkinter import messagebox
        try:
            if not self.tray:
                import pystray
                from desktop.ui import brand_image
                icon = brand_image()
                self.tray = pystray.Icon("mmm", icon, "mmm 正在本地运行", menu=pystray.Menu(
                    pystray.MenuItem("打开管理器", lambda: self.events.put(("show", None)), default=True),
                    pystray.MenuItem("停止并退出", lambda: self.events.put(("exit", None)))))
                self.tray.run_detached()
            self.root.withdraw()
        except Exception as exc:
            messagebox.showinfo("托盘未就绪", f"请使用任务栏最小化；启动窗口仍保留。\n{exc}")

    def diagnostics(self):
        from tkinter import filedialog, messagebox
        import platform
        path = filedialog.asksaveasfilename(title="导出不含账号数据的诊断摘要", defaultextension=".json", initialfile="creatorhub-diagnostics.json")
        if path:
            # Allowlist only: no raw logs, paths, config, tokens, accounts or DB.
            data = {"app_version": version(), "os": platform.system(), "os_release": platform.release(),
                    "python": platform.python_version(), "frozen": bool(getattr(sys, "frozen", False)),
                    "service_running": bool(self.process and self.process.poll() is None),
                    "exit_code": self.process.poll() if self.process else None}
            try:
                Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                messagebox.showinfo("已导出", "摘要不包含配置、账号、日志或本机目录。")
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc))

    def close(self):
        from tkinter import messagebox
        if self.closing:
            return
        if not messagebox.askyesno("停止并退出", "退出会停止本地任务。确定停止服务并退出？"):
            return
        self.closing = True
        self.view.phase("stopping")
        self.status.set("正在停止本地服务…")
        self.progress.start()
        threading.Thread(target=self.stop_worker, daemon=True).start()

    def stop_worker(self):
        # Wait for a concurrent pre-start backup/spawn to finish before closing.
        self.launch_done.wait()
        if self.process and self.process.poll() is None:
            (self.home / "runtime" / f"{self.session}.stop").touch()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # Only terminate our owned child tree, never all Python/Chrome.
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
                else:
                    self.process.terminate()
                self.process.wait()
        self.events.put(("stopped", None))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--session", default="")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--update-health-check", action="store_true")
    parser.add_argument("--shell-smoke-test", action="store_true")
    parser.add_argument("--skip-browser-install", action="store_true")
    parser.add_argument("--legacy-ui", action="store_true", help="Use the legacy emergency window")
    parser.add_argument("--no-autostart", action="store_true")
    parser.add_argument("--fingerprint", action="store_true", help="Print this machine fingerprint and exit")
    args = parser.parse_args()
    if args.fingerprint:
        from app.license_core import machine_fingerprint
        print(machine_fingerprint())
        return 0
    if args.update_health_check:
        return update_health_check()
    if args.shell_smoke_test:
        from desktop.webview_smoke import smoke
        return smoke()
    if args.smoke_test:
        return smoke_test()
    home = user_directory()
    prepare_home(home)
    if os.name == "nt" and getattr(sys, "frozen", False) and recover_interrupted_update(home):
        return 0
    if args.serve:
        if not args.session or any(c not in "0123456789abcdef" for c in args.session):
            raise ValueError("Invalid desktop session")
        return serve(home, args.session, not args.skip_browser_install, args.parent_pid)
    try:
        if args.legacy_ui:
            ui = Launcher(home)
            ui.root.mainloop()
        else:
            from desktop.web_shell import run_desktop
            run_desktop(home, install_browser=not args.skip_browser_install)
    except Exception as exc:
        from tkinter import messagebox
        messagebox.showerror("mmm 启动提示", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    # Windowed frozen executables may not initialize Python's standard streams.
    if sys.stdout is None or sys.stderr is None:
        home = user_directory()
        (home / "logs").mkdir(parents=True, exist_ok=True)
        stream = (home / "logs" / f"process-{os.getpid()}.log").open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
    try:
        result = main()
    except Exception:
        import traceback
        traceback.print_exc()
        result = 1
    raise SystemExit(result)
