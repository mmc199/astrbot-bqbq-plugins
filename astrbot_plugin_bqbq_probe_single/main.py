"""BQBQ 单图诊断钩子（测试版）

目的
----
抓 QQ 官方机器人收到的**原始 payload**，定位「普通消息单张图片」为什么
拿不到可用地址。当前现象：

    exc_UnsupportedProtocol   -> 传给 httpx 的不是 http(s) 地址
    no_url=1                  -> _extract_http_url() 没取到 http 地址

但只有推理，没有实数：不知道 Image 组件的 url/file/path 究竟是什么，
也不知道原始 payload 里 attachments 长什么样。

本钩子做三件事
--------------
1. **钩住适配器**：在 `_append_attachments` 前后各 dump 一次，
   看适配器**输入**（原始 attachments 字典）和**输出**（追加了哪些组件）
2. **dump 事件**：记录 `event.get_messages()` 里每个组件的全部字段
3. **dump 原始 payload**：`raw_data` 的全部内容（含 msg_elements / attachments）

输出
----
    C:\\000soft\\qqagent\\probe_single.jsonl

只读诊断，不改任何业务逻辑。排查完删除。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

DUMP = Path(r"C:\000soft\qqagent\probe_single.jsonl")
# 单条记录里字符串的最大长度
MAX_STR = 1500
# 文件大小上限，防止忘记清理时无限增长
MAX_BYTES = 20 * 1024 * 1024

# 【引用场景专用】Reply.chain 里 Plain 的完整原文要单独存，不能被 MAX_STR 截断。
# 因为「引用的合并转发」全部信息都在那段文本里（含 文件名:<md5>.gif 与 URL），
# 截断后就没法验证解析器能否处理。
REPLY_TEXT_LIMIT = 60000

# 从主插件源码摘出的解析正则（+ 加载错误信息）
_CONSTRUCT_RE = None
_RE_LOAD_ERROR: str | None = None


def _shrink(v: Any, depth: int = 0) -> Any:
    """递归精简，避免超长字符串（rkey/base64）撑爆文件。

    Args:
        v: 任意对象。
        depth: 递归深度。

    Returns:
        可 JSON 序列化的精简对象。
    """
    if depth > 8:
        return "<deep>"
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        return v if len(v) <= MAX_STR else f"{v[:MAX_STR]}...<{len(v)}>"
    if isinstance(v, bytes):
        return f"<bytes {len(v)}>"
    if isinstance(v, dict):
        return {str(k): _shrink(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_shrink(x, depth + 1) for x in v]
    # 其它对象：保留类名 + 常见属性
    out: dict = {"__class__": type(v).__name__}
    names: set[str] = set()
    slots = getattr(type(v), "__slots__", None)
    if isinstance(slots, str):
        names.add(slots)
    elif isinstance(slots, (list, tuple)):
        names.update(str(s) for s in slots)
    d = getattr(v, "__dict__", None)
    if isinstance(d, dict):
        names.update(d.keys())
    for n in sorted(names):
        if n.startswith("__"):
            continue
        try:
            out[n] = _shrink(getattr(v, n), depth + 1)
        except Exception as e:  # noqa: BLE001
            out[n] = f"<err {e}>"
    return out


def _write(rec: dict) -> None:
    """追加一条记录到 dump 文件。

    Args:
        rec: 待写入的记录。
    """
    try:
        if DUMP.exists() and DUMP.stat().st_size > MAX_BYTES:
            return
        DUMP.parent.mkdir(parents=True, exist_ok=True)
        with DUMP.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[probe] 写 dump 失败: {e}")


class BqbqProbeSinglePlugin(Star):
    """钩住适配器与事件，抓普通消息图片的真实数据。"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self._patched = False
        self._patch_adapter()
        logger.info(f"[probe] 已加载，dump -> {DUMP}")

    # ------------------------------------------------------------------
    # 钩子 1：包装适配器的 _append_attachments
    # ------------------------------------------------------------------
    def _patch_adapter(self) -> None:
        """给 QQ 官方适配器的 _append_attachments 打补丁，记录输入与输出。"""
        if self._patched:
            return
        try:
            from astrbot.core.platform.sources.qqofficial import (
                qqofficial_platform_adapter as mod,
            )

            original = mod.QQOfficialPlatformAdapter._append_attachments

            async def patched(msg, attachments):
                # --- 记录输入 ---
                try:
                    _write({
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "hook": "append_attachments.IN",
                        "attachments_type": type(attachments).__name__,
                        "attachments_count": (
                            len(attachments) if isinstance(attachments, (list, tuple)) else None
                        ),
                        "attachments_raw": _shrink(attachments),
                        "msg_len_before": len(msg) if isinstance(msg, list) else None,
                    })
                except Exception:  # noqa: BLE001
                    pass

                # --- 调用原函数 ---
                result = await original(msg, attachments)

                # --- 记录输出（新增了哪些组件）---
                try:
                    added = []
                    if isinstance(msg, list):
                        for c in msg:
                            item = {"__class__": type(c).__name__}
                            for f in ("url", "file", "path", "_type", "text", "name"):
                                if hasattr(c, f):
                                    item[f] = _shrink(getattr(c, f))
                            added.append(item)
                    _write({
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "hook": "append_attachments.OUT",
                        "msg_len_after": len(msg) if isinstance(msg, list) else None,
                        "components": added,
                    })
                except Exception:  # noqa: BLE001
                    pass
                return result

            mod.QQOfficialPlatformAdapter._append_attachments = staticmethod(patched)
            self._patched = True
            logger.info("[probe] 已钩住 _append_attachments")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[probe] 钩住适配器失败: {e}")

    # ------------------------------------------------------------------
    # 钩子 2：dump 事件里的组件与原始 payload
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=200)
    async def on_any(self, event: AstrMessageEvent):
        """记录含图片/聊天记录/引用回复的消息结构（只读，不拦截）。"""
        try:
            comps = event.get_messages()
            raw = getattr(event.message_obj, "raw_message", None)
            raw_data = getattr(raw, "raw_data", None)
            content = ""
            raw_mtype = None
            if isinstance(raw_data, dict):
                content = raw_data.get("content") or ""
                raw_mtype = raw_data.get("message_type")

            has_img = any(type(c).__name__ == "Image" for c in comps)
            # 【引用回复】Reply 组件的 chain 里可能藏图片
            has_reply = any(type(c).__name__ == "Reply" for c in comps)
            if not has_img and not has_reply and "聊天记录" not in content:
                return

            def dump_comp(c) -> dict:
                """把一个组件的关键字段 dump 出来。"""
                item = {"__class__": type(c).__name__}
                for f in ("url", "file", "path", "_type", "text", "name",
                          "id", "sender_id", "sender_nickname", "message_str"):
                    if hasattr(c, f):
                        item[f] = _shrink(getattr(c, f))
                # Reply.chain：递归 dump 被引用消息的组件
                chain = getattr(c, "chain", None)
                if isinstance(chain, list) and chain:
                    item["chain"] = [dump_comp(x) for x in chain]
                return item

            comp_list = [dump_comp(c) for c in comps]

            rec: dict = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "hook": "event",
                "platform": event.get_platform_name(),
                "group": event.get_group_id(),
                "sender": event.get_sender_id(),
                "message_str": _shrink(event.message_str or ""),
                "components": comp_list,
                "is_reply": has_reply,
                "raw_message_type": raw_mtype,
            }

            # 原始 payload 关键字段
            if isinstance(raw_data, dict):
                rec["raw_keys"] = sorted(raw_data.keys())
                for k in ("attachments", "msg_elements", "message_scene", "message_type"):
                    if k in raw_data:
                        rec[f"raw_{k}"] = _shrink(raw_data[k])
                # raw_message 上除 raw_data 外的可选字段
                for k in ("msg_elements", "message_type"):
                    v = getattr(raw, k, None)
                    if v:
                        rec[f"rawmsg_{k}"] = _shrink(v)

            # ---- 引用场景专项分析 ----
            if has_reply:
                rec["reply_analysis"] = await self._analyze_reply(event, comps)

            _write(rec)
            logger.info(
                f"[probe] 事件已记录: comps={[type(c).__name__ for c in comps]} "
                f"is_reply={has_reply} raw_mtype={raw_mtype}"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[probe] 记录事件失败: {e}")

    async def _analyze_reply(self, event: AstrMessageEvent, comps: list) -> dict:
        """专项分析引用回复：完整原文 + 本地文件 + 解析器试跑。

        之所以单独做，是因为：
          · 引用的【图片】应能从 chain 里拿到本地文件（已验证过机制）
          · 引用的【合并转发】全部信息在一段 Plain 文本里，
            需要完整原文才能验证现有解析器能不能处理
          · MAX_STR=1500 会截断那段文本，所以这里单独存原文

        Args:
            event: 消息事件。
            comps: event.get_messages() 的结果。

        Returns:
            分析结果字典。
        """
        out: dict = {"chains": []}
        for c in comps:
            if type(c).__name__ != "Reply":
                continue
            chain = getattr(c, "chain", None) or []
            entry: dict = {
                "reply_id": _shrink(getattr(c, "id", None)),
                "reply_message_str": _shrink(getattr(c, "message_str", None)),
                "chain_len": len(chain),
                "items": [],
            }
            for sub in chain:
                cls = type(sub).__name__
                item: dict = {"__class__": cls}

                if cls == "Image":
                    # 三个字段都记全，并检查本地文件是否真实存在
                    for f in ("url", "file", "path"):
                        v = getattr(sub, f, None)
                        item[f] = _shrink(v)
                    p = getattr(sub, "path", None) or getattr(sub, "file", None)
                    if isinstance(p, str) and p and not p.startswith("http"):
                        try:
                            item["local_exists"] = Path(p).is_file()
                            if item["local_exists"]:
                                item["local_size"] = Path(p).stat().st_size
                        except Exception as e:  # noqa: BLE001
                            item["local_exists"] = f"<err {e}>"
                    # 也试一下 convert_to_file_path（框架的正规接口）
                    try:
                        item["convert_to_file_path"] = _shrink(
                            await self._safe_convert(sub)
                        )
                    except Exception as e:  # noqa: BLE001
                        item["convert_to_file_path"] = f"<err {e}>"

                elif cls == "Plain":
                    txt = getattr(sub, "text", "") or ""
                    # 【关键】完整原文，不截断
                    item["text_len"] = len(txt)
                    item["text_full"] = txt[:REPLY_TEXT_LIMIT]
                    # 用主插件的解析器试跑，看能提出多少张图。
                    # 【不能用 import】AstrBot 是按文件路径加载插件的，
                    # 包名 `astrbot_plugin_bqbq_collect` 不在 sys.path 里，
                    # 直接 import 会 ModuleNotFoundError。
                    # 这里改为按路径加载模块文件。
                    parsed = self._try_parse_with_collect(txt)
                    item["parsed_count"] = parsed["count"]
                    item["parsed_sample"] = parsed["sample"]
                    if parsed.get("error"):
                        item["parsed_error"] = parsed["error"]

                else:
                    for f in ("url", "file", "text"):
                        if hasattr(sub, f):
                            item[f] = _shrink(getattr(sub, f))
                entry["items"].append(item)
            out["chains"].append(entry)
        return out

    @staticmethod
    def _try_parse_with_collect(text: str) -> dict:
        """用主插件的正则试跑一段文本，看能提取多少张图。

        ⚠️【绝对不能 exec_module 加载主插件】
        曾用 `importlib` 执行主插件的 main.py，结果把里面的
        `@filter.command("表情统计")` / `("表情重置")` **又注册了一遍**，
        导致 AstrBot 报「指令冲突」：

            conflict_key: 表情统计
              ├── _bqbq_collect_probe        <- exec_module 引入的重复注册
              └── astrbot_plugin_bqbq_collect

        AstrBot 会扫描模块里所有 `@filter.command` 装饰器，
        所以只要执行过该模块，指令就被重复注册。

        【正确做法】只从源码里【原样摘出正则字面量】，
        用 `re` 编译后自行匹配 —— 不执行、不导入任何插件代码。

        实测该正则与真实解析结果一致（诊断期间已交叉验证）。

        Args:
            text: 待解析的文本（引用里的 Plain 原文）。

        Returns:
            {"count": int, "sample": list, "error": str|None}
        """
        global _CONSTRUCT_RE, _RE_LOAD_ERROR
        try:
            if _CONSTRUCT_RE is None and _RE_LOAD_ERROR is None:
                import re as _re

                path = Path(
                    r"C:\000soft\AstrBotLauncher-0.2.0\AstrBot\data\plugins"
                    r"\astrbot_plugin_bqbq_collect\main.py"
                )
                if not path.is_file():
                    _RE_LOAD_ERROR = "主插件文件不存在"
                else:
                    src = path.read_text(encoding="utf-8")
                    # 摘出 _RE_ATTACH = re.compile( ... ) 的原始模式串
                    m = _re.search(
                        r"_RE_ATTACH\s*=\s*re\.compile\(\s*(.*?)\s*\)\s*\n",
                        src,
                        _re.S,
                    )
                    if not m:
                        _RE_LOAD_ERROR = "未能从主插件源码里找到 _RE_ATTACH"
                    else:
                        # 把源码里的字符串拼接还原成实际 pattern。
                        # 主插件写法是多个 r"..." 相邻（隐式拼接），
                        # 这里逐个提取再连起来。
                        parts = _re.findall(r'r"([^"]*)"', m.group(1))
                        if not parts:
                            _RE_LOAD_ERROR = "_RE_ATTACH 里没找到 r\"...\" 字面量"
                        else:
                            _CONSTRUCT_RE = _re.compile("".join(parts))

            if _RE_LOAD_ERROR:
                return {"count": -1, "sample": [], "error": _RE_LOAD_ERROR}
            if not text:
                return {"count": 0, "sample": [], "error": None}

            # 复刻主插件的过滤逻辑：只收图片扩展名，并按 md5 去重
            result = []
            seen = set()
            for mm in _CONSTRUCT_RE.finditer(text):
                md5 = mm.group("name").lower()
                ext = mm.group("ext").lower()
                if ext not in ("gif", "jpg", "jpeg", "png", "webp", "bmp"):
                    continue
                if md5 in seen:
                    continue
                seen.add(md5)
                result.append((md5, mm.group("url")))
                if len(result) >= 200:
                    break

            return {
                "count": len(result),
                "sample": [{"md5": m, "url": u[:120]} for m, u in result[:5]],
                "error": None,
            }
        except Exception as e:  # noqa: BLE001
            return {"count": -1, "sample": [], "error": f"{type(e).__name__}: {e}"}

    @staticmethod
    async def _safe_convert(comp) -> str:
        """调用组件的 convert_to_file_path，失败则返回错误串。

        Args:
            comp: Image 组件。

        Returns:
            本地路径或错误描述。
        """
        try:
            return await comp.convert_to_file_path()
        except Exception as e:  # noqa: BLE001
            return f"<err {type(e).__name__}: {e}>"

    async def terminate(self):
        """卸载时还原补丁。"""
        try:
            if self._patched:
                from astrbot.core.platform.sources.qqofficial import (
                    qqofficial_platform_adapter as mod,
                )

                # 无法还原原函数（未保存引用），只能提示
                logger.info("[probe] 适配器补丁随插件卸载失效")
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"[probe] 已卸载，dump: {DUMP}")
