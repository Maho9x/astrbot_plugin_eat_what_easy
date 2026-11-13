"""今天吃什么 / 喝什么 —— 移植自 Koishi 插件 koishi-plugin-eat-what-pro。

功能与上游保持一致：
  * 关键词触发（默认「吃什么」「喝什么」）或指令 `/eat` `/drink`
  * 每个用户 1 小时内不重复（历史按用户分别记录 foods / drinks）
  * 可选启用额外图库 extra_foods / extra_drinks
  * 支持自定义时间关键词，按「前缀 + 长度降序」匹配（中午吃什么 → 中午）
  * 回复格式：[@用户] + 空格 + "{时间词}吃{菜名}喵！" + 图片
"""

from __future__ import annotations

import os
import random
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

PLUGIN_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PLUGIN_DIR / "assets"

DEFAULT_TIME_KEYWORDS = [
    "今天",
    "早上",
    "中午",
    "下午",
    "晚上",
    "夜宵",
    "明天",
    "明天早上",
    "明天中午",
    "明天下午",
    "明天晚上",
]

HISTORY_TTL_MS = 3600 * 1000  # 1 小时

# 额外图库（彩蛋）按更低权重抽取：普通图 3 : 彩蛋图 1
NORMAL_WEIGHT = 3
EXTRA_WEIGHT = 1

# 默认回复模板（与移植前的固定文案等价）
DEFAULT_TEMPLATE = "{{time}}吃{{dishes}}喵！"
IMAGE_MARKER = "{{image}}"

TYPE_LABEL = {"foods": "食物", "drinks": "饮料"}

# 零宽空格：respond 阶段会丢弃「strip() 后为空」的 Plain 段，用它保住 @ 那条消息
ZERO_WIDTH = "\u200b"


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _is_qq_official(event) -> bool:
    """是否运行在 QQ 官方机器人适配器上。

    注意：get_platform_id() 返回的是平台配置里的 id（用户可自定义，如「QQ官方bot」），
    get_platform_name() 才是适配器名（qq_official）。所以这里三处都看，任一命中即可。
    """
    candidates: list[str] = []
    for attr in ("get_platform_name", "get_platform_id"):
        try:
            getter = getattr(event, attr, None)
            if callable(getter):
                candidates.append(str(getter() or ""))
        except Exception:  # noqa: BLE001
            pass

    meta = getattr(event, "platform_meta", None)
    if meta is not None:
        for attr in ("name", "description"):
            try:
                candidates.append(str(getattr(meta, attr, "") or ""))
            except Exception:  # noqa: BLE001
                pass

    for value in candidates:
        low = value.strip().lower()
        if "qq_official" in low or "qqofficial" in low or "机器人官方" in value:
            return True
    return False

