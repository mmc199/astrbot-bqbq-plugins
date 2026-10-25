"""YouTube 解析与下载（基于 yt-dlp，走本地代理）。

要点：
- 固定在"无需登录"的客户端上取流，避免 "Sign in to confirm you're not a bot"
- 只取 360p 左右、音视频已合一的 mp4（itag 18），无需再合并、无需 ffmpeg 转码
- 代理不通时抛出明确的异常，由上层提示用户开启代理
"""

import asyncio
import functools
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import yt_dlp
from astrbot.api import logger

from ..exception import DownloadException, ParseException
from ..download.utils import generate_file_name

# yt-dlp 超时（秒）
YTDLP_SOCKET_TIMEOUT = 30
YTDLP_EXTRACT_TIMEOUT = 90
YTDLP_DOWNLOAD_TIMEOUT = 600

# 独立线程池：yt-dlp 是阻塞调用，卡死时不应拖垮整个插件
_ytdlp_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ytdlp")

# 优先这些"无需登录"的客户端。
# 实测（2026-10）：android 客户端对普通视频和 Shorts 都能拿到可下载的 itag 18；
# android_vr 对普通视频会返回 403 Forbidden，所以放在后面且不作为唯一来源。
_PLAYER_CLIENTS = ["android", "android_vr", "web_safari", "web"]

# format 18 = 360p mp4，音视频合一、免登录、免合并
_FORMAT_SELECTOR = "18/b[height<=360][ext=mp4]/b[height<=360]/best[height<=480]/best"


class ProxyUnavailable(ParseException):
    """代理不可用（供上层给出"请打开代理"的提示）"""


async def check_proxy(proxy: str, timeout: float = 6.0) -> bool:
    """检测代理是否可用（能连通 YouTube）

    Args:
        proxy: 形如 http://127.0.0.1:10808
        timeout: 超时秒数

    Returns:
        True 表示代理已开启且可访问 YouTube
    """
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout, verify=False) as c:
            r = await c.get("https://www.youtube.com/generate_204")
            return r.status_code < 500
    except Exception as e:
        logger.warning(f"代理检测失败 ({proxy}): {type(e).__name__}")
        return False


async def _run_ytdlp(func, *args, timeout: float, **kwargs) -> Any:
    """在线程池中执行 yt-dlp 阻塞调用，并施加整体超时"""
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs)
    future = loop.run_in_executor(_ytdlp_executor, call)
    # wait_for 只取消 await，底层线程仍在跑；靠 socket_timeout 让它自己退出
    return await asyncio.wait_for(future, timeout=timeout)


class _LimitedSizeDict(OrderedDict):
    """限制大小的缓存，避免长跑后内存增长"""

    def __init__(self, *args, max_size: int = 20, **kwargs):
        self.max_size = max_size
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if len(self) > self.max_size:
            self.popitem(last=False)


_info_cache: _LimitedSizeDict = _LimitedSizeDict()
_info_cache_lock = asyncio.Lock()


class YoutubeParser:
    """YouTube 解析器（普通视频 + Shorts）"""

    def __init__(self, proxy: str | None = "http://127.0.0.1:10808"):
        self.proxy = proxy

    # ---------------- yt-dlp options ----------------

    def _base_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": YTDLP_SOCKET_TIMEOUT,
            # 只要不需登录的客户端，避免 bot 校验
            "extractor_args": {"youtube": {"player_client": _PLAYER_CLIENTS}},
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        return opts

    # ---------------- 解析 ----------------

    async def get_video_info(self, url: str) -> dict[str, Any]:
        """获取视频元信息（含各清晰度格式列表）"""
        async with _info_cache_lock:
            if cached := _info_cache.get(url):
                return cached

        opts = {**self._base_opts(), "skip_download": True}

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = await _run_ytdlp(
                    ydl.extract_info, url, download=False, timeout=YTDLP_EXTRACT_TIMEOUT
                )
        except asyncio.TimeoutError:
            logger.warning(f"获取 YouTube 视频信息超时 | url: {url}")
            raise ParseException("获取视频信息超时，可能是代理过慢") from None
        except Exception as e:
            msg = str(e)
            logger.warning(f"获取 YouTube 视频信息失败 | {url} | {msg[:200]}")
            if self._looks_like_proxy_error(msg):
                raise ProxyUnavailable("代理不可用") from e
            raise ParseException(f"获取视频信息失败: {msg[:120]}") from e

        if not info:
            raise ParseException("获取视频信息失败")

        async with _info_cache_lock:
            _info_cache[url] = info
        return info

    @staticmethod
    def _looks_like_proxy_error(msg: str) -> bool:
        lowered = msg.lower()
        return any(
            k in lowered
            for k in (
                "connection refused",
                "proxy",
                "timed out",
                "tunnel connection failed",
                "unable to connect",
                "network is unreachable",
            )
        )

    @staticmethod
    def pick_format(info: dict[str, Any]) -> dict[str, Any] | None:
        """挑选一个免登录、音视频合一的低清晰度格式

        优先 itag 18（360p mp4，音视频同文件）。
        """
        formats = info.get("formats") or []

        def has_audio(f):
            return f.get("acodec") not in (None, "none")

        def has_video(f):
            return f.get("vcodec") not in (None, "none")

        av = [f for f in formats if has_video(f) and has_audio(f)]

        # 1) 精确 itag 18
        for f in av:
            if str(f.get("format_id")) == "18":
                return f
        # 2) 其它 360p 以下的合一 mp4
        small = [f for f in av if (f.get("height") or 0) <= 360]
        if small:
            small.sort(key=lambda f: (f.get("height") or 0) == 0, reverse=True)
            small.sort(key=lambda f: abs((f.get("height") or 0) - 360))
            return small[0]
        # 3) 任意合一
        if av:
            av.sort(key=lambda f: (f.get("height") or 0))
            return av[0]
        return None

    # ---------------- 下载 ----------------

    async def download_video(self, url: str, cache_dir: Path) -> Path:
        """下载 YouTube 视频到缓存目录

        Returns:
            下载后的视频文件路径
        """
        info = await self.get_video_info(url)
        video_id = info.get("id") or generate_file_name(url)
        video_path = cache_dir / f"ytb_{video_id}.mp4"

        if video_path.exists() and video_path.stat().st_size > 0:
            logger.info(f"命中 YouTube 缓存: {video_path.name}")
            return video_path

        opts = {
            **self._base_opts(),
            "outtmpl": str(video_path),
            "format": _FORMAT_SELECTOR,
            "merge_output_format": "mp4",
        }

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                await _run_ytdlp(ydl.download, [url], timeout=YTDLP_DOWNLOAD_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"YouTube 视频下载超时 | {url}")
            raise DownloadException("视频下载超时") from None
        except Exception as e:
            msg = str(e)
            logger.warning(f"YouTube 视频下载失败 | {url} | {msg[:200]}")
            if self._looks_like_proxy_error(msg):
                raise ProxyUnavailable("代理不可用") from e
            raise DownloadException(f"视频下载失败: {msg[:120]}") from e

        # yt-dlp 可能因容器选择落到别的扩展名
        if not video_path.exists():
            for p in cache_dir.glob(f"ytb_{video_id}.*"):
                if p.suffix in (".mp4", ".mkv", ".webm"):
                    return p
            raise DownloadException("视频下载失败：未找到输出文件")

        return video_path
