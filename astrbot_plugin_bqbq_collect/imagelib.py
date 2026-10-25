"""图片库查询与标签管理（对齐 bqbq_v2 后端）。

从旧 nonebot 插件 `bqbq/__init__ (3).py` 迁移而来，但接口与展示方式
都做了实质调整 —— 详见下面每一节的说明。

===============================================================================
 一、与旧插件的接口差异（迁移时全部实测确认）
===============================================================================

旧插件连的是**旧 Flask 后端（端口 5000）**，本插件连 **bqbq_v2（8000）**。
接口路径、方法、参数、返回字段全都不一样：

    功能            旧（5000）                          新（8000，本插件）
    ───────────────────────────────────────────────────────────────────────
    搜索            GET  /api/search                    POST /api/search
                    ?include=&exclude=&offset=&limit    JSON body
    语义搜索        GET  /api/semantic_search           **已移除，不再支持**
    上传            POST /api/check_upload              POST /api/images/upload
    存标签          POST /api/save_tags                 POST /api/update_tags
                    {"filename": ..., "tags": [...]}    {"md5": ..., "tags": [...]}

  【最容易踩的坑：返回字段】
  旧接口返回 `item['url']`，可直接拼成图片地址。
  新接口返回 `item['filename']`，是**分片相对路径**（如 `be/a5/bea5....gif`）。
  图片地址必须自己拼：{backend}/images/{filename}
  （缩略图：{backend}/thumbnails/{filename}）

  【另一个坑：update_tags 用 md5 不用 filename】
  旧接口按 filename 定位，新的按 **md5** 定位。
  好在搜索结果的 SearchResultItem 里同时有 md5 和 filename，够用。

===============================================================================
 二、展示方式的根本改变（qqofficial 的限制）
===============================================================================

旧插件用 `MessageSegment.node_custom` 发**合并转发卡片**装多张图。
这在 QQ 官方机器人上**做不到**，原因在适配器源码里写死了：

  · `qqofficial_message_event.py:966`
        else:
            logger.debug(f"qq_official 忽略 {i.type}")
    → `Nodes` 组件走 else 分支被**静默忽略**（只打 debug 日志，不报错）。
      你会以为发送成功，实际什么都没收到。

  · `qqofficial_message_event.py:922`
        elif isinstance(i, Image) and not image_base64:
    → **一条消息只能带一张图**，后续 Image 全部跳过。

所以本插件改成：
  · `bqbq <关键词>`      → 返回**编号 + 标签列表**的文字（不发图）
  · `bqbq看 1 3 5`       → 把指定编号的图**拼成一张竖排长图**发出

拼图方案的好处：一条消息看完多张图，接近原插件的观感；
代价：**动图只显示第一帧**（拼图后是静态的）。

===============================================================================
 三、原插件里没有迁移的部分及原因
===============================================================================

  · **静默存图**（自动收集群里所有 GIF）
    非官方协议（NapCat/LLOneBot）能收到群内全部消息，所以能"默默"收集。
    QQ 官方机器人只能收到**私聊 + @机器人**的消息，收不到就无从收集。
    官方文档确认全量模式（GROUP_MESSAGE_CREATE）仍在灰度，个人主体拿不到。
    → 迁了也是死代码。

  · **管理员指令**（whitelist/blacklist/notify/sic/conf/reboot）
    AstrBot 有 WebUI 配置界面，命令管理价值不大。
    且原插件里这部分本来就是占位（只有 conf 和 sic 有实现，其余是注释省略）。

  · **语义搜索**
    新后端没有这个接口。原插件是在标签搜索返回 0 条时用它兜底。

  · **按 QQ 号的名单**
    官方机器人拿不到 QQ 号，`get_sender_id()` 返回的是 openid。
    现有收集插件已经用 openid 重做了白名单，这里沿用。
"""
from __future__ import annotations

import asyncio
import io
import re
import time
from pathlib import Path

import httpx

