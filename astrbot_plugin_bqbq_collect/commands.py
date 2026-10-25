"""BQBQ 图片库命令：搜索 / 查标签 / 加标签 / 删除。

从旧 nonebot 插件迁移。接口与展示方式的差异见 imagelib.py 顶部说明。

===============================================================================
 一、命令总览（2026-10 简化后）
===============================================================================

    bqbq <关键词>             搜索，返回 **4 列网格预览图**（不发原图）
    bqbq看 编号…              发这些编号对应的原图
    引用图片 + bqbq           查这张图的标签
    引用图片 + bqbq add 标签   加标签（**追加**，保留原标签）
    引用图片 + bqbq tag 标签   设置标签（**覆盖**，丢弃原标签）
    引用图片 + bqbq tag       清空全部标签
    引用图片 + bqbq del 标签   从标签里**删掉**指定标签
    引用图片 + bqbq del       整张图标记 trash_bin（移入回收站）

【三种写标签的方式，容易混】
    bqbq add 猫     原 [狗]      -> [狗, 猫]     追加
    bqbq tag 猫     原 [狗]      -> [猫]         覆盖
    bqbq del 狗     原 [狗, 猫]  -> [猫]         删除指定的

【del 的两种用法】
    带参数  -> 删掉这些标签（图还在库里）
    不带参数 -> 打上 trash_bin，图进回收站

    两个方向都能走：
        bqbq del            移入回收站
        bqbq del trash_bin  从回收站捞出来（恢复）

【唯一的保护措施】
    tag 覆盖时若原图带 trash_bin（在回收站），自动保留该标签 ——
    否则一次覆盖会把图"复活"，不符合直觉。
    （这条不增加记忆负担：正常覆盖一个还在库里的图根本不会触发。）

【用词说明：引用 ≠ 回复】
  · **引用**：用户长按别人的消息选"引用"，再发 bqbq。
    框架把它包成 `Reply` 组件，被引用的内容在 `Reply.chain` 里。
    本模块的 `_reply_images()` 读的就是这个 chain。
  · **回复**：机器人发出消息。下文说"一次回复最多 4 条"指的是这个。

两者别混：命令文档里一律用"引用图片"。

【命令的历史沿革】
  · `bqbq收图` 已移除 —— 触发词统一收敛到裸 `bqbq`
    （收集由 main.py 处理，本模块只管"带关键词"的情况）
  · `bqbq看 N` 曾一度移除（当时搜完直接出图），后来又加回来 ——
    因为改成"搜索只发预览"后，必须有单独命令取原图

===============================================================================
 二、展示方案：4 列网格预览 + 按需取原图
===============================================================================

qqofficial 的限制（实测适配器源码）：
  · `Nodes`（合并转发）被**静默忽略** —— qqofficial_message_event.py:966
  · 一条消息**只能带一张图** —— qqofficial_message_event.py:922
  · 没有撤回接口（适配器未实现 QQ 的 DELETE messages API）

【流程】
    bqbq 猫             -> 1 条消息：4 列网格预览（缩略图拼接，带编号）
    bqbq看 1 3          -> 最多 3 条消息：这些编号的原图

    特例：搜索只命中 1 张时**直接发原图**，不拼图。
    拼图是为"多张挑一张"服务的；只有一张时拼图反而绕路
    （用户还得再发编号，且拼图里的图是缩过的）。

【搜索语法：空格 = AND，+ = OR】
    bqbq soyo anon        同时带这两个标签（交集）
    bqbq soyo+anon        任一即可（并集）
    bqbq soyo -anon       有 soyo 但不含 anon

【为什么不用后端的 include_tags】实测它是 OR（并集）语义：
    soyo        12 张
    anon         9 张
    [soyo,anon] 19 张      <- 并集，多标签无法锁定单张
改用高级模式（keywords 二维数组）后，组内 OR、组间 AND 都可控：
    [[soyo],[anon]]  2 张  <- 真交集，能精确挑图
这也是旧 nonebot 插件原本的用法，迁移时一度被简化掉了。

【为什么搜索不再自动补发原图】
早期版本搜完就补发 3 张原图，一条搜索发 4 条消息，刷屏严重；
而且用户往往只对其中一两张感兴趣，其余是白发的。
现在改成"预览带编号 -> 用户按需取"。

【为什么预览用网格而不是竖排】
竖排 5 张就 1656px 高，手机上要滑很久。
4 列网格横向铺满，并把画布高宽比限制在 MAX_ASPECT(2.0) 以内
（竖屏手机约 2.17），保证一屏基本能看完。

【预览用缩略图，原图才发原图】
实测同一张图 原图 402KB / 缩略图 65KB，拼 20 张能省十几 MB。
预览只是"看个大概"，不需要原始分辨率。

【关于撤回】QQ 官方 API 支持撤回（DELETE /v2/groups/{openid}/messages/{id}，
限 2 分钟内），但 AstrBot 的 qqofficial 适配器**未实现**该接口。
要实现得绕过框架直接调 HTTP API，成本高、易碎，故不做。
预览消息留着反而有用 —— 它是编号索引，用户想再选别的还能看。

===============================================================================
 三、与收集插件的分工
===============================================================================

    消息内容              谁处理
    ────────────────────────────────────────────
    bqbq                  收集（main.py，无关键词）
    bqbq <关键词>          搜索（本模块）
    引用图片 + bqbq        查标签（本模块）
    引用图片 + bqbq add/del 标签管理（本模块）

判定见 is_lib_command。
"""
from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .imagelib import (
    GRID_COLS,
    MAX_ROWS,
    MAX_VIEW,
    TRASH_TAG,
    ImageLibClient,
    _parse_index_list,
    _parse_range,
)

