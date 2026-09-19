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
from typing import List, Optional

from telethon import TelegramClient, events
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    InviteHashExpiredError,
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import Message

# --------------------------------------------------------------------------- #
#                                  КОНФИГ                                     #
# --------------------------------------------------------------------------- #

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

# Источник: @username, https://t.me/+хеш, https://t.me/c/1916432895/1 или -100...
SOURCE = os.getenv("SOURCE", "").strip()
# Куда репостить: @username вашего канала или -100...
TARGET = os.getenv("TARGET", "").strip()

# Пауза перед отправкой копии, сек. Не ставьте 0 — мгновенные реакции выглядят
# как бот и повышают шанс ограничений на аккаунте.
SEND_DELAY = float(os.getenv("SEND_DELAY", "2"))
ALBUM_WAIT = 2.0                      # сколько ждём остальные части альбома

STATE_FILE = Path(__file__).resolve().parent / "userbot_state.json"

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


def load_last_id() -> int:
    """Последний скопированный ID — чтобы после рестарта догнать пропущенное."""
    if STATE_FILE.exists():
        try:
            return int(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_id", 0))
        except (ValueError, OSError):
            log.error("Не читается %s, начинаю с нуля", STATE_FILE.name)
    return 0


def save_last_id(value: int) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_id": value}), encoding="utf-8")
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- #
#                            ВСТУПЛЕНИЕ В КАНАЛ                               #
# --------------------------------------------------------------------------- #


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
                log.info("Аккаунт уже состоит в этом канале")
                return await client.get_entity(ref)
            except InviteHashExpiredError:
                raise RuntimeError("Ссылка-приглашение просрочена или отозвана")
            except FloodWaitError as err:
                raise RuntimeError(f"Telegram просит подождать {err.seconds} сек") from err

    # приватный канал по ID: t.me/c/1916432895/... или -1001916432895
    if "t.me/c/" in ref:
        ref = "-100" + ref.split("t.me/c/", 1)[1].split("/")[0]

    entity = await client.get_entity(int(ref) if ref.lstrip("-").isdigit() else ref)

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

    await asyncio.sleep(SEND_DELAY)      # имитируем живую задержку
    first = messages[0]

    try:
        if len(messages) > 1:
            # альбом: files и caption должны быть одной длины, иначе подписи
            # съедут на соседние картинки
            with_media = [m for m in messages if m.media]
            if not with_media:
                return await copy_messages(client, target, messages[:1])
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
        return await copy_messages(client, target, messages)
    except Exception as err:
        log.error("Не удалось скопировать %s: %s", [m.id for m in messages], err)
        return

    last = max(m.id for m in messages)
    save_last_id(last)
    log.info("Скопировано: %s", [m.id for m in messages])


async def catch_up(client: TelegramClient, source, target) -> None:
    """Досылает посты, вышедшие пока userbot был выключен."""
    last_id = load_last_id()
    if not last_id:
        log.info("Состояния нет — копирую только новые посты")
        return

    missed = [m async for m in client.iter_messages(source, min_id=last_id, limit=50)]
    if not missed:
        return
    log.info("Догоняю %s пропущенных постов", len(missed))

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


async def run() -> None:
    """Основной режим: слушаем источник и копируем всё новое."""
    for name, value in (("API_ID", API_ID), ("API_HASH", API_HASH),
                        ("SESSION_STRING", SESSION_STRING),
                        ("SOURCE", SOURCE), ("TARGET", TARGET)):
        if not value:
            log.error("Не задана переменная окружения %s", name)
            sys.exit(1)

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    log.info("Аккаунт: %s (@%s)", me.first_name, me.username or me.id)

    try:
        source = await resolve_source(client, SOURCE)
    except (RuntimeError, ChannelPrivateError, ValueError) as err:
        log.error("Источник недоступен: %s", err)
        await client.disconnect()
        sys.exit(1)

    target = await client.get_entity(int(TARGET) if TARGET.lstrip("-").isdigit() else TARGET)
    log.info("Репостинг: %s → %s",
             getattr(source, "title", source), getattr(target, "title", target))

    @client.on(events.Album(chats=source))
    async def on_album(event: events.Album.Event) -> None:
        """Альбом приходит одним событием — отправляем его целиком."""
        await copy_messages(client, target, list(event.messages))

    @client.on(events.NewMessage(chats=source))
    async def on_message(event: events.NewMessage.Event) -> None:
        """Одиночный пост. Части альбомов пропускаем — их заберёт on_album."""
        if event.message.grouped_id:
            return
        await copy_messages(client, target, [event.message])

    await catch_up(client, source, target)
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