from astrbot.api import logger

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 预览拼图排版
# ---------------------------------------------------------------------------
#
# 【为什么是网格而不是竖排】早期用"每张图竖着叠"，5 张就 1656px 高，
# 手机上要滑很久，而且宽度只用了 720 中的一部分，浪费屏幕。
# 改成 **4 列网格**后：横向铺满、纵向按需增长，一屏能看到更多张。
#
# 【高度约束】QQ 图片消息在手机上按宽度自适应，所以关键在于控制
# **高宽比**。竖屏手机常见是 19.5:9 ≈ 2.17，这里把画布高宽比
# 上限设为 2.0，保证一屏基本能看完（不达标就减行数）。
# 【为什么是 4 列 x 8 行】用户要求"最多 4x8"（列 x 行）。
# 4 列下每格宽约 165px，缩略图看得比 8 列清楚得多；
# 高度靠增加行数来堆（最多 8 行 = 32 格）。
#
# 注意行列别写反：这里是 **GRID_COLS 列 x MAX_ROWS 行**，
# 即"窄而高"的竖图，符合手机竖屏的浏览习惯。
GRID_COLS = 4               # 列数（横向最多 4 格）
CANVAS_W = 800              # 画布宽度（4 列下每格约 190px）
PAD = 8                     # 画布内边距
GAP = 5                     # 格子间距
CELL_H = 165                # 每个格子的图片区高度
LABEL_H = 26                # 每格下方标签区高度
MAX_ROWS = 8                # 行数（纵向最多 8 行）
MAX_ASPECT = 2.2            # 画布高宽比上限
# 说明：4x8 满格时高宽比约 (8*(165+26)+7*5+边距)/800 ≈ 1.98，
# 在竖屏手机（约 2.17）一屏内。上限给 2.2 留一点余量，
# 避免凑满 32 格时被削到 31 张这种别扭的结果。

BG_COLOR = (250, 250, 250)
LABEL_COLOR = (40, 40, 40)
BORDER_COLOR = (210, 210, 210)
CELL_BG = (245, 245, 245)
HINT_COLOR = (120, 120, 120)

# 编号颜色：静图深灰，动图橙色。
#
# 【为什么用颜色区分动图】4 列时每格约 190px，编号后面加 ".gif" 会挤占
# 标签的位置；而且标签用 "编号.标签" 拼接，扩展名里的点会把切分搞乱。
# 改成给编号上色后，宽度不变、一眼可分。
NUM_COLOR = (20, 20, 20)          # 静图：接近纯黑
GIF_COLOR = (230, 126, 34)        # 动图：橙色（对比够强，不刺眼）

# 预览图输出质量。见 stitch() 末尾的说明：
# 用 JPEG 而非 PNG，4x8 满格能省 79%（1161KB -> 247KB）。
JPEG_QUALITY = 82

# 一次搜索最多取多少条（后端 page_size 上限 100）
SEARCH_FETCH_LIMIT = 100
# 一次最多拼几张图（防止长图过高）
MAX_STITCH = 10

# 字体候选（Windows 常见中文字体）
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

# 触发词
CMD_PREFIX = "bqbq"

# 标签操作的词表在 commands.py 的 TAG_WORDS（只认 add / tag / del）。
# 这里原本还有 VIEW_WORDS / ADD_WORDS / TAG_WORDS / DEL_WORDS 四份，
# 与 commands.py 重复，已删除 —— 判定只在 parse_command 一处做。

# 一次最多发几张原图。
#
# 【为什么要限制】一次发太多会刷屏（每条消息一张图）。
# 3 张是个折中：够用，又不至于把对话顶上去。
MAX_VIEW = 3

# 单张预览图最多放多少个格子
MAX_STITCH = GRID_COLS * MAX_ROWS

# 搜索时不写范围的话，默认取多少张用于预览。
#
# 【为什么不再固定 5】早期默认只显示 5 张，搜到 13 张也只给看 5 张。
# 现在各取所需的写法是"先全给看"（最多 MAX_STITCH 张），
# 用户看中哪张就直接发它的编号取原图。
# 这里给一个足够大的数，实际由网格容量 MAX_STITCH 兜底。
DEFAULT_SHOW = MAX_STITCH

TRASH_TAG = "trash_bin"