# 收集插件使用的词，本模块要避开
COLLECT_WORDS = ("收图", "收集")

# 动图编号的标记。stitch 看到编号前有这个字符，就把编号画成橙色。
#
# 【为什么用前缀标记而不是别的办法】
# stitch 只收到 (图片字节, 标签字符串)，拿不到 filename，
# 没法自己判断是不是动图。所以由调用方在标签里标记。
# 用不可见的控制字符而不是 "GIF" 之类的可见文本，
# 是为了不占格子的宽度 —— 4 列时每格只有约 190px。
GIF_MARK = "\x00gif"

# 预览取图的**总时间预算（秒）**。
#
# 【为什么需要】QQ 被动消息必须锚定用户那条消息，且 **5 分钟内**回复完。
# 正常情况整个预览流程只要 0.7 秒（实测），余量 400 多倍。
# 但后端"半死不活"（能连上、每张图都卡到超时）时，
# 20 张 x 30 秒 = 600 秒，会超过窗口，导致什么都发不出去。
#
# 给一个预算后，超时就少取几张，保证还能出一张能看的预览。
# 取 60 秒是保守值：正常 0.2 秒取完，留足网络抖动余量。
PREVIEW_BUDGET = 60.0


def session_key(event: AstrMessageEvent) -> str:
    """缓存键：群聊按群+人，私聊按人 —— 避免多人搜图互相覆盖。"""
    gid = event.get_group_id() or ""
    sid = event.get_sender_id() or ""
    return f"{gid}:{sid}"


def strip_prefix(text: str) -> str:
    """去掉开头的 bqbq / /bqbq 前缀（大小写不敏感）。"""
    return re.sub(r"^\s*/?\s*bqbq\s*", "", text or "", flags=re.IGNORECASE).strip()


def _first_token(body: str) -> str:
    parts = body.split(None, 1)
    return parts[0].lower() if parts else ""


def dedup(tags) -> list[str]:
    """标签去重，保持原顺序。

    【为什么必须自己做】实测后端不去重 ——
    传 ["猫","猫","狗"] 会原样存回 ["猫","猫","狗"]。
    标签重复没有意义，还会让显示变乱，所以在客户端归并。

    Args:
        tags: 标签序列（可能有重复、空串）。

    Returns:
        去重后的列表，保序。
    """
    out: list[str] = []
    for t in tags:
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return out


def parse_search_syntax(query: str) -> tuple[list[list[str]], list[str]]:
    """把搜索语法解析成「包含组」和「排除词」。

    【语法】
        soyo anon        两个标签都要（空格 = AND）
        soyo+anon        任一即可（"+" 连成一组 = OR）
        soyo anon+taki   要有 soyo，且要有 anon 或 taki
        -恶心            排除含「恶心」的
        -a -b            同时排除（AND 排除）

    【为什么要这套语法】后端高级模式的关键字是二维数组，
    组内 OR、组间 AND。用户用一个 "+" 就能表达"或"，比记括号直观。

    Args:
        query: 用户输入的关键词部分（已去掉范围参数）。

    Returns:
        (include_groups, exclude_words)
        include_groups 形如 [["soyo"], ["anon","taki"]]，空表示不限制。
    """
    inc_groups: list[list[str]] = []
    exc: list[str] = []

    for token in query.split():
        # 排除词：-xxx
        if token.startswith("-") and len(token) > 1:
            exc.append(token[1:])
            continue
        # 去掉可能的前导 +（"bqbq +猫" 这种写法，等价于 "猫"）
        t = token[1:] if token.startswith("+") and len(token) > 1 else token
        if not t:
            continue
        # 组内 OR：用 + 连接的部分拆成一组
        parts = [p for p in t.split("+") if p]
        if parts:
            inc_groups.append(dedup(parts))

    return inc_groups, dedup(exc)


def format_search_syntax(groups: list[list[str]], exc: list[str]) -> str:
    """把人话还原出来，用于"没找到"时回显用户搜了什么。"""
    parts = []
    for g in groups:
        parts.append("+".join(g) if len(g) > 1 else g[0])
    text = " ".join(parts) if parts else "（空）"
    if exc:
        text += " " + " ".join("-" + e for e in exc)
    return text


def is_index_only(text: str) -> bool:
    """判断消息是否是「纯编号」（用于直接取原图）。

    【为什么单独判定】用户看完预览后要取原图，早期要求发「bqbq看 1 3」。
    但预览图上已经写了编号，再让用户加命令前缀是多余的 ——
    直接发 `1 3` 就行。

    识别规则（必须整条消息都是编号，避免误触发）：
        1            ✅
        1 3 5        ✅
        1,3,5        ✅
        1-5          ✅
        1 3 猫       ❌ 混了文字
        bqbq 1       ❌ 有前缀（走正常命令）
        今天天气 1    ❌

    Args:
        text: 消息纯文本（已 strip）。

    Returns:
        True 表示是纯编号消息。
    """
    if not text:
        return False
    # 只允许数字、空白、逗号、顿号、分号、连字符、波浪号
    if not re.fullmatch(r"[\d\s,，、;；\-~]+", text):
        return False
    # 必须至少有一个数字
    return bool(re.search(r"\d", text))


