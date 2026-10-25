"""抖音解析器 —— 纯 json 实现，不依赖 msgspec。

对照原版 parsers/douyin.py：
- 用 json.loads + dict.get 复刻原 msgspec Struct 的字段读取
- 保留"逐档探测清晰度、取 br 最小直链"的逻辑
"""

import asyncio
import json
import re
from random import choice

import httpx
from astrbot.api import logger

from ..constants import ANDROID_HEADER, COMMON_TIMEOUT, IOS_HEADER
from ..exception import ParseException
from .data import ImageContent, ParseResult, VideoContent
from .utils import get_redirect_url

# 匹配长链接中的类型和 ID
_SHARE_RE = re.compile(
    r"(?:(?:www\.|m\.)?(?:douyin|iesdouyin)\.com/(?:share/)?|jingxuan\.douyin\.com/m/)"
    r"(slides|video|note)/(\d+)"
)

_ROUTER_DATA_RE = re.compile(
    pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
    flags=re.DOTALL,
)

# 抖音直链的 br 参数即视频码率 (kbps)
_BR_RE = re.compile(r"[?&]br=(\d+)")
_RATIO_RE = re.compile(r"(?<=[?&])ratio=[^&]*")

# 由低到高的清晰度候选
_QUALITY_CANDIDATES = ("360p", "480p", "540p", "720p")


def _pick_url(url_list: list[str] | None) -> str | None:
    """从 url_list 中随机取一个（原版 Struct 行为）"""
    return choice(url_list) if url_list else None


