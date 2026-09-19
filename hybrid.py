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
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from telethon import TelegramClient, events, utils
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
from userbot import (
    _entity_with_cache,
    get_last_id,
    parse_sources,
    resolve_source,
    save_last_id,
)
# управление источниками прямо в чате с ботом: переслал пост — добавил канал,
# /remove — убрал
from control_bot import (
    build_router,
    load_config,
    load_sources,
    run_control_bot,
    save_config,
    save_sources,
)
from login_via_bot import read_saved_session

# --------------------------------------------------------------------------- #
#                                  КОНФИГ                                     #
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()          # публикует
API_ID = int(os.getenv("API_ID", "0"))                  # читает
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

# Источники обычно задаются в чате с ботом (пересланный пост) и хранятся в
# sources.json. SOURCES нужна только для первого запуска, чтобы не настраивать
# руками: несколько каналов через запятую, своя цель через "=>".
SOURCES = os.getenv("SOURCES", "") or os.getenv("SOURCE", "")
TARGET = os.getenv("TARGET", "").strip()   # цель по умолчанию

OWNER_ID = int(os.getenv("OWNER_ID", "0")) or None   # кому доверен мастер входа

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


# Публикуем по одному посту за раз, даже когда источников много: иначе
# несколько каналов разом упрутся в лимит Telegram на отправку.
send_lock = asyncio.Lock()


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


async def publish(client: TelegramClient, api: BotAPI, messages: List[Message],
                  target: str) -> None:
    """Скачивает пост аккаунтом и публикует его ботом в указанный канал."""
    messages = sorted((m for m in messages if m is not None), key=lambda m: m.id)
    if not messages:
        return
    async with send_lock:                    # очередь на публикацию общая
        await _publish_locked(client, api, messages, target)


async def _publish_locked(client: TelegramClient, api: BotAPI,
                          messages: List[Message], target: str) -> None:
    """Сама публикация — вызывается уже под send_lock."""
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
                                {"chat_id": target, "media": media_json}, files)
            if ok:
                save_last_id(STATE_FILE, messages[0].chat_id, messages[-1].id)
                log.info("Опубликован альбом %s", [m.id for m in messages])
                return
            log.error("Альбом %s не ушёл — отправляю по одному",
                      [m.id for m in messages])
        # альбом собрать не вышло — шлём части по отдельности
        for msg in messages:
            await _publish_locked(client, api, [msg], target)
        return

    # --- одиночный пост -------------------------------------------------- #
    kind = media_kind(first)
    if kind:
        blob = await fetch_media(client, first)
        if blob is not None:
            data: Dict[str, Any] = {"chat_id": target}
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
                        "chat_id": target, "text": cut(text, TG_TEXT_LIMIT),
                        "parse_mode": "HTML", "disable_web_page_preview": True})
                save_last_id(STATE_FILE, first.chat_id, first.id)
                log.info("Опубликован пост %s (%s)", first.id, kind)
            return
        # медиа не досталось — хотя бы текст не теряем

    if text:
        ok = await api.call("sendMessage", {
            "chat_id": target, "text": cut(text, TG_TEXT_LIMIT), "parse_mode": "HTML"})
        if ok:
            save_last_id(STATE_FILE, first.chat_id, first.id)
            log.info("Опубликован пост %s (текст)", first.id)
    else:
        log.info("Пост %s пустой — пропускаю", first.id)


async def catch_up(client: TelegramClient, api: BotAPI, source, target: str) -> None:
    """Досылает посты, вышедшие пока скрипт был выключен."""
    title = getattr(source, "title", utils.get_peer_id(source))
    last_id = get_last_id(STATE_FILE, utils.get_peer_id(source))
    if not last_id:
        log.info("Состояния по «%s» нет — публикую только новые посты", title)
        return
    missed = [m async for m in client.iter_messages(source, min_id=last_id, limit=50)]
    if not missed:
        return
    log.info("Догоняю %s пропущенных постов из «%s»", len(missed), title)

    groups: Dict[int, List[Message]] = {}
    singles: List[Message] = []
    for msg in reversed(missed):            # от старых к новым
        if msg.grouped_id:
            groups.setdefault(msg.grouped_id, []).append(msg)
        else:
            singles.append(msg)
    for msg in singles:
        await publish(client, api, [msg], target)
    for album in groups.values():
        await publish(client, api, album, target)


