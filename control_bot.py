# -*- coding: utf-8 -*-
"""
Управление источниками прямо в чате с ботом.

Что умеет:
  * переслали боту пост из канала — канал добавлен в источники;
  * прислали ссылку-приглашение — аккаунт вступит в канал и добавит его;
  * /list — список источников с номерами;
  * /remove N — убрать источник, копирование из него прекращается сразу;
  * /status — что сейчас копируется и куда.

Список живёт в sources.json, так что переживает перезапуск. Менять переменные
окружения на хостинге больше не нужно.
"""

import asyncio
import html as html_lib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from aiogram import Bot as AioBot
from aiogram import Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message as AioMessage
from aiogram.types import MessageOriginChannel
from telethon import TelegramClient, utils

log = logging.getLogger("control")

SOURCES_FILE = Path(__file__).resolve().parent / "sources.json"


# --------------------------------------------------------------------------- #
#                         СПИСОК ИСТОЧНИКОВ НА ДИСКЕ                          #
# --------------------------------------------------------------------------- #


def load_sources() -> List[Dict[str, Any]]:
    """Читает sources.json: [{chat_id, title, target}, ...]."""
    if not SOURCES_FILE.exists():
        return []
    try:
        data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
        return [s for s in data if isinstance(s, dict) and s.get("chat_id")]
    except (ValueError, OSError):
        log.error("Не читается %s — считаю список пустым", SOURCES_FILE.name)
        return []


def save_sources(sources: List[Dict[str, Any]]) -> None:
    tmp = SOURCES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SOURCES_FILE)


def _esc(text: Optional[str]) -> str:
    return html_lib.escape(text or "")


# --------------------------------------------------------------------------- #
#                               БОТ УПРАВЛЕНИЯ                                #
# --------------------------------------------------------------------------- #


def build_router(
    client: TelegramClient,
    routes: Dict[int, str],
    default_target: str,
    owner_id: Optional[int],
    resolve_source,
    check_target: Callable[[str], Any],
) -> Router:
    """
    Собирает роутер управления.

    routes — общий с репостером словарь {chat_id источника: целевой канал}.
    Правки применяются на лету: репостер смотрит в него на каждом посте,
    поэтому перезапуск после добавления или удаления не нужен.
    """
    router = Router()
    owner = {"id": owner_id}

    def allowed(message: AioMessage) -> bool:
        if owner["id"] is None and message.from_user:
            owner["id"] = message.from_user.id      # владельцем станет первый
            log.info("Владелец бота: %s", owner["id"])
        return message.from_user is not None and message.from_user.id == owner["id"]

    async def add_source(message: AioMessage, entity, title: str) -> None:
        """Ставит канал на копирование и сохраняет список."""
        chat_id = utils.get_peer_id(entity)
        if chat_id in routes:
            await message.answer(f"ℹ️ <b>{_esc(title)}</b> уже в списке источников.")
            return

        sources = load_sources()
        sources.append({"chat_id": chat_id, "title": title, "target": default_target})
        save_sources(sources)
        routes[chat_id] = default_target

        await message.answer(
            f"✅ Добавлен источник: <b>{_esc(title)}</b>\n"
            f"Копирую в {default_target}\n\n"
            f"Всего источников: {len(routes)}. Список — /list"
        )
        log.info("Источник добавлен: %s (%s)", title, chat_id)

    @router.message(CommandStart())
    async def cmd_start(message: AioMessage) -> None:
        if not allowed(message):
            return
        await message.answer(
            "👋 Я копирую посты из чужих каналов в ваш.\n\n"
            "<b>Добавить источник:</b> перешлите мне любой пост из нужного канала.\n"
            "Если аккаунта нет в канале, пришлите ссылку-приглашение "
            "<code>https://t.me/+…</code> — вступлю сам.\n\n"
            "<b>Команды:</b>\n"
            "/list — список источников\n"
            "/remove &lt;номер&gt; — убрать источник\n"
            "/status — что копируется сейчас"
        )

    @router.message(Command("list"))
    async def cmd_list(message: AioMessage) -> None:
        if not allowed(message):
            return
        sources = load_sources()
        if not sources:
            await message.answer(
                "Список источников пуст.\nПерешлите мне пост из канала, чтобы добавить.")
            return
        lines = [
            f"{i}. <b>{_esc(s.get('title'))}</b> → {s.get('target')}"
            f"{'' if s['chat_id'] in routes else '  ⚠️ недоступен'}"
            for i, s in enumerate(sources, 1)
        ]
        await message.answer(
            "<b>Источники:</b>\n" + "\n".join(lines) +
            "\n\nУбрать — <code>/remove номер</code>")

    @router.message(Command("remove"))
    async def cmd_remove(message: AioMessage) -> None:
        if not allowed(message):
            return
        parts = (message.text or "").split()
        sources = load_sources()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer(
                "Укажите номер из /list, например <code>/remove 2</code>")
            return
        index = int(parts[1])
        if not 1 <= index <= len(sources):
            await message.answer(f"❌ Нет источника с номером {index}. Смотрите /list")
            return

        removed = sources.pop(index - 1)
        save_sources(sources)
        routes.pop(removed["chat_id"], None)       # копирование прекращается сразу
        await message.answer(
            f"🗑 Убран источник: <b>{_esc(removed.get('title'))}</b>\n"
            f"Больше из него не копирую. Осталось источников: {len(sources)}"
        )
        log.info("Источник убран: %s (%s)", removed.get("title"), removed["chat_id"])

    @router.message(Command("status"))
    async def cmd_status(message: AioMessage) -> None:
        if not allowed(message):
            return
        sources = load_sources()
        active = sum(1 for s in sources if s["chat_id"] in routes)
        await message.answer(
            f"<b>Активных источников:</b> {active} из {len(sources)}\n"
            f"<b>Целевой канал по умолчанию:</b> {default_target}\n\n"
            "Подробнее — /list"
        )

    @router.message()
    async def on_any(message: AioMessage) -> None:
        """Пересланный пост или ссылка-приглашение — добавляем источник."""
        if not allowed(message):
            return

        origin = message.forward_origin
        if isinstance(origin, MessageOriginChannel):
            chat = origin.chat
            try:
                # читать канал будет аккаунт, поэтому проверяем именно его доступ
                entity = await client.get_entity(chat.id)
            except Exception:
                await message.answer(
                    f"⚠️ Канал <b>{_esc(chat.title)}</b> найден, но аккаунт, "
                    "от имени которого я читаю, в нём не состоит.\n\n"
                    "Пришлите ссылку-приглашение <code>https://t.me/+…</code> — "
                    "вступлю и добавлю канал."
                )
                return
            await add_source(message, entity, chat.title or str(chat.id))
            return

        text = (message.text or "").strip()
        if "t.me/" in text:
            try:
                entity = await resolve_source(client, text)
            except Exception as err:
                await message.answer(f"❌ Не получилось открыть канал: {err}")
                return
            await add_source(message, entity,
                             getattr(entity, "title", None) or text)
            return

        await message.answer(
            "Не понял. Перешлите пост из канала или пришлите ссылку на канал.\n"
            "Список источников — /list"
        )

    return router


async def run_control_bot(bot_token: str, router: Router) -> None:
    """Держит бота управления запущенным рядом с репостером."""
    bot = AioBot(token=bot_token,
                 default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    me = await bot.get_me()
    log.info("Управление источниками: пишите @%s (/start)", me.username)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