class DouyinParser:
    """抖音解析器"""

    def __init__(self, cookie: str | None = None):
        # 未登录时抖音对分享页做 SSR 降级 (videoInfoRes 整个不返回), 带登录 cookie 才能拿到数据
        cookies = {"Cookie": cookie} if cookie else {}
        self.ios_headers = {**IOS_HEADER, **cookies}
        self.android_headers = {
            "Accept": "application/json, text/plain, */*",
            **ANDROID_HEADER,
            **cookies,
        }

    @staticmethod
    def _build_iesdouyin_url(_type: str, video_id: str) -> str:
        return f"https://www.iesdouyin.com/share/{_type}/{video_id}"

    @staticmethod
    def _build_m_douyin_url(_type: str, video_id: str) -> str:
        return f"https://m.douyin.com/share/{_type}/{video_id}"

    async def parse_share_url(self, share_url: str) -> ParseResult:
        """解析抖音分享链接"""
        if matched := _SHARE_RE.search(share_url):
            _type, video_id = matched.group(1), matched.group(2)
            candidates = (
                self._build_m_douyin_url(_type, video_id),
                share_url,
                self._build_iesdouyin_url(_type, video_id),
            )
        else:
            # https://v.douyin.com/xxxxxx 等短链接, 需要重定向
            iesdouyin_url = await get_redirect_url(share_url, self.ios_headers)
            matched = re.search(r"(slides|video|note)/(\d+)", iesdouyin_url)
            if not matched:
                raise ParseException(f"无法从 {share_url} 中解析出 ID")
            _type, video_id = matched.group(1), matched.group(2)
            candidates = (
                self._build_m_douyin_url(_type, video_id),
                iesdouyin_url,
            )

        if _type == "slides":
            return await self.parse_slides(video_id)

        last_error: ParseException | None = None
        for url in candidates:
            try:
                return await self.parse_video(url)
            except ParseException as e:
                logger.warning(f"failed to parse {url[:60]}, error: {e}")
                last_error = e
                continue
        raise last_error or ParseException("作品已删除，或资源直链获取失败, 请稍后再试")

    async def parse_video(self, url: str) -> ParseResult:
        """解析抖音视频分享页"""
        async with httpx.AsyncClient(
            headers=self.ios_headers,
            verify=False,
            timeout=COMMON_TIMEOUT,
            follow_redirects=False,
        ) as client:
            response = await client.get(url)
            if response.status_code != 200:
                raise ParseException(f"status: {response.status_code}")
            text = response.text

        matched = _ROUTER_DATA_RE.search(text)
        if not matched or not matched.group(1):
            raise ParseException("can't find _ROUTER_DATA in html")

        try:
            router = json.loads(matched.group(1).strip())
        except json.JSONDecodeError as e:
            raise ParseException("解析 _ROUTER_DATA JSON 失败") from e

        loader_data = router.get("loaderData") or {}
        page = loader_data.get("video_(id)/page") or loader_data.get("note_(id)/page")
        if page is None:
            raise ParseException("can't find video_(id)/page or note_(id)/page in router data")

        video_info_res = page.get("videoInfoRes") or {}
        item_list = video_info_res.get("item_list") or []
        if not item_list:
            filter_list = video_info_res.get("filter_list") or []
            if filter_list:
                item = filter_list[0]
                raise ParseException(
                    item.get("detail_msg") or item.get("filter_reason") or "作品已删除"
                )
            raise ParseException("该作品需登录或 App 访问，无法解析")

        video_data = choice(item_list)

        # 图片 (图文/笔记) 优先
        images = video_data.get("images") or []
        video = video_data.get("video") or {}

        if images:
            pic_urls = [
                u for img in images if (u := _pick_url(img.get("url_list")))
            ]
            return ParseResult(
                title=video_data.get("desc") or "",
                cover_url=_pick_url((video.get("cover") or {}).get("url_list")),
                author=((video_data.get("author") or {}).get("nickname")) or "",
                content=ImageContent(pic_urls=pic_urls),
            )

        play_url = _pick_url((video.get("play_addr") or {}).get("url_list"))
        if play_url:
            play_url = play_url.replace("playwm", "play")
            return ParseResult(
                title=video_data.get("desc") or "",
                cover_url=_pick_url((video.get("cover") or {}).get("url_list")),
                author=((video_data.get("author") or {}).get("nickname")) or "",
                content=VideoContent(video_url=await self._lowest_bitrate_url(play_url)),
            )

        raise ParseException("未找到可下载的视频或图片内容")

    @staticmethod
    def _with_ratio(url: str, ratio: str) -> str:
        """把直链的 ratio 参数替换为指定清晰度 (不存在则追加)"""
        if _RATIO_RE.search(url):
            return _RATIO_RE.sub(f"ratio={ratio}", url)
        return f"{url}{'&' if '?' in url else '?'}ratio={ratio}"

    async def _lowest_bitrate_url(self, play_url: str) -> str:
        """探测各清晰度直链, 返回码率最低的一条

        抖音对不存在的 ratio 会回退到最高清 (而不是就近降级), 所以不能直接写死低清晰度,
        只能逐个探测再比较直链末尾的 br 参数, 取实际码率最小的那个。
        """
        candidates = list(_QUALITY_CANDIDATES)
        resolved = await asyncio.gather(
            *(
                get_redirect_url(self._with_ratio(play_url, r), self.ios_headers)
                for r in candidates
            ),
            return_exceptions=True,
        )

        best_url: str | None = None
        best_br: int | None = None
        for ratio, url in zip(candidates, resolved):
            if isinstance(url, BaseException):
                logger.warning(f"探测 {ratio} 直链失败: {url}")
                continue
            if (matched := _BR_RE.search(url)) is None:
                continue
            br = int(matched.group(1))
            logger.debug(f"抖音清晰度探测: {ratio} -> br={br}")
            if best_br is None or br < best_br:
                best_url, best_br = url, br

        if best_url is None:
            return await get_redirect_url(play_url, self.ios_headers)
        logger.info(f"抖音选用最低清晰度直链: br={best_br}")
        return best_url

    async def parse_slides(self, video_id: str) -> ParseResult:
        """解析抖音图集"""
        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {"aweme_ids": f"[{video_id}]", "request_source": "200"}
        async with httpx.AsyncClient(headers=self.android_headers, verify=False) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()

        try:
            slides_info = response.json()
        except json.JSONDecodeError as e:
            raise ParseException("解析图集 JSON 失败") from e

        aweme_details = slides_info.get("aweme_details") or []
        if not aweme_details:
            raise ParseException("can't find aweme_details in json")
        slides_data = aweme_details[0]

        images = slides_data.get("images") or []
        pic_urls = [u for img in images if (u := _pick_url(img.get("url_list")))]
        dynamic_urls = [
            u
            for img in images
            if (video := img.get("video"))
            and (u := _pick_url((video.get("play_addr") or {}).get("url_list")))
        ]

        title = slides_data.get("desc") or (
            (slides_data.get("share_info") or {}).get("share_desc_info") or ""
        )

        return ParseResult(
            title=title,
            cover_url="",
            author=((slides_data.get("author") or {}).get("nickname")) or "",
            content=ImageContent(pic_urls=pic_urls, dynamic_urls=dynamic_urls),
        )
