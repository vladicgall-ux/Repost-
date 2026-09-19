#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот для автоматического репостинга постов из чужого канала в свой.

Как работает:
  * канал-источник читается через публичную веб-версию https://t.me/s/<username>
    (userbot / Telethon НЕ нужен, только публичный канал);
  * новые посты копируются в целевой канал: сперва пробуем нативный copy_message
    (идеальное качество, если бот состоит в канале-источнике), при неудаче —
    пересобираем пост из распарсенного HTML (текст + фото/видео/документы);
  * настройки лежат в config.json, последний увиденный ID поста — в state.json.

Запуск:  BOT_TOKEN=123:ABC python main.py
"""

import asyncio
import html as html_lib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from bs4 import BeautifulSoup, NavigableString, Tag

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

# --------------------------------------------------------------------------- #
#                                  КОНФИГ                                     #
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"      # настройки (источник, цель, интервал)
STATE_FILE = BASE_DIR / "state.json"        # last_seen_id по каждому источнику

DEFAULT_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))   # период опроса, сек
MIN_INTERVAL = 15                            # чаще опрашивать t.me нет смысла
MAX_INTERVAL = 24 * 60 * 60

DELAY_BETWEEN_POSTS = 3.0    # пауза между репостами, чтобы не поймать FloodWait
MAX_POSTS_PER_CYCLE = 10     # сколько постов максимум отправляем за один цикл
MAX_MEDIA_SIZE = 45 * 1024 * 1024   # 45 МБ — лимит загрузки файла через Bot API

TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("repost-bot")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
#                            ХРАНИЛИЩЕ НАСТРОЕК                               #
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    """Настройки бота, сериализуются в config.json."""

    source_channel: Optional[str] = None     # username канала-источника без @
    target_channel: Optional[str] = None     # @username или -100... целевого канала
    last_post_id: int = 0                    # последний реально скопированный пост
    interval: int = DEFAULT_INTERVAL         # период опроса в секундах
    enabled: bool = False                    # включён ли репостинг
    owner_id: Optional[int] = None           # кому разрешено управлять ботом

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_channel": self.source_channel,
            "target_channel": self.target_channel,
            "last_post_id": self.last_post_id,
            "interval": self.interval,
            "enabled": self.enabled,
            "owner_id": self.owner_id,
        }


def load_config() -> Config:
    """Читает config.json; при отсутствии/повреждении возвращает значения по умолчанию."""
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = Config(
                source_channel=raw.get("source_channel"),
                target_channel=raw.get("target_channel"),
                last_post_id=int(raw.get("last_post_id") or 0),
                interval=int(raw.get("interval") or DEFAULT_INTERVAL),
                enabled=bool(raw.get("enabled")),
                owner_id=raw.get("owner_id"),
            )
            log.info("Конфиг загружен: %s", cfg.to_dict())
            return cfg
        except (ValueError, OSError) as err:
            log.error("Не удалось прочитать %s (%s), беру значения по умолчанию",
                      CONFIG_FILE.name, err)
    return Config()


def save_config(cfg: Config) -> None:
    """Атомарно сохраняет настройки на диск."""
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def load_state() -> Dict[str, int]:
    """state.json: {"channel_username": last_seen_post_id}."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in data.items()}
        except (ValueError, OSError, TypeError) as err:
            log.error("Не удалось прочитать %s (%s)", STATE_FILE.name, err)
    return {}


def save_state(state: Dict[str, int]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def get_last_seen(channel: str) -> int:
    return load_state().get(channel.lower(), 0)


def set_last_seen(channel: str, post_id: int) -> None:
    state = load_state()
    state[channel.lower()] = post_id
    save_state(state)


# --------------------------------------------------------------------------- #
#                         ПАРСИНГ ССЫЛОК TELEGRAM                             #
# --------------------------------------------------------------------------- #

# https://t.me/channel/123 , t.me/s/channel/123 , @channel , channel
POST_LINK_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/(?:s/)?(?P<name>[A-Za-z0-9_]{4,32})/(?P<post>\d+)"
)
CHANNEL_RE = re.compile(
    r"^(?:(?:https?://)?t(?:elegram)?\.me/(?:s/)?)?@?(?P<name>[A-Za-z0-9_]{4,32})/?$"
)


