# -*- coding: utf-8 -*-
"""
Вход в аккаунт прямо в чате с ботом — без терминала и без SSH.

Нужен, когда репостер крутится на хостинге, где нет интерактивной консоли,
чтобы ввести код из Telegram. Вы пишете боту /login и шаг за шагом отдаёте
номер, код и (если есть) пароль 2FA; на выходе SESSION_STRING сохраняется
в session.txt рядом со скриптом.

Делается один раз: дальше сессия читается из файла, мастер больше не появляется.

Роутер подключается к общему Dispatcher'у (см. control_bot.py) — с одним токеном
нельзя держать два независимых бота, они подерутся за getUpdates.
"""

import asyncio
import logging
import re
from contextlib import suppress
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message as AioMessage
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

log = logging.getLogger("login")

SESSION_FILE = Path(__file__).resolve().parent / "session.txt"


def read_saved_session() -> Optional[str]:
    """Читает ранее сохранённую SESSION_STRING."""
    if SESSION_FILE.exists():
        value = SESSION_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def save_session(value: str) -> None:
    """Кладёт SESSION_STRING в файл и закрывает его от посторонних."""
    SESSION_FILE.write_text(value, encoding="utf-8")
    with suppress(OSError):
        SESSION_FILE.chmod(0o600)      # на Windows просто ничего не сделает
    log.info("Сессия сохранена в %s", SESSION_FILE.name)


def forget_session() -> None:
    """Удаляет сохранённую сессию — для /logout."""
    with suppress(OSError):
        SESSION_FILE.unlink(missing_ok=True)


class Login(StatesGroup):
    """Шаги мастера входа."""

    phone = State()
    code = State()
    password = State()


