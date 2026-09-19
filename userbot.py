#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Userbot-вариант репостера на Telethon.

В отличие от main.py работает от имени ВАШЕГО аккаунта, а не бота, поэтому:
  * умеет вступать в канал по ссылке-приглашению (t.me/+хеш);
  * читает любые каналы, где состоит аккаунт, включая приватные;
  * получает посты мгновенно, без опроса веб-версии;
  * копирует всё как есть: альбомы, кружки, гифки, стикеры, опросы.

Цена: это автоматизация пользовательского аккаунта. Telegram такое не любит —
аккаунт могут ограничить или заблокировать. Держите на нём отдельный номер,
не рассылайте спам и не ставьте нулевые задержки.

Первый запуск (на своём компьютере, нужен ввод кода из Telegram):
    python userbot.py --login

Скрипт напечатает SESSION_STRING — положите её в переменные окружения хостинга.

Постоянный запуск:
    python userbot.py
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telethon import TelegramClient, events, utils
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    InviteHashExpiredError,
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import Message

# --------------------------------------------------------------------------- #
#                                  КОНФИГ                                     #
# --------------------------------------------------------------------------- #

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

# Источники — сколько угодно, через запятую или с новой строки. Каждый может быть
# @username, https://t.me/+хеш, https://t.me/c/1916432895/1 или -100...
# По умолчанию всё летит в TARGET; своя цель для источника пишется через "=>":
#     SOURCES="@news, @sport => @my_sport, https://t.me/+хеш"
SOURCES = os.getenv("SOURCES", "") or os.getenv("SOURCE", "")
# Куда репостить по умолчанию: @username вашего канала или -100...
TARGET = os.getenv("TARGET", "").strip()

# Пауза перед отправкой копии, сек. Не ставьте 0 — мгновенные реакции выглядят
# как бот и повышают шанс ограничений на аккаунте.
SEND_DELAY = float(os.getenv("SEND_DELAY", "2"))
ALBUM_WAIT = 2.0                      # сколько ждём остальные части альбома

# токен бота нужен только для мастера входа без терминала (см. LOGIN_BOT_TOKEN
# в README); на сам репостинг он не влияет — тут публикует аккаунт
LOGIN_BOT_TOKEN = os.getenv("LOGIN_BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0")) or None

STATE_FILE = Path(__file__).resolve().parent / "userbot_state.json"

# Публикуем строго по одному посту за раз, даже когда источников много:
# иначе несколько каналов разом упрутся в лимит Telegram на отправку.
send_lock = asyncio.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("userbot")
logging.getLogger("telethon").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
#                          СОСТОЯНИЕ (догон пропусков)                        #
# --------------------------------------------------------------------------- #


