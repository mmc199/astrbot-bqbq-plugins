"""快手解析器 —— 纯 httpx 实现。"""

import json
import re
import urllib.parse

import httpx
from astrbot.api import logger

from ..constants import COMMON_TIMEOUT, IOS_HEADER
from ..exception import ParseException
from .data import ImageContent, ParseResult, VideoContent
from .utils import get_redirect_url


class KuaishouParser:
    """快手解析器"""

    def __init__(self):
        self.v_headers = {
            **IOS_HEADER,
            "Referer": "https://v.kuaishou.com/",
        }

    async def parse_url(self, url: str) -> ParseResult:
        """解析快手链接获取视频信息"""
        location_url = await get_redirect_url(url, headers=self.v_headers)

        if len(location_url) <= 0:
            raise ParseException("failed to get location url from url")

        # /fw/long-video/ 返回结果不一样, 统一替换为 /fw/photo/ 请求
        location_url = location_url.replace("/fw/long-video/", "/fw/photo/")

        async with httpx.AsyncClient(
            headers=self.v_headers, timeout=COMMON_TIMEOUT, verify=False
        ) as client:
            response = await client.get(location_url)
            response.raise_for_status()
            response_text = response.text

        pattern = r"window\.INIT_STATE\s*=\s*(.*?)</script>"
        searched = re.search(pattern, response_text)

        if not searched or len(searched.groups()) < 1:
            raise ParseException("failed to parse video JSON info from HTML")

        json_text = searched.group(1).strip()
        try:
            json_data = json.loads(json_text)
        except json.JSONDecodeError as e:
            raise ParseException("failed to parse INIT_STATE payload") from e

        photo_data = {}
        for json_item in json_data.values():
            if isinstance(json_item, dict) and "result" in json_item and "photo" in json_item:
                photo_data = json_item
                break

        if not photo_data:
            raise ParseException("failed to parse photo info from INIT_STATE")

        if (result_code := photo_data["result"]) != 1:
            raise ParseException(f"获取作品信息失败: {result_code}")

        data = photo_data["photo"]

        # 视频直链
        video_content = None
        main_mv_urls = data.get("mainMvUrls") or []
        if main_mv_urls:
            video_content = VideoContent(video_url=main_mv_urls[0]["url"])

        # 图集
        ext_params_atlas = (data.get("ext_params") or {}).get("atlas") or {}
        atlas_cdn_list = ext_params_atlas.get("cdn") or []
        atlas_list = ext_params_atlas.get("list") or []
        images = []
        if atlas_cdn_list and atlas_list:
            images = [f"https://{atlas_cdn_list[0]}/{atlas}" for atlas in atlas_list]

        cover_urls = data.get("coverUrls") or []

        return ParseResult(
            title=data.get("caption", ""),
            cover_url=cover_urls[0]["url"] if cover_urls else None,
            author=data.get("userName", ""),
            content=video_content or ImageContent(pic_urls=images),
        )
