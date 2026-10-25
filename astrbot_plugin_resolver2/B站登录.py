"""B 站登录启动器（纯 Python，双击本文件即可运行）。

为什么是扫码 + CDP 抓取：
本机 IP 已被 B 站风控（web-interface/view 与网页都 412），本机浏览器打不开视频页；
但 passport 登录页与扫码接口不受影响，所以用账号登录即可绕开。

流程（与抖音的 capture_douyin_cookie.py 同一套路）：
1. 用独立的 Edge/Chrome 配置目录（.bili_profile）启动浏览器，开启 CDP 调试端口
   —— 不碰你日常用的浏览器，也不依赖 cookie 数据库解密（Edge 127+ 无法离线解密）
2. 停在 B 站登录页，用手机扫码登录
3. 检测到 SESSDATA + bili_jct 后读走 Cookie，校验账号，写进插件配置
4. B 站下次解析即生效（插件会自动重读配置），不用重启 AstrBot

用法：
    双击本文件                                   （推荐）
    pythonw 本文件                                （不弹控制台）
    python 本文件                                 （在控制台里看输出）
    python 本文件 --edge <msedge.exe 路径>        （自动找不到浏览器时手动指定）
    python 本文件 --timeout 600                   （延长等待扫码时间，秒）

若双击时报"没有关联的程序"：右键 → 打开方式 → 选择 Python。
若提示缺少 websockets：运行 <AstrBot>\\venv\\Scripts\\pip install websockets
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
PROFILE_DIR = PLUGIN_DIR / ".bili_profile"
START_URL = "https://passport.bilibili.com/login"
DEFAULT_TIMEOUT = 300

PLUGIN_NAME = PLUGIN_DIR.name
# 插件在 <AstrBot>/data/plugins/<插件名>/ 下，配置在 <AstrBot>/data/config/
CONFIG_PATH = PLUGIN_DIR.parent.parent / "config" / f"{PLUGIN_NAME}_config.json"

# 登录态判定：两个都在才算真正登录（bili_jct 是提交类接口需要的 csrf）
LOGIN_COOKIE_NAMES = ("SESSDATA", "bili_jct")
# 需要保存的 Cookie（其余统计用的不保存）
KEEP_COOKIE_NAMES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    "buvid3",
    "buvid4",
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
# 解释器自举：保证用的就是 AstrBot 那个装了依赖的 venv
# --------------------------------------------------------------------------


def _main_python() -> Path | None:
    """插件上两级是 <AstrBot>，它的 venv 在 <AstrBot>/venv/Scripts/python.exe"""
    candidates = [
        PLUGIN_DIR.parent.parent.parent / "venv" / "Scripts" / "python.exe",
        PLUGIN_DIR.parent.parent.parent / "venv" / "Scripts" / "pythonw.exe",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _ensure_right_interpreter() -> None:
    """双击时可能用的是系统 python（没装 websockets），切回 venv 解释器重跑"""
    if os.environ.get("BILI_LOGIN_REEXEC") == "1":
        return
    target = _main_python()
    if target is None:
        return
    try:
        if os.path.samefile(sys.executable, target):
            return
    except OSError:
        pass
    # 已经是 venv 的 pythonw.exe 也算对
    if Path(sys.executable).resolve().parent == target.parent:
        return
    env = dict(os.environ, BILI_LOGIN_REEXEC="1")
    args = [str(target), str(Path(__file__).resolve()), *sys.argv[1:]]
    try:
        subprocess.run(args, env=env)
    except Exception:
        return  # 切不过去就用当前解释器继续，下面还会检查依赖
    sys.exit(0)


# --------------------------------------------------------------------------
# 依赖检查（给出能直接照做的修复指引，而不是一堆 Traceback）
# --------------------------------------------------------------------------


def check_deps() -> tuple[bool, str]:
    missing = []
    for mod, pip_name in (("httpx", "httpx"), ("websockets", "websockets")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    if not missing:
        return True, ""
    venv_pip = PLUGIN_DIR.parent.parent.parent / "venv" / "Scripts" / "pip.exe"
    hint = f'"{venv_pip}" install ' + " ".join(missing) if venv_pip.exists() else (
        f"python -m pip install {' '.join(missing)}"
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
        r'  python B站登录.py --edge "C:\...\msedge.exe"'
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CDP:
    """极简 CDP 客户端：只发命令，丢弃事件"""

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
                continue  # 事件，忽略
            if "error" in message:
                raise RuntimeError(f"{method} 失败：{message['error']}")
            return message.get("result", {})


async def wait_for_page(port: int, timeout: float = 30) -> dict:
    """等待带调试端口的浏览器起来，返回一个可用的页面 target"""
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
                if "bilibili.com" in page.get("url", ""):
                    return page
            if pages:
                return pages[0]
            await asyncio.sleep(0.5)
    raise SystemExit("浏览器调试端口没有就绪，启动失败（可重试一次）")


def pick_cookies(cookies: list[dict]) -> dict[str, str]:
    """按域名过滤并去重（同名保留首次出现的精确域）"""
    picked: dict[str, str] = {}
    for cookie in cookies:
        domain = cookie.get("domain", "")
        name = cookie.get("name") or ""
        value = cookie.get("value")
        if "bilibili.com" not in domain or not name or not value:
            continue  # 偶尔有空名 cookie，会污染 Cookie 头
        picked.setdefault(name, value)
    return picked


def has_login(cookies: list[dict]) -> bool:
    names = {c.get("name") for c in cookies if c.get("value")}
    return all(n in names for n in LOGIN_COOKIE_NAMES)


async def poll_cookies(port: int, timeout: int) -> list[dict]:
    """连上页面 target，轮询 Cookie 直到登录成功或超时"""
    import websockets

    page = await wait_for_page(port)
    print(f"  已连接调试端口 :{port}")
    print(f"  页面：{page.get('url', '')[:70]}")

    deadline = time.time() + timeout
    waiting_shown = False
    failures = 0

    while time.time() < deadline:
        try:
            async with websockets.connect(
                page["webSocketDebuggerUrl"], max_size=None
            ) as ws:
                cdp = CDP(ws)
                await cdp.send("Network.enable")
                while time.time() < deadline:
                    result = await cdp.send("Network.getAllCookies")
                    cookies = result.get("cookies", [])
                    if has_login(cookies):
                        return cookies
                    if not waiting_shown:
                        print("\n请在打开的浏览器窗口里，用 B 站手机 App 扫码登录...")
                        print("（登录成功后本窗口会自动继续，不用手动关浏览器）")
                        waiting_shown = True
                    await asyncio.sleep(2)
        except Exception as e:
            # 浏览器被用户关掉：正常退出，别报成一堆错误
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
        "可以加大等待时间重试：python \"B站登录.py\" --timeout 600"
    )


# --------------------------------------------------------------------------
# 校验 + 写配置
# --------------------------------------------------------------------------


async def verify_cookie(cookie_str: str) -> tuple[bool, str]:
    """用插件自己的解析器校验 Cookie（与运行时完全同一套请求逻辑）"""
    if str(PLUGIN_DIR.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR.parent))
    try:
        from astrbot_plugin_resolver2.parsers.bilibili import BilibiliParser
    except Exception as e:  # AstrBot 依赖异常时退化为直接请求
        return await _verify_cookie_raw(cookie_str)

    try:
        p = BilibiliParser(cookie=cookie_str)
        ok, uname, _uid = await p.check_cookies()
        return ok, uname
    except Exception as e:
        return False, f"（校验失败 {type(e).__name__}）"


async def _verify_cookie_raw(cookie_str: str) -> tuple[bool, str]:
    """兜底：不依赖插件包，直接请求 nav"""
    import httpx

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Cookie": cookie_str,
    }
    try:
        async with httpx.AsyncClient(timeout=15, verify=False, trust_env=False) as c:
            data = (
                await c.get(
                    "https://api.bilibili.com/x/web-interface/nav", headers=headers
                )
            ).json()
        d = data.get("data") or {}
        return bool(d.get("isLogin")), d.get("uname", "")
    except Exception as e:
        return False, f"（校验失败 {type(e).__name__}）"


def write_config(cookie_str: str) -> Path:
    """写进插件配置，保留其它配置项（AstrBot 用 utf-8-sig 读写）"""
    if not CONFIG_PATH.exists():
        raise SystemExit(f"找不到插件配置：{CONFIG_PATH}")
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig") or "{}")
    data["bilibili_cookie"] = cookie_str
    data["bilibili_use_proxy"] = False
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    tmp.replace(CONFIG_PATH)
    return CONFIG_PATH


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


class LoginCancelled(Exception):
    """浏览器被用户关掉（不是错误，静默退出）"""


def release(proc: subprocess.Popen) -> None:
    """已经拿到登录态：立刻关浏览器，别让用户以为还要点"""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def pause(msg: str = "按回车键退出...") -> None:
    """双击运行时窗口会瞬间消失，停一下；控制台里运行则跳过"""
    if sys.stdin and sys.stdin.isatty():
        try:
            input(msg)
        except (EOFError, KeyboardInterrupt):
            pass


async def _run(args) -> int:
    browser = find_browser(args.edge)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    port = free_port()

    print("=" * 46)
    print("  B 站登录（扫码抓 Cookie 并写入插件配置）")
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
    # 已经拿到登录态，立刻放掉浏览器，避免用户以为还要手动关
    release(proc)

    jar = pick_cookies(cookies)
    if not has_login(cookies) or "SESSDATA" not in jar:
        print("\n[ERR] 没有抓到可用的 B 站登录 Cookie，请重试。")
        return 1

    cookie_str = "; ".join(f"{k}={jar[k]}" for k in KEEP_COOKIE_NAMES if k in jar)
    print(f"\n抓到 B 站 Cookie {len(jar)} 个，登录态相关："
          f"{[n for n in KEEP_COOKIE_NAMES if n in jar]}")

    ok, uname = await verify_cookie(cookie_str)
    if ok:
        print(f"Cookie 校验通过，已登录账号：{uname}")
    else:
        print(f"Cookie 已抓取，但校验未通过{uname or ''}（仍会写入配置）")

    saved = write_config(cookie_str)
    print(f"\n[OK] 已写入：{saved}")
    print("B 站下次解析即生效，不用重启 AstrBot。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="B 站扫码登录（resolver2 插件）")
    ap.add_argument("--edge", help="浏览器可执行文件路径（自动找不到时用）")
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
        # str 形式的退出信息是"用户可见的提示"，打印后按失败退出（别用 int() 硬转）
        if isinstance(e.code, int):
            code = e.code
        else:
            if e.code:
                print(f"\n{e.code}")
            code = 1
        if code != 0:
            print("[i] 可以用参数调整后重试：--timeout 600   --edge <msedge.exe 路径>")
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
