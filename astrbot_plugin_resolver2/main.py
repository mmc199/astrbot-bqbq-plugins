"""AstrBot 链接解析插件（移植自 nonebot-plugin-resolver2）。

支持 bilibili / 抖音 / 快手，自动检测横竖屏：
- 竖屏视频：原样发送
- 横屏视频：逆时针旋转 90 度后发送

仅在 httpx + ffmpeg 之上实现，不依赖 bilibili_api / yt_dlp。
"""

import re
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig

from .download import StreamDownloader
from .download.utils import (
    compress_video_for_upload,
    is_landscape_video,
    rotate_video_ccw90,
)
from .exception import ParseException, ResolverException
from .parsers import (
    BilibiliParser,
    DouyinParser,
    KuaishouParser,
    ProxyUnavailable,
    XiaoHongShuParser,
    YoutubeParser,
    check_proxy,
)
from .parsers.utils import get_redirect_url, is_proxy_alive, probe_proxy

# ---------------- 链接匹配正则 ----------------
# 因为要支持"消息里任意位置带链接"，这里用 re.search 而非 AstrBot 的 regex 过滤器
# （AstrBot 的 RegexFilter 内部用 re.match，只能从行首匹配）

BILI_BV_RE = re.compile(r"BV[1-9a-zA-Z]{10}")
BILI_AV_RE = re.compile(r"av(\d{6,})")
BILI_URL_RE = re.compile(
    r"https?://(?:space|www|live|m|t)?\.?bilibili\.com/[A-Za-z\d\._?%&+\-=/#]+"
)
BILI_SHORT_RE = re.compile(r"https?://(?:b23\.tv|bili2233\.cn)/[A-Za-z\d\._?%&+\-=/#]+")

DOUYIN_RE = re.compile(
    r"https?://(?:v\.douyin\.com|jx\.douyin\.com)/[a-zA-Z0-9_\-]+"
    r"|https?://(?:www\.|m\.)?(?:douyin|iesdouyin)\.com/(?:share/)?(?:slides|video|note)/\d+"
    r"|https?://jingxuan\.douyin\.com/m/(?:slides|video|note)/\d+"
)

KUAISHOU_RE = re.compile(
    r"https?://v\.kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+"
    r"|https?://(?:www\.)?kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+"
    r"|https?://(?:v\.m\.)?chenzhongtech\.com/fw/[A-Za-z\d._?%&+\-=/#]+"
)

# YouTube: 普通视频 / Shorts / 短链
YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.)?youtube\.com/(?:watch\?[A-Za-z\d._%&+\-=]*v=[A-Za-z0-9_-]{11}[A-Za-z\d._%&+\-=]*"
    r"|shorts/[A-Za-z0-9_-]{11}"
    r"|live/[A-Za-z0-9_-]{11}"
    r"|embed/[A-Za-z0-9_-]{11})"
    r"|https?://youtu\.be/[A-Za-z0-9_-]{11}"
)

# 小红书：正常链接 / 短链（xhslink.com 与 xhslink.cn 是两套域名，都要覆盖）
XHS_RE = re.compile(
    r"https?://(?:www\.)?xiaohongshu\.com/[A-Za-z0-9._?%&+=/#@\-]*"
    r"|https?://(?:www\.)?xhslink\.(?:com|cn)/[A-Za-z0-9._?%&+=/#@\-]*"
)

# 去掉链接尾部可能粘上的中文标点/空白
_TRAILING_JUNK = "，。、！？；：）)】》\"' \t\r\n"


def _clean_url(url: str) -> str:
    return url.rstrip(_TRAILING_JUNK)


# 默认缓存目录：沿用原 nonebot 插件的缓存位置，方便复用已下载的视频
DEFAULT_CACHE_DIR = Path(
    r"C:\Users\USER221806\AppData\Local\nonebot2\Cache\nonebot_plugin_resolver2"
)