# ===========================================================================
#  统一命令解析
#
#  【为什么要有这一层】此前同一组子命令词表散落在 4 处判定：
#      is_lib_command()  判断"是不是本模块的"
#      handle()          再判一遍决定分发到哪
#      _cmd_tag()        又判一遍是 add 还是 tag
#      还有各处对 VIEW_WORDS/DEL_WORDS 的重复比较
#  加一个新子命令要改好几处，容易漏（实际也漏过：DEL_WORDS 一开始
#  没进 is_lib_command，导致 bqbq del 被当成搜索）。
#
#  现在统一成"输入文本 -> Cmd 对象"一步解析，
#  判定与分发都读同一个结果，不会再有前后不一致。
# ===========================================================================

# 命令类型（用字符串而不是 Enum，便于日志和调试直读）
CMD_NONE = "none"        # 不是本模块的命令
CMD_COLLECT = "collect"  # 裸 bqbq -> 交给收集插件
CMD_SEARCH = "search"    # 带关键词 -> 搜索
CMD_VIEW = "view"        # 纯编号 1 3 -> 取原图
CMD_ADD = "add"          # 引用图 + bqbq add   -> 追加标签
CMD_TAG = "tag"          # 引用图 + bqbq tag   -> 覆盖标签
CMD_DEL = "del"          # 引用图 + bqbq del   -> 删标签 / trash_bin

# 保留词：bqbq 后面跟这些词时不走搜索，而是当作固定命令。
# 左边是用户敲的词，右边是给 main.py 用的命令名。
RESERVED: dict[str, str] = {
    "帮助": "help",     "help": "help",
    "统计": "stats",    "stats": "stats",
    "重置": "reset",    "reset": "reset",
    "绑定": "bind",     "bind": "bind",   "我的id": "bind",
    "白名单": "whitelist", "whitelist": "whitelist",
    "登录": "login",    "login": "login",
    "取回": "fetch",    "fetch": "fetch",
}

# 标签操作只认英文。中文「加/覆盖/删」不再作为命令词 ——
# 它们太常见，拿来当标签搜索词的概率远高于当命令，
# 只留英文的话整个判定表更短，也不会误伤搜索。
TAG_WORDS: dict[str, str] = {
    "add": CMD_ADD,
    "tag": CMD_TAG,
    "del": CMD_DEL,
}


class ParsedCommand:
    """一条消息解析后的结果。

    Attributes:
        kind: 命令类型（CMD_* 之一）。
        body: 去掉 bqbq 前缀后的剩余文本（纯编号命令则为原文）。
        first: body 的第一个词（小写），无则为空串。
        name: 命中保留词时，RESERVED 里对应的命令名（如 "help"）。
        is_lib: 是否由本模块处理（kind 不是 NONE/COLLECT）。
    """

    __slots__ = ("kind", "body", "first", "name")

    def __init__(self, kind: str, body: str = "", first: str = "", name: str = ""):
        self.kind = kind
        self.body = body
        self.first = first
        self.name = name

    @property
    def is_lib(self) -> bool:
        return self.kind in (
            CMD_SEARCH, CMD_VIEW, CMD_ADD, CMD_TAG, CMD_DEL,
        )

    def __repr__(self) -> str:  # 便于调试
        return f"ParsedCommand({self.kind!r}, body={self.body!r})"


def parse_command(text: str, has_quote: bool = False) -> ParsedCommand:
    """把消息文本解析成命令。**这是唯一的判定入口。**

    只有两种格式：

        1 3                 纯编号 -> 看原图（预览图上写的就是编号）
        bqbq xxx            bqbq  + 空格 + 内容

    `bqbq xxx` 的内容分三种：

        bqbq                空       -> 交给收集插件
        bqbq 帮助           保留词   -> 固定命令
        bqbq add 可爱       引用图   -> 标签操作
        bqbq 猫             其它     -> 搜索

    判定顺序（5 条）：
        1. 纯编号                    -> VIEW
        2. 无 bqbq 前缀               -> NONE
        3. 空 body                   -> COLLECT
        4. 保留词                     -> SEARCH + name
        5. 有引用图 + add/tag/del     -> ADD / TAG / DEL
        6. 其余                       -> SEARCH

    【为什么标签操作只认英文】「加 / 覆盖 / 删」当搜索词的概率远高于
    当命令（少打两个字母不值得），只留 add/tag/del 后判定表更短，
    也不会误伤「加」这个常见的标签字。

    【为什么没有 bqbq看】预览图上直接写着编号，看完发 `1 3` 就行。
    多一个「bqbq看」入口就多一条分支、多一份要记的规则。

    Args:
        text: 消息纯文本。
        has_quote: 这条消息是否引用了图片。只有引用图时，
            add/tag/del 才生效——否则它们只是搜索词。

    Returns:
        ParsedCommand
    """
    text = (text or "").strip()
    if not text:
        return ParsedCommand(CMD_NONE)

    # 1) 纯编号 -> 看原图
    #
    # 「只发数字」在 QQ 官方机器人场景下本来就是发给机器人的
    # （只有 @机器人 或私聊才收得到），不会误伤正常聊天。
    if is_index_only(text):
        return ParsedCommand(CMD_VIEW, body=text)

    # 2) 必须有 bqbq 前缀
    if not re.match(r"^\s*/?\s*bqbq", text, re.IGNORECASE):
        return ParsedCommand(CMD_NONE)

    body = strip_prefix(text)
    first = _first_token(body)

    # 3) 裸 bqbq -> 收集
    if not body:
        return ParsedCommand(CMD_COLLECT, body="", first="")

    # 4) 保留词 -> 固定命令（bqbq 帮助 / 统计 / 登录 ...）
    if first in RESERVED:
        return ParsedCommand(CMD_SEARCH, body, first, name=RESERVED[first])

    # 5) 标签操作：必须引用了图片，否则当成普通搜索词
    if has_quote and first in TAG_WORDS:
        return ParsedCommand(TAG_WORDS[first], body, first)

    # 6) 其余 -> 搜索
    return ParsedCommand(CMD_SEARCH, body, first)
    # 8) 其余 -> 搜索
    return ParsedCommand(CMD_SEARCH, body, first)