def build_login_router(
    credentials: Callable[[], Tuple[int, str]],
    on_session: Callable[[str], Awaitable[None]],
    allowed: Callable[[AioMessage], bool],
) -> Router:
    """
    Собирает роутер с мастером входа.

    credentials — откуда взять (api_id, api_hash) на момент запуска мастера;
                  они могут быть введены прямо перед этим командой /setup;
    on_session  — что сделать с готовой SESSION_STRING (сохранить, запустить репостинг);
    allowed     — проверка, что сообщение от владельца.
    """
    router = Router()
    # клиент живёт между шагами мастера: код проверяется тем же подключением,
    # которым запрашивался
    session: dict = {"client": None, "phone": None}

    async def drop_client() -> None:
        client: Optional[TelegramClient] = session.get("client")
        if client is not None:
            with suppress(Exception):
                await client.disconnect()
        session["client"] = None

    async def finish(message: AioMessage) -> None:
        """Общий финал: сохраняем сессию и передаём её наружу."""
        client: TelegramClient = session["client"]
        me = await client.get_me()
        value = client.session.save()
        save_session(value)
        session["client"] = None          # клиент дальше живёт у репостера
        await message.answer(
            f"✅ Вход выполнен: <b>{me.first_name}</b>\n\n"
            "🔒 <b>Удалите свои сообщения с кодом и паролем из этого чата.</b>"
        )
        await on_session(value)

    @router.message(Command("login"))
    async def cmd_login(message: AioMessage, state: FSMContext) -> None:
        if not allowed(message):
            return
        api_id, api_hash = credentials()
        if not (api_id and api_hash):
            await message.answer(
                "Сначала /setup — без api_id и api_hash подключиться к Telegram нельзя."
            )
            return

        await drop_client()
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()
        session["client"] = client

        await state.set_state(Login.phone)
        await message.answer(
            "🔑 <b>Вход в аккаунт, который будет читать каналы.</b>\n\n"
            "Пришлите номер телефона этого аккаунта в международном формате:\n"
            "<code>+79991234567</code>\n\n"
            "Отменить — /cancel"
        )

    @router.message(Command("cancel"))
    async def cmd_cancel(message: AioMessage, state: FSMContext) -> None:
        if not allowed(message):
            return
        await state.clear()
        await drop_client()
        await message.answer("Отменено.")

    @router.message(Login.phone)
    async def step_phone(message: AioMessage, state: FSMContext) -> None:
        if not allowed(message):
            return
        phone = re.sub(r"[^\d+]", "", message.text or "")
        if not re.fullmatch(r"\+?\d{7,15}", phone):
            await message.answer("❌ Не похоже на номер. Пример: <code>+79991234567</code>")
            return

        client: TelegramClient = session["client"]
        try:
            await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            await message.answer("❌ Telegram не знает такой номер. Проверьте и пришлите снова.")
            return
        except PhoneNumberBannedError:
            await message.answer("⛔️ Этот номер заблокирован в Telegram.")
            return
        except FloodWaitError as err:
            await message.answer(f"⏳ Telegram просит подождать {err.seconds} сек и повторить.")
            return

        session["phone"] = phone
        await state.set_state(Login.code)
        await message.answer(
            "📩 Код отправлен в Telegram (ищите сообщение от «Telegram»).\n\n"
            "⚠️ <b>Пришлите его с пробелами между цифрами</b>, например <code>1 2 3 4 5</code>.\n"
            "Если отправить код слитно, Telegram посчитает его засвеченным и аннулирует."
        )

    @router.message(Login.code)
    async def step_code(message: AioMessage, state: FSMContext) -> None:
        if not allowed(message):
            return
        code = re.sub(r"\D", "", message.text or "")
        if not code:
            await message.answer(
                "❌ В сообщении нет цифр. Пришлите код, например <code>1 2 3 4 5</code>")
            return

        client: TelegramClient = session["client"]
        try:
            await client.sign_in(phone=session["phone"], code=code)
        except SessionPasswordNeededError:
            await state.set_state(Login.password)
            await message.answer(
                "🔐 На аккаунте включена двухэтапная проверка.\n"
                "Пришлите пароль облачной защиты (тот, что запрашивается при входе).\n\n"
                "Сообщение с паролем сразу удалите."
            )
            return
        except PhoneCodeInvalidError:
            await message.answer("❌ Неверный код. Попробуйте ещё раз.")
            return
        except PhoneCodeExpiredError:
            await state.set_state(Login.phone)
            await message.answer("❌ Код истёк. Пришлите номер заново, отправлю новый код.")
            return
        except FloodWaitError as err:
            await message.answer(f"⏳ Telegram просит подождать {err.seconds} сек.")
            return

        await state.clear()
        await finish(message)

    @router.message(Login.password)
    async def step_password(message: AioMessage, state: FSMContext) -> None:
        if not allowed(message):
            return
        client: TelegramClient = session["client"]
        try:
            await client.sign_in(password=(message.text or "").strip())
        except PasswordHashInvalidError:
            await message.answer("❌ Неверный пароль. Попробуйте ещё раз.")
            return
        except FloodWaitError as err:
            await message.answer(f"⏳ Telegram просит подождать {err.seconds} сек.")
            return

        await state.clear()
        await finish(message)

    return router


async def session_via_bot(bot_token: str, api_id: int, api_hash: str,
                          owner_id: Optional[int] = None) -> str:
    """
    Самостоятельный вход: поднимает бота только ради мастера и возвращает
    SESSION_STRING. Нужен для userbot.py, где нет постоянного бота настройки.
    """
    # импорты локальные: при обычной работе aiogram-бот не поднимается
    from aiogram import Bot as AioBot
    from aiogram import Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import Message as AioMessage

    done: asyncio.Future = asyncio.get_running_loop().create_future()
    owner = {"id": owner_id}

    def allowed(message: "AioMessage") -> bool:
        if owner["id"] is None and message.from_user:
            owner["id"] = message.from_user.id
        return message.from_user is not None and message.from_user.id == owner["id"]

    async def on_session(value: str) -> None:
        if not done.done():
            done.set_result(value)

    bot = AioBot(token=bot_token,
                 default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_login_router(lambda: (api_id, api_hash),
                                         on_session, allowed))

    me = await bot.get_me()
    log.warning("=" * 68)
    log.warning("Сессии нет. Откройте @%s в Telegram и отправьте /login", me.username)
    log.warning("Терминал не нужен — весь вход проходит в чате с ботом.")
    log.warning("=" * 68)

    polling = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))
    try:
        return await done
    finally:
        # мастер отработал — polling обязан остановиться, иначе репостинг
        # так и не стартует. Сначала просим по-хорошему, потом снимаем задачу.
        with suppress(Exception):
            await dp.stop_polling()
        polling.cancel()
        with suppress(Exception):
            await polling
        with suppress(Exception):
            await bot.session.close()