def _resolve_cache_dir(configured: str) -> Path:
    """决定缓存目录

    优先使用配置项 cache_dir；留空则用原 nonebot 插件的缓存目录。
    若两者都无法创建（例如权限不足），回退到 AstrBot 的 plugin_data 目录，
    保证插件仍可正常工作。
    """
    candidates: list[Path] = []
    if configured.strip():
        candidates.append(Path(configured.strip()))
    candidates.append(DEFAULT_CACHE_DIR)
    candidates.append(StarTools.get_data_dir("astrbot_plugin_resolver2"))

    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            # 实际写一个临时文件，确认目录真的可写（mkdir 成功但不可写的情况存在）
            probe = path / ".write_test"
            probe.write_bytes(b"")
            probe.unlink(missing_ok=True)
            return path
        except Exception as e:
            logger.warning(f"缓存目录不可用，尝试下一个: {path} ({type(e).__name__}: {e})")

    # 理论上不会走到这里（最后一个候选是 AstrBot 自己的数据目录）
    raise RuntimeError("没有可用的缓存目录")


@register("astrbot_plugin_resolver2", "fllesser", "链接分享自动解析", "1.0.0")
class Resolver2Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.cache_dir = _resolve_cache_dir(self.config.get("cache_dir") or "")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.bili_parser = BilibiliParser(
            cookie=self.config.get("bilibili_cookie") or None,
            proxy=self.config.get("bilibili_proxy") or None,
        )
        self.douyin_parser = DouyinParser(self.config.get("douyin_cookie") or None)
        self.xhs_parser = XiaoHongShuParser(
            cookie=self.config.get("xiaohongshu_cookie") or None
        )
        self.kuaishou_parser = KuaishouParser()
        self.youtube_parser = YoutubeParser(
            self.config.get("youtube_proxy") or None
        )
        self.downloader = StreamDownloader(
            self.cache_dir, max_size_mb=int(self.config.get("max_size_mb", 10000))
        )

        # B 站代理解析结果缓存（按 配置值 缓存，配置改了会自动重新探测）
        self._bili_proxy_state: tuple[str, str, str | None] | None = None

        logger.info(
            f"resolver2 插件已加载, 缓存目录: {self.cache_dir}, "
            f"B 站登录: {'已配置 Cookie' if self.bili_parser.cookie else '未登录（可用 /bili登录 扫码）'}"
        )

    # ---------------- B 站请求方式（登录 > 代理 > 直连） ----------------

    def _reload_bili_cookie(self) -> None:
        """从配置里重新读取 Cookie（扫码登录器写完后下次解析即生效，无需重启）"""
        cookie = (self.config.get("bilibili_cookie") or "").strip()
        if cookie != self.bili_parser.cookie:
            self.bili_parser.cookie = cookie
            self.bili_parser._apply_cookie()
            logger.info(f"B 站 Cookie 已更新: {'有' if cookie else '无'}")

    async def _bili_proxy(self) -> tuple[str | None, str]:
        """按开关决定本次 B 站请求是否走代理

        Returns:
            (proxy, situation)
            proxy 为 None 表示直连；situation 取值：
              off       开关关闭，直连
              ok        代理可用，走代理
              unhealthy 代理进程/端口不通，直连
              unusable  代理端口通但出不了网，直连
        """
        if not self.config.get("bilibili_use_proxy", False):
            return None, "off"

        proxy = (self.config.get("bilibili_proxy") or "").strip()
        if not proxy:
            return None, "off"

        signature = "enabled" + proxy
        if self._bili_proxy_state and self._bili_proxy_state[0] == signature:
            _, situation, cached_proxy = self._bili_proxy_state
            return cached_proxy, situation

        # 先做纯 TCP 探测，端口没开就完全不用代理（省掉最长 6s 的超时）
        if not is_proxy_alive(proxy):
            logger.warning(f"B 站代理端口未监听（代理未开启？），本次直连: {proxy}")
            situation, chosen = "unhealthy", None
        elif await probe_proxy(proxy):
            logger.info(f"B 站走代理: {proxy}")
            situation, chosen = "ok", proxy
        else:
            logger.warning(f"B 站代理不可用（端口通但出网失败），本次直连: {proxy}")
            situation, chosen = "unusable", None

        self._bili_proxy_state = (signature, situation, chosen)
        return chosen, situation

    # ---------------- 权限判断 ----------------

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """私聊/群聊开关判断"""
        group_id = event.get_group_id()
        if group_id:
            return bool(self.config.get("enable_group", True))
        return bool(self.config.get("enable_private", True))

    # ---------------- 发送视频（核心：横竖屏检测） ----------------

    async def _send_video(
        self,
        event: AstrMessageEvent,
        video_path: Path,
        *,
        max_upload_size_mb: int | None = None,
    ):
        """检测横竖屏并发送：横屏逆时针旋转 90 度，竖屏原样发送"""
        if max_upload_size_mb:
            try:
                video_path = await compress_video_for_upload(
                    video_path, max_size_mb=max_upload_size_mb
                )
            except Exception as e:
                logger.warning(f"视频压缩失败，发送原视频: {e}")

        if self.config.get("auto_rotate_landscape", True):
            try:
                if await is_landscape_video(video_path):
                    logger.info(f"检测到横屏视频，自动逆时针旋转 90 度: {video_path.name}")
                    video_path = await rotate_video_ccw90(video_path)
                else:
                    logger.info(f"检测到竖屏视频，原样发送: {video_path.name}")
            except Exception as e:
                logger.warning(f"视频横竖屏处理失败，发送原视频: {e}")

        yield event.chain_result([Comp.Video.fromFileSystem(str(video_path))])

    # ---------------- 各平台处理 ----------------

    async def _handle_bilibili(self, event: AstrMessageEvent, text: str):
        """处理哔哩哔哩链接 / BV 号

        本机 IP 被 B 站风控（412）时，用扫码登录器（B站登录.bat）写入 Cookie 即可绕开；
        代理（bilibili_use_proxy）作为备选，只在对端可用时启用。
        """
        self._reload_bili_cookie()
        proxy, situation = await self._bili_proxy()

        bvid: str | None = None
        avid: int | None = None
        page_num = 1
        url = ""

        if matched := BILI_SHORT_RE.search(text):
            # b23.tv / bili2233.cn 短链需要解析重定向
            short = _clean_url(matched.group(0))
            url = await get_redirect_url(short, self.bili_parser.headers, proxy=proxy)
            if url == short:
                logger.info(f"链接 {short} 无效，忽略")
                return

        if not url and (matched := BILI_URL_RE.search(text)):
            url = _clean_url(matched.group(0))

        # 从链接中提取 BV / av 号（含分 P 参数）
        if url:
            if m := re.search(r"/(BV[1-9a-zA-Z]{10})", url):
                bvid = m.group(1)
            elif m := re.search(r"/av(\d{6,})", url):
                avid = int(m.group(1))
            if m := re.search(r"(?:&|\?)p=(\d{1,3})", url):
                page_num = int(m.group(1))

        # 纯 BV 号（不带链接）
        if not bvid and not avid:
            if m := BILI_BV_RE.search(text):
                bvid = m.group(0)
            elif m := BILI_AV_RE.search(text):
                avid = int(m.group(1))

        if not bvid and not avid:
            logger.info(f"未能从消息中提取 B 站视频号，忽略: {text[:60]}")
            return

        # 代理复用实例，本次请求使用上面判断出的代理
        self.bili_parser.proxy = proxy
        info = await self.bili_parser.parse_video_info(
            bvid=bvid, avid=avid, page_num=page_num
        )

        max_duration = int(self.config.get("video_duration_maximum", 86400))
        if info["video_duration"] > max_duration:
            logger.info(f"视频时长 {info['video_duration']}s 超过 {max_duration}s, 忽略")
            yield event.plain_result("视频时长超过限制，已跳过下载")
            return

        # 以视频号为文件名，命中缓存则直接复用
        file_name = f"{bvid or avid}-{info['page_num']}.mp4"
        video_path = self.cache_dir / file_name
        if not video_path.exists():
            video_urls: list[str] = info["video_urls"]
            if info.get("need_merge"):
                video_path = await self._download_and_merge(
                    video_urls, file_name, proxy=proxy
                )
            else:
                video_path = await self.downloader.download_video(
                    video_urls[0],
                    video_name=file_name,
                    ext_headers=self.bili_parser.headers,
                    proxy=proxy,
                )

        if self.config.get("send_title", False):
            title = info.get("title") or ""
            quality = info.get("quality") or ""
            yield event.plain_result(
                f"哔哩哔哩 | {title}" + (f"（{quality}）" if quality else "")
            )

        async for result in self._send_video(event, video_path):
            yield result

    async def _download_and_merge(
        self, urls: list[str], file_name: str, *, proxy: str | None
    ) -> Path:
        """下载 DASH 的音/视频两路流并合并成一个 mp4（登录后高清流是分离的）"""
        from .download.utils import merge_av

        paths = []
        for i, url in enumerate(urls):
            paths.append(
                await self.downloader.download_file_by_stream(
                    url,
                    file_name=f"{Path(file_name).stem}.part{i}{Path(file_name).suffix}",
                    ext_headers=self.bili_parser.headers,
                    proxy=proxy,
                )
            )
        out = self.cache_dir / file_name
        await merge_av(v_path=paths[0], a_path=paths[1], output_path=out)
        logger.info(f"已合并音视频: {out.name}")
        return out

    async def _handle_douyin(self, event: AstrMessageEvent, text: str):
        """处理抖音链接"""
        matched = DOUYIN_RE.search(text)
        if not matched:
            return
        share_url = _clean_url(matched.group(0))

        parse_result = await self.douyin_parser.parse_share_url(share_url)

        # 图片 / 动态图集
        if pic_urls := parse_result.pic_urls:
            paths = await self.downloader.download_imgs_without_raise(pic_urls)
            if paths:
                yield event.chain_result(
                    [Comp.Image.fromFileSystem(str(p)) for p in paths]
                )

        if dynamic_urls := parse_result.dynamic_urls:
            for url in dynamic_urls:
                try:
                    p = await self.downloader.download_video(url)
                    async for result in self._send_video(event, p):
                        yield result
                except Exception as e:
                    logger.warning(f"下载抖音动态图失败: {e}")

        if parse_result.pic_urls:
            return

        if video_url := parse_result.video_url:
            video_path = await self.downloader.download_video(video_url)
            if self.config.get("send_title", False):
                yield event.plain_result(f"抖音 | {parse_result.title}")
            async for result in self._send_video(
                event,
                video_path,
                max_upload_size_mb=int(self.config.get("douyin_max_upload_size", 45)),
            ):
                yield result

    async def _handle_xiaohongshu(self, event: AstrMessageEvent, text: str):
        """处理小红书链接（图文 / 视频）

        小红书直链带时效签名，下载图片和视频都必须带上 Referer/UA/Cookie，
        否则 CDN 会 403 —— 这里统一把解析器的请求头交给下载器。
        """
        matched = XHS_RE.search(text)
        if not matched:
            return
        url = _clean_url(matched.group(0))

        # Cookie 可能由小红书登录器刚刚写入，解析前重新读一次
        cookie = (self.config.get("xiaohongshu_cookie") or "").strip()
        if cookie != self.xhs_parser.cookie:
            self.xhs_parser.cookie = cookie
            self.xhs_parser._apply_cookie()

        parse_result = await self.xhs_parser.parse_url(url)
        headers = self.xhs_parser.headers

        # 图文
        if pic_urls := parse_result.pic_urls:
            if self.config.get("send_title", False) and parse_result.title:
                yield event.plain_result(f"小红书 | {parse_result.title}")
            paths = await self.downloader.download_imgs_without_raise(
                pic_urls, ext_headers=headers
            )
            if paths:
                yield event.chain_result(
                    [Comp.Image.fromFileSystem(str(p)) for p in paths]
                )
            return

        # 视频（与 B 站一致：下载 → 横竖屏处理 → 发送）
        if video_url := parse_result.video_url:
            video_path = await self.downloader.download_video(
                video_url, ext_headers=headers
            )
            if self.config.get("send_title", False):
                yield event.plain_result(f"小红书 | {parse_result.title}")
            async for result in self._send_video(event, video_path):
                yield result

    async def _handle_kuaishou(self, event: AstrMessageEvent, text: str):
        """处理快手链接"""
        matched = KUAISHOU_RE.search(text)
        if not matched:
            return
        url = _clean_url(matched.group(0))

        parse_result = await self.kuaishou_parser.parse_url(url)

        if self.config.get("send_title", False):
            title = f"快手 | {parse_result.title}-{parse_result.author}"
            if cover_url := parse_result.cover_url:
                try:
                    cover_path = await self.downloader.download_img(cover_url)
                    yield event.chain_result(
                        [Comp.Plain(title), Comp.Image.fromFileSystem(str(cover_path))]
                    )
                except Exception:
                    yield event.plain_result(title)
            else:
                yield event.plain_result(title)

        if video_url := parse_result.video_url:
            video_path = await self.downloader.download_video(video_url)
            async for result in self._send_video(event, video_path):
                yield result
        elif pic_urls := parse_result.pic_urls:
            paths = await self.downloader.download_imgs_without_raise(pic_urls)
            if paths:
                yield event.chain_result(
                    [Comp.Image.fromFileSystem(str(p)) for p in paths]
                )

    async def _handle_youtube(self, event: AstrMessageEvent, text: str):
        """处理 YouTube 链接（普通视频 / Shorts）

        代理不通时提示用户开启代理。
        """
        matched = YOUTUBE_RE.search(text)
        if not matched:
            return
        url = _clean_url(matched.group(0))

        proxy = self.config.get("youtube_proxy") or ""
        # 先检查代理，给出明确提示而不是让用户等超时
        if proxy and not await check_proxy(proxy):
            logger.warning(f"YouTube 代理不可用: {proxy}")
            yield event.plain_result("代理未开启或不可用")
            return

        try:
            info = await self.youtube_parser.get_video_info(url)
        except ProxyUnavailable:
            yield event.plain_result("代理未开启或不可用")
            return

        title = info.get("title") or ""
        uploader = info.get("uploader") or ""
        duration = int(info.get("duration") or 0)

        max_duration = int(self.config.get("video_duration_maximum", 86400))
        if duration > max_duration:
            yield event.plain_result(f"视频时长 {duration}s 超过限制，已跳过下载")
            return

        # 选一个免登录、音视频合一的低清晰度
        fmt = self.youtube_parser.pick_format(info)
        if not fmt:
            yield event.plain_result("未找到可下载的清晰度（可能需要登录）")
            return
        logger.info(
            f"YouTube 选用清晰度: id={fmt.get('format_id')} "
            f"{fmt.get('height')}p ext={fmt.get('ext')}"
        )

        if self.config.get("send_title", False):
            label = f"YouTube | {title}"
            if uploader:
                label += f" - {uploader}"
            yield event.plain_result(label)

        video_path = await self.youtube_parser.download_video(url, self.cache_dir)
        # 走统一的横竖屏逻辑：Shorts 竖屏原样发，横屏转 90 度
        async for result in self._send_video(event, video_path):
            yield result

    # ---------------- 统一入口 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，发现支持的链接即解析下载

        使用 ALL + 自行 re.search，因此：
        - 链接出现在消息任意位置都能识别
        - 私聊、群聊都生效（受插件配置开关控制）
        """
        text = event.get_message_str()
        if not text:
            return

        if not self._is_allowed(event):
            return

        # 快速预筛，避免无关消息进入处理流程
        if not re.search(
            r"(bilibili\.com|b23\.tv|bili2233\.cn|BV[1-9a-zA-Z]{10}"
            r"|douyin\.com|kuaishou\.com|chenzhongtech\.com"
            r"|xiaohongshu\.com|xhslink\.(?:com|cn)"
            r"|youtube\.com|youtu\.be)",
            text,
        ):
            return

        handlers = []
        if self.config.get("enable_bilibili", True):
            handlers.append(self._handle_bilibili)
        if self.config.get("enable_douyin", True):
            handlers.append(self._handle_douyin)
        if self.config.get("enable_kuaishou", True):
            handlers.append(self._handle_kuaishou)
        if self.config.get("enable_xiaohongshu", True):
            handlers.append(self._handle_xiaohongshu)
        if self.config.get("enable_youtube", True):
            handlers.append(self._handle_youtube)

        for handler in handlers:
            try:
                async for result in handler(event, text):
                    yield result
            except (ParseException, ResolverException) as e:
                logger.warning(f"解析失败: {e}")
                yield event.plain_result(f"解析失败：{e}")
            except Exception as e:
                logger.error(f"处理链接时发生异常: {e}", exc_info=True)
                yield event.plain_result("解析出错，请稍后再试")

    async def terminate(self):
        """插件卸载清理"""
        logger.info("resolver2 插件已卸载")
