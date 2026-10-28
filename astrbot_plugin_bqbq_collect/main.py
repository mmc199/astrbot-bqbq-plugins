"""BQBQ 表情包收集（AstrBot 版）

功能
----
1. **单张图片**：用户 @机器人 或私聊发图并带触发词，自动转存
2. **合并转发**：解析「聊天记录」里嵌套的所有图片，批量转存
3. **引用回复**：引用一张图片 / 一条合并转发后再发触发词，收集被引用的内容
4. 提供 `表情统计` 命令查看进度

⚠️ 私聊与群聊结构完全不同（改代码前必看）
--------------------------------------------
这是最容易踩坑的地方 —— 两者不只是"有没有群号"，组件构成、
文本内容、raw_data 字段都不一样。实测数据：

  字段             私聊                    群聊(@机器人/引用)
  ─────────────────────────────────────────────────────────
  get_group_id()   ''（空）                群 openid
  raw_message_type 0                       103（引用消息）
  components       [At, Plain, Image]      [Reply, At, Plain]
  图片在哪          顶层 Image               Reply.chain 里
  message_str      '' 或 '[表情]'           'bqbq'（用户打的字）
  raw_data 键       含 attachments          含 group_id/msg_elements

两个直接后果：

  ① 私聊消息【没有文字】-> 不能用触发词把关，否则永远收不到
  ② 群聊的图片藏在 Reply.chain -> 只过滤顶层 Image 会漏掉

所以：
  · 私聊 -> 发图即收（用户主动发图 = 明确意图）
  · 群聊 -> 必须带触发词 bqbq（@机器人 可能只是顺便说话）
  · 取图要同时覆盖顶层 Image 和 Reply.chain 里的 Image
    （两者机制相同，框架对二者都做了本地化，可共用一套处理逻辑）

【触发词：2026-10 简化为裸 bqbq】
     bqbq            收集（本插件）
     bqbq <关键词>    搜索（commands.py）

早期版本用 "bqbq收图"，现已移除。原注释担心裸词误触发
（'今天不收图了'、'我帮你收图片'），但现在要求完整出现 "bqbq" 前缀，
且官方机器人只收到私聊/@机器人 的消息，误触发面很小。

⚠️ 私聊白名单：只能用 openid，不能用 QQ 号
------------------------------------------------
官方机器人拿不到 QQ 号 —— `event.get_sender_id()` 返回的是 openid
（形如 <32位openid>）。

而且 openid **无法反推 QQ 号**（已实测 6 种方法：protobuf / md5 / sha1 /
base64 / 字节变换 / 时间戳，全部失败）。官方文档也明确：
  「不同 bot(AppID) 获取到的用户 openid 均不相同」
说明它是 f(QQ号, AppID密钥)，密钥在腾讯服务端。

所以白名单只能填 openid。配置项：
    private_whitelist_enabled  启用开关（默认关）
    private_whitelist          openid 列表，逗号或换行分隔

⚠️ 若更换 bot（AppID 变了），openid 会变，需要重新填写。

【怎么拿到自己的 openid】用绑定命令，不要翻日志：

    bqbq绑定      -> 机器人直接回复你的 openid，转给管理员即可
    bqbq白名单    -> 管理员查看当前名单（脱敏显示）

openid 只对当前这一个 bot 有效、不是 QQ 号，所以回复给请求者是安全的。

⚠️ 前提：QQ 官方机器人当前收不到群里的所有消息
------------------------------------------------
**这不影响插件逻辑**（收到消息就处理），但决定了很多设计取舍，
所以必须知道。

### 实际能收到的范围

  ✅ 私聊消息
  ✅ @了机器人的群消息
  ❌ 群里其他人之间发的消息

### ⚠️ 原因不是"技术不支持"，而是"权限未开放"

技术上 API 是存在的（官方文档明确写了两个事件）：

  GROUP_AT_MESSAGE_CREATE  -> 只收 @机器人 的消息（默认）
  GROUP_MESSAGE_CREATE     -> 全量模式，收群内每一条消息

官方文档原文：
  「当机器人开启了"接收所有消息"功能后，群里的每一条消息
    （不限于@机器人）都会推送此事件。」

但官方 FAQ 说明该能力**处于灰度**：
  「公开全量使用：当前该能力正在对【企业开发者灰度】，
    后续会推全到个人认证，请耐心等待。」

且还需**群主**在手机 QQ 的群机器人设置里允许"接收群内全部消息"
（入口在群主侧，不是开发者后台的配置项）。

**结论**：个人主体当前拿不到全量模式，所以官方机器人实际只能收到
私聊和 @机器人的消息。若将来权限开放，插件无需改动，只是能收到的
消息范围变宽。

### 带来的设计影响

  · 「自动收集群里所有表情包」当前做不到，只能靠用户主动 @ 触发
  · 单次消息里图不会太多，skip 也不会像想象中那样频繁出现
  · 机器人不需要担心"群消息量太大"，压力主要来自单条消息里的图数量

对照：非官方协议（如 OneBot / aiocqhttp）能收到群内全部消息，
所以旧的 `bqbq` 插件可以「默默收集」，而官方机器人必须靠用户主动触发。

与 bqbq_v2 后端的关系
---------------------
后端 FastAPI 跑在 127.0.0.1:8000，提供两个接口：

    GET  /api/images/check-md5/{md5}   -> {"exists": true/false}  预查重
    POST /api/images/upload            -> 上传原图，后端负责：
                                          · 按 md5 分片存储
                                          · 生成缩略图
                                          · 写入数据库
                                          · 异步同步到 R2（CDN）

所以插件**不需要**自己算分片路径或写数据库，只管下载 + POST。

为什么要解析文本
----------------
AstrBot 的 qqofficial 适配器**不解析合并转发**（源码里搜不到 forward/node 处理）。
实测确认，合并转发的全部信息是一个纯文本 content 字段，格式为：

    [Parachutes的聊天记录]
    === 消息 1 ===
    [消息类型] 合并转发消息
    --- 第1条 ---
        [发送者] Parachutes
        [附件1] 类型:动图 文件名:83F73AFE....gif 尺寸:500x500 大小:1.8MB URL:https://...

    raw_data.message_scene.ext = ["msg_idx=REFIDX_..."]   # 无 URL
    raw_data.msg_elements      = []                        # 空
    components                 = [At, Plain]               # 无 Node/Image

所以只能正则解析文本。好在这正是 QQ 客户端渲染的稳定格式，且：
  · 文件名就是 MD5（32 位十六进制）-> 可直接去重，无需下载后再算
  · URL 自带 rkey，有时效，需要尽快下载

⚠️ 修改本文件前必读的四个坑
---------------------------
都是本项目**实际踩过并复现**的，详见 `bqbq_v2/ASTRBOT_PLUGIN.md`：

1. **`should_call_llm()` 参数是反的**
   消费端判断是 `not event.call_llm`（stage.py:59），
   所以要【禁止】LLM 必须传 `True`，传 `False` 完全无效。

2. **框架会把 Image 组件的字段覆盖成本地路径**
   `preprocess_stage` 把图片下到 `data/temp/`，然后把
   `url`/`file`/`path` 全改成该路径。
   所以【普通消息应直接读本地文件】，拿本地路径去 httpx 会报
   `UnsupportedProtocol`。但【合并转发不会被本地化】（图片在 Plain 文本里）。

3. **解析逐行文本绝不能开 `re.DOTALL`**
   否则 `.*?` 跨行吞掉下一条，把下一条的 URL 配到本条目的 md5 上 —— 污染数据库。
   必须用 `[^\n]*?`。

4. **`_last_report` 每个分支都要赋值**
   漏掉则 `total==0`，表现是"永远不回复"。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools

# 图片库命令（搜索 / 看标签 / 看图 / 加标签 / 删除）
#
# 【从旧 nonebot 插件迁移】原文件：
#     C:\000soft\nonebot2-2.4.4\nonebot\plugins\bqbq\__init__ (3).py
#
# 接口从旧 Flask 后端（5000）换到了 bqbq_v2（8000），
# 展示方式从「合并转发卡片」换成了「列表 + 拼图」——
# 因为 qqofficial 适配器一条消息只能带一张图，且会静默忽略 Nodes 组件。
# 详见 imagelib.py / commands.py 顶部的完整说明。
from .commands import LibCommands, is_lib_command, session_key  # noqa: E402
from .imagelib import ImageLibClient  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 后端地址（可在 WebUI 配置里覆盖）
DEFAULT_BACKEND = "http://127.0.0.1:8000"

# 框架存放临时媒体的目录。preprocess_stage 会把【普通消息】的图片
# 下载到 data/temp/media_image_*.jpg，并覆盖组件的 url/file/path。
MEDIA_TEMP_DIR = Path(
    r"C:\000soft\AstrBotLauncher-0.2.0\AstrBot\data\temp"
)

# 上一条消息里"已存在"的图，最多展示多少个的标签。
# 超出就只报数量 —— 合并转发可能一次带 200 张，全列出来会刷屏。
SKIP_TAGS_MAX_SHOW = 5

# 在 temp 目录里按 md5 找缓存文件时，最多扫描多少个最近文件。
# 框架文件名不含 md5，只能算内容匹配，所以要限制扫描量。
CACHE_SCAN_LIMIT = 60

# ---------------------------------------------------------------------------
# 触发词
# ---------------------------------------------------------------------------
# 【2026-10 简化】触发词统一为裸 "bqbq"。
#
#     bqbq            收集（本插件，无参数）
#     bqbq <关键词>    搜索（commands.py，带参数）
#
# 原来的 "bqbq收图" 已移除 —— 用户要求只保留 bqbq。
# 原注释担心裸词误触发（"今天不收图了"），但：
#   · 现在要求完整出现 "bqbq" 前缀，不是随便一个"收图"
#   · 官方机器人只收到私聊/@机器人 的消息，消息量本身很少
#   · 私聊是"发图即收"，不看文字
# 所以误触发面很小。
#
# 【为什么不能只靠常量】裸词必须匹配**整条消息**，
# 不能是"bqbq 猫"这种带参数的（那要交给搜索）。
# 所以判定逻辑写在 _has_trigger 里，这里只留显式词表。
TRIGGER_KEYWORDS: tuple[str, ...] = ()

# 裸触发词：整条消息（去掉前缀后）等于这些值之一
BARE_TRIGGERS = ("bqbq", "/bqbq")

# 仿 QQ 客户端的 UA：某些 CDN 会校验，用它更稳
HTTP_HEADERS = {
    "User-Agent": "QQ/9.1.95.29869 CFNetwork/1568.100.1 Darwin/24.0.0",
}

# 公共 rkey 接口（与 onebot 版 bqbq 插件用的是同一个）
#
# 【实测结论 2026-xx】这些公共接口目前已全部不可用：
#   · llob.linyuchen.net/rkey   -> DNS/连接失败 (ConnectError)
#   · api.qqsuu.cn/api/dm-rkey  -> HTTP 200 但内容为"接口不存在"
#   · api.oioweb.cn/api/qq/rkey -> 自签证书, SSL 校验失败
# 因此 rkey 刷新阶段实际上【永远失败】, 必须依赖下面的构造 URL 兜底。
# 保留该常量是为了将来接口恢复时能自动生效, 且失败后只冷却 5 分钟不拖慢批量。
RKEY_API = "https://llob.linyuchen.net/rkey"

# rkey 不可用时的冷却时间(秒)。公共接口已全挂, 缩短冷却避免每条消息都白等一次连接超时
RKEY_COOLDOWN = 300
# rkey 接口自身的超时(秒)。默认 60s 会让每条消息都卡住, 这里单独收紧
RKEY_TIMEOUT = 8

# 构造 gchat.qpic.cn URL 用的公共查询串
# 实测这套构造命中率约 74%；取不到的通常是 QQ 已清理的旧图
CONSTRUCT_QUERY = (
    "?term=255&is_origin=0&client_proto=qq&client_appid=537325647"
    "&client_type=android&client_ver=9.1.95&client_down_type=auto&client_aio_type=aio"
)

# 图片 magic bytes，防止拿到错误页/空响应
_IMAGE_MAGIC = (b"GIF8", b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"BM")

# 从合并转发文本里提取：文件名:xxx.gif  尺寸:500x500  大小:1.8MB URL:https://...
#
# 【为什么不能加 re.DOTALL】
# 中间的 `[^\n]*?` 必须限定在【同一行内】。若让它跨行匹配（DOTALL 或 .），
# 当某条附件没有 URL 时（如"类型:表情"这类），正则会一路吞到【下一条】，
# 把下一条的 URL 配到本条目的 md5 上 —— 造成 md5 与图片内容不符。
# 实测复现：第1条无URL + 第2条有URL -> 错误地把 第1条的md5 配给 第2条的URL。
#
# 用 [^\n]*? 而不是 .*?，从根上杜绝跨行。
_RE_ATTACH = re.compile(
    r"文件名:(?P<name>[0-9A-Fa-f]{32})\.(?P<ext>[A-Za-z0-9]+)"
    r"[^\n]*?"
    r"URL:(?P<url>https?://\S+)"
)
# 只要 32 位十六进制文件名，即可确认是图片（QQ 用 md5 命名）

# 限制单条消息最多处理多少个附件，防止超大转发把任务卡死
MAX_ITEMS_PER_MESSAGE = 200
# 下载并发数（对 QQ CDN 别太激进）
DOWNLOAD_CONCURRENCY = 4
# 下载超时（秒）
DOWNLOAD_TIMEOUT = 60
# 上传超时（秒）
UPLOAD_TIMEOUT = 60


class BqbqCollectPlugin(Star):
    """表情包收集插件：普通消息 + 合并转发。"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        cfg = config or {}
        self.backend = str(cfg.get("backend_url") or DEFAULT_BACKEND).rstrip("/")
        # bot 自身的凭证，供上传、改标签、购物车取回等需要鉴权的调用使用。
        #
        # 【不写默认值】这是真实可用的凭证，仓库是公开的不能硬编码。
        # 必须在 config 里填 bot_client_id，并在后端
        # auth_granted_clients 表里登记同一条（kind='bot'）。
        #
        # 【为什么要在启动时就吼一声】留空的话不会立刻出错，
        # 而是每次上传/改标签都被后端以 401「未登录：缺少 client_id」
        # 拒绝 —— 从日志里完全看不出是"配置忘了填"。
        # 这里在加载时就打印明确提示，省得以后排查半天。
        self.bot_client_id = str(cfg.get("bot_client_id") or "").strip()
        if not self.bot_client_id:
            logger.warning(
                "[BQBQ] 未配置 bot_client_id！上传图片、修改标签、购物车取回"
                "都会因缺少凭证被后端拒绝（HTTP 401 未登录：缺少 client_id）。\n"
                "        请在本插件配置里填写 bot_client_id，并在后端 "
                "auth_granted_clients 表登记同一条（kind='bot'）。"
            )
        self.enabled = bool(cfg.get("enabled", True))
        self.save_merged = bool(cfg.get("collect_merged", True))
        self.save_normal = bool(cfg.get("collect_normal", True))
        # 引用回复里的图片/合并转发（Reply.chain）
        self.save_reply = bool(cfg.get("collect_reply", True))
        # 回复行为：只要处理过图片就回结果（含"已存在"），关掉则完全静默
        self.reply_result = bool(cfg.get("reply_result", True))

        # ---- 私聊白名单 ----
        # 官方机器人拿不到 QQ 号，`event.get_sender_id()` 给的是 openid，
        # 所以白名单填 openid（形如 <32位openid>）。
        # 未启用时私聊对所有人开放（默认）。
        self.private_whitelist_enabled = bool(
            cfg.get("private_whitelist_enabled", False)
        )
        raw_wl = cfg.get("private_whitelist") or []
        if isinstance(raw_wl, str):
            # 兼容用户在 WebUI 里用逗号/换行分隔填一串
            raw_wl = [x.strip() for x in raw_wl.replace(",", "\n").split("\n")]
        # 统一大写去空格，方便比对（openid 是十六进制，大小写不敏感）
        self.private_whitelist = {
            str(x).strip().upper() for x in raw_wl if str(x).strip()
        }

        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_bqbq_collect"))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 统计
        self.stats = {
            "total": 0,        # 发现的图片总数
            "skip": 0,         # MD5 已存在，跳过
            "new": 0,          # 新增成功
            "dup": 0,          # 上传后发现重复
            "fail": 0,         # 两阶段都失败
            "no_url": 0,       # 既无本地文件也无 http 地址（提前跳过）
            "local_ok": 0,     # 从框架已下载的本地文件读取成功
            "rkey_ok": 0,      # 阶段1（原链接 + 有效 rkey）成功
            "direct_ok": 0,    # 原链接直连成功（无 rkey 可用时）
            "construct_ok": 0, # 阶段2（构造 URL）成功
        }

        # rkey 缓存（取不到时静默降级到构造 URL）
        self._rkey = {"value": None, "expire": 0.0, "cooldown_until": 0.0}

        # 失败原因计数，便于诊断到底卡在哪一环
        self._fail_reasons: dict[str, int] = {}

        # 上一条消息的处理结果差值（用于 reply 汇报）
        self._last_report: dict[str, int] = {}

        # 上一条消息里"已存在"的图的标签，格式 [(md5, [tags]), ...]
        #
        # 【为什么单独存】统计里只有 skip 计数，但用户更关心
        # "我发的这张图原本是什么标签"。_check_exists 顺手把它记下来，
        # _format_report 汇总输出。每次处理新消息时清空。
        self._skip_tags: list[tuple[str, list[str]]] = []

        # 串行化上传，避免并发过高把后端/隧道打满
        self._lock = asyncio.Lock()

        # ---- 图片库命令（迁移自旧 nonebot 插件）----
        # 搜索 / 看标签 / 看图 / 加标签 / 删除
        self.imagelib = ImageLibClient(self.backend, self.bot_client_id)
        self.lib_cmds = LibCommands(self.imagelib, reply_result=self.reply_result)
        # 管理/个人类子命令（bqbq 统计 / bqbq 帮助 / bqbq 登录 ...）
        # 需要读写插件自身状态，由本类实现、注入给 LibCommands 分发。
        self.lib_cmds.on_reserved = self._handle_reserved

        logger.info(
            f"[BQBQ] 收集插件已加载 backend={self.backend} "
            f"普通消息={self.save_normal} 合并转发={self.save_merged}"
        )

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def _stats_str(self) -> str:
        """统计摘要。

        【为什么不再显示「下载来源」】那一行（rkey/直连/构造URL）
        是排查 QQ 图片下载失败时加的临时诊断，日常对用户没有意义。
        下载失败时下面的「失败原因」已经给出具体原因，够用了。

        【为什么不再显示后端地址】同上，属于调试信息；
        bot 连哪个后端写在配置里，不需要每次统计都念一遍。
        """
        s = self.stats
        extra = ""
        if s.get("no_url"):
            extra = f" 无地址:{s['no_url']}"
        return (
            f"总数:{s['total']} 跳过:{s['skip']} "
            f"新增:{s['new']} 重复:{s['dup']} 失败:{s['fail']}{extra}"
            + (
                "\n失败原因: "
                + " ".join(f"{k}={v}" for k, v in sorted(self._fail_reasons.items()))
                if self._fail_reasons
                else ""
            )
        )

    # ------------------------------------------------------------------
    # 网络
    # ------------------------------------------------------------------
    async def _check_exists(
        self, client: httpx.AsyncClient, md5: str
    ) -> tuple[bool, list[str]]:
        """向后端预查重，并取回该图已有的标签。

        【2026-10 增强】后端给 check-md5 补上了 tags 字段
        （原接口只有 exists/filename/time_refreshed）。
        这样"图已存在"时能顺手把标签告诉用户，
        用户不用再引用一次图发 bqbq 去查。

        同时把标签累积到 `self._skip_tags`，供 _format_report 汇总输出。

        Args:
            client: 复用的 httpx 客户端。
            md5: 图片 md5（小写）。

        Returns:
            (是否存在, 标签列表)。不存在或请求失败时标签为空列表。
        """
        if not md5:
            return False, []
        try:
            r = await client.get(f"{self.backend}/api/images/check-md5/{md5}")
            if r.status_code != 200:
                return False, []
            d = r.json()
            if not d.get("exists"):
                return False, []
            # tags 可能是新接口的数组，也可能是老后端的缺失字段
            raw = d.get("tags")
            tags = [str(t) for t in raw if str(t)] if isinstance(raw, list) else []
            # 累积起来，回复时一并展示（只留前若干个，避免刷屏）
            if tags and len(self._skip_tags) < SKIP_TAGS_MAX_SHOW:
                self._skip_tags.append((md5, tags))
            return True, tags
        except Exception:
            return False, []

    # ------------------------------------------------------------------
    # 下载（两阶段：rkey -> 构造 URL）
    # ------------------------------------------------------------------
    async def _fetch_rkey(self, client: httpx.AsyncClient, force: bool = False) -> str | None:
        """从公共接口获取 group rkey（带缓存与失败冷却）。

        注意：公共 rkey 接口随时可能失效（实测曾出现连接被强制关闭）。
        取不到时返回 None，调用方应直接走构造 URL。

        Args:
            client: 复用的 httpx 客户端。
            force: 忽略缓存强制刷新。

        Returns:
            rkey 字符串；不可用返回 None。
        """
        now = time.time()
        # 命中缓存
        if not force and self._rkey["value"] and now < self._rkey["expire"]:
            return self._rkey["value"]
        # 处于失败冷却期，直接放弃，避免每个附件都去撞墙
        if now < self._rkey["cooldown_until"]:
            return None

        try:
            # 单独收紧超时：主 client 的超时是 60s，若 rkey 接口 DNS 挂了，
            # 每条消息都会白等 60s，合并转发场景会非常明显。
            r = await client.get(
                RKEY_API, headers=HTTP_HEADERS, timeout=RKEY_TIMEOUT
            )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            j = r.json()
            val = j.get("group_rkey") or j.get("rkey")
            if not val:
                raise RuntimeError("响应里没有 group_rkey")
            self._rkey["value"] = str(val)
            # 保守按 30 分钟缓存（接口通常给更长的有效期）
            self._rkey["expire"] = now + 1800
            logger.info("[BQBQ] 已获取 rkey")
            return self._rkey["value"]
        except Exception as e:  # noqa: BLE001
            # 失败后冷却 5 分钟，避免反复重试拖慢批量处理
            self._rkey["cooldown_until"] = now + 300
            logger.debug(f"[BQBQ] rkey 获取失败（将走构造 URL）: {e}")
            return None

    @staticmethod
    def _replace_rkey(url: str, rkey: str) -> str:
        """把 URL 里的 rkey 参数替换掉。

        Args:
            url: 原始 URL。
            rkey: 新的 rkey。

        Returns:
            替换后的 URL。
        """
        return re.sub(r"([?&])rkey=[^&]*", lambda m: f"{m.group(1)}rkey={rkey}", url)

    @staticmethod
    def _extract_http_url(comp) -> str:
        """从 Image 组件里取出可用的 http(s) 地址。

        【重要：普通消息通常取不到 http 地址 —— 这是正常的】
        实测（dump 原始 payload 对比）发现框架的处理流程是：

          1. 适配器 `_append_attachments` 创建 Image 组件：
                 url = ""  file = "https://multimedia.nt.qq.com.cn/download?..."

          2. **框架的 preprocess_stage 会把图片下载到本地**，
             然后把三个字段全部覆盖成本地路径：
                 component.file = <本地jpg>
                 component.path = <本地jpg>
                 component.url  = <本地jpg>       # 原始 URL 就此丢失

        所以到插件手里时，url/file/path 都是形如
        `...\\data\\temp\\media_image_2026...jpg` 的本地路径，
        **根本不存在 http 地址**。

        结论：普通消息应当走 `_extract_local_path()` 读本地文件，
        而不是拿本地路径去 httpx 请求（那会报 UnsupportedProtocol）。

        Args:
            comp: AstrBot 的 Image 组件。

        Returns:
            以 http:// 或 https:// 开头的地址；取不到返回空串。
        """
        for field in ("url", "file"):
            v = getattr(comp, field, None)
            if not isinstance(v, str):
                continue
            v = v.strip()
            if v.startswith("http://") or v.startswith("https://"):
                return v
        return ""

    @staticmethod
    def _extract_local_path(comp) -> str:
        """从 Image 组件里取出框架已下载好的本地文件路径。

        框架的 preprocess_stage 会把图片落到 `data/temp/media_image_*.jpg`，
        并覆盖组件的 url/file/path 三个字段，所以这里任取其一即可。

        Args:
            comp: AstrBot 的 Image 组件。

        Returns:
            存在的本地文件绝对路径；没有则返回空串。
        """
        for field in ("path", "file", "url"):
            v = getattr(comp, field, None)
            if not isinstance(v, str):
                continue
            v = v.strip()
            # 排除 http(s) 与 base64 这两种非本地形式
            if v.startswith(("http://", "https://", "base64://", "data:")):
                continue
            try:
                if v and Path(v).is_file():
                    return v
            except Exception:  # noqa: BLE001
                continue
        return ""

    @staticmethod
    def _guess_md5(url: str) -> str:
        """尽力从图片 URL 里找出 md5，供「构造 URL」兜底使用。

        【实测结论】QQ 普通消息的 URL 形如
            https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=<b64>&rkey=..
        其中 `fileid` 是 protobuf 编码的 **file_uuid**（形如 `12 14 <20B>`），
        它 **不是** md5。已用真实样本逐一比对 5/5 全部不含 md5 字节，
        因此【无法】从这类 URL 反推出 md5。

        也就是说：普通消息没有 md5 -> 构造 URL 兜底不可用 -> 只能靠原链接。
        而原链接的 rkey 有时效，公共 rkey 接口又已失效，所以普通消息的
        成功率天然低于合并转发（后者文本里直接带 md5）。

        这里只做保守的、有把握的提取：URL 里若显式出现 32 位十六进制才采纳，
        绝不猜测。

        Args:
            url: 图片地址。

        Returns:
            32 位小写 md5；无法确定时返回空串。
        """
        if not url:
            return ""
        m = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])", url)
        return m.group(1).lower() if m else ""

    def _construct_urls(self, md5: str) -> list[str]:
        """按 md5 构造 gchat.qpic.cn 候选 URL。

        实测这套构造方式命中率约 74%；取不到的基本是 QQ 服务器已清理的旧图，
        换任何参数都一样（已验证 6 种变体）。

        Args:
            md5: 32 位 md5。

        Returns:
            候选 URL 列表，按命中率排序。
        """
        m = md5.upper()
        return [
            f"https://gchat.qpic.cn/gchatpic_new/0/0-0-{m}/0/{m}.jpg{CONSTRUCT_QUERY}",
            f"https://gchat.qpic.cn/gchatpic_new/0/0-0-{m}/0/{m}.png{CONSTRUCT_QUERY}",
            f"https://gchat.qpic.cn/gchatpic_new/0/0-0-{m}/0/{m}.gif{CONSTRUCT_QUERY}",
        ]

    async def _download(
        self, client: httpx.AsyncClient, url: str, md5: str = ""
    ) -> tuple[bytes | None, str]:
        """两阶段下载：先 rkey，失败再构造 URL。

        Args:
            client: 复用的 httpx 客户端。
            url: 原图 URL（带 rkey，来自消息内容）。
            md5: 图片 md5，用于构造回退 URL。

        Returns:
            (图片字节或 None, 使用的方法名)。方法名为
            "rkey" / "direct" / "construct" / "fail:<原因>"。
        """
        last_reason = "unknown"

        # ---- 阶段 1：原链接 + 有效 rkey ----
        # 原链接自带 rkey，先替换成新取到的（旧的可能已过期），失败则原样试
        rkey = await self._fetch_rkey(client)
        if rkey:
            candidates = [self._replace_rkey(url, rkey), url]
        else:
            candidates = [url]

        for u in candidates:
            data, reason = await self._try_get(client, u)
            if data is not None:
                # 只有确实用上了 rkey 才算阶段 1 成功；
                # 否则说明命中的是原链接本身（可能已是构造 URL）
                if rkey:
                    self.stats["rkey_ok"] += 1
                    return data, "rkey"
                self.stats["direct_ok"] += 1
                return data, "direct"
            last_reason = reason

        # ---- 阶段 2：构造 gchat.qpic.cn URL ----
        if md5:
            for u in self._construct_urls(md5):
                data, reason = await self._try_get(client, u)
                if data is not None:
                    self.stats["construct_ok"] += 1
                    return data, "construct"
                last_reason = reason

        self._fail_reasons[last_reason] = self._fail_reasons.get(last_reason, 0) + 1
        return None, f"fail:{last_reason}"

    async def _try_get(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[bytes | None, str]:
        """GET 一个 URL 并校验确实是图片，同时给出失败原因。

        实测两种失败信号完全不同，必须区分（否则无法定位问题）：

          · 原链接 multimedia.nt.qq.com.cn
              404 + JSON {"retcode":-5503042,"retmsg":"file has expired"}
              -> rkey / 会话签名过期，换新 rkey 可能still能救

          · 构造 URL gchat.qpic.cn
              404 + 空 body + header "x-errno: -6101"
              -> 腾讯 NWS 存储层【没有这个对象】，
                 与该 URL 是否过期无关（该 URL 纯 MD5 构造，无时效参数）

        Args:
            client: 复用的 httpx 客户端。
            url: 目标地址。

        Returns:
            (图片字节或 None, 失败原因)。成功时原因为空字符串。
        """
        try:
            r = await client.get(url, headers=HTTP_HEADERS)
            if r.status_code == 200:
                data = r.content
                if data and data[:4].startswith(_IMAGE_MAGIC):
                    return data, ""
                return None, "not_image"
            # 存储层对象不存在（gchatpic）
            if r.headers.get("x-errno"):
                return None, f"x-errno={r.headers['x-errno']}"
            # 业务层过期（原链接）
            try:
                j = r.json()
                if isinstance(j, dict) and j.get("retmsg"):
                    return None, f"retmsg={j['retmsg']}"
            except Exception:
                pass
            return None, f"http_{r.status_code}"
        except Exception as e:  # noqa: BLE001
            return None, f"exc_{type(e).__name__}"

    async def _upload(self, client: httpx.AsyncClient, data: bytes) -> tuple[bool, bool]:
        """上传到后端。

        Args:
            client: 复用的 httpx 客户端。
            data: 图片字节。

        Returns:
            (是否成功, 是否新增)。新增为 False 表示后端判定重复。
        """
        # 【必须带 client_id】后端上传接口已加 require_edit 鉴权，
        # 不带会返回 401。这个函数是"收集流程"用的上传通道，
        # 与 imagelib.ImageLibClient.upload 是两条独立路径，
        # 之前只改了后者，漏了这里 —— 表现为收图一直"失败:1"
        # 而日志里看不出原因（401 被当成普通失败吞掉）。
        url = f"{self.backend}/api/images/upload"
        if self.bot_client_id:
            url = f"{url}?client_id={self.bot_client_id}"
        try:
            r = await client.post(
                url,
                files={"file": ("auto.gif", data, "image/gif")},
            )
            if r.status_code != 200:
                # 把状态码和响应体前 120 字打进日志，否则 401/413/500
                # 全都看起来一样，排查时完全靠猜。
                logger.warning(
                    f"[BQBQ] 上传失败 HTTP {r.status_code}: {r.text[:120]}"
                )
                return False, False
            return True, not bool(r.json().get("exists", False))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[BQBQ] 上传异常: {type(e).__name__}: {e}")
            return False, False

    async def _process_one(
        self, client: httpx.AsyncClient, md5: str, url: str
    ) -> None:
        """处理单张图片：预查重 -> 本地文件优先 -> 两阶段下载 -> 上传。

        Args:
            client: 复用的 httpx 客户端。
            md5: 图片 md5（小写）。
            url: 图片地址（来自消息内容，带 rkey）。
        """
        self.stats["total"] += 1

        # 1) 预查重：文件名就是 md5，命中就不用下载了
        #    （标签由 _check_exists 记入 self._skip_tags，回复时展示）
        if (await self._check_exists(client, md5))[0]:
            self.stats["skip"] += 1
            return

        # 2) 本地缓存优先
        # 框架的 preprocess_stage 会把【普通消息】的图片下载到 data/temp/，
        # 但【合并转发】的图片藏在 Plain 文本里，框架不会处理它们，
        # 所以这一步对合并转发通常不命中 —— 保留它是为了兜住
        # "图片恰好已被别处本地化" 的情况，避免重复走网络。
        cached = self._find_cached_local(md5)
        if cached:
            try:
                data = Path(cached).read_bytes()
                if data[:4].startswith(_IMAGE_MAGIC):
                    async with self._lock:
                        ok, is_new = await self._upload(client, data)
                    if ok:
                        self.stats["new" if is_new else "dup"] += 1
                        self.stats["local_ok"] += 1
                        logger.info(f"[BQBQ] ✅ {md5[:12]} 本地缓存 {len(data)}B")
                        return
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[BQBQ] 读取本地缓存失败 {cached}: {e}")

        # 3) 两阶段网络下载（rkey -> 构造 URL）
        data, method = await self._download(client, url, md5)
        if data is None:
            self.stats["fail"] += 1
            logger.info(f"[BQBQ] 下载失败 {md5[:12]} 原因={method}")
            return

        # 4) 上传（加锁，避免并发过高）
        async with self._lock:
            ok, is_new = await self._upload(client, data)
        if not ok:
            self.stats["fail"] += 1
            logger.info(f"[BQBQ] 上传失败 {md5}")
            return

        if is_new:
            self.stats["new"] += 1
        else:
            self.stats["dup"] += 1
        logger.info(f"[BQBQ] ✅ {md5[:12]} {method} {len(data)}B {'新增' if is_new else '重复'}")

    def _find_cached_local(self, md5: str) -> str:
        """在框架的 temp 目录里找有没有该 md5 对应的已下载文件。

        框架用 `media_image_<时间戳>_<随机>.jpg` 命名，**不含 md5**，
        所以这里按文件内容算 md5 来匹配 —— 只在 temp 目录不大时可行。
        为控制开销，最多扫最近 N 个文件。

        Args:
            md5: 目标图片的小写 md5。

        Returns:
            命中文件的绝对路径；没有则返回空串。
        """
        try:
            if not MEDIA_TEMP_DIR.is_dir():
                return ""
            files = sorted(
                MEDIA_TEMP_DIR.glob("media_image_*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:CACHE_SCAN_LIMIT]
            for p in files:
                try:
                    if not p.is_file():
                        continue
                    # 文件名的 md5 前缀是无意义的，只能算内容
                    if hashlib.md5(p.read_bytes()).hexdigest() == md5:
                        return str(p)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_forward_text(text: str) -> list[tuple[str, str]]:
        """从合并转发文本里提取 (md5, url) 列表。

        实测格式（QQ 客户端渲染，AstrBot 原样放进 content）：

            --- 第1条 ---
                [发送者] Parachutes
                [附件1] 类型:动图 文件名:83F73AFE....gif 尺寸:500x500 大小:1.8MB URL:https://...

        Args:
            text: 合并转发的文本内容。

        Returns:
            [(md5小写, url), ...]，已去重。
        """
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for m in _RE_ATTACH.finditer(text):
            md5 = m.group("name").lower()
            ext = m.group("ext").lower()
            # 只收图片扩展名；视频/文件跳过
            if ext not in ("gif", "jpg", "jpeg", "png", "webp", "bmp"):
                continue
            if md5 in seen:
                continue
            seen.add(md5)
            result.append((md5, m.group("url")))
            if len(result) >= MAX_ITEMS_PER_MESSAGE:
                break
        return result

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=50)
    async def on_message(self, event: AstrMessageEvent):
        """收到消息时尝试收集表情包。

        【如何禁止默认 LLM 请求】
        这群里绝大多数消息就是纯图片/表情包，不需要 AI 回复。若不管，
        消息会继续流进 AstrBot 默认 LLM 链路，没配模型时报
        「未找到任何可用的对话模型（提供商）」。

        注意 `should_call_llm` 的语义很容易搞反（实测踩过坑）：

            astr_message_event.py:  self.call_llm = False      # 默认值
            stage.py:               if ... and not event.call_llm:  # 满足则【调用】LLM

        也就是说，消费端判断的是 `not event.call_llm`，所以：

            should_call_llm(True)  -> call_llm=True  -> not True  = False -> 跳过 LLM ✅
            should_call_llm(False) -> call_llm=False -> not False = True  -> 调用 LLM ❌

        方法名是"should_call_llm"（是否应该调用），但要【禁止】就得传 True。
        这里传 True 表示"已处理，别再走 LLM"。
        """
        if not self.enabled:
            return
        try:
            # ---- 图片库命令优先（搜索 / 标签管理）----
            #
            # 【为什么要抢在收集之前】两者都以 bqbq 开头。
            # is_lib_command 把「无参数」判给收集，其余判给搜索：
            #     bqbq            -> False -> 走收集
            #     bqbq 猫          -> True  -> 走搜索
            #     bqbq add 猫      -> True  -> 加标签
            #     bqbq del         -> True  -> 删除
            #
            # 【为什么不用 filter.command】需要兼容 bqbq / /bqbq / Bqbq
            # 三种写法和中文子命令（看/add/del/加/删），普通函数判定更直接。
            if is_lib_command(event):
                event.should_call_llm(True)   # 库命令不需要 LLM
                async with httpx.AsyncClient(
                    timeout=DOWNLOAD_TIMEOUT, trust_env=False, follow_redirects=True
                ) as client:
                    async for r in self.lib_cmds.handle(event, client):
                        yield r
                return

            self._last_report = {}
            # 同时清空上一轮"已存在"的标签，避免跨消息串味
            self._skip_tags = []
            handled = await self._handle(event)
            # 只有确实处理了表情包的消息才掐 LLM；普通聊天不受影响
            if handled:
                event.should_call_llm(True)
                # 按配置回复处理结果（默认只在新图>0 时回复，避免刷屏）
                if self.reply_result:
                    msg = self._format_report()
                    if msg:
                        yield event.plain_result(msg)
                        # 【注意】这里绝对不要调用 event.stop_event()。
                        # RespondStage 位于本 Stage 的【后续】阶段，靠洋葱模型
                        # 递归进入；一旦 is_stopped() 为真，scheduler 会在
                        # 进入后续阶段【之前】就 break，回复反而发不出去。
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[BQBQ] 处理消息异常: {e}")

    def _format_report(self) -> str:
        """把上一条消息的处理结果格式化成给用户看的短句。

        返回空字符串表示不需要回复（没有新图且无需汇报失败）。

        Returns:
            回复文本；空串表示不回复。
        """
        d = self._last_report or {}
        new = d.get("new", 0)
        dup = d.get("dup", 0)
        skip = d.get("skip", 0)
        fail = d.get("fail", 0)
        total = d.get("total", 0)
        no_url = d.get("no_url", 0)

        if total == 0 and no_url == 0:
            return ""
        # 【什么时候回复】
        # 只要"处理过图片"就回复：
        #   · new    > 0  新增了图
        #   · dup    > 0  上传后才知道重复（并发竞态）
        #   · skip   > 0  预查重命中，已存在
        #   · fail   > 0  下载/上传失败（否则用户不知道出过问题）
        #   · no_url > 0  组件没给出可用地址
        #
        # 【为什么 skip 也要回复】
        # 官方机器人当前只能收到【私聊】和【@机器人】的消息 —— 都是用户
        # 主动触发的行为。用户特意丢一张图过来，机器人不该毫无反应，
        # 否则无法区分"收到了但已存在"和"压根没收到"。
        #
        # 之前担心 skip 回复会刷屏，那个顾虑在官方机器人场景下不成立：
        # 收不到群内全量消息，不会有大批量被动涌入。
        #
        # 想回到"只在有新增/失败时回复"：把下面的 skip == 0 去掉即可。
        if (
            new == 0
            and dup == 0
            and skip == 0
            and fail == 0
            and no_url == 0
        ):
            return ""

        parts = [f"表情包收集：新增 {new}"]
        if dup:
            parts.append(f"重复 {dup}")
        if skip:
            parts.append(f"已有 {skip}")
        if fail:
            parts.append(f"失败 {fail}")
        if no_url:
            parts.append(f"无地址 {no_url}")
        head = "，".join(parts)

        lines = [head]

        # ---- 「已存在」的图，把它们的标签一并列出 ----
        #
        # 【为什么值得展示】用户发一张图过来，如果图已在库里，
        # 他最想知道的其实是"这图原本是什么标签"，而不是"已存在"这三个字。
        # 标签由 _check_exists 顺手从 check-md5 拿到（后端 2026-10 起返回）。
        #
        # 标签为空的图不展示 —— 只说"已存在（无标签）"对用户没信息量，
        # 数量已经在 head 里的"已有 N"体现了。
        tag_lines = [
            "、".join(t) for _, t in self._skip_tags if t
        ]
        if tag_lines:
            if len(tag_lines) == 1:
                lines.append(f"已有图片的标签：{tag_lines[0]}")
            else:
                lines.append(f"已有图片的标签（前 {len(tag_lines)} 张）：")
                for i, t in enumerate(tag_lines, 1):
                    lines.append(f"  {i}. {t}")
            if skip > len(tag_lines):
                lines.append(f"  （其余 {skip - len(tag_lines)} 张无标签或未展示）")

        # 失败时附上原因，便于排查
        if fail and self._fail_reasons:
            reasons = " ".join(f"{k}×{v}" for k, v in sorted(self._fail_reasons.items()))
            lines.append(f"失败原因：{reasons}")

        return "\n".join(lines)

    async def _handle(self, event: AstrMessageEvent) -> bool:
        """解析一条消息并收集其中的表情包。

        【触发规则：私聊与群聊不同】

          私聊 -> **不需要**触发词。用户主动私聊发图 = 明确意图。
                  实测私聊消息的 message_str 是 '' 或 '[表情]'，
                  根本没有文字，强制要触发词会永远收不到。

          群聊 -> **必须**带触发词 `bqbq`（无参数）。
                  因为要 @机器人 才会推送消息，用户可能只是
                  "@机器人 顺便说句话"，附带的图未必想收集。
                  带参数的（如 `bqbq 猫`）会被 commands.py 当搜索处理。

        判据是 `event.get_group_id()`：为空表示私聊。

        Args:
            event: AstrBot 消息事件。

        Returns:
            True 表示这条消息是表情包收集（调用方应阻止默认 LLM 请求）。
        """
        raw = getattr(event.message_obj, "raw_message", None)
        raw_data = getattr(raw, "raw_data", None)
        content = ""
        if isinstance(raw_data, dict):
            content = raw_data.get("content") or ""

        text = content or (event.message_str or "")

        # 私聊（无群号）
        is_private = not event.get_group_id()

        if is_private:
            # ---- 私聊白名单：防止别人私聊触发收集 ----
            #
            # 【为什么用 openid 而不是 QQ 号】
            # 官方机器人拿不到 QQ 号，`event.get_sender_id()` 返回的是
            # openid（形如 <32位openid>）。
            # 官方文档明确「不同 bot 获取到的 openid 均不相同」，
            # 且无法反推 QQ 号（已实测 6 种方法均失败）。
            #
            # 所以白名单只能填 openid。
            # ⚠️ 若更换 bot（AppID 变了），openid 会变，需要重新填写。
            if self.private_whitelist_enabled:
                sender = str(event.get_sender_id() or "").strip().upper()
                if sender not in self.private_whitelist:
                    logger.info(
                        f"[BQBQ] 私聊非白名单用户，忽略: {sender}"
                    )
                    return False
            # 私聊不需要触发词（用户主动发图 = 明确意图）
        else:
            # ---- 触发词检查（仅群聊）----
            if not self._has_trigger(text):
                return False

        # ---- 引用回复：先处理引用链里的图片/转发 ----
        # 结构（实测）：
        #   引用一张图片 -> Reply.chain = [Plain, Image]  （Image 已本地化）
        #   引用合并转发 -> Reply.chain = [Plain]，文本里有 文件名:<md5>.gif 与 URL
        #   引用一段文字 -> Reply.chain = [Plain]，无图
        handled_reply = await self._handle_reply(event)

        # ---- 1) 合并转发：从文本里挖 ----
        if self.save_merged and ("合并转发" in text or "的聊天记录" in text):
            items = self._parse_forward_text(text)
            if items:
                logger.info(f"[BQBQ] 合并转发: 发现 {len(items)} 张图，开始处理")
                # 记录本条消息前后的差值，用于汇报"本次处理结果"
                before = dict(self.stats)
                async with httpx.AsyncClient(
                    timeout=DOWNLOAD_TIMEOUT, trust_env=False, follow_redirects=True
                ) as client:
                    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

                    async def worker(md5: str, url: str) -> None:
                        async with sem:
                            await self._process_one(client, md5, url)

                    await asyncio.gather(*(worker(m, u) for m, u in items))
                delta = {k: self.stats[k] - before.get(k, 0) for k in self.stats}
                logger.info(f"[BQBQ] 合并转发完成: {self._stats_str()}")
                # ⚠️ 必须赋值！_format_report() 读的是 _last_report，
                # 若这里漏掉，total 恒为 0，表现为"永远不回复"。
                # （普通消息分支同理，见本文件下方对应位置）
                self._last_report = delta
                return True
            # 是"聊天记录"但没解析出图片（纯文字转发）-> 交给 LLM
            return False

        # ---- 2) 普通图片：顶层 Image + 引用链里的 Image，用同一套逻辑 ----
        # 【为什么放一起】实测 Reply.chain 里的 Image 与顶层 Image 机制完全
        # 相同（框架对两者都做了本地化），所以合并处理，代码只写一遍。
        images = [c for c in event.get_messages() if type(c).__name__ == "Image"]
        if self.save_reply:
            images += self._reply_chain_images(event)
        if self.save_normal and images:
            logger.info(f"[BQBQ] 图片: 发现 {len(images)} 张（含引用链）")
            # 【必须】记录本条消息前后的差值，否则 _last_report 一直是空 {},
            # _format_report() 会因 total==0 直接返回空串 —— 表现为"永远不回复"。
            before = dict(self.stats)
            async with httpx.AsyncClient(
                timeout=DOWNLOAD_TIMEOUT, trust_env=False, follow_redirects=True
            ) as client:
                for comp in images:
                    await self._process_image_comp(client, comp)
            logger.info(f"[BQBQ] 图片处理完成: {self._stats_str()}")
            # ⚠️ 必须赋值！曾在此处漏掉，导致「单张图永远不回复」：
            # _format_report() 读 _last_report，为空则 total==0 -> 返回空串。
            self._last_report = {k: self.stats[k] - before.get(k, 0) for k in self.stats}
            return True

        # 走到这里说明有触发词但没找到可处理的图（例如只发了触发词）
        # handled_reply 为真时也返回 True，让调用方掐掉 LLM 请求。
        return handled_reply

    # ------------------------------------------------------------------
    # 引用回复
    # ------------------------------------------------------------------
    @staticmethod
    def _reply_chain_images(event: AstrMessageEvent) -> list:
        """取出所有 Reply 组件 chain 里的 Image。

        实测结构（引用一张图片时）：
            Reply.chain = [Plain, Image]      # Image 已被框架本地化
        引用文字 / 引用合并转发时 chain 里只有 Plain，这里返回空列表，
        文本部分由 `_handle_reply` 另行处理。

        Args:
            event: 消息事件。

        Returns:
            Image 组件列表。
        """
        out = []
        for c in event.get_messages():
            if type(c).__name__ != "Reply":
                continue
            for sub in (getattr(c, "chain", None) or []):
                if type(sub).__name__ == "Image":
                    out.append(sub)
        return out

    async def _handle_reply(self, event: AstrMessageEvent) -> bool:
        """处理引用回复里的合并转发文本。

        【为什么单独处理】引用的合并转发，其内容在 `Reply.chain[0]` 的
        Plain 文本里（实测 33991 字符，含 90 个 `文件名:<md5>.gif` 与 URL），
        与顶层"直接转发的聊天记录"格式一致，所以复用同一个解析器。

        引用的【图片】不在这里处理 —— 它们由 `_reply_chain_images` 统一
        取出后走 `_process_image_comp`（与顶层图片同一套逻辑）。

        Args:
            event: 消息事件。

        Returns:
            True 表示处理过引用内容（调用方应阻止 LLM）。
        """
        if not (self.save_reply and self.save_merged):
            return False

        done = False
        for c in event.get_messages():
            if type(c).__name__ != "Reply":
                continue
            for sub in (getattr(c, "chain", None) or []):
                if type(sub).__name__ != "Plain":
                    continue
                sub_text = getattr(sub, "text", "") or ""
                if "合并转发" not in sub_text and "聊天记录" not in sub_text:
                    continue
                items = self._parse_forward_text(sub_text)
                if not items:
                    continue
                logger.info(f"[BQBQ] 引用的合并转发: 发现 {len(items)} 张图，开始处理")
                before = dict(self.stats)
                async with httpx.AsyncClient(
                    timeout=DOWNLOAD_TIMEOUT, trust_env=False, follow_redirects=True
                ) as client:
                    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

                    async def worker(md5: str, url: str) -> None:
                        async with sem:
                            await self._process_one(client, md5, url)

                    await asyncio.gather(*(worker(m, u) for m, u in items))
                # 合并到 _last_report（可能与后续普通分支叠加）
                delta = {k: self.stats[k] - before.get(k, 0) for k in self.stats}
                self._last_report = delta
                logger.info(f"[BQBQ] 引用的合并转发完成: {self._stats_str()}")
                done = True
        return done

    # ------------------------------------------------------------------
    # 单张图片处理（顶层与引用链共用）
    # ------------------------------------------------------------------
    async def _process_image_comp(self, client: httpx.AsyncClient, comp) -> None:
        """处理一个 Image 组件：本地文件优先 -> 网络兜底。

        【为什么抽出来】顶层 `event.get_messages()` 里的 Image 与
        `Reply.chain` 里的 Image **机制完全相同**（框架的 preprocess_stage
        对两者都做了本地化，见 stage.py:130 与 165），所以共用同一套逻辑。

        Args:
            client: 复用的 httpx 客户端。
            comp: AstrBot 的 Image 组件。
        """
        # 【优先读本地文件】
        # 框架的 preprocess_stage 已把图片下载到 data/temp/，
        # 并把组件的 url/file/path 全部覆盖成本地路径。
        local = self._extract_local_path(comp)
        if local:
            self.stats["total"] += 1
            try:
                data = Path(local).read_bytes()
            except Exception as e:  # noqa: BLE001
                self.stats["fail"] += 1
                logger.warning(f"[BQBQ] 读取本地图片失败 {local}: {e}")
                return
            if not data[:4].startswith(_IMAGE_MAGIC):
                self.stats["fail"] += 1
                logger.info(f"[BQBQ] 本地文件不是图片，跳过: {local}")
                return

            # 【预查重】本地文件的 md5 是现成的，先查一次再决定要不要上传。
            md5 = hashlib.md5(data).hexdigest()
            if (await self._check_exists(client, md5))[0]:
                self.stats["skip"] += 1
                logger.info(f"[BQBQ] 已存在，跳过上传 {md5[:12]}")
                return

            async with self._lock:
                ok, is_new = await self._upload(client, data)
            if not ok:
                self.stats["fail"] += 1
            elif is_new:
                self.stats["new"] += 1
                self.stats["local_ok"] += 1
            else:
                # 预查重没命中但上传后说重复：并发竞态，仍算 dup
                self.stats["dup"] += 1
            return

        # 【兜底】没有本地文件时，尝试用 http 地址下载
        url = self._extract_http_url(comp)
        if not url:
            self.stats["no_url"] += 1
            logger.info(
                f"[BQBQ] 该图片组件既无本地文件也无 http 地址: "
                f"url={getattr(comp, 'url', None)!r} "
                f"file={getattr(comp, 'file', None)!r} "
                f"path={getattr(comp, 'path', None)!r}"
            )
            return
        self.stats["total"] += 1
        md5 = self._guess_md5(url)
        data, _method = await self._download(client, url, md5=md5)
        if data is None:
            self.stats["fail"] += 1
            return
        # 【预查重】URL 里未必有 md5，所以下载后算一次再决定是否上传。
        real_md5 = hashlib.md5(data).hexdigest()
        if (await self._check_exists(client, real_md5))[0]:
            self.stats["skip"] += 1
            logger.info(f"[BQBQ] 已存在，跳过上传 {real_md5[:12]}")
            return
        async with self._lock:
            ok, is_new = await self._upload(client, data)
        if not ok:
            self.stats["fail"] += 1
        elif is_new:
            self.stats["new"] += 1
        else:
            self.stats["dup"] += 1

    @staticmethod
    def _has_trigger(text: str) -> bool:
        """判断消息是否触发收集。

        【2026-10 简化】只认裸 "bqbq"（原 "bqbq收图" 已移除）。

        必须是**整条消息**等于 bqbq / /bqbq（允许前后空格）。
        带参数的一律不算 —— "bqbq 猫" 要交给搜索，
        判定见 commands.is_lib_command（它先跑，不会走到这里）。

        Args:
            text: 消息纯文本。

        Returns:
            True 表示触发收集。
        """
        if not text:
            return False
        low = text.lower().strip()
        return low in BARE_TRIGGERS

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    # 保留词：bqbq 统计 / bqbq 帮助 / bqbq 登录 ...
    # ------------------------------------------------------------------
    #
    # 这些以前各占一个 @filter.command（bqbq统计、bqbq绑定 ...），
    # 会跟「搜索『统计』」撞车，命名也不统一。
    #
    # 现在统一成「bqbq + 空格 + 保留词」，词表在 commands.py 的
    # RESERVED 里，这里只负责执行。加命令 = 表里加一条 + 这里加个分支。
    async def _handle_reserved(self, event: AstrMessageEvent, name: str, body: str):
        """执行保留词命令。

        Args:
            event: 消息事件。
            name: RESERVED 映射出的命令名（help/stats/reset/bind/
                whitelist/login/fetch）。
                whitelist/login/fetch）。
            body: 去掉「bqbq 子命令」后的剩余文本（登录验证码等）。

        Yields:
            回复结果。
        """
        if name == "help":
            yield event.plain_result(
                "表情包机器人命令\n"
                "\n"
                "只有两种写法：\n"
                "  1 3              看第 1、3 张原图（编号见预览图）\n"
                "  bqbq xxx         其余都是 bqbq + 空格 + 内容\n"
                "\n"
                "【搜索】\n"
                "  bqbq 猫           搜索（自动展开同义词）\n"
                "  bqbq 猫 可爱       多个词 = 同时满足\n"
                "\n"
                "【标签】先引用图片再发\n"
                "  bqbq add 可爱      追加标签\n"
                "  bqbq tag 猫 可爱   替换全部标签\n"
                "  bqbq del 可爱      删掉指定标签\n"
                "  bqbq del          整张丢进回收站\n"
                "\n"
                "【其它】\n"
                "  bqbq 登录 4821     网页登录（先点网页企鹅按钮拿码）\n"
                "  bqbq 取回          取购物车里的表情包\n"
                "  bqbq 绑定          查看自己的 openid\n"
                "  bqbq 统计          收集数量\n"
                "  bqbq 重置          清零统计\n"
                "  bqbq 白名单        查看私聊白名单\n"
            )
            return

        if name == "stats":
            yield event.plain_result(f"表情包收集统计\n{self._stats_str()}")
            return

        if name == "reset":
            for k in self.stats:
                self.stats[k] = 0
            yield event.plain_result("统计已重置")

            # ------------------------------------------------------------------
            # 绑定：让用户自己拿到 openid
            # ------------------------------------------------------------------
            return

        if name == "bind":
            sender = str(event.get_sender_id() or "").strip()
            where = "群聊" if event.get_group_id() else "私聊"

            in_list = sender.upper() in self.private_whitelist
            if self.private_whitelist_enabled:
                status = "已在白名单中 ✅" if in_list else "不在白名单中 ❌"
            else:
                status = "（白名单功能当前未启用）"

            yield event.plain_result(
                "BQBQ 身份标识\n"
                f"你的 openid：\n{sender}\n\n"
                f"来源：{where}\n"
                f"白名单状态：{status}\n\n"
                "把上面这串 openid 发给管理员，即可加入私聊白名单。\n"
                "注意：这不是 QQ 号，只对本机器人有效。"
            )
            return

        if name == "whitelist":
            if not self._is_admin(event):
                yield event.plain_result(
                    "只有管理员可以查看白名单\n"
                    "（admins_id 需填 openid，不是 QQ 号 —— 可先发「bqbq绑定」获取）"
                )
                return

            head = "已启用" if self.private_whitelist_enabled else "未启用"
            lines = [f"私聊白名单：{head}", f"共 {len(self.private_whitelist)} 人"]
            if self.private_whitelist:
                for i, x in enumerate(sorted(self.private_whitelist), 1):
                    lines.append(f"  {i}. {x}")
            else:
                lines.append("  （空）")
            lines.append("")
            lines.append(f"你自己的 openid：{event.get_sender_id()}")
            yield event.plain_result("\n".join(lines))

            # ------------------------------------------------------------------
            # 网页登录 & 购物车取回
            # ------------------------------------------------------------------
            return

        if name == "login":
            code = (event.message_str or "").strip()
            # 容错：用户可能连指令一起发，取最后一段数字
            if not code.isdigit():
                digits = [s for s in code.split() if s.isdigit()]
                code = digits[-1] if digits else code

            if not code.isdigit() or len(code) != 4:
                yield event.plain_result(
                    "用法：bqbq登录 4821\n"
                    "（4 位验证码请在网页点「QQ 登录」后查看）"
                )
                return

            try:
                async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                    r = await client.post(
                        f"{self.backend}/api/auth/confirm",
                        json={"code": code, "openid": str(event.get_sender_id() or "")},
                    )
                    data = r.json()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[BQBQ] 登录请求失败: {e}")
                yield event.plain_result(f"登录失败：连不上后端（{type(e).__name__}）")
                return

            if data.get("success"):
                yield event.plain_result("登录成功，请回到网页继续操作 ✅")
            else:
                yield event.plain_result(f"登录失败：{data.get('error') or '验证码无效'}")
            return

        if name == "fetch":
            openid = str(event.get_sender_id() or "")
            if not openid:
                yield event.plain_result("取回失败：拿不到你的身份标识")
                return

            # bot 自身也需已登录（用它的固定 client_id 通过 require_edit）
            bot_cid = getattr(self, "bot_client_id", "")

            try:
                async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
                    r = await client.get(
                        f"{self.backend}/api/cart/outbox",
                        params={"openid": openid, "limit": 5, "client_id": bot_cid},
                    )
                    data = r.json()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[BQBQ] 取回请求失败: {e}")
                yield event.plain_result(f"取回失败：连不上后端（{type(e).__name__}）")
                return

            if "detail" in data:
                yield event.plain_result(f"取回失败：{data.get('detail')}")
                return

            items = data.get("items") or []
            if not items:
                yield event.plain_result(data.get("message") or "没有待取回的表情包")
                return

            # 逐张下载后发送；单张失败不影响其它
            sent = 0
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                for it in items:
                    url = it.get("url") or ""
                    if not url:
                        continue
                    # 后端返回的是相对路径，补成绝对地址
                    if url.startswith("/"):
                        url = self.backend + url
                    try:
                        resp = await client.get(url)
                        if resp.status_code != 200:
                            continue
                        yield event.chain_result([Image.fromBytes(resp.content)])
                        sent += 1
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[BQBQ] 发送图片失败 {it.get('md5')}: {e}")

            remain = data.get("remaining") or 0
            if remain > 0:
                yield event.plain_result(f"已发送 {sent} 张，还剩 {remain} 张，请再发一次「bqbq取回」")
            elif sent == 0:
                yield event.plain_result("图片下载失败，请稍后重试")
            return

        yield event.plain_result(f"未知命令：{body}，发「bqbq 帮助」看看用法")


    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """判断发送者是否为 AstrBot 管理员。

        AstrBot 在事件构造时已根据 admins_id 配置设好 role，
        这里直接读即可（见 astr_message_event.py 的 role 字段）。

        Args:
            event: 消息事件。

        Returns:
            True 表示管理员。
        """
        try:
            return getattr(event, "role", "") == "admin"
        except Exception:  # noqa: BLE001
            return False

    async def terminate(self):
        """插件卸载。"""
        logger.info(f"[BQBQ] 收集插件已卸载，最终统计: {self._stats_str()}")