def _load_font(size: int):
    """加载中文字体。找不到就退回 PIL 默认字体（不支持中文，但不会崩）。"""
    from PIL import ImageFont
    for p in _FONT_CANDIDATES:
        try:
            if Path(p).is_file():
                return ImageFont.truetype(p, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _parse_range(text: str) -> tuple[str, int, int]:
    """解析「关键词 + 范围」，返回 (关键词, start_idx, end_idx)。

    支持旧插件的写法：
        "猫"           -> ("猫", 0, **全部**)    # 不写范围就全要
        "猫 1:5"       -> ("猫", 0, 5)
        "猫 [2/4]"     -> ("猫", 1, 4)          # 第 2~4 张
        "猫 3,7"       -> ("猫", 2, 7)

    【2026-10 改动：默认不再截断到 5 张】
    早期默认 end=5，导致搜到 13 张却只显示 5 张。
    用户要求"尽量全发出来"（网格最多 8x4=32 格），
    所以这里返回一个足够大的 end，由调用方按网格容量截断。

    Args:
        text: 用户输入（不含 bqbq 前缀）。

    Returns:
        (关键词, start_idx, end_idx)。end_idx 是"取到第几条"（不含）。
    """
    pattern = r"^(.*?)(?:\s*(?:\[)?(\d+)[/:,](\d+)(?:\])?)?$"
    m = re.search(pattern, text.strip())
    if not m:
        return text.strip(), 0, DEFAULT_SHOW
    query = (m.group(1) or "").strip()
    start, end = 0, DEFAULT_SHOW
    if m.group(2) and m.group(3):
        s_u, e_u = int(m.group(2)), int(m.group(3))
        start = max(0, s_u - 1)
        end = e_u
    if end <= start:
        end = start + DEFAULT_SHOW
    return query, start, end


def _parse_index_list(text: str) -> list[int]:
    """解析「bqbq看」后面的编号列表。

    支持：1 3 5 / 1,3,5 / 1-3 / 1、3、5
    返回 1-based 的编号列表（去重、保序、限制数量）。
    """
    if not text:
        return []
    # 统一分隔符
    norm = re.sub(r"[，、,;；]", " ", text)
    out: list[int] = []
    for tok in norm.split():
        rng = re.match(r"^(\d+)\s*[-~]\s*(\d+)$", tok)
        if rng:
            a, b = int(rng.group(1)), int(rng.group(2))
            if a > b:
                a, b = b, a
            out.extend(range(a, b + 1))
            continue
        if tok.isdigit():
            out.append(int(tok))
    # 去重保序
    seen = set()
    uniq = []
    for n in out:
        if n >= 1 and n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq[:MAX_STITCH]


class ImageLibClient:
    """bqbq_v2 图片库客户端：搜索 / 查标签 / 改标签 / 取图 / 拼图。"""

    def __init__(self, backend: str, client_id: str = ""):
        """初始化。

        Args:
            backend: 后端根地址，如 http://127.0.0.1:8000
            client_id: bot 的长期凭证，形如 bot_astrbot_<24位随机>。
                后端写接口（上传 / 改标签）已加 require_edit 鉴权，
                请求必须携带它，否则返回 401。
        """
        self.backend = backend.rstrip("/")
        self.client_id = (client_id or "").strip()
        # 搜索结果缓存：按用户/会话存最近一次结果，供「bqbq看」使用
        # {session_key: (timestamp, [items])}
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._cache_ttl = 600.0     # 10 分钟过期
        # 同义词膨胀映射（关键词 -> 同义词列表），首次搜索时惰性加载并缓存。
        # None 表示还没加载；空 dict 表示加载过但规则树为空。
        self._expand_map: dict[str, list[str]] | None = None
        self._expand_ts: float = 0.0
        self._expand_ttl: float = 300.0   # 5 分钟后自动刷新，规则改动能生效

    # ------------------------------------------------------------------
    # 鉴权辅助
    # ------------------------------------------------------------------
    def _with_cid(self, url: str) -> str:
        """给 URL 追加 client_id 查询参数（用于 multipart / 无 body 的请求）。"""
        if not self.client_id:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}client_id={self.client_id}"

    def _body(self, payload: dict) -> dict:
        """给 JSON 请求体补上 client_id。"""
        if self.client_id:
            return {**payload, "client_id": self.client_id}
        return payload

    # ------------------------------------------------------------------
    # 同义词膨胀
    # ------------------------------------------------------------------
    async def _load_expand_map(self, client: httpx.AsyncClient) -> dict[str, list[str]]:
        """从后端规则树构建「关键词 -> 同义词列表」映射。

        【为什么插件自己做】后端 /api/search 的 keywords 模式（二维数组）
        语义是「组内 OR、组间 AND」，但**不会自动膨胀** —— 文档写明
        「每个子数组是一个标签膨胀后的关键词列表」，膨胀由调用方完成。
        网页版就是这么做的（见 Gallery.vue 的 expandKeywordsToGroups）。

        插件之前只传 [[soyo]]，实测只搜到 12 张；网页传
        [[长崎素世,长崎爽世,长期素食,长期爽食,soyo]] 能搜到 13 张。
        差的那 1 张就是没膨胀导致的。

        规则树结构（GET /api/rules）：
            groups      [{group_id, group_name, is_enabled}]
            keywords    [{keyword, group_id, is_enabled}]
            hierarchy   [{parent_id, child_id}]

        膨胀语义：关键词命中某个组后，把该组**及其所有子孙组**的
        启用关键词都收进来（与网页展开父节点时一致）。

        Returns:
            {关键词: [同义词...]}，关键词包含自身。
        """
        try:
            r = await client.get(f"{self.backend}/api/rules")
            if r.status_code != 200:
                logger.warning(f"[BQBQ] 规则树返回 {r.status_code}，跳过膨胀")
                return {}
            d = r.json()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[BQBQ] 规则树获取失败，跳过膨胀: {e}")
            return {}

        groups = d.get("groups") or []
        keywords = d.get("keywords") or []
        hierarchy = d.get("hierarchy") or []

        enabled_groups = {g["group_id"] for g in groups if g.get("is_enabled", 1)}
        # 组 -> 该组的启用关键词
        group_kws: dict[int, list[str]] = {}
        # 关键词 -> 所属组
        kw_groups: dict[str, list[int]] = {}
        for k in keywords:
            if not k.get("is_enabled", 1):
                continue
            gid = k["group_id"]
            if gid not in enabled_groups:
                continue
            word = (k.get("keyword") or "").strip()
            if not word:
                continue
            group_kws.setdefault(gid, []).append(word)
            kw_groups.setdefault(word, []).append(gid)

        # 父子关系
        children: dict[int, list[int]] = {}
        for h in hierarchy:
            p, c = h.get("parent_id"), h.get("child_id")
            if p is None or c is None:
                continue
            children.setdefault(p, []).append(c)

        def collect_descendants(gid: int) -> set[int]:
            """收该组及其所有子孙组。"""
            seen = {gid}
            stack = [gid]
            while stack:
                cur = stack.pop()
                for child in children.get(cur, []):
                    if child not in seen:
                        seen.add(child)
                        stack.append(child)
            return seen

        # 预计算：每个组展开后的全部关键词
        result: dict[str, list[str]] = {}
        for word, gids in kw_groups.items():
            merged: set[str] = {word}
            for gid in gids:
                for sub in collect_descendants(gid):
                    merged.update(group_kws.get(sub, []))
            result[word] = sorted(merged)

        logger.info(f"[BQBQ] 膨胀映射已加载：{len(result)} 个关键词")
        return result

    async def _ensure_expand_map(self, client: httpx.AsyncClient) -> None:
        """惰性加载膨胀映射，超过 TTL 自动刷新。"""
        now = time.time()
        if self._expand_map is not None and (now - self._expand_ts) < self._expand_ttl:
            return
        self._expand_map = await self._load_expand_map(client)
        self._expand_ts = now

    def expand(self, word: str) -> list[str]:
        """返回该词膨胀后的同义词列表（含自身）。未命中或未加载则原样返回。"""
        if not self._expand_map:
            return [word]
        return self._expand_map.get(word, [word])


    def cache_results(self, key: str, items: list[dict]) -> None:
        """记住最近一次搜索结果，供后续「bqbq看 N」取用。"""
        self._cache[key] = (time.time(), items)
        # 顺手清掉过期项，避免无限增长
        now = time.time()
        for k in [k for k, (t, _) in self._cache.items() if now - t > self._cache_ttl]:
            self._cache.pop(k, None)

    def get_cached(self, key: str) -> list[dict]:
        """取回最近一次搜索结果；过期或无则返回空列表。"""
        v = self._cache.get(key)
        if not v:
            return []
        ts, items = v
        if time.time() - ts > self._cache_ttl:
            self._cache.pop(key, None)
            return []
        return items

    # ------------------------------------------------------------------
    # 网络
    # ------------------------------------------------------------------
    async def search(
        self,
        client: httpx.AsyncClient,
        include: list[str],
        exclude: list[str] | None = None,
        or_groups: list[list[str]] | None = None,
    ) -> tuple[list[dict], int]:
        """搜索图片，支持**组内 OR、组间 AND**。

        【为什么改用高级模式】
        早期用的是简化模式（`include_tags: [...]`），实测它的语义是
        **所有标签做 OR（并集）**，而不是 AND：

            soyo        12 张
            anon         9 张
            [soyo,anon] 19 张   <- 12+9-2，明显是并集

        后果是**多标签无法锁定单张**：想找"同时带 soyo 和 anon"的图
        （实际只有 2 张），插件会返回 19 张，用户没法精确挑。

        后端的高级模式（`keywords` 二维数组）语义是**组内 OR、组间 AND**：

            [[soyo]]           12 张
            [[soyo],[anon]]     2 张   <- 交集，这才是 AND
            [[soyo,anon]]      19 张   <- 组内 OR

        这也正是旧 nonebot 插件用的模式（`+标签` / `-标签` 语法），
        迁移时简化掉了，现在恢复。

        参数映射：
            用户输入          参数
            ─────────────────────────────────────────
            soyo anon         or_groups=[["soyo"],["anon"]]     两标签都要
            soyo+anon         or_groups=[["soyo","anon"]]       任一即可
            -trash            exclude=["trash"]

        Args:
            client: 复用的 httpx 客户端。
            include: 必须全部满足的标签（每个独立成组 -> AND）。
            exclude: 排除的标签。
            or_groups: 可选。直接指定分组，格式 [[同组标签...], ...]，
                组内 OR、组间 AND。给了它就忽略 include。

        Returns:
            (results, total)。results 每项含 md5 / filename / tags 等。
        """
        # 先确保膨胀映射已加载（与网页版行为一致，默认开启膨胀）
        await self._ensure_expand_map(client)

        # 组装 keywords（二维数组）：组内 OR、组间 AND
        #
        # 【关键：每个词都要膨胀】后端 keywords 模式不会自动膨胀，
        # 必须由调用方把同义词一起塞进同一个子数组里。
        # 例：soyo -> ["长崎素世","长崎爽世","长期素食","长期爽食","soyo"]
        #     不膨胀只搜到 12 张，膨胀后 13 张（与网页一致）。
        if or_groups is not None:
            kws = []
            for g in or_groups:
                merged: list[str] = []
                for t in g:
                    if t:
                        for w in self.expand(t):
                            if w not in merged:
                                merged.append(w)
                if merged:
                    kws.append(merged)
        else:
            kws = []
            for t in include:
                if not t:
                    continue
                merged: list[str] = []
                for w in self.expand(t):
                    if w not in merged:
                        merged.append(w)
                kws.append(merged)

        # 排除：trash_bin 永远排掉，其余按 AND 排除（同样要膨胀）
        ex = [t for t in (exclude or []) if t]
        if TRASH_TAG not in ex:
            ex.append(TRASH_TAG)

        body = {
            "keywords": kws,
            "excludes": [self.expand(t) for t in ex],
            "limit": SEARCH_FETCH_LIMIT,
            "offset": 0,
        }
        try:
            r = await client.post(f"{self.backend}/api/search", json=body)
            if r.status_code != 200:
                logger.warning(f"[BQBQ] 搜索返回 {r.status_code}: {r.text[:200]}")
                return [], 0
            d = r.json()
            return d.get("results") or [], int(d.get("total") or 0)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[BQBQ] 搜索失败: {e}")
            return [], 0

    async def check_md5_ex(
        self, client: httpx.AsyncClient, md5: str
    ) -> tuple[bool, str, list[str], bool]:
        """同 check_md5，但额外返回「标签是否可信」。

        【2026-10 后端已补接口】现在 check-md5 直接返回 tags，
        所以 tags_exact 恒为 True（只要图存在）。
        保留这个返回值是为了**不改调用方签名** —— 而且语义上仍值得区分：
        接口异常/超时时 exact=False，调用方就不该用空列表覆盖写。

        Returns:
            (是否存在, filename, tags, tags_exact)
        """
        exists, filename, tags = await self.check_md5(client, md5)
        return exists, filename, tags, bool(getattr(self, "_last_tags_exact", True))

    async def check_md5(
        self, client: httpx.AsyncClient, md5: str
    ) -> tuple[bool, str, list[str]]:
        """按 md5 查图是否存在，并取回标签。

        【2026-10 后端增强】此接口曾经只返回 exists/filename/time_refreshed，
        没有 tags，导致插件必须翻 30 页（3000 条）去"碰运气"找标签，
        而且旧图根本找不到（全库 44 万张，只有 183 张有标签）。

        已在后端补齐 —— `GET /api/images/check-md5/{md5}` 现在返回：
            {"exists":..., "filename":..., "time_refreshed":...,
             "tags":[...], "tag_count":N, "width":..., "height":...,
             "file_size":..., "is_trash":...}

        Returns:
            (是否存在, filename, tags)
        """
        md5 = (md5 or "").strip().lower()
        if not md5:
            self._last_tags_exact = False
            return False, "", []
        try:
            r = await client.get(f"{self.backend}/api/images/check-md5/{md5}")
            if r.status_code != 200:
                self._last_tags_exact = False
                logger.warning(f"[BQBQ] 查重返回 {r.status_code}")
                return False, "", []
            d = r.json()
            if not d.get("exists"):
                # 图不存在 —— 这是**确定的结论**，不是"查不到"
                self._last_tags_exact = True
                return False, "", []
            tags = d.get("tags")
            if isinstance(tags, list):
                self._last_tags_exact = True
                return True, d.get("filename", ""), [str(t) for t in tags if str(t)]
            # 后端异常（例如旧版本没这个字段）—— 标签不可信
            self._last_tags_exact = False
            logger.warning("[BQBQ] 响应里没有 tags 字段，后端可能未升级")
            return True, d.get("filename", ""), []
        except Exception as e:  # noqa: BLE001
            self._last_tags_exact = False
            logger.warning(f"[BQBQ] 查重失败: {e}")
            return False, "", []

    # ------------------------------------------------------------------
    # 【已删除】fetch_tags / fetch_tags_ex / fetch_tags_by_filename
    #
    # 这三方法原本用来"分页扫 /api/images 找标签"，因为旧版
    # check-md5 接口不返回 tags。它的致命缺陷：
    #   · 慢：约 72ms/页，最多扫 30 页 ≈ 2.2 秒/次
    #   · 不可靠：全库 44 万张，只扫前 3000 条，旧图**永远找不到**
    #     （实测 id=1 那张确实有 "窒息 猫" 标签，但扫不到）
    #   · 有数据风险：返回空列表无法区分"真没标签"和"没扫到"，
    #     曾因此加标签时把原有标签覆盖清空
    #
    # 2026-10 已在后端补齐 tags 字段（check-md5 直接返回），
    # 这三方法随之删除。相关历史记录保留在本文件与 README 中。
    # ------------------------------------------------------------------

    async def upload(
        self, client: httpx.AsyncClient, data: bytes, filename: str = "auto.gif"
    ) -> dict:
        """上传图片，返回后端响应。

        【为什么要传真实扩展名】后端已改成按**文件头**判定格式，
        文件名只是兜底，所以这里传什么名字都不影响正确性。
        但仍按旧插件的习惯传 auto.gif —— 保持行为一致，便于对照日志。

        Returns:
            后端 JSON：{"success": bool, "exists": bool, "md5": str, ...}
        """
        try:
            r = await client.post(
                self._with_cid(f"{self.backend}/api/images/upload"),
                files={"file": (filename, data, "image/gif")},
            )
            if r.status_code != 200:
                return {"success": False, "exists": False, "msg": f"HTTP {r.status_code}"}
            return r.json()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[BQBQ] 上传失败: {e}")
            return {"success": False, "exists": False, "msg": str(e)}

    async def update_tags(
        self, client: httpx.AsyncClient, md5: str, tags: list[str]
    ) -> tuple[bool, str]:
        """更新标签。

        【接口变化】旧版 `POST /api/save_tags` 用 filename 定位；
        新版 `POST /api/update_tags` 用 **md5** 定位。

        Returns:
            (是否成功, 说明)
        """
        try:
            r = await client.post(
                f"{self.backend}/api/update_tags",
                json=self._body({"md5": md5, "tags": tags}),
            )
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            d = r.json()
            ok = bool(d.get("success", d.get("exists", True)))
            return ok, str(d.get("message") or d.get("msg") or "")
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    async def fetch_image(
        self, client: httpx.AsyncClient, filename: str, thumb: bool = False
    ) -> bytes | None:
        """按 filename 取图字节。

        【字段变化】旧接口直接给 `url`，新接口给 `filename`（分片相对路径），
        需要自己拼 `{backend}/images/{filename}`。

        【缩略图】拼图预览时用缩略图能大幅省流量：
        实测同一张图 原图 402KB / 缩略图 65KB，小 6 倍。
        缩略图接口是 `/thumbnails/{filename}`，后端会在缺失时就地生成。
        但**最终发给用户的应该是原图**（缩略图只有 600px，看清标签文字费劲）。

        Args:
            client: 复用的 httpx 客户端。
            filename: 分片相对路径。
            thumb: True 取缩略图（用于拼图预览），False 取原图。

        Returns:
            图片字节；失败返回 None。
        """
        if not filename:
            return None
        rel = filename.replace("\\", "/").lstrip("/")
        kind = "thumbnails" if thumb else "images"
        url = f"{self.backend}/{kind}/{rel}"
        try:
            r = await client.get(url, timeout=30)
            if r.status_code == 200 and r.content:
                return r.content
            # 缩略图生成失败时退回原图，保证功能可用
            if thumb:
                logger.debug(f"[BQBQ] 缩略图不可用，回退原图: {rel}")
                return await self.fetch_image(client, filename, thumb=False)
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[BQBQ] 取图失败 {filename}: {e}")
            return None

    # ------------------------------------------------------------------
    # 拼接长图
    # ------------------------------------------------------------------
    @staticmethod
    def stitch(
        images: list[tuple[bytes, str]],
        header: str = "",
        footer: str = "",
    ) -> bytes | None:
        """把多张图拼成网格预览图，**顶部和底部可写提示文字**。

        【为什么把文字画进图里】早期提示语是单独一条文字消息，
        用户要往上翻才能看到"怎么看原图"。画进图里之后，
        图片本身就是完整的交互说明，转发给别人也带着说明。

        【为什么是 8 列】用户要求"尽量全发出来，最多 8x4"。
        8 列时每格宽约 105px，缩略图缩到这个尺寸仍能看清轮廓，
        足够"挑图"用；选出编号后再发原图看细节。

        【高度约束】QQ 按手机宽度自适应显示，所以关键是控制**高宽比**。
        竖屏手机常见 19.5:9 ≈ 2.17，这里上限设为 MAX_ASPECT(2.0)，
        超了就减少行数——宁可少显示几张，也不要用户滑半天。

        【代价】动图只显示第一帧（拼图后是静态的）。

        Args:
            images: [(图片字节, 标签文字), ...]
            header: 顶部提示（如"找到 13 张"），空则不画
            footer: 底部提示（如"获取原图：1 2 3"），空则不画

        Returns:
            JPEG 字节；全部失败时返回 None。
            （原本是 PNG，为省流量改成了 JPEG，见下方保存处的说明）
        """
        from PIL import Image, ImageDraw

        if not images:
            return None

        cols = max(1, min(GRID_COLS, len(images)))
        max_cells = cols * MAX_ROWS
        images = images[:max_cells]

        cell_w = (CANVAS_W - PAD * 2 - GAP * (cols - 1)) // cols

        # 字号随列数自适应：8 列时格子窄，字要小一点才放得下
        label_size = 17 if cols >= 6 else 19
        font = _load_font(label_size)
        head_font = _load_font(22)
        tail_font = _load_font(20)

        HEADER_H = 34 if header else 0
        FOOTER_H = 32 if footer else 0

        # ---- 逐格解析并缩放 ----
        cells: list[tuple[object, str]] = []
        for raw, label in images:
            try:
                im = Image.open(io.BytesIO(raw))
                im.load()
                if getattr(im, "is_animated", False):
                    im.seek(0)          # 动图取第一帧
                im = im.convert("RGBA")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[BQBQ] 跳过无法解析的图: {e}")
                continue
            # 等比缩放到格子内（contain，不裁切、不变形）
            im.thumbnail((cell_w, CELL_H), Image.LANCZOS)
            cells.append((im, label or ""))

        if not cells:
            return None

        # ---- 计算行数，必要时削减以满足高宽比 ----
        rows = (len(cells) + cols - 1) // cols
        while rows > 1:
            h = (HEADER_H + FOOTER_H + PAD * 2
                 + rows * (CELL_H + LABEL_H) + (rows - 1) * GAP)
            if h / CANVAS_W <= MAX_ASPECT:
                break
            rows -= 1
        shown = min(len(cells), rows * cols)
        cells = cells[:shown]

        total_h = (HEADER_H + FOOTER_H + PAD * 2
                   + rows * (CELL_H + LABEL_H) + (rows - 1) * GAP)
        canvas = Image.new("RGB", (CANVAS_W, total_h), BG_COLOR)
        draw = ImageDraw.Draw(canvas)

        # ---- 顶部提示 ----
        if header:
            draw.rectangle([0, 0, CANVAS_W, HEADER_H - 1], fill=(238, 242, 248))
            draw.text((PAD, 6), header, fill=(30, 60, 110), font=head_font)

        # ---- 网格 ----
        y_base = HEADER_H + PAD
        for idx, (im, label) in enumerate(cells):
            r, c = divmod(idx, cols)
            x0 = PAD + c * (cell_w + GAP)
            y0 = y_base + r * (CELL_H + LABEL_H + GAP)

            draw.rectangle([x0, y0, x0 + cell_w - 1, y0 + CELL_H - 1],
                           fill=CELL_BG, outline=BORDER_COLOR)

            px = x0 + (cell_w - im.width) // 2
            py = y0 + (CELL_H - im.height) // 2
            if im.mode == "RGBA":
                canvas.paste(im, (px, py), im)
            else:
                canvas.paste(im, (px, py))

            # ---- 标签：编号大 + 标签小 ----
            #
            # 【为什么分两段画】8 列时每格仅约 105px 宽，
            # "1.无标签" 这种会被截断成"1.无标…"，编号反而看不清。
            # 拆开后编号用大字号（用户主要靠它操作），
            # 标签用小字灰色接在后面，放不下就省略。
            if label:
                ty = y0 + CELL_H + 2
                # 动图标记：调用方在标签最前面塞一个 "\x00gif"，
                # 这里识别并去掉，同时把编号画成橙色。
                #
                # 【为什么用标记而不是让 stitch 自己判断】stitch 只收到
                # (图片字节, 标签文字)，拿不到 filename，无法区分静图/动图。
                is_gif = label.startswith("\x00gif")
                if is_gif:
                    label = label[len("\x00gif"):]
                num, _, tag = label.partition(".")
                # 动图编号用橙色，静图用默认深灰 —— 4 列格子小，
                # 靠颜色区分比在后面加 ".gif" 更省地方也更醒目。
                num_color = GIF_COLOR if is_gif else NUM_COLOR
                try:
                    draw.text((x0 + 2, ty), num, fill=num_color, font=font)
                    if tag:
                        num_w = draw.textlength(num, font=font)
                        small = _load_font(max(11, label_size - 5))
                        avail = cell_w - num_w - 5
                        t = tag
                        while t and draw.textlength(t, font=small) > avail:
                            t = t[:-1]
                        if t != tag and len(t) > 1:
                            t = t[:-1] + "…"
                        if t:
                            draw.text((x0 + 2 + num_w + 3, ty + 3), t,
                                      fill=(115, 115, 115), font=small)
                except Exception:
                    draw.text((x0 + 2, ty), label, fill=num_color, font=font)

        # ---- 底部提示 ----
        if footer:
            fy = total_h - FOOTER_H
            draw.rectangle([0, fy, CANVAS_W, total_h - 1], fill=(238, 242, 248))
            text = footer
            try:
                while text and draw.textlength(text, font=tail_font) > CANVAS_W - PAD * 2:
                    text = text[:-1]
                if text != footer and text:
                    text = text[:-1] + "…" if len(text) > 1 else "…"
            except Exception:
                pass
            draw.text((PAD, fy + 5), text, fill=(30, 60, 110), font=tail_font)

        # ---- 输出 JPEG 而不是 PNG ----
        #
        # 【为什么】预览图内容本身就是"缩略图拼盘"，PNG 的无损压缩毫无收益：
        # 实测 4x8 满格（800x1645）
        #     PNG        1161 KB
        #     JPEG q82    247 KB   <- 省 79%
        # 而画质差别在"看个大概挑图"的场景下看不出来。
        #
        # 代价：文字边缘会有轻微 JPEG 压缩痕迹，不影响可读性。
        buf = io.BytesIO()
        canvas.convert("RGB").save(
            buf, format="JPEG", quality=JPEG_QUALITY, optimize=True
        )
        return buf.getvalue()