def has_quoted_image(event: AstrMessageEvent) -> bool:
    """这条消息是否引用了图片。

    用于消解「bqbq add xxx」的歧义：
      引用了图片 -> 标签操作
      没引用图片 ->「add」只是搜索词，走搜索

    【为什么自己实现而不复用 LibCommands._reply_images】
    后者是类的静态方法，模块级函数里调不到。之前写成
    `_reply_images(event)` 会抛 NameError，被 except 吞掉后
    恒返回 False —— 于是 add/tag/del 全部失效（永远判成没引用图）。

    这里独立实现，只判断「有没有 Image」，不需要返回列表。

    Args:
        event: 消息事件。

    Returns:
        True 表示引用的内容里有图片。
    """
    try:
        for c in event.get_messages():
            if type(c).__name__ != "Reply":
                continue
            for sub in (getattr(c, "chain", None) or []):
                if type(sub).__name__ == "Image":
                    return True
    except Exception:  # noqa: BLE001
        return False
    return False


def is_lib_command(event: AstrMessageEvent) -> bool:
    """判断这条消息是否由本模块处理（main.py 的路由入口）。

    Returns:
        True 表示本模块处理；False 表示交给收集插件或忽略。
    """
    has_quote = has_quoted_image(event)
    return parse_command(event.message_str or "", has_quote=has_quote).is_lib



