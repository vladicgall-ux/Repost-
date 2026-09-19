#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Гибридный репостер: userbot ЧИТАЕТ источник, бот ПУБЛИКУЕТ.

Зачем: userbot (Telethon) умеет вступать в приватный канал по ссылке-приглашению —
боту такое недоступно. Но публикует в ваш канал обычный бот через Bot API,
поэтому в целевом канале виден бот, а не ваш личный аккаунт: удобно, когда канал
ведёт команда.

Схема:
    приватный источник → Telethon (ваш аккаунт, только чтение)
                       → скачивание медиа в память
                       → Bot API sendPhoto/sendVideo/sendMediaGroup
                       → ваш канал (от имени бота)

Первый запуск (на своём компьютере — нужен ввод кода из Telegram):
    python userbot.py --login      # печатает SESSION_STRING

Постоянный запуск:
    python hybrid.py
"""

import asyncio
import html as html_lib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    Message,
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityItalic,
    MessageEntityMentionName,
    MessageEntityPre,
    MessageEntitySpoiler,
    MessageEntityStrike,
    MessageEntityTextUrl,
    MessageEntityUnderline,
)

# resolve_source умеет вступать по t.me/+хеш — переиспользуем, чтобы логика
# вступления жила в одном месте
from userbot import resolve_source

# --------------------------------------------------------------------------- #
#                                  КОНФИГ                                     #
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()          # публикует
API_ID = int(os.getenv("API_ID", "0"))                  # читает
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

SOURCE = os.getenv("SOURCE", "").strip()   # @channel, t.me/+хеш, t.me/c/<id>/<post>, -100...
TARGET = os.getenv("TARGET", "").strip()   # @username вашего канала или -100...

SEND_DELAY = float(os.getenv("SEND_DELAY", "2"))
ALBUM_WAIT = 2.0
MAX_UPLOAD = 45 * 1024 * 1024        # лимит загрузки через Bot API ~50 МБ
TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024

API_URL = "https://api.telegram.org/bot{token}/{method}"
STATE_FILE = Path(__file__).resolve().parent / "hybrid_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("hybrid")
logging.getLogger("telethon").setLevel(logging.WARNING)


def load_last_id() -> int:
    if STATE_FILE.exists():
        try:
            return int(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_id", 0))
        except (ValueError, OSError):
            log.error("Не читается %s", STATE_FILE.name)
    return 0


def save_last_id(value: int) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_id": value}), encoding="utf-8")
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- #
#                      РАЗМЕТКА: entities → HTML для Bot API                  #
# --------------------------------------------------------------------------- #

# Штатный html.unparse из Telethon не годится: он молча теряет спойлеры и
# выдаёт <tg-emoji>, который Bot API у обычных (непремиум) ботов отклоняет.
# Поэтому собираем HTML сами по offset'ам.

def _tags_for(entity: Any) -> Optional[Tuple[str, str]]:
    """Пара (открывающий, закрывающий) тег для сущности Telegram."""
    if isinstance(entity, MessageEntityBold):
        return "<b>", "</b>"
    if isinstance(entity, MessageEntityItalic):
        return "<i>", "</i>"
    if isinstance(entity, MessageEntityUnderline):
        return "<u>", "</u>"
    if isinstance(entity, MessageEntityStrike):
        return "<s>", "</s>"
    if isinstance(entity, MessageEntitySpoiler):
        return "<tg-spoiler>", "</tg-spoiler>"
    if isinstance(entity, MessageEntityCode):
        return "<code>", "</code>"
    if isinstance(entity, MessageEntityBlockquote):
        return "<blockquote>", "</blockquote>"
    if isinstance(entity, MessageEntityPre):
        lang = getattr(entity, "language", "") or ""
        if lang:
            return f'<pre><code class="language-{html_lib.escape(lang, quote=True)}">', "</code></pre>"
        return "<pre>", "</pre>"
    if isinstance(entity, MessageEntityTextUrl):
        return f'<a href="{html_lib.escape(entity.url, quote=True)}">', "</a>"
    if isinstance(entity, MessageEntityMentionName):
        return f'<a href="tg://user?id={entity.user_id}">', "</a>"
    # ссылки, хештеги, @упоминания, кастомные эмодзи Telegram размечает сам —
    # тегов не нужно, текст остаётся как есть
    return None


def entities_to_html(text: str, entities: Optional[List[Any]]) -> str:
    """
    Превращает текст + entities в HTML для Bot API.

    Offset'ы Telegram считаются в UTF-16, а не в символах Python, поэтому
    работаем на уровне 2-байтовых единиц: иначе эмодзи и любые символы вне BMP
    сдвигают всю разметку.
    """
    if not text:
        return ""
    raw = text.encode("utf-16-le")
    units = [raw[i:i + 2] for i in range(0, len(raw), 2)]

    opens: Dict[int, List[str]] = {}
    closes: Dict[int, List[str]] = {}
    for ent in entities or []:
        tags = _tags_for(ent)
        if not tags:
            continue
        start, end = ent.offset, ent.offset + ent.length
        if start < 0 or end > len(units) or start >= end:
            continue
        opens.setdefault(start, []).append(tags[0])
        closes.setdefault(end, []).insert(0, tags[1])   # закрываем в обратном порядке

    esc = {"<": "&lt;", ">": "&gt;", "&": "&amp;"}
    out: List[bytes] = []
    for idx in range(len(units) + 1):
        for tag in closes.get(idx, []):
            out.append(tag.encode("utf-16-le"))
        for tag in opens.get(idx, []):
            out.append(tag.encode("utf-16-le"))
        if idx < len(units):
            char = units[idx].decode("utf-16-le", errors="surrogatepass")
            out.append(esc[char].encode("utf-16-le") if char in esc else units[idx])
    return b"".join(out).decode("utf-16-le", errors="surrogatepass")


TAG_RE = __import__("re").compile(r"<[^>]+>")


def cut(text: str, limit: int) -> str:
    """Обрезает под лимит Telegram; при обрезке снимает разметку, чтобы не порвать тег."""
    if len(text) <= limit:
        return text
    plain = html_lib.escape(html_lib.unescape(TAG_RE.sub("", text)))
    return plain[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
#                              КЛИЕНТ BOT API                                 #
# --------------------------------------------------------------------------- #


class BotAPI:
    """Тонкая обёртка над HTTP-вызовами Bot API — публикует от имени бота."""

    def __init__(self, token: str, session: aiohttp.ClientSession) -> None:
        self.token = token
        self.session = session

    async def call(self, method: str, data: Dict[str, Any],
                   files: Optional[Dict[str, Tuple[str, bytes]]] = None,
                   tries: int = 3) -> Optional[Dict[str, Any]]:
        """Вызывает метод Bot API с обработкой FloodWait (429 + retry_after)."""
        url = API_URL.format(token=self.token, method=method)
        clean = {k: v for k, v in data.items() if v is not None}

        for attempt in range(1, tries + 1):
            if files:
                # с файлами — multipart, вложенные структуры уходят строкой JSON
                form = aiohttp.FormData()
                for key, value in clean.items():
                    form.add_field(key, value if isinstance(value, str) else json.dumps(value))
                for field, (filename, blob) in files.items():
                    form.add_field(field, blob, filename=filename,
                                   content_type="application/octet-stream")
                kwargs: Dict[str, Any] = {"data": form}
            else:
                # без файлов — обычный JSON: быстрее и без возни с типами
                kwargs = {"json": clean}

            try:
                async with self.session.post(
                    url, timeout=aiohttp.ClientTimeout(total=300), **kwargs
                ) as resp:
                    payload = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                log.error("Сеть при %s: %s (попытка %s/%s)", method, err, attempt, tries)
                await asyncio.sleep(2 * attempt)
                continue

            if payload.get("ok"):
                return payload.get("result")

            retry_after = (payload.get("parameters") or {}).get("retry_after")
            if retry_after:
                log.warning("FloodWait: жду %s сек", retry_after)
                await asyncio.sleep(retry_after + 1)
                continue

            log.error("Bot API %s: %s", method, payload.get("description"))
            return None
        return None


# --------------------------------------------------------------------------- #
#                        ОПРЕДЕЛЕНИЕ ТИПА МЕДИА                               #
# --------------------------------------------------------------------------- #


def media_kind(msg: Message) -> Optional[str]:
    """Какой метод Bot API подходит для этого сообщения."""
    if msg.photo:
        return "photo"
    if msg.video_note:
        return "video_note"
    if msg.gif:
        return "animation"
    if msg.video:
        return "video"
    if msg.voice:
        return "voice"
    if msg.audio:
        return "audio"
    if msg.sticker:
        return "sticker"
    if msg.document:
        return "document"
    return None


SEND_METHOD = {
    "photo": "sendPhoto", "video": "sendVideo", "animation": "sendAnimation",
    "voice": "sendVoice", "audio": "sendAudio", "document": "sendDocument",
    "sticker": "sendSticker", "video_note": "sendVideoNote",
}
DEFAULT_EXT = {
    "photo": ".jpg", "video": ".mp4", "animation": ".mp4", "voice": ".ogg",
    "audio": ".mp3", "document": ".bin", "sticker": ".webp", "video_note": ".mp4",
}


def file_name(msg: Message, kind: str) -> str:
    """Имя файла для загрузки: своё, если есть, иначе по типу."""
    name = getattr(msg.file, "name", None) if msg.file else None
    if name:
        return name
    ext = (getattr(msg.file, "ext", None) if msg.file else None) or DEFAULT_EXT.get(kind, ".bin")
    return f"{kind}_{msg.id}{ext}"


async def fetch_media(client: TelegramClient, msg: Message) -> Optional[bytes]:
    """Скачивает медиа в память; None — если слишком большое или не скачалось."""
    size = getattr(msg.file, "size", 0) if msg.file else 0
    if size and size > MAX_UPLOAD:
        log.error("Пост %s: файл %.1f МБ больше лимита Bot API — пропускаю медиа",
                  msg.id, size / 1048576)
        return None
    try:
        return await client.download_media(msg, file=bytes)
    except Exception as err:
        log.error("Не скачалось медиа поста %s: %s", msg.id, err)
        return None


# --------------------------------------------------------------------------- #
#                               ПУБЛИКАЦИЯ                                    #
# --------------------------------------------------------------------------- #


async def publish(client: TelegramClient, api: BotAPI, messages: List[Message]) -> None:
    """Скачивает пост аккаунтом и публикует его ботом."""
    messages = sorted((m for m in messages if m is not None), key=lambda m: m.id)
    if not messages:
        return

    await asyncio.sleep(SEND_DELAY)
    first = messages[0]
    text = entities_to_html(first.message or "", first.entities)

    # --- альбом ---------------------------------------------------------- #
    if len(messages) > 1:
        media_json: List[Dict[str, Any]] = []
        files: Dict[str, Tuple[str, bytes]] = {}
        for idx, msg in enumerate(messages[:10]):
            kind = media_kind(msg)
            if kind not in ("photo", "video", "document", "audio"):
                continue
            blob = await fetch_media(client, msg)
            if blob is None:
                continue
            field = f"file{idx}"
            files[field] = (file_name(msg, kind), blob)
            item: Dict[str, Any] = {"type": kind, "media": f"attach://{field}"}
            caption = entities_to_html(msg.message or "", msg.entities)
            if caption:
                item["caption"] = cut(caption, TG_CAPTION_LIMIT)
                item["parse_mode"] = "HTML"
            media_json.append(item)

        if len(media_json) > 1:
            ok = await api.call("sendMediaGroup",
                                {"chat_id": TARGET, "media": media_json}, files)
            if ok:
                save_last_id(messages[-1].id)
                log.info("Опубликован альбом %s", [m.id for m in messages])
                return
            log.error("Альбом %s не ушёл — отправляю по одному",
                      [m.id for m in messages])
        # альбом собрать не вышло — шлём части по отдельности
        for msg in messages:
            await publish(client, api, [msg])
        return

    # --- одиночный пост -------------------------------------------------- #
    kind = media_kind(first)
    if kind:
        blob = await fetch_media(client, first)
        if blob is not None:
            data: Dict[str, Any] = {"chat_id": TARGET}
            # у кружков и стикеров подписи нет — текст уйдёт отдельным сообщением
            if text and kind not in ("sticker", "video_note"):
                data["caption"] = cut(text, TG_CAPTION_LIMIT)
                data["parse_mode"] = "HTML"
            ok = await api.call(SEND_METHOD[kind], data,
                                {kind: (file_name(first, kind), blob)})
            if ok:
                # длинный текст в подпись не влез — досылаем отдельно
                if text and (len(text) > TG_CAPTION_LIMIT
                             or kind in ("sticker", "video_note")):
                    await api.call("sendMessage", {
                        "chat_id": TARGET, "text": cut(text, TG_TEXT_LIMIT),
                        "parse_mode": "HTML", "disable_web_page_preview": True})
                save_last_id(first.id)
                log.info("Опубликован пост %s (%s)", first.id, kind)
            return
        # медиа не досталось — хотя бы текст не теряем

    if text:
        ok = await api.call("sendMessage", {
            "chat_id": TARGET, "text": cut(text, TG_TEXT_LIMIT), "parse_mode": "HTML"})
        if ok:
            save_last_id(first.id)
            log.info("Опубликован пост %s (текст)", first.id)
    else:
        log.info("Пост %s пустой — пропускаю", first.id)


async def catch_up(client: TelegramClient, api: BotAPI, source) -> None:
    """Досылает посты, вышедшие пока скрипт был выключен."""
    last_id = load_last_id()
    if not last_id:
        log.info("Состояния нет — публикую только новые посты")
        return
    missed = [m async for m in client.iter_messages(source, min_id=last_id, limit=50)]
    if not missed:
        return
    log.info("Догоняю %s пропущенных постов", len(missed))

    groups: Dict[int, List[Message]] = {}
    singles: List[Message] = []
    for msg in reversed(missed):            # от старых к новым
        if msg.grouped_id:
            groups.setdefault(msg.grouped_id, []).append(msg)
        else:
            singles.append(msg)
    for msg in singles:
        await publish(client, api, [msg])
    for album in groups.values():
        await publish(client, api, album)


# --------------------------------------------------------------------------- #
#                                  ЗАПУСК                                     #
# --------------------------------------------------------------------------- #


async def check_target(api: BotAPI) -> bool:
    """Проверяет, что бот админ целевого канала и может публиковать."""
    me = await api.call("getMe", {})
    if not me:
        log.error("Неверный BOT_TOKEN")
        return False
    log.info("Публикует бот: @%s", me.get("username"))

    member = await api.call("getChatMember",
                            {"chat_id": TARGET, "user_id": str(me["id"])})
    if not member:
        log.error("Бот не видит канал %s — добавь его туда администратором", TARGET)
        return False
    if member.get("status") != "administrator":
        log.error("Бот в %s не администратор (статус: %s)", TARGET, member.get("status"))
        return False
    if not member.get("can_post_messages"):
        log.error("У бота нет права «Публикация сообщений» в %s", TARGET)
        return False
    return True


async def run() -> None:
    for name, value in (("BOT_TOKEN", BOT_TOKEN), ("API_ID", API_ID),
                        ("API_HASH", API_HASH), ("SESSION_STRING", SESSION_STRING),
                        ("SOURCE", SOURCE), ("TARGET", TARGET)):
        if not value:
            log.error("Не задана переменная окружения %s", name)
            sys.exit(1)

    async with aiohttp.ClientSession() as http:
        api = BotAPI(BOT_TOKEN, http)
        if not await check_target(api):
            sys.exit(1)

        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.start()
        me = await client.get_me()
        log.info("Читает аккаунт: %s (@%s)", me.first_name, me.username or me.id)

        try:
            source = await resolve_source(client, SOURCE)
        except Exception as err:
            log.error("Источник недоступен: %s", err)
            await client.disconnect()
            sys.exit(1)
        log.info("Репостинг: %s → %s (публикует бот)",
                 getattr(source, "title", source), TARGET)

        @client.on(events.Album(chats=source))
        async def on_album(event: events.Album.Event) -> None:
            await publish(client, api, list(event.messages))

        @client.on(events.NewMessage(chats=source))
        async def on_message(event: events.NewMessage.Event) -> None:
            if event.message.grouped_id:        # части альбома заберёт on_album
                return
            await publish(client, api, [event.message])

        await catch_up(client, api, source)
        log.info("Слушаю новые посты. Остановить — Ctrl+C")
        await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено")