def parse_post_link(text: str) -> Optional[tuple[str, int]]:
    """Из ссылки на пост достаёт (username канала, id поста)."""
    m = POST_LINK_RE.search(text.strip())
    if not m:
        return None
    return m.group("name"), int(m.group("post"))


def parse_channel_ref(text: str) -> Optional[str]:
    """Достаёт @username канала из ссылки/юзернейма. Приватные ссылки (+hash) не поддерживаются."""
    text = text.strip()
    if text.startswith("-100") and text[1:].isdigit():      # числовой chat_id
        return text
    m = CHANNEL_RE.match(text)
    if not m:
        return None
    name = m.group("name")
    if name.lower() in {"s", "joinchat", "c", "addstickers", "proxy"}:
        return None
    return "@" + name


# --------------------------------------------------------------------------- #
#                      ПАРСИНГ ВЕБ-ВЕРСИИ КАНАЛА t.me/s/                      #
# --------------------------------------------------------------------------- #


@dataclass
class Post:
    """Один распарсенный пост из веб-версии канала."""

    post_id: int
    text_html: str = ""                       # текст в Telegram-HTML
    photos: List[str] = field(default_factory=list)
    videos: List[str] = field(default_factory=list)
    documents: List[tuple[str, str]] = field(default_factory=list)  # (url, имя файла)
    link: str = ""

    @property
    def has_media(self) -> bool:
        return bool(self.photos or self.videos or self.documents)

    @property
    def is_empty(self) -> bool:
        return not self.text_html.strip() and not self.has_media


# Теги, которые Telegram понимает в parse_mode=HTML
INLINE_TAGS = {
    "b": "b", "strong": "b",
    "i": "i", "em": "i",
    "u": "u", "ins": "u",
    "s": "s", "strike": "s", "del": "s",
    "code": "code", "pre": "pre",
    "blockquote": "blockquote",
    "tg-spoiler": "tg-spoiler",
}

BG_URL_RE = re.compile(r"background-image\s*:\s*url\(['\"]?(?P<url>[^'\")]+)")


def _node_to_html(node: Any) -> str:
    """Рекурсивно превращает узел HTML веб-версии в HTML, понятный Telegram."""
    if isinstance(node, NavigableString):
        return html_lib.escape(str(node))
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    classes = node.get("class") or []

    if name == "br":
        return "\n"
    if name in {"script", "style"}:
        return ""
    # <i class="emoji"><b>🏳️</b></i> — это обычный эмодзи, а не курсив
    if "emoji" in classes:
        return html_lib.escape(node.get_text())

    inner = "".join(_node_to_html(child) for child in node.children)

    if name == "a":
        href = node.get("href", "")
        # внутренние ссылки-«хештеги» вида ?q=... оставляем просто текстом
        if href.startswith("http") and "t.me/s/" not in href:
            return f'<a href="{html_lib.escape(href, quote=True)}">{inner}</a>'
        return inner
    if name == "span" and "tg-spoiler" in classes:
        return f"<tg-spoiler>{inner}</tg-spoiler>"
    if name in INLINE_TAGS:
        tag = INLINE_TAGS[name]
        return f"<{tag}>{inner}</{tag}>"
    if name in {"div", "p"}:
        return inner + "\n"
    return inner