class LibCommands:
    """把命令逻辑收在一个类里，便于注入客户端与配置。"""

    def __init__(self, client: ImageLibClient, reply_result: bool = True):
        self.client = client
        self.reply_result = reply_result
        # 保留词的处理函数，由主插件注入（要读写统计、白名单等本模块
        # 不持有的状态）。签名：async def(event, name, body) -> AsyncGenerator
        self.on_reserved = None

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def handle(self, event: AstrMessageEvent, http):
        """处理一条库命令。

        判定只在 parse_command 里做一次，这里只按结果分发，
        不重复判断任何词表。

        Yields:
            AstrBot 的回复结果对象。
        """
        # 判定与分发用同一份结论。
        #
        # has_quote 必须传：add/tag/del 只在引用图片时才生效，
        # 没引用时「加」等词会正常走搜索。
        has_quote = has_quoted_image(event)
        cmd = parse_command(event.message_str or "", has_quote=has_quote)

        # 保留词（帮助/统计/登录...）：交给主插件处理，因为要用到
        # 统计计数、白名单、购物车凭证等本模块不持有的状态。
        if cmd.name and self.on_reserved is not None:
            async for r in self.on_reserved(event, cmd.name, cmd.body):
                yield r
        elif cmd.kind == CMD_VIEW:
            # 纯编号，或 bqbq看 N
            async for r in self._cmd_view(event, http, cmd.body):
                yield r
        elif cmd.kind in (CMD_ADD, CMD_TAG, CMD_DEL):
            async for r in self._cmd_tag(
                event, http, cmd.body, cmd.first, cmd.kind
            ):
                yield r
        elif cmd.kind in (CMD_COLLECT, CMD_NONE):
            # 裸 bqbq 且引用了图片 -> 查标签
            #
            # 【迁移自原插件的 check 行为】原插件里 `bqbq` 不带参数时
            # handle_search 直接 return，实际生效的是 handle_manage 的 check 分支。
            # _cmd_check 内部会判断有没有引用图片，没有就什么都不发。
            async for r in self._cmd_check(event, http):
                yield r
        else:
            # CMD_SEARCH
            async for r in self._cmd_search(event, http, cmd.body):
                yield r

    # ------------------------------------------------------------------
    # 查标签（引用图片 + bqbq）
    # ------------------------------------------------------------------
    async def _cmd_check(self, event: AstrMessageEvent, http):
        images = self._reply_images(event)
        if not images:
            return
        data = await self._fetch_component(images[0], http)
        if not data:
            # 【诊断信息】以前只提示「读取失败」，看不出是框架没本地化、
            # 还是路径不对。这里把组件三个字段打出来，便于定位。
            comp = images[0]
            yield event.plain_result(
                "读取被引用的图片失败。\n"
                f"url = {getattr(comp, 'url', None)!r}\n"
                f"file = {getattr(comp, 'file', None)!r}\n"
                f"path = {getattr(comp, 'path', None)!r}\n"
                "\n"
                "通常是框架没有把图片下载到本地。可以试试：\n"
                "  1. 重新发一张图，再引用它\n"
                "  2. 把上面三行发给维护者"
            )
            return

        import hashlib

        md5 = hashlib.md5(data).hexdigest()
        exists, filename, tags, tags_exact = await self.client.check_md5_ex(http, md5)

        if not exists:
            # 不在库里 -> 顺手上传（与原插件 check 行为一致）
            res = await self.client.upload(http, data)
            if res.get("exists"):
                exists, filename, tags, tags_exact = await self.client.check_md5_ex(
                    http, md5
                )
            elif res.get("success"):
                yield event.plain_result("这张图不在库里，已收藏 ✅\n（暂无标签）")
                return
            else:
                yield event.plain_result(
                    f"这张图不在库里，收藏失败：{res.get('msg') or '未知错误'}"
                )
                return

        # 区分「真的没标签」和「后端异常拿不到」
        #
        # 【2026-10 后端已补接口】check-md5 现在直接返回 tags，
        # 所以 tags_exact 基本恒为 True。只有接口异常/后端未升级时才为 False。
        if not tags_exact:
            tag_text = "（读取失败，请检查后端是否已更新）"
        elif tags:
            tag_text = "、".join(tags)
        else:
            tag_text = "（无标签）"

        yield event.plain_result(
            f"这张图已在库中 ✅\n"
            f"标签：{tag_text}\n\n"
            f"加标签（保留原有）：引用这张图发「bqbq add 标签1 标签2」\n"
            f"设置标签（覆盖原有）：引用这张图发「bqbq tag 标签1 标签2」\n"
            f"删除某个标签：引用这张图发「bqbq del 标签1」\n"
            f"整张图移入回收站：引用这张图发「bqbq del」"
        )

    # ------------------------------------------------------------------
    # 搜索：拼图预览 + 原图
    # ------------------------------------------------------------------
    async def _cmd_search(self, event: AstrMessageEvent, http, body: str):
        """搜索 -> **只发拼图预览**，不发原图。

        【为什么改成只发预览】早期版本搜完就补发 3 张原图，一条搜索要发 4 条
        消息，刷屏严重；而且用户往往只对其中一两张感兴趣，其余是白发的。
        现在改成：预览里带编号，用户看中哪张就用 `bqbq看 编号` 取原图。

        预览用**缩略图**拼接（实测省 21 倍流量：2190KB -> 103KB）。
        """
        query, start, end = _parse_range(body)

        # ---- 解析搜索语法 ----
        #
        # 【为什么不能用简单的标签列表】后端简化模式（include_tags）
        # 实测是 **OR（并集）**：
        #       soyo        12 张
        #       anon         9 张
        #       [soyo,anon] 19 张   <- 并集，不是交集
        # 结果是多标签**无法锁定单张**（"同时带 soyo 和 anon" 只有 2 张，
        # 但会返回 19 张）。所以改用高级模式，自己组装分组。
        #
        # 语法（组内 OR、组间 AND）：
        #     soyo anon        两个标签都要（AND）
        #     soyo+anon        任一即可（OR）—— "+" 把词连成一组
        #     -恶心            排除
        #     soyo anon -恶心  soyo 且 anon，且不含恶心
        inc_groups, exc = parse_search_syntax(query)

        results, total = await self.client.search(
            http, include=[], exclude=exc, or_groups=inc_groups
        )

        if not results:
            kw = format_search_syntax(inc_groups, exc)
            yield event.plain_result(
                f"没有找到匹配「{kw}」的表情包。\n"
                f"提示：空格 = 都要满足，+ 连接 = 满足其一，- 前缀 = 排除。"
            )
            return

        # 缓存完整结果，供「bqbq看 编号」取用
        self.client.cache_results(session_key(event), results)

        shown = results[start:end]
        if not shown:
            yield event.plain_result(
                f"该范围内没有结果（共 {total} 条，"
                f"你请求的是第 {start + 1}~{end} 条）。"
            )
            return

        # 4 列网格，最多 GRID_COLS*MAX_ROWS 格
        max_cells = GRID_COLS * MAX_ROWS
        picked = list(enumerate(shown[:max_cells], start=start + 1))

        # ---- 只会显示一张：直接发原图，不拼预览 ----
        #
        # 【判据是"实际会显示几张"，不是"搜索总数"】
        # 拼图是为"多张挑一张"服务的。只要这次**实际要展示的**
        # 只有一张，拼图就是绕路：用户还得再发编号，
        # 而且拼图里的图是缩过的，不如原图清楚。
        #
        # 两种触发情形：
        #     bqbq 窒息             总共就 1 张
        #     bqbq soyo anon 1:1    总 2 张，但用户只要第 1 张
        # 两者都该直接出图 —— 早期只判 total==1，第二种漏了。
        if len(shown) == 1:
            item = shown[0]
            data = await self.client.fetch_image(http, item.get("filename") or "")
            if data:
                tags = item.get("tags") or []
                logger.info(
                    f"[BQBQ] 只显示一张，直接发原图"
                    f"（tags={tags}, total={total}）"
                )
                async for r in self._send_bytes(event, data, start + 1):
                    yield r
                return
            yield event.plain_result("图片没取到，可能已被删除或后端不可用。")
            return

        # ---- 总时间预算 ----
        #
        # 【为什么要这个】QQ 被动消息必须锚定用户那条消息，
        # 且**5 分钟内**必须回复完。正常情况整个流程只要 0.7 秒，
        # 但后端"半死不活"（能连上但每张缩略图都卡到超时）时，
        # 20 张 × 30 秒 = 600 秒，会超过窗口导致回复失败。
        #
        # 这里给取图阶段一个总预算：超时就少取几张，
        # 保证还能出一张能看的预览，而不是什么都发不出去。
        import time as _time

        deadline = _time.monotonic() + PREVIEW_BUDGET

        pairs = []
        for n, item in picked:
            if _time.monotonic() > deadline:
                logger.warning(
                    f"[BQBQ] 预览取图超预算（{PREVIEW_BUDGET}s），"
                    f"已取 {len(pairs)}/{len(picked)} 张"
                )
                break
            thumb = await self.client.fetch_image(
                http, item.get("filename") or "", thumb=True
            )
            if not thumb:
                continue
            tags = item.get("tags") or []
            # 动图的编号用橙色渲染（见 imagelib.stitch 里对 GIF_MARK 的处理）。
            #
            # 【为什么不用扩展名区分】试过在编号后写 "1.gif"，但标签是
            # 用 "编号.标签" 的格式拼的，扩展名里的点会把它切断，
            # 而且多几个字符在 4 列格子里也显得挤。
            # 改成给编号上色：动图橙色、静图默认深灰，一眼就能分开。
            fn = item.get("filename") or ""
            is_gif = fn.rsplit(".", 1)[-1].lower() == "gif"
            # 无标签的就不写"无标签"了 —— 那是噪音，占地方还看不出信息。
            # 有标签才接在编号后面（渲染时编号大、标签小）。
            if tags:
                label = f"{n}.{'、'.join(tags)}"
            else:
                label = str(n)
            # 标记必须放在**整个 label 的最前面**，stitch 靠 startswith 识别，
            # 然后剥掉它再按 "." 切编号和标签。
            if is_gif:
                label = GIF_MARK + label
            pairs.append((thumb, label))

        if not pairs:
            yield event.plain_result("这些图片都没取到，可能已被删除或后端不可用。")
            return

        # ---- 提示文字直接画进图片 ----
        #
        # 【为什么不再单发一条文字】早期是"文字提示 + 图片"两条消息，
        # 用户要往上翻才能看到怎么看原图；而且转发给别人时说明会丢。
        # 画进图里之后，图片本身就是完整的交互说明。
        if total > len(shown):
            header = f"找到 {total} 张，显示第 {start + 1}~{start + len(shown)} 张"
        else:
            header = f"找到 {total} 张"

        got = [n for n, _ in picked[:len(pairs)]]
        footer = "获取原图：直接发编号（如 " + " ".join(str(n) for n in got[:3]) + "）"

        import asyncio

        stitched = await asyncio.to_thread(
            ImageLibClient.stitch, pairs, header, footer
        )

        if not stitched:
            # 拼图失败才退回文字，保证用户至少知道结果
            yield event.plain_result(f"{header}\n{footer}\n（预览生成失败）")
            return

        logger.info(
            f"[BQBQ] 预览拼图: {len(pairs)} 张, "
            f"{GRID_COLS}列 x {(len(pairs) + GRID_COLS - 1) // GRID_COLS}行, "
            f"{len(stitched) / 1024:.0f} KB"
        )
        yield event.image_result(_save_temp(stitched, "bqbq_preview"))

    async def _cmd_view(self, event: AstrMessageEvent, http, body: str):
        """发编号对应的**原图**。

        两种调用方式：
            （直接发）1 3 5      <- 预览图上写的就是这个用法
            bqbq看 1 3 5         <- 带前缀的写法，保留兼容

        编号来自上一次搜索的预览图。支持：
            1
            1 3 5
            1-5
            1,3,5

        一次最多发 MAX_VIEW 张，避免刷屏。
        """
        # body 就是用户发的编号原文（parse_command 只在纯编号时才给
        # CMD_VIEW），不需要再剥任何前缀。
        args = body.strip()

        cached = self.client.get_cached(session_key(event))
        if not cached:
            yield event.plain_result(
                "还没有搜索结果。先发「bqbq 关键词」搜一下。"
            )
            return

        # 支持 "bqbq看 猫" 这种：先搜再看
        idxs = _parse_index_list(args)
        if not idxs:
            query, start, end = _parse_range(args)
            inc_groups, exc = parse_search_syntax(query)
            if not inc_groups and not exc:
                yield event.plain_result(
                    "用法：bqbq看 编号\n"
                    "例如：bqbq看 1 3 5   或   bqbq看 1-5\n"
                    "（编号来自预览图）"
                )
                return
            results, _ = await self.client.search(
                http, include=[], exclude=exc, or_groups=inc_groups
            )
            if not results:
                yield event.plain_result(f"没有找到匹配「{query}」的表情包。")
                return
            self.client.cache_results(session_key(event), results)
            cached = results
            idxs = list(range(start + 1, min(end, start + MAX_VIEW) + 1))

        picked = [(n, cached[n - 1]) for n in idxs[:MAX_VIEW] if 1 <= n <= len(cached)]
        if not picked:
            yield event.plain_result(
                f"编号超出范围（上次共 {len(cached)} 张）。"
            )
            return

        sent = 0
        for n, item in picked:
            data = await self.client.fetch_image(http, item.get("filename") or "")
            if not data:
                continue
            sent += 1
            async for r in self._send_bytes(event, data, n):
                yield r

        if sent == 0:
            yield event.plain_result("这些图片都没取到，可能已被删除或后端不可用。")

    async def _send_bytes(self, event: AstrMessageEvent, data: bytes, n: int):
        """把图片字节落盘后作为回复发出。

        【为什么不能直接传 bytes】
        `event.image_result(url_or_path)` 只接受**路径或 URL 字符串**
        （astr_message_event.py:408）。传 bytes 进去，框架会在
        `url.startswith(...)` 处抛 "startswith first arg must be bytes"。
        （实测踩到过：拼图成功但发送时报这个错。）

        所以先写临时文件，再传路径。
        """
        path = _save_temp(data, f"bqbq_{n}")
        if not path:
            return
        yield event.image_result(path)

    # ------------------------------------------------------------------
    # 标签管理（需引用图片）
    # ------------------------------------------------------------------
    async def _cmd_tag(
        self, event: AstrMessageEvent, http, body: str, first: str, kind: str = ""
    ):
        """标签管理：add（追加）/ tag（覆盖）/ del（删除 / 进回收站）。

        四种语义：
            bqbq add 猫 可爱   原标签 + 猫 + 可爱（保序去重）
            bqbq tag 猫 可爱   直接变成 猫 可爱（**原有标签全部丢弃**）
            bqbq tag           清空全部标签
            bqbq del 猫        从标签里删掉「猫」
            bqbq del           整张图打上 trash_bin（进回收站）

        【实现要点：后端 update_tags 本就是覆盖】
            UPDATE images SET tags = ? WHERE md5 = ?
        所以 tag 只需把用户给的标签直接传过去，不需要任何额外处理。
        真正需要客户端干预的只有两处：
            · add  要把原标签一起带上（否则就变成覆盖了）
            · 去重 后端不查重（实测传 [猫,猫,狗] 原样存回）

        Args:
            kind: parse_command 解析出的命令类型（CMD_ADD/TAG/DEL）。
        """
        # kind 由 parse_command 判定（已考虑引用图片），这里直接用。
        is_overwrite = kind == CMD_TAG
        is_add = kind == CMD_ADD
        is_del = kind == CMD_DEL

        # ---- 取被引用的图片 ----
        images = self._reply_images(event)
        if not images:
            hint = (
                "例如：引用图片后发「bqbq tag 猫 可爱」" if is_overwrite
                else "例如：引用图片后发「bqbq add 猫 可爱」" if is_add
                else "例如：引用图片后发「bqbq del 猫」（删标签）\n"
                     "      或「bqbq del」（整张图移入回收站）"
            )
            yield event.plain_result(
                "请先**引用一张图片**，再发这条命令。\n" + hint
            )
            return

        data = await self._fetch_component(images[0], http)
        if not data:
            # 【诊断信息】以前只提示「读取失败」，看不出是框架没本地化、
            # 还是路径不对。这里把组件三个字段打出来，便于定位。
            comp = images[0]
            yield event.plain_result(
                "读取被引用的图片失败。\n"
                f"url = {getattr(comp, 'url', None)!r}\n"
                f"file = {getattr(comp, 'file', None)!r}\n"
                f"path = {getattr(comp, 'path', None)!r}\n"
                "\n"
                "通常是框架没有把图片下载到本地。可以试试：\n"
                "  1. 重新发一张图，再引用它\n"
                "  2. 把上面三行发给维护者"
            )
            return

        import hashlib

        md5 = hashlib.md5(data).hexdigest()

        # ---- 取当前标签 ----
        #
        # 【为什么要 tags_exact】后端 check-md5 已返回 tags，正常恒为 True。
        # 但接口异常/超时/后端未升级时会是 False ——
        # 此时**绝不能**用空列表覆盖写回，那会把原有标签清空。
        # 实测踩过这个坑（当时后端没这个字段，加标签时把原标签清掉了）。
        exists, filename, old_tags, tags_exact = await self.client.check_md5_ex(http, md5)
        if not exists:
            # 不在库里就顺手上传
            res = await self.client.upload(http, data)
            if not (res.get("success") or res.get("exists")):
                yield event.plain_result(
                    f"上传失败：{res.get('msg') or '未知错误'}"
                )
                return
            exists, filename, old_tags, tags_exact = await self.client.check_md5_ex(
                http, md5
            )
            if not exists:
                yield event.plain_result("上传后仍查不到该图，请检查后端。")
                return

        if not tags_exact:
            yield event.plain_result(
                "⚠️ 读取标签失败（接口异常或后端未更新），"
                "本次**不做修改**以免覆盖已有标签。\n"
                "请检查后端版本，或直接在网页端改标签。"
            )
            return

        # ---- 计算最终标签 ----
        new_words = body[len(first):].split()

        if is_del:
            # del 有两种用法：
            #   bqbq del 猫 狗   从标签里**删掉**这些标签
            #   bqbq del         整张图标记 trash_bin（移入回收站）
            if new_words:
                # ---- 按名删除标签 ----
                #
                # 【为什么允许删 trash_bin】那正好是"从回收站恢复"的操作。
                # 早期版本单独拦了它（担心语义矛盾），但那反而多一条要记的规则：
                # 用户只需知道「del 是删标签，不带参数才是进回收站」这一条就够。
                #   bqbq del trash_bin   -> 从回收站捞出来（恢复）
                #   bqbq del             -> 移入回收站
                removed = [w for w in dedup(new_words) if w in old_tags]
                if not removed:
                    yield event.plain_result(
                        f"这些标签不存在：{'、'.join(dedup(new_words))}\n"
                        f"当前标签：{'、'.join(old_tags) if old_tags else '（空）'}"
                    )
                    return
                final = [t for t in old_tags if t not in removed]
                kept_trash = False
                action = "删除"          # 模板是「标签已{action} ✅」
                detail = "已删除：" + "、".join(removed)
            else:
                # ---- 标记回收站 ----
                if TRASH_TAG in old_tags:
                    yield event.plain_result("这张图已经在回收站里了。")
                    return
                final = old_tags + [TRASH_TAG]
                kept_trash = False
                action = "移入回收站"
                detail = ""

        elif is_overwrite:
            # 覆盖 = 用户给的为准（不带标签即清空）
            final = dedup(new_words)
            # 保护：图在回收站时保留 trash_bin，否则一次覆盖就把图"复活"了
            kept_trash = TRASH_TAG in old_tags and TRASH_TAG not in final
            if kept_trash:
                final.append(TRASH_TAG)
            action = "清空" if not new_words else "覆盖"
            detail = ""

        else:
            # 追加 = 原标签 + 新标签
            if not new_words:
                yield event.plain_result("请提供标签。例如：bqbq add 猫 可爱")
                return
            final = dedup(old_tags + new_words)
            kept_trash = False
            action = "追加"
            detail = ""

        # ---- 写回（三种模式共用一次调用）----
        ok, msg = await self.client.update_tags(http, md5, final)
        if not ok:
            yield event.plain_result(f"操作失败：{msg}")
            return

        # ---- 回复 ----
        if action == "移入回收站":
            yield event.plain_result("已移入回收站 🗑")
            return

        lines = [
            f"标签已{action} ✅",
            f"原标签：{'、'.join(old_tags) if old_tags else '（空）'}",
            f"新标签：{'、'.join(final) if final else '（空）'}",
        ]
        if detail:
            lines.append(detail)
        if kept_trash:
            lines.append("提示：这张图在回收站里，已自动保留 trash_bin 标签。")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _reply_images(event: AstrMessageEvent) -> list:
        """取出**引用**（Reply 组件）里的 Image。

        【框架结构】用户"引用"某条消息后，框架把它包成 `Reply` 组件
        （components.py:584），被引用的内容放在 `Reply.chain` 里：

            引用一张图片 -> Reply.chain = [Plain, Image]
            引用一段文字 -> Reply.chain = [Plain]        # 没有 Image
            引用合并转发 -> Reply.chain = [Plain]        # 文本里含图片 URL

        【为什么读 chain 而不是顶层】顶层 `event.get_messages()` 只有
        `[Reply, Plain]`，图片在 Reply 内部。漏掉 chain 就取不到图。

        【框架已本地化】preprocess_stage 会把引用里的图片下载到本地，
        并覆盖组件的 url/file/path（见 main.py 的详细说明），
        所以 `_read_local()` 直接读文件即可，不必走网络。

        Returns:
            Image 组件列表；没有引用或引用里没图时返回空列表。
        """
        out = []
        for c in event.get_messages():
            if type(c).__name__ != "Reply":
                continue
            for sub in (getattr(c, "chain", None) or []):
                if type(sub).__name__ == "Image":
                    out.append(sub)
        return out

    @staticmethod
    def _read_local(comp) -> bytes | None:
        """从 Image 组件读本地文件字节（只碰本地，不发网络请求）。"""
        from pathlib import Path

        for field in ("path", "file", "url"):
            v = getattr(comp, field, None)
            if not isinstance(v, str):
                continue
            v = v.strip()
            if v.startswith(("http://", "https://", "base64://", "data:")):
                continue
            try:
                if v and Path(v).is_file():
                    return Path(v).read_bytes()
            except Exception:
                continue
        return None

    @staticmethod
    async def _fetch_component(comp, http) -> bytes | None:
        """读取 Image 组件的字节：先本地，再网络 / base64 兜底。

        【为什么要兜底】框架通常会把图片下载到 data/temp 并覆盖组件的
        url/file/path，此时 _read_local 直接就能读到。但实测有些情况
        （引用历史消息、临时文件被清理）组件里仍是原始 http 地址，
        此时只读本地会直接失败 —— 表现为「add 不好使」。

        这里补两条路：
            http(s)://   用传入的 httpx 客户端下载
            base64://    直接解码

        Args:
            comp: Image 组件。
            http: 复用的 httpx.AsyncClient。

        Returns:
            图片字节；全部失败返回 None。
        """
        import base64

        # 【必须带类名前缀】_read_local 是同一个类的 staticmethod，
        # 在方法体内直接写 _read_local(comp) 找不到这个名字（类体不是
        # 作用域），会抛 NameError: name '_read_local' is not defined。
        # 这个异常会一路冒到 main.py 的 on_message，表现为
        # 「[BQBQ] 处理消息异常: name '_read_local' is not defined」，
        # 而 add/tag/del 全部无响应。
        data = LibCommands._read_local(comp)
        if data:
            return data

        for field in ("url", "file", "path"):
            v = getattr(comp, field, None)
            if not isinstance(v, str):
                continue
            v = v.strip()

            if v.startswith("base64://"):
                try:
                    return base64.b64decode(v[len("base64://"):])
                except Exception:
                    continue

            if v.startswith(("http://", "https://")) and http is not None:
                try:
                    r = await http.get(v)
                    if r.status_code == 200 and r.content:
                        return r.content
                except Exception:
                    continue

        return None


# ---------------------------------------------------------------------------
# 临时文件
# ---------------------------------------------------------------------------
_TEMP_DIR = None


def _save_temp(data: bytes, stem: str) -> str | None:
    """把图片字节写入临时文件，返回绝对路径。

    【为什么需要】`event.image_result()` 只接受路径/URL，
    不接受 bytes（见 _send_bytes 的说明）。

    文件放在插件 data 目录下的 temp 子目录，用 md5 命名避免重复写。
    """
    global _TEMP_DIR
    import hashlib
    from pathlib import Path

    try:
        if _TEMP_DIR is None:
            # 与 main.py 用同一个 data 目录约定
            base = Path(__file__).resolve().parent / "temp"
            base.mkdir(parents=True, exist_ok=True)
            _TEMP_DIR = base
        h = hashlib.md5(data).hexdigest()
        # 按内容探测扩展名，避免文件名与内容不符（我们刚修过这类问题）
        ext = ".jpg"
        if data[:3] == b"\xff\xd8\xff":
            ext = ".jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            ext = ".gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        p = _TEMP_DIR / f"{stem}_{h[:12]}{ext}"
        if not p.is_file():
            p.write_bytes(data)
        return str(p)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[BQBQ] 写临时图片失败: {e}")
        return None
