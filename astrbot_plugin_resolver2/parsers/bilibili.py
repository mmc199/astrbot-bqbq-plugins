"""哔哩哔哩解析器 —— 纯 HTTP 实现，不依赖 bilibili_api。

背景：本机 IP 已被 B 站风控，`web-interface/view` 固定返回 412，网页也打不开，
所以不能把 view 接口当作唯一入口。这里按"可用性"依次降级：

1. `web-interface/view`            —— 正常能拿到 title/cid/分P
2. `player/playurl`（带 Cookie）   —— 不再请求 view，只取 cid 与合并流直链
3. `player/wbi/view`               —— 需要 Cookie 的 view 变体
只有当以上全部被拒时，才抛"B 站拒绝访问"。

登录：
- Cookie 由扫码登录器（bili_login.py）写入插件配置 `bilibili_cookie`
- 未登录：html5 合并流（mp4，音视频同一文件，通常 360P）
- 已登录：先尝试 DASH 高清（音视频分离，交给 ffmpeg 合并），失败自动退回合并流
"""

import httpx
from astrbot.api import logger

from ..constants import BILI_HEADER, COMMON_TIMEOUT
from ..exception import ParseException
from .utils import build_mounts

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
PLAYER_VIEW_API = "https://api.bilibili.com/x/player/wbi/view"
PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

# fnval 位标志
FNVAL_DURL = 1  # mp4/flv 直链（音视频同一文件）
FNVAL_DASH = 16  # DASH（音视频分离）
FNVAL_8K = 128
FNVAL_DOLBY = 256

# 登录后可尝试的最高清晰度（qn=80 即 1080P）
QN_1080P = 80
QN_360P = 16