# --------------------------------------------------------------------------- #
#                                  ЗАПУСК                                     #
# --------------------------------------------------------------------------- #


async def check_targets(api: BotAPI, targets: List[str]) -> List[str]:
    """Оставляет только те каналы, где бот админ с правом публикации."""
    me = await api.call("getMe", {})
    if not me:
        log.error("Неверный BOT_TOKEN")
        return []
    log.info("Публикует бот: @%s", me.get("username"))

    good: List[str] = []
    for target in targets:
        member = await api.call("getChatMember",
                                {"chat_id": target, "user_id": str(me["id"])})
        if not member:
            log.error("Бот не видит канал %s — добавь его туда администратором", target)
        elif member.get("status") != "administrator":
            log.error("Бот в %s не администратор (статус: %s)",
                      target, member.get("status"))
        elif not member.get("can_post_messages"):
            log.error("У бота нет права «Публикация сообщений» в %s", target)
        else:
            good.append(target)
    return good


# --------------------------------------------------------------------------- #
#                      РЕПОСТЕР, УПРАВЛЯЕМЫЙ ИЗ ЧАТА                          #
# --------------------------------------------------------------------------- #


class App:
    """
    Состояние репостера. Поднимается не при старте процесса, а тогда, когда
    в чате с ботом введено всё нужное: ключи приложения, вход в аккаунт и
    целевой канал. Поэтому из окружения обязателен только BOT_TOKEN.
    """

    def __init__(self, http: aiohttp.ClientSession) -> None:
        self.api = BotAPI(BOT_TOKEN, http)
        self.cfg: Dict[str, Any] = load_config()
        self.routes: Dict[int, str] = {}
        self.client: Optional[TelegramClient] = None
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self._seed_from_env()

    # --- настройки ------------------------------------------------------ #

    def _seed_from_env(self) -> None:
        """Переменные окружения — лишь значения по умолчанию для первого запуска."""
        changed = False
        for key, value in (("api_id", API_ID), ("api_hash", API_HASH),
                           ("target", TARGET), ("owner_id", OWNER_ID)):
            if value and not self.cfg.get(key):
                self.cfg[key] = value
                changed = True
        if changed:
            self.save()

    def save(self) -> None:
        save_config(self.cfg)

    def has_session(self) -> bool:
        return bool(SESSION_STRING or read_saved_session())

    # --- вспомогательное для бота настройки ----------------------------- #

    async def check_target(self, target: str) -> Tuple[bool, str]:
        """Проверяет, что бот админ канала и может публиковать."""
        me = await self.api.call("getMe", {})
        if not me:
            return False, "Неверный токен бота."
        member = await self.api.call("getChatMember",
                                     {"chat_id": target, "user_id": str(me["id"])})
        if not member:
            return False, (f"Не вижу канал {target}. Добавьте меня туда "
                           "администратором и повторите.")
        if member.get("status") != "administrator":
            return False, f"Я в {target} не администратор (статус: {member.get('status')})."
        if not member.get("can_post_messages"):
            return False, "У меня нет права «Публикация сообщений» в этом канале."
        return True, "ok"

    async def resolve(self, ref: str):
        """Находит канал аккаунтом, при необходимости вступая в него."""
        if self.client is None:
            raise RuntimeError("аккаунт не подключён, сначала /login")
        return await resolve_source(self.client, ref)

    async def on_session(self, session_string: str) -> None:
        """Вызывается мастером входа после успешной авторизации."""
        await self.restart()

    # --- запуск и остановка --------------------------------------------- #

    def ready(self) -> Optional[str]:
        """Чего не хватает для запуска; None — можно стартовать."""
        if not (self.cfg.get("api_id") and self.cfg.get("api_hash")):
            return "нет api_id/api_hash (/setup)"
        if not self.has_session():
            return "нет входа в аккаунт (/login)"
        if not self.cfg.get("target"):
            return "не задан целевой канал (/target)"
        return None

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.client is not None:
            with suppress(Exception):
                await self.client.disconnect()
            self.client = None

    async def restart(self, message: Any = None) -> None:
        """Поднимает репостинг заново — после входа, смены цели и т.п."""
        blocker = self.ready()
        if blocker:
            log.info("Репостинг пока не запущен: %s", blocker)
            return
        await self.stop()
        try:
            await self._start()
        except Exception as err:
            log.exception("Не удалось запустить репостинг: %s", err)
            if message is not None:
                with suppress(Exception):
                    await message.answer(f"❌ Не удалось запустить репостинг: {err}")

    async def _start(self) -> None:
        """Подключает аккаунт, восстанавливает источники, вешает обработчики."""
        session_string = SESSION_STRING or read_saved_session()
        self.client = TelegramClient(
            StringSession(session_string), self.cfg["api_id"], self.cfg["api_hash"])
        await self.client.start()
        me = await self.client.get_me()
        log.info("Читает аккаунт: %s (@%s)", me.first_name, me.username or me.id)

        await self._restore_sources()

        # фильтруем по routes вручную: список источников меняется во время
        # работы, перерегистрировать обработчики не нужно
        @self.client.on(events.Album())
        async def on_album(event: events.Album.Event) -> None:
            target = self.routes.get(event.chat_id)
            if target:
                await publish(self.client, self.api, list(event.messages), target)

        @self.client.on(events.NewMessage())
        async def on_message(event: events.NewMessage.Event) -> None:
            if event.message.grouped_id:      # части альбома заберёт on_album
                return
            target = self.routes.get(event.chat_id)
            if target:
                await publish(self.client, self.api, [event.message], target)

        self.running = True
        self._task = asyncio.create_task(self.client.run_until_disconnected())
        log.info("Репостинг запущен, источников: %s", len(self.routes))

    async def _restore_sources(self) -> None:
        """Поднимает сохранённые источники и догоняет пропущенное."""
        self.routes.clear()
        stored = load_sources()
        if not stored and SOURCES:
            log.info("sources.json пуст — беру источники из переменной SOURCES")
            stored = [{"ref": ref, "title": ref, "target": target}
                      for ref, target in parse_sources(SOURCES, self.cfg["target"])]

        resolved: List[Dict[str, Any]] = []
        for item in stored:
            ref = item.get("ref") or item.get("chat_id")
            try:
                source = await resolve_source(self.client, str(ref))
            except Exception as err:
                # недоступный источник не должен мешать остальным
                log.error("Источник %s пропущен: %s", item.get("title", ref), err)
                resolved.append(item)        # останется в /list с пометкой
                continue
            chat_id = utils.get_peer_id(source)
            target = item.get("target") or self.cfg["target"]
            self.routes[chat_id] = target
            resolved.append({"chat_id": chat_id,
                             "title": getattr(source, "title", None) or str(ref),
                             "target": target})
            log.info("Маршрут: «%s» → %s", resolved[-1]["title"], target)
        save_sources(resolved)               # нормализуем файл: ссылки → chat_id

        for item in resolved:
            if item.get("chat_id") in self.routes:
                source = await self.client.get_entity(item["chat_id"])
                await catch_up(self.client, self.api, source,
                               self.routes[item["chat_id"]])


async def run() -> None:
    """Из окружения обязателен только BOT_TOKEN — остальное вводится в чате."""
    if not BOT_TOKEN:
        log.error("Не задана переменная окружения BOT_TOKEN "
                  "(токен бота от @BotFather)")
        sys.exit(1)

    async with aiohttp.ClientSession() as http:
        app = App(http)
        blocker = app.ready()
        if blocker:
            log.warning("=" * 68)
            log.warning("Репостинг ещё не настроен: %s", blocker)
            log.warning("Откройте бота в Telegram и отправьте /start")
            log.warning("=" * 68)
        else:
            await app.restart()

        # бот настройки живёт всегда: через него вводят ключи, входят в
        # аккаунт, задают целевой канал и добавляют источники
        await run_control_bot(BOT_TOKEN, build_router(app))
        await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено")
