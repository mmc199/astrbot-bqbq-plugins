"""小红书解析器 —— 移植自 nonebot_plugin_resolver2/parsers/xiaohongshu.py。

与原版的差异（2026-10 实测）：
- 原版从 `window.__INITIAL_STATE__` 的
  `note.noteDetailMap[<id>].note` 里取数据；但小红书现在**只对带 xsec_token 的
  链接返回该字段**，裸链接（/explore/<id>）一律返回空 `noteDetailMap`。
  所以这里优先用链接里的 xsec_token，缺失时明确提示"链接不完整"。
- 原版用 pydantic 定义模型；这里改用纯 dict 读取，减少依赖。

另外，小红书网页直链是**带时效签名**的，下载时必须带上 Referer/UA，
否则会 403 —— 由调用方通过 headers 传入（见 BILI/XHS 的到 downloader 的调用）。
"""

import json
import re
from urllib.parse import parse_qs, urlparse

import httpx
from astrbot.api import logger

from ..constants import COMMON_TIMEOUT
from ..exception import ParseException
from .data import ImageContent, ParseResult, VideoContent
from .utils import build_mounts, get_redirect_url

# 小红书网页端 UA / 头，缺 Referer 会被拒
XHS_HEADER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/webp,image/apng,*/*;q=0.8",
    "Referer": "https://www.xiaohongshu.com/",
}

_NOTE_ID_RE = re.compile(r"(?:/explore/|/discovery/item/|source=note&noteId=)(\w+)")
_STATE_RE = re.compile(r"window\.__INITIAL_STATE__=(.*?)</script>", re.S)

# 视频清晰度优先级（h264 兼容性最好）
_STREAM_KEYS = ("h264", "h265", "av1", "h266")


class XiaoHongShuParser:
    """小红书解析器（图文 / 视频）"""

    def __init__(self, cookie: str | None = None, proxy: str | None = None):
        self.headers = XHS_HEADER.copy()
        self.cookie = (cookie or "").strip()
        self.proxy = proxy
        self._apply_cookie()

    def _apply_cookie(self) -> None:
        if self.cookie:
            self.headers["Cookie"] = self.cookie
        else:
            self.headers.pop("Cookie", None)

    async def check_cookies(self, cookies: dict[str, str] | None = None) -> tuple[bool, str]:
        """判断 Cookie 里是否带登录态（web_session）

        小红书的 user/me 接口有签名校验（无签名返回 406），
        所以这里只做本地判定：带上 web_session 即视为已登录。
        """
        jar = cookies if cookies is not None else self._cookie_dict()
        logged = bool(jar.get("web_session"))
        return logged, jar.get("webId", "") or ""

    def _cookie_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in self.cookie.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    async def parse_url(self, url: str) -> ParseResult:
        """解析小红书链接

        Args:
            url: 小红书链接（或 xhslink 短链）

        Returns:
            ParseResult: 图文或视频内容

        Raises:
            ParseException: 链接不完整 / 失效 / 未带 xsec_token
        """
        self._apply_cookie()

        # 短链先展开（xhslink.com / xhslink.cn 都会 302 到带 xsec_token 的真实链接）
        if "xhslink" in url:
            url = await get_redirect_url(url, self.headers, proxy=self.proxy)
            logger.info(f"小红书短链展开: {url[:90]}")

        matched = _NOTE_ID_RE.search(url)
        if not matched:
            raise ParseException("小红书分享链接不完整")
        xhs_id = matched.group(1)

        params = parse_qs(urlparse(url).query)
        xsec_source = params.get("xsec_source", [None])[0] or "pc_feed"
        xsec_token = params.get("xsec_token", [None])[0]

        # 没有 xsec_token 时，小红书不会返回笔记数据（实测 noteDetailMap 为空）
        full_url = (
            f"https://www.xiaohongshu.com/explore/{xhs_id}"
            f"?xsec_source={xsec_source}&xsec_token={xsec_token}"
        )

        try:
            async with httpx.AsyncClient(
                headers=self.headers,
                timeout=COMMON_TIMEOUT,
                verify=False,
                follow_redirects=True,
                mounts=build_mounts(self.proxy),
            ) as client:
                response = await client.get(full_url)
        except httpx.HTTPError as e:
            raise ParseException(f"请求小红书失败: {type(e).__name__}") from e

        if response.status_code in (403, 461):
            raise ParseException("小红书拒绝了请求（可能需要登录 Cookie 或链接已过期）")

        matched = _STATE_RE.search(response.text)
        if not matched:
            raise ParseException("小红书分享链接失效或内容已删除")

        # 页面里内嵌的是 JS 对象，含 undefined，需替换后再解析
        try:
            json_obj = json.loads(matched.group(1).replace("undefined", "null"))
        except ValueError as e:
            raise ParseException("小红书页面结构变化，解析失败") from e

        detail_map = json_obj.get("note", {}).get("noteDetailMap") or {}
        note = (detail_map.get(xhs_id) or {}).get("note") or {}
        if not note:
            if not xsec_token:
                raise ParseException(
                    "小红书链接缺少 xsec_token，请复制 App 里「分享→复制链接」得到的完整链接"
                )
            raise ParseException("小红书笔记不可见（可能需要登录或笔记已删除）")

        title = (note.get("title") or "").strip()
        desc = (note.get("desc") or "").strip()
        title_desc = f"{title}\n{desc}".strip()
        user = note.get("user") or {}

        video_url = self._pick_video_url(note)
        if video_url:
            logger.info(f"小红书视频: {title[:30]}")
            content: VideoContent | ImageContent = VideoContent(video_url=video_url)
        else:
            urls = [
                img.get("urlDefault") or img.get("url")
                for img in (note.get("imageList") or [])
                if img.get("urlDefault") or img.get("url")
            ]
            logger.info(f"小红书图文: {title[:30]}, {len(urls)} 张图")
            content = ImageContent(pic_urls=urls)

        return ParseResult(
            title=title_desc,
            cover_url="",
            content=content,
            author=user.get("nickname"),
        )

    @staticmethod
    def _pick_video_url(note: dict) -> str | None:
        """按 h264 > h265 > av1 > h266 取视频直链"""
        if note.get("type") != "video":
            return None
        stream = ((note.get("video") or {}).get("media") or {}).get("stream") or {}
        for key in _STREAM_KEYS:
            items = stream.get(key) or []
            if items and items[0].get("masterUrl"):
                return items[0]["masterUrl"]
        return None
