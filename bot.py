import asyncio
import logging
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

import config
from phrases import PHRASES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()


async def send_daily_call(chat_id: int) -> None:
    phrase = random.choice(PHRASES)
    await bot.send_message(chat_id=chat_id, text=phrase)
    logger.info(f"Sent daily call to chat {chat_id}")


async def scheduler() -> None:
    tz = ZoneInfo(config.TIMEZONE)

    while True:
        now = datetime.now(tz)
        target_minute = random.randint(0, 59)

        target = now.replace(hour=21, minute=target_minute, second=0, microsecond=0)
        if now >= target:
            target = target.replace(day=target.day + 1)

        wait_seconds = (target - now).total_seconds()
        logger.info(f"Next call at {target.strftime('%Y-%m-%d %H:%M')} — waiting {wait_seconds:.0f}s")

        await asyncio.sleep(wait_seconds)

        if random.random() <= config.CALL_PROBABILITY:
            for chat_id in config.CHAT_IDS:
                try:
                    await send_daily_call(chat_id)
                except Exception as e:
                    logger.error(f"Failed to send to {chat_id}: {e}")
        else:
            logger.info("Skipped today (random day off)")


@dp.message(Command("братан"))
async def cmd_bratan(message: Message) -> None:
    phrase = random.choice(PHRASES)
    await message.answer(phrase)


async def main() -> None:
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