class BilibiliParser:
    """哔哩哔哩解析器（支持扫码登录 Cookie）"""

    def __init__(self, cookie: str | None = None, proxy: str | None = None):
        self.headers = BILI_HEADER.copy()
        self.cookie = (cookie or "").strip()
        self.proxy = proxy
        self._apply_cookie()

    def _apply_cookie(self) -> None:
        if self.cookie:
            self.headers["Cookie"] = self.cookie
        else:
            self.headers.pop("Cookie", None)

    # ---------------- 对外 ----------------

    async def check_cookies(self, cookies: dict[str, str] | None = None) -> tuple[bool, str, int]:
        """校验 Cookie 是否有效，返回 (是否登录, 昵称, uid)

        Args:
            cookies: 显式指定要校验的 Cookie；不传则用实例自带的 Cookie
        """
        headers = {**self.headers}
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        try:
            async with httpx.AsyncClient(
                timeout=COMMON_TIMEOUT,
                verify=False,
                headers=headers,
                mounts=build_mounts(self.proxy),
            ) as client:
                r = await client.get(NAV_API)
                data = r.json()
        except Exception as e:
            logger.warning(f"校验 B 站 Cookie 失败: {type(e).__name__}: {e}")
            return False, "", 0
        d = data.get("data") or {}
        return bool(d.get("isLogin")), d.get("uname", "") or "", int(d.get("mid") or 0)

    async def parse_video_info(
        self,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_num: int = 1,
        prefer_hires: bool = True,
    ) -> dict:
        """解析视频信息并返回可下载地址

        Args:
            bvid: BV 号
            avid: av 号
            page_num: 分 P 序号，从 1 开始
            prefer_hires: 已登录时是否尝试更高清晰度（失败自动退回 360P 合并流）

        Returns:
            dict: title / video_urls / video_duration / page_num
                  / need_merge / quality
        """
        if not bvid and not avid:
            raise ParseException("bvid 和 avid 至少指定一项")
        self._apply_cookie()

        meta, pages, rejected = await self._fetch_meta(bvid=bvid, avid=avid)
        cid = meta.get("cid")
        duration = meta.get("duration", 0)
        title = meta.get("title", "")

        # 分 P：page_num 从 1 开始，超出范围时取模回绕（与原版一致）
        page_idx = 0
        if pages and len(pages) > 1:
            page_idx = (page_num - 1) % len(pages)
            page = pages[page_idx]
            cid = page.get("cid") or cid
            duration = page.get("duration", duration)
            if part := (page.get("part") or "").strip():
                title += f"\n分集: {part}"

        if not cid:
            raise ParseException(
                "未能绕过 B 站风控拿到 cid，请重新扫码登录" if rejected else "未获取到 cid"
            )

        # 1) 已登录且允许时，先试高清 DASH
        if self.cookie and prefer_hires:
            hires = await self._try_hires(bvid=bvid, avaliable_avid=avid, cid=cid)
            if hires:
                hires.update(
                    title=title, video_duration=duration, page_num=page_idx + 1
                )
                return hires

        # 2) 合并流（音视频同一文件）
        data = await self._get_json(
            PLAYURL_API,
            {
                "platform": "html5",
                "high_quality": 1,
                "qn": QN_360P,
                "fnval": FNVAL_DURL,
                "fnver": 0,
                "fourk": 0,
                "bvid": bvid or "",
                "avid": "" if bvid else (avid or ""),
                "cid": cid,
                "web_location": 1315873,
            },
            "获取视频下载地址",
        )
        durl = (data.get("data") or {}).get("durl") or []
        if durl:
            return {
                "title": title,
                "video_urls": [d["url"] for d in durl],
                "video_duration": duration,
                "page_num": page_idx + 1,
                "need_merge": False,
                "quality": "360P",
            }

        raise ParseException("未获取到视频直链（可能为付费/受限视频）")

    # ---------------- 元信息（多接口降级） ----------------

    async def _fetch_meta(
        self, *, bvid: str | None, avid: int | None
    ) -> tuple[dict, list[dict], bool]:
        """按可用性依次尝试拿元信息，返回 (元信息, 分P列表, 是否曾被拒)"""
        vid = {"bvid": bvid} if bvid else {"aid": avid}
        rejected = False

        attempts = [("web/view", VIEW_API, vid)]
        if self.cookie:
            attempts.append(("player/wbi/view", PLAYER_VIEW_API, vid))

        for name, url, params in attempts:
            try:
                data = await self._get_json(url, params, "获取视频信息")
            except ParseException as e:
                if "412" in str(e):
                    rejected = True
                logger.info(f"{name} 不可用（{e}）")
                continue
            if data.get("code") == 0:
                info = data.get("data") or {}
                return info, info.get("pages") or [], rejected
            logger.info(f"{name} 返回 {data.get('code')}: {data.get('message')}")

        # 最后手段：player/playurl 不带 cid（只要 bvid），用登录态换 cid
        if bvid and self.cookie:
            try:
                data = await self._get_json(
                    PLAYURL_API,
                    {"bvid": bvid, "fnval": FNVAL_DURL, "qn": QN_360P, "platform": "html5"},
                    "获取视频信息",
                )
                if data.get("code") == 0:
                    d = data.get("data") or {}
                    return {
                        "cid": d.get("cid"),
                        "duration": int(d.get("timelength") or 0) // 1000,
                    }, [], rejected
            except ParseException as e:
                if "412" in str(e):
                    rejected = True
                logger.info(f"player/playurl(无 cid) 不可用（{e}）")

        return {}, [], rejected

    # ---------------- 高清（已登录） ----------------

    async def _try_hires(
        self, *, bvid: str | None, avaliable_avid: int | None, cid
    ) -> dict | None:
        """已登录时尝试拿 DASH 高清流；不可用返回 None"""
        try:
            data = await self._get_json(
                PLAYURL_API,
                {
                    "fnval": FNVAL_DASH | FNVAL_DOLBY | FNVAL_8K,
                    "fnver": 0,
                    "fourk": 0,
                    "qn": QN_1080P,
                    "bvid": bvid or "",
                    "avid": "" if bvid else (avaliable_avid or ""),
                    "cid": cid,
                    "platform": "pc",
                },
                "获取高清下载地址",
            )
        except ParseException as e:
            logger.info(f"高清流不可用，退回合并流（{e}）")
            return None

        if data.get("code") != 0:
            return None
        d = data.get("data") or {}
        dash = d.get("dash") or {}
        videos = dash.get("video") or []
        audios = (dash.get("audio") or []) or ([dash["dolby"]["audio"][0]] if (dash.get("dolby") or {}).get("audio") else [])
        if not videos:
            return None

        # 1080P 及以下里挑最高的一路（排除 AV1，兼容性差）
        def qn(item: dict) -> int:
            return int(item.get("id") or 0)

        videos = [v for v in videos if qn(v) <= QN_1080P]
        if not videos:
            return None
        best = max(videos, key=qn)
        audio = max(audios, key=qn) if audios else None

        urls = [best["baseUrl"]]
        if audio:
            urls.append(audio["baseUrl"])
        logger.info(
            f"已登录，使用高清流 qn={qn(best)}"
            f"{'（音视频分离，将合并）' if audio else ''}"
        )
        return {
            "video_urls": urls,
            "need_merge": bool(audio),
            "quality": f"{best.get('height') or qn(best)}P",
        }

    # ---------------- 底层请求 ----------------

    async def _get_json(self, url: str, params: dict, what: str) -> dict:
        """GET 并解析 JSON，把 B 站的 HTTP 错误翻译成人话"""
        try:
            async with httpx.AsyncClient(
                timeout=COMMON_TIMEOUT,
                verify=False,
                headers=self.headers,
                mounts=build_mounts(self.proxy),
            ) as client:
                resp = await client.get(url, params=params)
        except httpx.HTTPError as e:
            via = f"（代理 {self.proxy}）" if self.proxy else "（直连）"
            raise ParseException(f"{what}请求失败{via}: {type(e).__name__}") from e

        if resp.status_code == 412:
            logger.warning(
                f"B 站返回 412（IP 被风控）| 代理: {self.proxy or '无'} | "
                f"Cookie: {'有' if self.cookie else '无'}"
            )
            raise ParseException("B 站拒绝访问（412，本机 IP 被风控）")
        if resp.status_code != 200:
            raise ParseException(f"{what}失败: HTTP {resp.status_code}")

        try:
            return resp.json()
        except ValueError as e:
            raise ParseException(f"{what}失败: 响应不是合法 JSON") from e
