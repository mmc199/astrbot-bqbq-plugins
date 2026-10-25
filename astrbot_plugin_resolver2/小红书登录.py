"""小红书登录 Cookie 抓取器（移植自 抖音登录.py，套路完全一致）。

原理: 用一个独立的 Edge/Chrome 配置目录启动浏览器, 通过 CDP (Chrome DevTools Protocol)
直接读取 Cookie, 不依赖 cookie 数据库解密 (Edge 127+ 的 App-Bound 加密无法离线解密),
也不会和正在使用的浏览器抢占 cookie 锁。

用法:
    双击本文件                                   （推荐）
    python 本文件 --browser <msedge.exe 路径>     （自动找不到浏览器时手动指定）
    python 本文件 --timeout 600                   （延长等待扫码时间，秒）

流程:
1. 用独立配置目录打开小红书首页, 等你在页面上扫码登录
2. 检测到 web_session 后读走 Cookie, 写进插件配置的 xiaohongshu_cookie
3. 插件下次解析即生效, 不用重启 AstrBot

登录态保存在本文件同目录的 .xhs_profile 下, 再次运行会复用它
（通常还是登录态, 秒出结果）, 所以 Cookie 过期后重跑一次就能刷新。

关于 xsec_token（重要）:
    小红书现在只对**带 xsec_token 的链接**返回笔记数据。登录与否都改变不了这一点,
    所以登录器解决的是"风控 / 需要登录才能看"的那部分, 链接本身仍须是
    App 里「分享 → 复制链接」得到的完整链接。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
PROFILE_DIR = PLUGIN_DIR / ".xhs_profile"
START_URL = "https://www.xiaohongshu.com/"
DEFAULT_TIMEOUT = 300

PLUGIN_NAME = PLUGIN_DIR.name
CONFIG_PATH = PLUGIN_DIR.parent.parent / "config" / f"{PLUGIN_NAME}_config.json"

# 登录态判定：小红书登录态在 web_session 上（a1 / webId 是匿名标识）
LOGIN_COOKIE_NAMES = ("web_session",)
# 需要保存的 Cookie：登录态 + 小红书画象/风控标识，少一个都容易被判定为新设备
KEEP_COOKIE_NAMES = (
    "web_session",
    "a1",
    "webId",
    "gid",
    "xsecappid",
    "websectiga",
    "sec_poison_id",
    "acw_tc",
    "abRequestId",
    "webBuild",
    "loadts",
    "unread",
)

BROWSER_CANDIDATES = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
)


# --------------------------------------------------------------------------
# 解释器自举
# --------------------------------------------------------------------------


def _main_python() -> Path | None:
    for c in (
        PLUGIN_DIR.parent.parent.parent / "venv" / "Scripts" / "python.exe",
        PLUGIN_DIR.parent.parent.parent / "venv" / "Scripts" / "pythonw.exe",
    ):
        if c.exists():
            return c
    return None


def _ensure_right_interpreter() -> None:
    if os.environ.get("XHS_LOGIN_REEXEC") == "1":
        return
    target = _main_python()
    if target is None:
        return
    try:
        if os.path.samefile(sys.executable, target):
            return
    except OSError:
        pass
    if Path(sys.executable).resolve().parent == target.parent:
        return
    env = dict(os.environ, XHS_LOGIN_REEXEC="1")
    try:
        subprocess.run([str(target), str(Path(__file__).resolve()), *sys.argv[1:]], env=env)
    except Exception:
        return
    sys.exit(0)


def check_deps() -> tuple[bool, str]:
    missing = []
    for mod in ("httpx", "websockets"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if not missing:
        return True, ""
    venv_pip = PLUGIN_DIR.parent.parent.parent / "venv" / "Scripts" / "pip.exe"
    hint = (
        f'"{venv_pip}" install ' + " ".join(missing)
        if venv_pip.exists()
        else f"python -m pip install {' '.join(missing)}"
    )
    return False, hint


# --------------------------------------------------------------------------
# 浏览器 / CDP
# --------------------------------------------------------------------------


def find_browser(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise SystemExit(f"指定的浏览器不存在：{p}")
    for path in BROWSER_CANDIDATES:
        if path.exists():
            return path
    raise SystemExit(
        "没有找到 Edge 或 Chrome。\n"
        "请确认已安装 Microsoft Edge，或用参数手动指定：\n"
        r'  python 小红书登录.py --browser "C:\...\msedge.exe"'
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CDP:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._next_id = 1

    async def send(self, method: str, **params):
        msg_id = self._next_id
        self._next_id += 1
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            message = json.loads(await self.ws.recv())
            if message.get("id") != msg_id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method} 失败：{message['error']}")
            return message.get("result", {})


async def wait_for_page(port: int, timeout: float = 30) -> dict:
    import httpx

    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        while time.time() < deadline:
            try:
                targets = (await client.get(f"http://127.0.0.1:{port}/json/list")).json()
            except Exception:
                await asyncio.sleep(0.5)
                continue
            pages = [t for t in targets if t.get("type") == "page"]
            for page in pages:
                if "xiaohongshu.com" in page.get("url", ""):
                    return page
            if pages:
                return pages[0]
            await asyncio.sleep(0.5)
    raise SystemExit("浏览器调试端口没有就绪，启动失败（可重试一次）")


def pick_cookies(cookies: list[dict]) -> dict[str, str]:
    picked: dict[str, str] = {}
    for cookie in cookies:
        domain = cookie.get("domain", "")
        name = cookie.get("name") or ""
        value = cookie.get("value")
        if "xiaohongshu" not in domain or not name or not value:
            continue
        picked.setdefault(name, value)
    return picked


def has_login(cookies: list[dict]) -> bool:
    return any(
        c.get("name") in LOGIN_COOKIE_NAMES and c.get("value") for c in cookies
    )


class LoginCancelled(Exception):
    """浏览器被用户关掉（不是错误，静默退出）"""


def release(proc: subprocess.Popen) -> None:
    """已经拿到登录态：立刻关浏览器"""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def poll_cookies(port: int, timeout: int) -> list[dict]:
    import websockets

    page = await wait_for_page(port)
    print(f"  已连接调试端口 :{port}")
    print(f"  页面：{page.get('url', '')[:70]}")

    deadline = time.time() + timeout
    waiting_shown = False
    failures = 0

    while time.time() < deadline:
        try:
            async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
                cdp = CDP(ws)
                await cdp.send("Network.enable")
                while time.time() < deadline:
                    result = await cdp.send("Network.getAllCookies")
                    cookies = result.get("cookies", [])
                    if has_login(cookies):
                        return cookies
                    if not waiting_shown:
                        print("\n请在打开的浏览器窗口里登录小红书（扫码或手机号）...")
                        print("（登录成功后本窗口会自动继续，不用手动关浏览器）")
                        waiting_shown = True
                    await asyncio.sleep(2)
        except Exception as e:
            if "ConnectionClosed" in type(e).__name__:
                raise LoginCancelled() from e
            failures += 1
            if failures >= 5:
                raise SystemExit(
                    f"连接浏览器调试端口失败（{type(e).__name__}），请重试"
                )
            await asyncio.sleep(1)
            try:
                page = await wait_for_page(port, timeout=10)
            except SystemExit:
                raise LoginCancelled()

    raise SystemExit(
        f"等待登录超时（{timeout}s），没检测到 {LOGIN_COOKIE_NAMES}。\n"
        '可以加大等待时间重试：python "小红书登录.py" --timeout 600'
    )


# --------------------------------------------------------------------------
# 写配置
# --------------------------------------------------------------------------


def write_config(cookie_str: str) -> Path:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"找不到插件配置：{CONFIG_PATH}")
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig") or "{}")
    data["xiaohongshu_cookie"] = cookie_str
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    tmp.replace(CONFIG_PATH)
    return CONFIG_PATH


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def pause(msg: str = "按回车键退出...") -> None:
    if sys.stdin and sys.stdin.isatty():
        try:
            input(msg)
        except (EOFError, KeyboardInterrupt):
            pass


async def _run(args) -> int:
    browser = find_browser(args.browser)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    port = free_port()

    print("=" * 46)
    print("  小红书登录（扫码抓 Cookie 并写入插件配置）")
    print("=" * 46)
    print(f"浏览器   : {browser}")
    print(f"配置目录 : {PROFILE_DIR}")
    print(f"调试端口 : {port}")
    print(f"目标配置 : {CONFIG_PATH}")
    print()

    proc = subprocess.Popen(
        [
            str(browser),
            f"--user-data-dir={PROFILE_DIR}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msEdgeFirstRunExperience",
            START_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        cookies = await poll_cookies(port, args.timeout)
    except BaseException:
        release(proc)
        raise
    release(proc)

    jar = pick_cookies(cookies)
    if not has_login(cookies):
        print("\n[ERR] 没有抓到小红书登录 Cookie，请重试。")
        return 1

    cookie_str = "; ".join(f"{k}={jar[k]}" for k in KEEP_COOKIE_NAMES if k in jar)
    print(f"\n抓到小红书 Cookie {len(jar)} 个，登录态相关："
          f"{[n for n in KEEP_COOKIE_NAMES if n in jar]}")

    saved = write_config(cookie_str)
    print(f"\n[OK] 已写入：{saved}（{len(cookie_str)} 字符）")
    print("小红书下次解析即生效，不用重启 AstrBot。")
    print("\n提醒：链接必须是 App 里「分享 → 复制链接」得到的完整链接（带 xsec_token），")
    print("      只有 /explore/<id> 的话小红书不会返回笔记内容。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="小红书扫码登录（resolver2 插件）")
    ap.add_argument("--browser", help="浏览器可执行文件路径（自动找不到时用）")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help=f"等待扫码的秒数，默认 {DEFAULT_TIMEOUT}")
    args = ap.parse_args()

    ok, hint = check_deps()
    if not ok:
        print("[ERR] 缺少依赖，请先运行：\n  " + hint)
        pause()
        return 1

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except LoginCancelled:
        print("\n浏览器已关闭，登录已取消")
        return 1
    except SystemExit as e:
        if isinstance(e.code, int):
            code = e.code
        else:
            if e.code:
                print(f"\n{e.code}")
            code = 1
        if code != 0:
            print("[i] 可以用参数调整后重试：--timeout 600   --browser <msedge.exe 路径>")
        return code
    except Exception as e:
        import traceback

        traceback.print_exc()
        print(f"\n[ERR] {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    _ensure_right_interpreter()
    code = main()
    if code != 0 and code != 130:
        pause()
    sys.exit(code)