def _extract_media(msg: Tag, post: Post) -> None:
    """Собирает прямые ссылки на фото/видео/документы поста."""
    # фото: ссылка спрятана в inline-стиле background-image
    for wrap in msg.select("a.tgme_widget_message_photo_wrap"):
        m = BG_URL_RE.search(wrap.get("style", ""))
        if m and m.group("url") not in post.photos:
            post.photos.append(m.group("url"))

    # видео и кружки: <video class="tgme_widget_message_video" src="...">
    for video in msg.select("video.tgme_widget_message_video, video.tgme_widget_message_roundvideo"):
        src = video.get("src")
        if src and src not in post.videos:
            post.videos.append(src)

    # голосовые и аудио отдаём как документы
    for audio in msg.select("audio.tgme_widget_message_voice, audio.tgme_widget_message_audio"):
        src = audio.get("src")
        if src:
            post.documents.append((src, "audio.ogg"))

    # файлы
    for doc in msg.select("a.tgme_widget_message_document_wrap, a.tgme_widget_message_document"):
        href = doc.get("href")
        if not href or not href.startswith("http"):
            continue
        title_el = doc.select_one(".tgme_widget_message_document_title")
        title = title_el.get_text(strip=True) if title_el else "file"
        post.documents.append((href, title))

    # стикер — отдаём как картинку webp
    for sticker in msg.select("i.tgme_widget_message_sticker"):
        src = sticker.get("data-webp") or sticker.get("data-sticker")
        if src:
            post.photos.append(src)


def parse_channel_html(page_html: str, channel: str) -> List[Post]:
    """Разбирает HTML страницы t.me/s/<channel> в список постов (по возрастанию ID)."""
    soup = BeautifulSoup(page_html, "html.parser")
    posts: Dict[int, Post] = {}

    for msg in soup.select("div.tgme_widget_message"):
        data_post = msg.get("data-post") or ""
        if "/" not in data_post:
            continue
        try:
            post_id = int(data_post.rsplit("/", 1)[1])
        except ValueError:
            continue

        # превью внешней ссылки содержит свои картинки — выбрасываем, иначе дубли
        for preview in msg.select("a.tgme_widget_message_link_preview"):
            preview.decompose()
        post = posts.setdefault(
            post_id,
            Post(post_id=post_id, link=f"https://t.me/{channel}/{post_id}"),
        )

        text_el = msg.select_one("div.tgme_widget_message_text")
        if text_el is not None and not post.text_html:
            post.text_html = _node_to_html(text_el).strip()

        _extract_media(msg, post)

        # веб-версия не отдаёт часть медиа (кружки, гифки, опросы) — предупреждаем,
        # но только если из поста вообще ничего не удалось вытащить
        if post.is_empty and msg.select_one(".message_media_not_supported"):
            log.warning("Пост %s/%s: медиа недоступно в веб-версии, будет пропущен "
                        "(поможет добавить бота в канал-источник — тогда сработает "
                        "нативный copy_message)", channel, post_id)

    return [posts[pid] for pid in sorted(posts)]


async def fetch_channel_posts(session: aiohttp.ClientSession, channel: str) -> List[Post]:
    """Скачивает и парсит публичную веб-версию канала. Кидает RuntimeError при проблемах."""
    url = f"https://t.me/s/{channel}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 404:
                raise RuntimeError(f"Канал @{channel} не найден")
            if resp.status != 200:
                raise RuntimeError(f"t.me ответил HTTP {resp.status}")
            page = await resp.text()
    except asyncio.TimeoutError as err:
        raise RuntimeError("Таймаут при обращении к t.me") from err
    except aiohttp.ClientError as err:
        raise RuntimeError(f"Сетевая ошибка: {err}") from err

    posts = parse_channel_html(page, channel)
    if not posts and "tgme_page_context_link" not in page and "tgme_widget_message" not in page:
        raise RuntimeError(
            f"Канал @{channel} закрыт для просмотра без подписки "
            "или у него отключён веб-превью"
        )
    return posts