def load_state(path: Path) -> Dict[str, int]:
    """
    Последний скопированный ID по каждому источнику: {"<chat_id>": 17544}.
    Старый формат с единственным {"last_id": N} читается как есть — при первом
    же сохранении файл переедет на новую схему.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        log.error("Не читается %s, начинаю с нуля", path.name)
        return {}
    if "last_id" in raw:                       # состояние от версии с одним источником
        return {"_legacy": int(raw["last_id"])}
    return {str(k): int(v) for k, v in raw.items()}


def get_last_id(path: Path, chat_id: int) -> int:
    state = load_state(path)
    return state.get(str(chat_id), state.get("_legacy", 0))


def save_last_id(path: Path, chat_id: int, value: int) -> None:
    state = load_state(path)
    state.pop("_legacy", None)                 # мигрируем на схему по каналам
    state[str(chat_id)] = value
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def parse_sources(raw: str, default_target: str) -> List[Tuple[str, str]]:
    """
    Разбирает список источников в пары (ссылка, целевой канал).
    Разделители — запятая и перенос строки, своя цель задаётся через "=>".
    """
    pairs: List[Tuple[str, str]] = []
    for chunk in raw.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=>" in chunk:
            src, _, dst = chunk.partition("=>")
            pairs.append((src.strip(), dst.strip()))
        else:
            pairs.append((chunk, default_target))
    return pairs


# --------------------------------------------------------------------------- #
#                            ВСТУПЛЕНИЕ В КАНАЛ                               #
# --------------------------------------------------------------------------- #


async def _entity_with_cache(client: TelegramClient, ref):
    """
    Достаёт entity, при необходимости прогрев кэш диалогов.

    У свежей SESSION_STRING кэш пустой, и приватный канал по числовому ID
    не находится («Could not find the input entity»), даже если аккаунт в нём
    состоит. Один проход по диалогам это чинит.
    """
    try:
        return await client.get_entity(ref)
    except ValueError:
        log.info("Канала нет в кэше сессии — читаю список диалогов")
        await client.get_dialogs()
        return await client.get_entity(ref)


async def resolve_source(client: TelegramClient, ref: str):
    """
    Находит канал-источник и при необходимости вступает в него.
    Именно это бот делать не умеет — здесь работает полноценный аккаунт.
    """
    ref = ref.strip()

    # приватная ссылка-приглашение: t.me/+хеш или t.me/joinchat/хеш
    for marker in ("t.me/+", "t.me/joinchat/"):
        if marker in ref:
            invite_hash = ref.split(marker, 1)[1].strip("/")
            try:
                updates = await client(ImportChatInviteRequest(invite_hash))
                log.info("Вступил в канал по приглашению")
                return updates.chats[0]
            except UserAlreadyParticipantError:
                # аккаунт уже в канале — забираем его через проверку приглашения,
                # это надёжнее, чем резолвить саму ссылку
                log.info("Аккаунт уже состоит в этом канале")
                invite = await client(CheckChatInviteRequest(invite_hash))
                chat = getattr(invite, "chat", None)
                if chat is not None:
                    return chat
                return await _entity_with_cache(client, ref)
            except InviteHashExpiredError:
                raise RuntimeError("Ссылка-приглашение просрочена или отозвана")
            except FloodWaitError as err:
                raise RuntimeError(f"Telegram просит подождать {err.seconds} сек") from err

    # приватный канал по ID: t.me/c/1916432895/... или -1001916432895
    if "t.me/c/" in ref:
        ref = "-100" + ref.split("t.me/c/", 1)[1].split("/")[0]

    entity = await _entity_with_cache(
        client, int(ref) if ref.lstrip("-").isdigit() else ref)

    # в публичный канал вступаем явно, иначе не придут события о новых постах
    try:
        await client(JoinChannelRequest(entity))
        log.info("Подписался на канал")
    except UserAlreadyParticipantError:
        pass
    except Exception as err:            # приватный канал уже разрешён выше
        log.info("Вступать не потребовалось (%s)", type(err).__name__)

    return entity


# --------------------------------------------------------------------------- #
#                               КОПИРОВАНИЕ                                   #
# --------------------------------------------------------------------------- #


async def copy_messages(client: TelegramClient, target, messages: List[Message]) -> None:
    """
    Отправляет копию поста в целевой канал.
    send_message/send_file копируют содержимое БЕЗ пометки «Переслано из».
    Нужна пометка — замените тело на client.forward_messages(target, messages).
    """
    messages = [m for m in messages if m is not None]
    if not messages:
        return
    first = messages[0]

    # очередь на отправку общая для всех источников
    async with send_lock:
        await asyncio.sleep(SEND_DELAY)  # имитируем живую задержку
        return await _do_copy(client, target, messages, first)


async def _do_copy(client: TelegramClient, target, messages: List[Message],
                   first: Message) -> None:
    """Сама отправка — вызывается уже под send_lock."""
    try:
        if len(messages) > 1:
            # альбом: files и caption должны быть одной длины, иначе подписи
            # съедут на соседние картинки
            with_media = [m for m in messages if m.media]
            if not with_media:
                return await _do_copy(client, target, messages[:1], first)
            files = [m.media for m in with_media]
            captions = [m.text or "" for m in with_media]
            await client.send_file(target, files, caption=captions)
        elif first.media:
            await client.send_file(target, first.media, caption=first.text or "",
                                   formatting_entities=first.entities)
        elif first.text:
            await client.send_message(target, first.text,
                                      formatting_entities=first.entities,
                                      link_preview=bool(first.web_preview))
        else:
            log.info("Пост %s пустой — пропускаю", first.id)
            return
    except FloodWaitError as err:
        log.warning("FloodWait: жду %s сек", err.seconds)
        await asyncio.sleep(err.seconds + 1)
        return await _do_copy(client, target, messages, first)
    except Exception as err:
        log.error("Не удалось скопировать %s: %s", [m.id for m in messages], err)
        return

    save_last_id(STATE_FILE, first.chat_id, max(m.id for m in messages))
    log.info("Скопировано из %s: %s", first.chat_id, [m.id for m in messages])


async def catch_up(client: TelegramClient, source, target) -> None:
    """Досылает посты, вышедшие пока userbot был выключен."""
    last_id = get_last_id(STATE_FILE, utils.get_peer_id(source))
    if not last_id:
        log.info("Состояния по %s нет — копирую только новые посты",
                 getattr(source, "title", utils.get_peer_id(source)))
        return

    missed = [m async for m in client.iter_messages(source, min_id=last_id, limit=50)]
    if not missed:
        return
    log.info("Догоняю %s пропущенных постов из %s",
             len(missed), getattr(source, "title", utils.get_peer_id(source)))

    # группируем альбомы обратно и идём от старых к новым
    groups: dict = {}
    singles: List[Message] = []
    for msg in reversed(missed):
        if msg.grouped_id:
            groups.setdefault(msg.grouped_id, []).append(msg)
        else:
            singles.append(msg)

    for msg in singles:
        await copy_messages(client, target, [msg])
    for album in groups.values():
        await copy_messages(client, target, album)


# --------------------------------------------------------------------------- #
#                                  ЗАПУСК                                     #
# --------------------------------------------------------------------------- #


async def interactive_login() -> None:
    """Разовый вход с вводом кода из Telegram — печатает SESSION_STRING."""
    if not (API_ID and API_HASH):
        log.error("Заполни API_ID и API_HASH (получить: https://my.telegram.org/apps)")
        sys.exit(1)

    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        me = await client.get_me()
        print("\n" + "=" * 70)
        print(f"Вход выполнен: {me.first_name} (@{me.username or me.id})")
        print("\nSESSION_STRING (положи в переменные окружения хостинга):\n")
        print(client.session.save())
        print("\n" + "=" * 70)
        print("⚠️  Эта строка = полный доступ к аккаунту. Никому не показывай,")
        print("    не клади в git. Отозвать: Telegram → Настройки → Устройства.")
        print("=" * 70 + "\n")


async def get_session() -> str:
    """
    Достаёт SESSION_STRING: из переменной окружения, из сохранённого файла
    или — если ничего нет и задан LOGIN_BOT_TOKEN — через чат с ботом.
    """
    if SESSION_STRING:
        return SESSION_STRING
    # импорт отложенный: когда сессия уже есть, aiogram не нужен вовсе
    from login_via_bot import read_saved_session, session_via_bot

    saved = read_saved_session()
    if saved:
        log.info("Сессия взята из session.txt")
        return saved
    if LOGIN_BOT_TOKEN:
        return await session_via_bot(LOGIN_BOT_TOKEN, API_ID, API_HASH, OWNER_ID)
    log.error("Нет SESSION_STRING. Либо получите её командой "
              "`python userbot.py --login`, либо задайте LOGIN_BOT_TOKEN, "
              "чтобы войти прямо в чате с ботом (нужно на хостинге без терминала)")
    sys.exit(1)


async def run() -> None:
    """Основной режим: слушаем все источники и копируем всё новое."""
    for name, value in (("API_ID", API_ID), ("API_HASH", API_HASH),
                        ("SOURCES", SOURCES), ("TARGET", TARGET)):
        if not value:
            log.error("Не задана переменная окружения %s", name)
            sys.exit(1)

    session_string = await get_session()
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    log.info("Аккаунт: %s (@%s)", me.first_name, me.username or me.id)

    # chat_id источника -> куда репостить его посты
    routes: Dict[int, Any] = {}
    sources: List[Any] = []
    for ref, target_ref in parse_sources(SOURCES, TARGET):
        try:
            source = await resolve_source(client, ref)
            target = await _entity_with_cache(
                client,
                int(target_ref) if target_ref.lstrip("-").isdigit() else target_ref)
        except Exception as err:
            # один недоступный источник не должен ронять остальные
            log.error("Источник %s пропущен: %s", ref, err)
            continue
        # ключ — «помеченный» id (-100...), в таком же виде его отдаёт event.chat_id
        routes[utils.get_peer_id(source)] = target
        sources.append(source)
        log.info("Маршрут: %s → %s",
                 getattr(source, "title", source.id), getattr(target, "title", target))

    if not sources:
        log.error("Ни один источник не доступен — останавливаюсь")
        await client.disconnect()
        sys.exit(1)
    log.info("Источников подключено: %s", len(sources))

    @client.on(events.Album(chats=sources))
    async def on_album(event: events.Album.Event) -> None:
        """Альбом приходит одним событием — отправляем его целиком."""
        target = routes.get(event.chat_id)
        if target is not None:
            await copy_messages(client, target, list(event.messages))

    @client.on(events.NewMessage(chats=sources))
    async def on_message(event: events.NewMessage.Event) -> None:
        """Одиночный пост. Части альбомов пропускаем — их заберёт on_album."""
        if event.message.grouped_id:
            return
        target = routes.get(event.chat_id)
        if target is not None:
            await copy_messages(client, target, [event.message])

    for source in sources:
        await catch_up(client, source, routes[utils.get_peer_id(source)])

    log.info("Слушаю новые посты. Остановить — Ctrl+C")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        if "--login" in sys.argv:
            asyncio.run(interactive_login())
        else:
            asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено")
