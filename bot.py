import asyncio
import logging
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

import config
from phrases import PRAISE_PHRASES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()


def get_praise(member: str) -> str:
    phrase = random.choice(PRAISE_PHRASES)
    return phrase.format(name=member)


async def scheduler() -> None:
    tz = ZoneInfo(config.TIMEZONE)

    while True:
        now = datetime.now(tz)

        # Ждём до начала следующего часа
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        wait = (next_hour - datetime.now(tz)).total_seconds()
        logger.info(f"Next tick at {next_hour.strftime('%Y-%m-%d %H:%M')}, sleeping {int(wait)}s")
        await asyncio.sleep(wait)

        now = datetime.now(tz)

        # Отправляем только с 9:00 до 22:00 включительно
        if 9 <= now.hour <= 22:
            member = random.choice(config.MEMBERS)
            text = get_praise(member)

            for chat_id in config.CHAT_IDS:
                try:
                    await bot.send_message(chat_id=chat_id, text=text)
                    logger.info(f"Praised {member} in chat {chat_id}")
                except Exception as e:
                    logger.error(f"Failed to send to {chat_id}: {e}")
        else:
            logger.info(f"Skipping hour {now.hour} — outside active window (9-22)")


@dp.message(Command("братан"))
async def cmd_bratan(message: Message) -> None:
    member = random.choice(config.MEMBERS)
    await message.answer(get_praise(member))


async def main() -> None:
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())