@register(
    "astrbot_plugin_eat_what_easy",
    "Maho9x",
    "今天吃什么/喝什么，随机返回菜品图片",
    "1.1.0",
)
class EatWhatPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # userId -> {"foods": [...], "drinks": [...], "timestamp": int}
        self._history: dict[str, dict] = {}

    # ---------------- 配置 ----------------
    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default) if hasattr(self.config, "get") else default
        return default if value is None else value

    def _keywords(self, key: str, default: list[str]) -> list[str]:
        raw = self._cfg(key, default)
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item).strip()]
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
        return list(default)

    @property
    def foods_keywords(self) -> list[str]:
        return self._keywords("foodsKeyword", ["吃什么"])

    @property
    def drinks_keywords(self) -> list[str]:
        return self._keywords("drinksKeyword", ["喝什么"])

    @property
    def time_keywords(self) -> list[str]:
        keywords = self._keywords("timeKeywords", DEFAULT_TIME_KEYWORDS)
        return keywords or list(DEFAULT_TIME_KEYWORDS)

    def _default_time_word(self) -> str:
        return str(self._cfg("defaultTimeWord", "今天") or "今天")

    def _extract_time_word(self, text: str) -> str:
        # 与上游一致：按长度降序，避免「明天」抢先于「明天中午」
        for word in sorted(self.time_keywords, key=len, reverse=True):
            if word and text.startswith(word):
                return word
        return self._default_time_word()

    # ---------------- 图库 ----------------
    def _collect_files(self, kind: str) -> list[str]:
        """返回形如 "foods/xxx.jpg" / "extra_foods/xxx.jpg" 的相对路径列表。"""
        base = ASSETS_DIR / kind
        files: list[str] = []
        if base.is_dir():
            files.extend(f"{kind}/{name}" for name in os.listdir(base))

        extra_enabled = _as_bool(self._cfg("useExtraFoods", False)) if kind == "foods" else _as_bool(
            self._cfg("useExtraDrinks", False)
        )
        if extra_enabled:
            extra_dir = ASSETS_DIR / f"extra_{kind}"
            if extra_dir.is_dir():
                files.extend(f"extra_{kind}/{name}" for name in os.listdir(extra_dir))
        return files

    def _pick(self, kind: str, user_id: str) -> str | None:
        file_list = self._collect_files(kind)
        if not file_list:
            return None

        now = int(time.time() * 1000)
        record = self._history.get(user_id)
        if record is None or now - record.get("timestamp", 0) > HISTORY_TTL_MS:
            record = {"foods": [], "drinks": [], "timestamp": now}
            self._history[user_id] = record

        available = [item for item in file_list if item not in record[kind]]
        if not available:
            record[kind] = []
            available = file_list

        weights = [EXTRA_WEIGHT if item.startswith("extra_") else NORMAL_WEIGHT for item in available]
        picked = random.choices(available, weights=weights, k=1)[0]
        record[kind].append(picked)
        return picked

    # ---------------- 消息构造 ----------------
    def _template(self, kind: str) -> str:
        key = "foodsTemplate" if kind == "foods" else "drinksTemplate"
        template = str(self._cfg(key, DEFAULT_TEMPLATE) or "").strip()
        return template or DEFAULT_TEMPLATE

    @staticmethod
    def _render_template(template: str, time_word: str, dishes: str) -> tuple[str, str]:
        """把模板渲染成 (图片前文字, 图片后文字)。

        {{time}}   → 时间词（如「中午」）
        {{dishes}} → 菜名（图片文件名）
        {{image}}  → 图片位置标记；不写则图片附在文字最后
        """
        body = template.replace("{{time}}", time_word).replace("{{dishes}}", dishes)
        if IMAGE_MARKER in body:
            before, _, after = body.partition(IMAGE_MARKER)
            after = after.replace(IMAGE_MARKER, "")  # 多余的标记忽略
            return before.rstrip(), after.lstrip()
        return body, ""

    async def _build_results(self, event: AstrMessageEvent, kind: str, time_word: str):
        """产出 1~2 条消息。

        QQ 官方 bot 在消息带图片时会改用 msg_type=7（纯文本 content），At 序列化出的
        `<qqbot-at-user id="..." />` 不会被解析；纯 At 的消息链又会被 respond 阶段跳过。
        开启 `qq_official_optimize` 时，用 event.send() 直接发两条：
        先 markdown（msg_type=2）单独 @ 对方，再发「文案 + 图片」。
        """
        user_id = str(event.get_sender_id() or "")
        optimize = _as_bool(self._cfg("qq_official_optimize", False)) and _is_qq_official(event)
        picked = self._pick(kind, user_id)

        if picked is None:
            text = f"图库里没有找到任何{TYPE_LABEL.get(kind, '食物')}图片。"
            if optimize:
                try:
                    await event.send(MessageChain(chain=[At(qq=user_id), Plain(ZERO_WIDTH)]).use_markdown(True))
                    await event.send(MessageChain(chain=[Plain(text)]))
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"QQ 官方 bot 拆条发送失败，回退为单条：{exc}")
            yield event.chain_result([At(qq=user_id), Plain(text)])
            return

        dishes = os.path.basename(picked).split(".")[0]
        before, after = self._render_template(self._template(kind), time_word, dishes)

        content = []
        if before:
            content.append(Plain(" " + before))
        content.append(Image.fromFileSystem(str(ASSETS_DIR / picked)))
        if after:
            content.append(Plain(after))

        if optimize and user_id:
            try:
                await event.send(MessageChain(chain=[At(qq=user_id), Plain(ZERO_WIDTH)]).use_markdown(True))
                await event.send(MessageChain(chain=content))
                logger.info("QQ 官方 bot 优化：@ 与图文拆分为两条发送")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"QQ 官方 bot 拆条发送失败，回退为单条：{exc}")

        yield event.chain_result([At(qq=user_id), *content])

    # ---------------- 事件处理 ----------------
    @filter.command("eat")
    async def eat_command(self, event: AstrMessageEvent):
        if not _as_bool(self._cfg("useCommand", False)):
            return
        event.stop_event()
        async for result in self._build_results(event, "foods", self._default_time_word()):
            yield result

    @filter.command("drink")
    async def drink_command(self, event: AstrMessageEvent):
        if not _as_bool(self._cfg("useCommand", False)):
            return
        event.stop_event()
        async for result in self._build_results(event, "drinks", self._default_time_word()):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_handler(self, event: AstrMessageEvent):
        if not _as_bool(self._cfg("useKeyword", True), True):
            return

        text = (event.message_str or "").strip()
        if not text:
            return
        # 指令形式交给上面的指令处理器（避免 /eat 之类被关键词再触发一次）
        if text.lstrip("/／").split(" ")[0].strip().lower() in {"eat", "drink"}:
            return

        if any(kw in text for kw in self.foods_keywords):
            event.stop_event()
            async for result in self._build_results(event, "foods", self._extract_time_word(text)):
                yield result
        elif any(kw in text for kw in self.drinks_keywords):
            event.stop_event()
            async for result in self._build_results(event, "drinks", self._extract_time_word(text)):
                yield result

    async def terminate(self):
        self._history.clear()
        logger.info("今天吃什么插件已卸载")