async def download(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    """Скачивает файл, если он не слишком большой. None — если не получилось."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                log.error("Скачивание %s: HTTP %s", url, resp.status)
                return None
            size = resp.content_length or 0
            if size > MAX_MEDIA_SIZE:
                log.error("Файл %s слишком большой (%.1f МБ)", url, size / 1048576)
                return None
            data = await resp.content.read(MAX_MEDIA_SIZE + 1)
            if len(data) > MAX_MEDIA_SIZE:
                log.error("Файл %s превысил лимит при чтении", url)
                return None
            return data
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        log.error("Не удалось скачать %s: %s", url, err)
        return None


# --------------------------------------------------------------------------- #
#                            ОТПРАВКА В КАНАЛ                                 #
# --------------------------------------------------------------------------- #


TAG_RE = re.compile(r"<[^>]+>")


def _cut(text: str, limit: int) -> str:
    """
    Обрезает текст под лимит Telegram. Если резать приходится — снимаем разметку
    целиком, иначе получится незакрытый тег и Bad Request от Bot API.
    """
    if len(text) <= limit:
        return text
    plain = html_lib.unescape(TAG_RE.sub("", text))
    plain = html_lib.escape(plain)
    return plain[: limit - 1] + "…"


async def with_retry(coro_factory, tries: int = 3):
    """Выполняет запрос к Bot API с обработкой FloodWait (RetryAfter)."""
    for attempt in range(1, tries + 1):
        try:
            return await coro_factory()
        except TelegramRetryAfter as err:
            wait = err.retry_after + 1
            log.warning("FloodWait: ждём %s сек (попытка %s/%s)", wait, attempt, tries)
            await asyncio.sleep(wait)
        except TelegramBadRequest as err:
            # «message to copy not found» и т.п. — повторять бессмысленно
            log.error("BadRequest: %s", err.message)
            raise
    raise RuntimeError("Превышено число попыток из-за FloodWait")


async def try_copy_message(bot: Bot, target: str, source: str, post_id: int) -> bool:
    """
    Пробует нативный copy_message (сохраняет форматирование и альбомы).
    Работает, только если бот имеет доступ к каналу-источнику.
    """
    try:
        await with_retry(lambda: bot.copy_message(
            chat_id=target,
            from_chat_id=f"@{source}",
            message_id=post_id,
        ))
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as err:
        log.info("copy_message для %s/%s не сработал (%s) — пересобираю пост вручную",
                 source, post_id, getattr(err, "message", err))
        return False


async def send_post_manually(
    bot: Bot,
    session: aiohttp.ClientSession,
    target: str,
    post: Post,
) -> bool:
    """Собирает пост заново из распарсенного HTML и отправляет в целевой канал."""
    text = post.text_html

    # 1. Альбом из нескольких фото/видео
    media_items: List[Any] = []
    for url in post.photos[:10]:
        media_items.append(("photo", url))
    for url in post.videos[:10]:
        media_items.append(("video", url))

    if len(media_items) > 1:
        group: List[Any] = []
        for idx, (kind, url) in enumerate(media_items[:10]):
            caption = _cut(text, TG_CAPTION_LIMIT) if idx == 0 and text else None
            cls = InputMediaPhoto if kind == "photo" else InputMediaVideo
            group.append(cls(media=url, caption=caption, parse_mode=ParseMode.HTML))
        try:
            await with_retry(lambda: bot.send_media_group(chat_id=target, media=group))
            return True
        except TelegramBadRequest:
            # Telegram не смог скачать файлы по ссылке — грузим байтами
            log.info("Альбом по URL не принят, пробую загрузить файлы вручную")
            group = []
            for idx, (kind, url) in enumerate(media_items[:10]):
                blob = await download(session, url)
                if blob is None:
                    continue
                caption = _cut(text, TG_CAPTION_LIMIT) if idx == 0 and text else None
                cls = InputMediaPhoto if kind == "photo" else InputMediaVideo
                name = f"media_{idx}.{'jpg' if kind == 'photo' else 'mp4'}"
                group.append(cls(media=BufferedInputFile(blob, filename=name),
                                 caption=caption, parse_mode=ParseMode.HTML))
            if group:
                await with_retry(lambda: bot.send_media_group(chat_id=target, media=group))
                return True
            return False

    # 2. Одиночное медиа
    if len(media_items) == 1:
        kind, url = media_items[0]
        caption = _cut(text, TG_CAPTION_LIMIT) if text else None
        sender = bot.send_photo if kind == "photo" else bot.send_video
        field_name = "photo" if kind == "photo" else "video"
        try:
            await with_retry(lambda: sender(**{
                "chat_id": target, field_name: url, "caption": caption,
            }))
        except TelegramBadRequest:
            blob = await download(session, url)
            if blob is None:
                return False
            name = f"media.{'jpg' if kind == 'photo' else 'mp4'}"
            await with_retry(lambda: sender(**{
                "chat_id": target,
                field_name: BufferedInputFile(blob, filename=name),
                "caption": caption,
            }))
        # если текст не влез в подпись — досылаем отдельным сообщением
        if text and len(text) > TG_CAPTION_LIMIT:
            await with_retry(lambda: bot.send_message(
                chat_id=target, text=_cut(text, TG_TEXT_LIMIT),
                disable_web_page_preview=True))
        return True

    # 3. Документы
    sent_any = False
    for url, filename in post.documents[:5]:
        blob = await download(session, url)
        if blob is None:
            continue
        await with_retry(lambda: bot.send_document(
            chat_id=target,
            document=BufferedInputFile(blob, filename=filename),
            caption=_cut(text, TG_CAPTION_LIMIT) if text and not sent_any else None,
        ))
        sent_any = True
    if sent_any:
        return True

    # 4. Только текст
    if text:
        await with_retry(lambda: bot.send_message(
            chat_id=target,
            text=_cut(text, TG_TEXT_LIMIT),
            disable_web_page_preview=False,
        ))
        return True

    return False


# --------------------------------------------------------------------------- #
#                         ФОНОВЫЙ ЦИКЛ РЕПОСТИНГА                             #
# --------------------------------------------------------------------------- #


class Reposter:
    """Держит фоновую задачу опроса канала-источника."""

    def __init__(self, bot: Bot, cfg: Config) -> None:
        self.bot = bot
        self.cfg = cfg
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Запускает цикл опроса (idempotent)."""
        if self.running:
            return
        self._task = asyncio.create_task(self._loop(), name="repost-loop")
        log.info("Цикл репостинга запущен, интервал %s сек", self.cfg.interval)

    async def stop(self) -> None:
        """Останавливает цикл опроса."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            log.info("Цикл репостинга остановлен")

    async def close(self) -> None:
        await self.stop()
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
        return self._session

    async def _loop(self) -> None:
        """Бесконечный цикл: раз в interval секунд проверяем новые посты."""
        while True:
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # цикл не должен падать никогда
                log.exception("Ошибка в цикле репостинга: %s", err)
            await asyncio.sleep(max(MIN_INTERVAL, self.cfg.interval))

    async def _check_once(self) -> None:
        """Одна проверка канала-источника."""
        cfg = self.cfg
        if not (cfg.enabled and cfg.source_channel and cfg.target_channel):
            return

        session = await self._get_session()
        try:
            posts = await fetch_channel_posts(session, cfg.source_channel)
        except RuntimeError as err:
            log.error("Источник @%s недоступен: %s", cfg.source_channel, err)
            return

        last_seen = max(get_last_seen(cfg.source_channel), cfg.last_post_id)
        fresh = [p for p in posts if p.post_id > last_seen and not p.is_empty]
        if not fresh:
            log.info("Новых постов нет (последний ID %s)", last_seen)
            return

        fresh = fresh[:MAX_POSTS_PER_CYCLE]
        log.info("Найдено новых постов: %s", len(fresh))

        for post in fresh:
            try:
                ok = await try_copy_message(
                    self.bot, cfg.target_channel, cfg.source_channel, post.post_id
                )
                if not ok:
                    ok = await send_post_manually(
                        self.bot, session, cfg.target_channel, post
                    )
            except TelegramForbiddenError as err:
                # бота выгнали из целевого канала — выключаем репостинг
                log.error("Нет доступа к целевому каналу: %s", err.message)
                cfg.enabled = False
                save_config(cfg)
                await self._notify_owner(
                    "⛔️ Репостинг остановлен: бот потерял доступ к целевому каналу."
                )
                return
            except Exception as err:
                log.error("Пост %s не отправлен: %s", post.link, err)
                ok = False

            if ok:
                cfg.last_post_id = post.post_id
                set_last_seen(cfg.source_channel, post.post_id)
                save_config(cfg)
                log.info("Репостнут %s", post.link)
            else:
                log.error("Пропускаю пост %s", post.link)
                # всё равно двигаем указатель, чтобы не залипнуть на битом посте
                cfg.last_post_id = post.post_id
                set_last_seen(cfg.source_channel, post.post_id)
                save_config(cfg)

            await asyncio.sleep(DELAY_BETWEEN_POSTS)

    async def _notify_owner(self, text: str) -> None:
        if self.cfg.owner_id:
            try:
                await self.bot.send_message(self.cfg.owner_id, text)
            except Exception as err:
                log.error("Не смог уведомить владельца: %s", err)


# --------------------------------------------------------------------------- #
#                              ХЭНДЛЕРЫ / FSM                                 #
# --------------------------------------------------------------------------- #

router = Router()
config = load_config()
reposter: Optional[Reposter] = None


class Setup(StatesGroup):
    """Пошаговая настройка: сначала источник, потом цель."""

    waiting_source = State()
    waiting_target = State()


def is_owner(message: Message) -> bool:
    """Управлять ботом может только владелец (первый, кто вызвал /start)."""
    if config.owner_id is None:
        return True
    return message.from_user is not None and message.from_user.id == config.owner_id


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Шаг 1 — просим ссылку на любой пост канала-источника."""
    if config.owner_id is None and message.from_user:
        config.owner_id = message.from_user.id
        save_config(config)
        log.info("Владелец бота: %s", config.owner_id)
    if not is_owner(message):
        await message.answer("Этот бот приватный.")
        return

    await state.set_state(Setup.waiting_source)
    await message.answer(
        "👋 <b>Привет!</b> Я копирую посты из чужого канала в твой.\n\n"
        "<b>Шаг 1 из 2.</b> Пришли ссылку на любой пост канала-источника, например:\n"
        "<code>https://t.me/durov/123</code>\n\n"
        "Копировать буду посты, вышедшие <i>после</i> этого.\n"
        "Канал должен быть публичным (доступен по адресу t.me/s/имя).\n\n"
        "Отмена — /stop"
    )


@router.message(Setup.waiting_source)
async def step_source(message: Message, state: FSMContext) -> None:
    """Разбираем ссылку на пост и проверяем доступность канала."""
    if not is_owner(message):
        return
    parsed = parse_post_link(message.text or "")
    if not parsed:
        await message.answer(
            "❌ Не похоже на ссылку на пост.\n"
            "Нужен формат <code>https://t.me/имя_канала/123</code>"
        )
        return

    channel, post_id = parsed
    await message.answer(f"🔎 Проверяю канал @{channel}…")

    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        try:
            posts = await fetch_channel_posts(session, channel)
        except RuntimeError as err:
            await message.answer(
                f"❌ {err}\n\nПопробуй другой канал — нужен публичный, "
                "без обязательной подписки."
            )
            return

    latest = posts[-1].post_id if posts else post_id
    config.source_channel = channel
    config.last_post_id = post_id
    save_config(config)
    set_last_seen(channel, post_id)

    await state.set_state(Setup.waiting_target)
    await message.answer(
        f"✅ Источник: <b>@{channel}</b>\n"
        f"Последний пост в канале сейчас: <code>{latest}</code>, "
        f"копировать начну с <code>{post_id + 1}</code>.\n\n"
        "<b>Шаг 2 из 2.</b> Пришли ссылку или @username <b>своего</b> канала.\n"
        "Бот уже должен быть там админом с правом «Публикация сообщений»."
    )


@router.message(Setup.waiting_target)
async def step_target(message: Message, state: FSMContext) -> None:
    """Проверяем права бота в целевом канале и запускаем репостинг."""
    global reposter
    if not is_owner(message):
        return
    target = parse_channel_ref(message.text or "")
    if not target:
        await message.answer(
            "❌ Не понял канал. Пришли <code>@username</code> или "
            "<code>https://t.me/username</code>."
        )
        return

    bot: Bot = message.bot
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=target, user_id=me.id)
    except TelegramForbiddenError:
        await message.answer("❌ Бот не имеет доступа к этому каналу. Добавь его админом.")
        return
    except TelegramBadRequest as err:
        await message.answer(
            f"❌ Не могу открыть канал {target}: {err.message}\n"
            "Проверь, что бот добавлен в канал администратором."
        )
        return

    if member.status != ChatMemberStatus.ADMINISTRATOR:
        await message.answer(
            f"❌ Бот в канале {target} со статусом <code>{member.status}</code>.\n"
            "Нужен статус администратора."
        )
        return
    if not getattr(member, "can_post_messages", False):
        await message.answer(
            "❌ У бота нет права <b>«Публикация сообщений»</b>.\n"
            "Включи его в настройках администратора канала и пришли ссылку снова."
        )
        return

    config.target_channel = target
    config.enabled = True
    save_config(config)
    await state.clear()

    assert reposter is not None
    reposter.start()

    await message.answer(
        "🚀 <b>Готово, репостинг запущен!</b>\n\n"
        f"Источник: <b>@{config.source_channel}</b>\n"
        f"Цель: <b>{target}</b>\n"
        f"Проверка каждые <b>{config.interval}</b> сек.\n\n"
        "Команды: /status, /stop, /set_interval &lt;секунды&gt;"
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext) -> None:
    """Останавливает репостинг (настройки сохраняются)."""
    if not is_owner(message):
        return
    await state.clear()
    config.enabled = False
    save_config(config)
    if reposter:
        await reposter.stop()
    await message.answer("⏹ Репостинг остановлен. Возобновить — /start.")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Показывает текущие настройки и последний скопированный пост."""
    if not is_owner(message):
        return
    running = "🟢 работает" if (reposter and reposter.running and config.enabled) \
        else "🔴 остановлен"
    src = f"@{config.source_channel}" if config.source_channel else "— не задан"
    dst = config.target_channel or "— не задан"
    await message.answer(
        f"<b>Статус:</b> {running}\n"
        f"<b>Источник:</b> {src}\n"
        f"<b>Целевой канал:</b> {dst}\n"
        f"<b>Последний скопированный ID:</b> <code>{config.last_post_id}</code>\n"
        f"<b>Интервал проверки:</b> {config.interval} сек"
    )


@router.message(Command("set_interval"))
async def cmd_set_interval(message: Message) -> None:
    """/set_interval 120 — меняет период опроса."""
    if not is_owner(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer(
            f"Использование: <code>/set_interval 60</code>\n"
            f"Допустимо от {MIN_INTERVAL} до {MAX_INTERVAL} секунд."
        )
        return
    value = int(parts[1].strip())
    if not MIN_INTERVAL <= value <= MAX_INTERVAL:
        await message.answer(f"❌ Интервал должен быть от {MIN_INTERVAL} до {MAX_INTERVAL} сек.")
        return
    config.interval = value
    save_config(config)
    await message.answer(f"✅ Новый интервал проверки: <b>{value}</b> сек.")


@router.message(F.text)
async def fallback(message: Message) -> None:
    """Любое сообщение вне сценария."""
    if not is_owner(message):
        return
    await message.answer("Не понял команду. Настройка — /start, справка — /status.")


# --------------------------------------------------------------------------- #
#                                  ЗАПУСК                                     #
# --------------------------------------------------------------------------- #


async def main() -> None:
    global reposter

    if not BOT_TOKEN:
        log.error("Переменная окружения BOT_TOKEN не задана. Получи токен у @BotFather.")
        sys.exit(1)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    reposter = Reposter(bot, config)

    # если настройки уже есть и репостинг был включён — продолжаем после перезапуска
    if config.enabled and config.source_channel and config.target_channel:
        reposter.start()
        log.info("Репостинг восстановлен: @%s → %s",
                 config.source_channel, config.target_channel)

    me = await bot.get_me()
    log.info("Бот запущен: @%s (id=%s)", me.username, me.id)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await reposter.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено пользователем")
