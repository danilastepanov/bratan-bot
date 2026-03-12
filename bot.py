import asyncio
import logging
import random
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

import config
from phrases import HOLIDAY_PHRASES, MEDIA_PHRASES, PRAISE_PHRASES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# --- Shuffle-деки: гарантируют отсутствие повторов подряд ---
_phrase_deck: deque[str] = deque()
_member_deck: deque[str] = deque()
_media_deck: deque[str] = deque()

MEDIA_REACTION_CHANCE = 0.15  # 15% шанс реакции на фото/стикер


def _refill(deck: deque, source: list) -> None:
    shuffled = source.copy()
    random.shuffle(shuffled)
    deck.extend(shuffled)


def next_phrase() -> str:
    if not _phrase_deck:
        _refill(_phrase_deck, PRAISE_PHRASES)
    return _phrase_deck.popleft()


def next_member() -> str:
    if not _member_deck:
        _refill(_member_deck, config.MEMBERS)
    return _member_deck.popleft()


def next_media_phrase(name: str | None = None) -> str:
    if not _media_deck:
        _refill(_media_deck, MEDIA_PHRASES)
    phrase = _media_deck.popleft()
    if name and "{name}" in phrase:
        return phrase.format(name=name)
    # Если имя не передано, пропускаем фразы с {name}
    if "{name}" in phrase:
        fallback = [p for p in MEDIA_PHRASES if "{name}" not in p]
        return random.choice(fallback)
    return phrase


def get_praise(member: str, tz: ZoneInfo | None = None) -> str:
    if tz:
        today = datetime.now(tz)
        holiday_phrases = HOLIDAY_PHRASES.get((today.month, today.day))
        if holiday_phrases:
            phrase = random.choice(holiday_phrases)
            logger.info(f"Holiday ({today.month}/{today.day}) — using holiday phrase")
            return phrase.format(name=member)
    return next_phrase().format(name=member)


# --- Планировщик: 1 сообщение в день в случайное время между 12:00 и 17:59 ---
async def scheduler() -> None:
    tz = ZoneInfo(config.TIMEZONE)

    while True:
        now = datetime.now(tz)

        # Выбираем случайное время внутри окна
        target_hour = random.randint(12, 17)
        target_minute = random.randint(0, 59)

        # Пробуем сегодня — если уже прошло, берём завтра
        candidate = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)

        wait = (candidate - now).total_seconds()
        logger.info(
            f"Next praise at {candidate.strftime('%Y-%m-%d %H:%M')} "
            f"({int(wait // 3600)}h {int(wait % 3600 // 60)}m from now)"
        )
        await asyncio.sleep(wait)

        member = next_member()
        for chat_id in config.CHAT_IDS:
            text = get_praise(member, tz)  # праздник или обычная фраза; каждый чат своя
            try:
                await bot.send_message(chat_id=chat_id, text=text)
                logger.info(f"Praised {member} in chat {chat_id}")
            except Exception as e:
                logger.error(f"Failed to send to {chat_id}: {e}")


# --- Реакция на фото и стикеры (15% шанс) ---
@dp.message(F.chat.id.in_(config.CHAT_IDS) & (F.photo | F.sticker))
async def on_media(message: Message) -> None:
    if random.random() > MEDIA_REACTION_CHANCE:
        return
    username = message.from_user.username
    name = f"@{username}" if username else message.from_user.first_name
    # Реагируем только если автор есть в списке участников
    sender = f"@{username}" if username else None
    member = sender if sender in config.MEMBERS else None
    await message.reply(next_media_phrase(member or name))
    logger.info(f"Media reaction sent for {name} in chat {message.chat.id}")


# --- Команда /братан: только в разрешённых чатах ---
@dp.message(Command("братан"))
async def cmd_bratan(message: Message) -> None:
    if message.chat.id not in config.CHAT_IDS:
        return
    tz = ZoneInfo(config.TIMEZONE)
    member = next_member()
    await message.answer(get_praise(member, tz))


# --- Запуск с graceful shutdown ---
async def main() -> None:
    task = asyncio.create_task(scheduler())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        logger.info("Bot stopped gracefully")


if __name__ == "__main__":
    asyncio.run(main())
