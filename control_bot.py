# -*- coding: utf-8 -*-
"""
Настройка и управление репостером прямо в чате с ботом.

Из переменных окружения нужен только BOT_TOKEN — всё остальное вводится командами:

    /setup              api_id и api_hash с my.telegram.org/apps
    /login              вход в аккаунт, который читает каналы
    /target @канал      куда публиковать
    пересланный пост    добавить канал-источник
    /list, /remove N    посмотреть и убрать источники

Настройки лежат в bot_config.json, список источников — в sources.json,
поэтому перезапуск хостинга ничего не теряет.
"""

import html as html_lib
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from aiogram import Bot as AioBot
from aiogram import Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message as AioMessage
from aiogram.types import MessageOriginChannel
from telethon import utils

from login_via_bot import build_login_router, forget_session

log = logging.getLogger("control")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "bot_config.json"
SOURCES_FILE = BASE_DIR / "sources.json"


# --------------------------------------------------------------------------- #
#                            НАСТРОЙКИ НА ДИСКЕ                               #
# --------------------------------------------------------------------------- #


def load_config() -> Dict[str, Any]:
    """Читает bot_config.json: api_id, api_hash, target, owner_id."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        log.error("Не читается %s — считаю настройки пустыми", CONFIG_FILE.name)
        return {}


def save_config(cfg: Dict[str, Any]) -> None:
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def load_sources() -> List[Dict[str, Any]]:
    """Читает sources.json: [{chat_id, title, target}, ...]."""
    if not SOURCES_FILE.exists():
        return []
    try:
        data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
        return [s for s in data if isinstance(s, dict)]
    except (ValueError, OSError):
        log.error("Не читается %s — считаю список пустым", SOURCES_FILE.name)
        return []


def save_sources(sources: List[Dict[str, Any]]) -> None:
    tmp = SOURCES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SOURCES_FILE)


def _esc(text: Optional[str]) -> str:
    return html_lib.escape(str(text or ""))


class Setup(StatesGroup):
    """Шаги ввода api_id и api_hash."""

    api_id = State()
    api_hash = State()


# --------------------------------------------------------------------------- #
#                                  РОУТЕР                                     #
# --------------------------------------------------------------------------- #


def build_router(app: Any) -> Router:
    """
    Собирает роутер управления.

    app — объект репостера (см. hybrid.py). Нужны его поля и методы:
        app.cfg              словарь настроек
        app.routes           {chat_id источника: целевой канал}
        app.client           клиент Telethon или None
        app.save()           сохранить настройки
        app.restart()        поднять/перезапустить репостинг, вернуть текст статуса
        app.check_target()   проверить права бота в канале
        app.resolve(ref)     найти канал аккаунтом (и вступить, если нужно)
    """
    # Порядок важен: aiogram сначала отдаёт событие хендлерам самого роутера
    # и только потом вложенным. Поэтому catch-all живёт в отдельном роутере,
    # подключённом последним, иначе он съедал бы /login и остальные команды.
    root = Router()
    router = Router()      # команды
    fallback = Router()    # всё остальное: пересланные посты и ссылки

    # кому из чужих уже объяснили, что бот занят — чтобы не отвечать на каждое
    # их сообщение
    refused: set = set()

    async def allowed(message: AioMessage) -> bool:
        """
        Владелец у бота один: им становится тот, кто первым написал.
        Дальше все чужие сообщения отклоняются, сменить владельца нельзя.
        """
        if not message.from_user:
            return False
        user_id = message.from_user.id
        owner_id = app.cfg.get("owner_id")

        if owner_id is None:
            app.cfg["owner_id"] = user_id
            app.save()
            log.info("Владелец бота: %s (%s)", user_id, message.from_user.full_name)
            await message.answer(
                "👑 <b>Вы владелец этого бота.</b>\n"
                "Управлять им больше никто не сможет.\n"
            )
            return True

        if user_id == owner_id:
            return True

        # чужой: отвечаем один раз и больше не реагируем
        log.warning("Чужой пользователь %s (%s) — отказано",
                    user_id, message.from_user.full_name)
        if user_id not in refused:
            refused.add(user_id)
            await message.answer("⛔️ У этого бота уже есть владелец.")
        return False

    def next_step() -> str:
        """Подсказка, чего не хватает для запуска."""
        if not (app.cfg.get("api_id") and app.cfg.get("api_hash")):
            return ("Следующий шаг: /setup — ввести api_id и api_hash "
                    "с my.telegram.org/apps")
        if not app.has_session():
            return "Следующий шаг: /login — войти в аккаунт, который читает каналы"
        if not app.cfg.get("target"):
            return "Следующий шаг: /target @ваш_канал — куда публиковать"
        if not app.routes:
            return "Следующий шаг: перешлите мне пост из канала, который надо копировать"
        return f"✅ Всё настроено, копирую из {len(app.routes)} источников."

    # --- мастер входа подключаем первым --------------------------------- #
    root.include_router(build_login_router(
        credentials=lambda: (app.cfg.get("api_id"), app.cfg.get("api_hash")),
        on_session=app.on_session,
        allowed=allowed,
    ))

    @router.message(CommandStart())
    async def cmd_start(message: AioMessage) -> None:
        if not await allowed(message):
            return
        await message.answer(
            "👋 Я копирую посты из чужих каналов в ваш.\n\n"
            "<b>Настройка:</b>\n"
            "/setup — api_id и api_hash\n"
            "/login — вход в аккаунт-читатель\n"
            "/target @канал — куда публиковать\n\n"
            "<b>Источники:</b> перешлите мне пост из нужного канала.\n"
            "Нет доступа к каналу — пришлите ссылку <code>https://t.me/+…</code>\n\n"
            "<b>Ещё:</b> /list, /remove номер, /status, /owner, /logout\n\n"
            + next_step()
        )

    # --- api_id и api_hash --------------------------------------------- #

    @router.message(Command("setup"))
    async def cmd_setup(message: AioMessage, state: FSMContext) -> None:
        if not await allowed(message):
            return
        await state.set_state(Setup.api_id)
        await message.answer(
            "🔧 <b>Ключи приложения Telegram.</b>\n\n"
            "Они нужны библиотеке, чтобы вообще подключиться к Telegram, — "
            "это не логин и не пароль, а идентификатор программы.\n\n"
            "1. Откройте <b>my.telegram.org/apps</b> и войдите по номеру;\n"
            "2. API development tools → заполните любые «App title» и «Short name»;\n"
            "3. Пришлите сюда <b>api_id</b> (число).\n\n"
            "Отменить — /cancel"
        )

    @router.message(Setup.api_id)
    async def step_api_id(message: AioMessage, state: FSMContext) -> None:
        if not await allowed(message):
            return
        value = (message.text or "").strip()
        if not value.isdigit():
            await message.answer("❌ api_id — это число, например <code>1234567</code>.")
            return
        await state.update_data(api_id=int(value))
        await state.set_state(Setup.api_hash)
        await message.answer("Принято. Теперь пришлите <b>api_hash</b> "
                             "(32 символа из букв и цифр).")

    @router.message(Setup.api_hash)
    async def step_api_hash(message: AioMessage, state: FSMContext) -> None:
        if not await allowed(message):
            return
        value = (message.text or "").strip()
        if len(value) < 30:
            await message.answer("❌ Не похоже на api_hash — там 32 символа.")
            return
        data = await state.get_data()
        app.cfg["api_id"] = data["api_id"]
        app.cfg["api_hash"] = value
        app.save()
        await state.clear()
        await message.answer(
            "✅ Ключи сохранены.\n\n"
            "🔒 Сообщения с api_hash лучше удалить из чата.\n\n" + next_step())

    # --- целевой канал -------------------------------------------------- #

    @router.message(Command("target"))
    async def cmd_target(message: AioMessage) -> None:
        if not await allowed(message):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            current = app.cfg.get("target") or "не задан"
            await message.answer(
                f"Сейчас публикую в: <b>{_esc(current)}</b>\n\n"
                "Сменить: <code>/target @ваш_канал</code>\n"
                "Бот должен быть там админом с правом «Публикация сообщений».")
            return

        target = parts[1].strip()
        if target.startswith("https://t.me/"):
            target = "@" + target.rsplit("/", 1)[-1]
        if not (target.startswith("@") or target.lstrip("-").isdigit()):
            await message.answer("❌ Пришлите <code>@username</code> канала или его ID.")
            return

        ok, why = await app.check_target(target)
        if not ok:
            await message.answer(f"❌ {why}")
            return

        app.cfg["target"] = target
        app.save()
        # старые источники продолжают идти в свои каналы, если им задана цель
        for chat_id, old in list(app.routes.items()):
            if old == app.cfg.get("previous_target") or old is None:
                app.routes[chat_id] = target
        await message.answer(f"✅ Публикую в <b>{_esc(target)}</b>\n\n" + next_step())
        await app.restart(message)

    # --- источники ------------------------------------------------------ #

    async def add_source(message: AioMessage, entity, title: str) -> None:
        """Ставит канал на копирование и сохраняет список."""
        chat_id = utils.get_peer_id(entity)
        target = app.cfg.get("target")
        if not target:
            await message.answer("Сначала задайте целевой канал: "
                                 "<code>/target @ваш_канал</code>")
            return
        if chat_id in app.routes:
            await message.answer(f"ℹ️ <b>{_esc(title)}</b> уже в списке источников.")
            return

        sources = [s for s in load_sources() if s.get("chat_id") != chat_id]
        sources.append({"chat_id": chat_id, "title": title, "target": target})
        save_sources(sources)
        app.routes[chat_id] = target

        await message.answer(
            f"✅ Добавлен источник: <b>{_esc(title)}</b>\n"
            f"Копирую в {_esc(target)}\n\n"
            f"Всего источников: {len(app.routes)}. Список — /list")
        log.info("Источник добавлен: %s (%s)", title, chat_id)

    @router.message(Command("list"))
    async def cmd_list(message: AioMessage) -> None:
        if not await allowed(message):
            return
        sources = load_sources()
        if not sources:
            await message.answer(
                "Список источников пуст.\nПерешлите мне пост из канала, чтобы добавить.")
            return
        lines = [
            f"{i}. <b>{_esc(s.get('title'))}</b> → {_esc(s.get('target'))}"
            f"{'' if s.get('chat_id') in app.routes else '  ⚠️ недоступен'}"
            for i, s in enumerate(sources, 1)
        ]
        await message.answer("<b>Источники:</b>\n" + "\n".join(lines) +
                             "\n\nУбрать — <code>/remove номер</code>")

    @router.message(Command("remove"))
    async def cmd_remove(message: AioMessage) -> None:
        if not await allowed(message):
            return
        parts = (message.text or "").split()
        sources = load_sources()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("Укажите номер из /list, например <code>/remove 2</code>")
            return
        index = int(parts[1])
        if not 1 <= index <= len(sources):
            await message.answer(f"❌ Нет источника с номером {index}. Смотрите /list")
            return

        removed = sources.pop(index - 1)
        save_sources(sources)
        app.routes.pop(removed.get("chat_id"), None)   # копирование встаёт сразу
        await message.answer(
            f"🗑 Убран источник: <b>{_esc(removed.get('title'))}</b>\n"
            f"Больше из него не копирую. Осталось: {len(sources)}")
        log.info("Источник убран: %s (%s)", removed.get("title"), removed.get("chat_id"))

    # --- служебное ------------------------------------------------------ #

    @router.message(Command("status"))
    async def cmd_status(message: AioMessage) -> None:
        if not await allowed(message):
            return
        sources = load_sources()
        active = sum(1 for s in sources if s.get("chat_id") in app.routes)
        keys = "✅ заданы" if app.cfg.get("api_id") else "❌ нет (/setup)"
        session = "✅ выполнен" if app.has_session() else "❌ нет (/login)"
        await message.answer(
            f"<b>Ключи приложения:</b> {keys}\n"
            f"<b>Вход в аккаунт:</b> {session}\n"
            f"<b>Целевой канал:</b> {_esc(app.cfg.get('target') or 'не задан')}\n"
            f"<b>Активных источников:</b> {active} из {len(sources)}\n"
            f"<b>Репостинг:</b> {'🟢 работает' if app.running else '🔴 остановлен'}\n\n"
            + next_step())

    @router.message(Command("owner"))
    async def cmd_owner(message: AioMessage) -> None:
        if not await allowed(message):
            return
        await message.answer(
            f"👑 Владелец бота: <code>{app.cfg.get('owner_id')}</code> — это вы.\n\n"
            "Сменить владельца нельзя. Если бот попал не в те руки, остановите его, "
            "удалите <code>bot_config.json</code> и запустите заново — "
            "владельцем станет тот, кто напишет первым."
        )

    @router.message(Command("logout"))
    async def cmd_logout(message: AioMessage) -> None:
        if not await allowed(message):
            return
        await app.stop()
        forget_session()
        await message.answer("Сессия удалена. Войти заново — /login")

    @fallback.message()
    async def on_any(message: AioMessage) -> None:
        """Пересланный пост или ссылка на канал — добавляем источник."""
        if not await allowed(message):
            return
        if app.client is None:
            await message.answer("Сначала войдите в аккаунт: /login\n\n" + next_step())
            return

        origin = message.forward_origin
        if isinstance(origin, MessageOriginChannel):
            chat = origin.chat
            try:
                # читать канал будет аккаунт, поэтому проверяем именно его доступ
                entity = await app.client.get_entity(chat.id)
            except Exception:
                await message.answer(
                    f"⚠️ Канал <b>{_esc(chat.title)}</b> найден, но аккаунт, "
                    "от имени которого я читаю, в нём не состоит.\n\n"
                    "Пришлите ссылку-приглашение <code>https://t.me/+…</code> — "
                    "вступлю и добавлю канал.")
                return
            await add_source(message, entity, chat.title or str(chat.id))
            return

        text = (message.text or "").strip()
        if "t.me/" in text or text.startswith("@"):
            try:
                entity = await app.resolve(text)
            except Exception as err:
                await message.answer(f"❌ Не получилось открыть канал: {_esc(err)}")
                return
            await add_source(message, entity, getattr(entity, "title", None) or text)
            return

        await message.answer("Не понял. Перешлите пост из канала или пришлите ссылку.\n\n"
                             + next_step())

    root.include_router(router)
    root.include_router(fallback)
    return root


async def run_control_bot(bot_token: str, router: Router) -> None:
    """Держит бота настройки запущенным рядом с репостером."""
    bot = AioBot(token=bot_token,
                 default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    me = await bot.get_me()
    log.info("Настройка и управление: напишите @%s команду /start", me.username)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
