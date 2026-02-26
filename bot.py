import asyncio
import logging
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

import config
from phrases import PHRASES, WARM_UP_PHRASES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()


async def scheduler() -> None:
    tz = ZoneInfo(config.TIMEZONE)

    while True:
        now = datetime.now(tz)
        target_minute = random.randint(0, 59)

        # Основной зов — случайная минута между 21:00 и 21:59
        main_call = now.replace(hour=21, minute=target_minute, second=0, microsecond=0)
        if now >= main_call:
            main_call = main_call + timedelta(days=1)

        # Подготовительная фраза — за 30 минут до основного зова
        warm_up_call = main_call - timedelta(minutes=30)

        logger.info(f"Warm-up at {warm_up_call.strftime('%Y-%m-%d %H:%M')}, main call at {main_call.strftime('%Y-%m-%d %H:%M')}")

        # Решаем заранее — будем ли слать сегодня
        send_today = random.random() <= config.CALL_PROBABILITY

        # Ждём до подготовительной фразы
        wait_warm_up = (warm_up_call - datetime.now(tz)).total_seconds()
        if wait_warm_up > 0:
            await asyncio.sleep(wait_warm_up)

        if send_today:
            for chat_id in config.CHAT_IDS:
                try:
                    phrase = random.choice(WARM_UP_PHRASES)
                    await bot.send_message(chat_id=chat_id, text=phrase)
                    logger.info(f"Sent warm-up to chat {chat_id}")
                except Exception as e:
                    logger.error(f"Failed warm-up to {chat_id}: {e}")

        # Ждём ещё 30 минут до основного зова
        wait_main = (main_call - datetime.now(tz)).total_seconds()
        if wait_main > 0:
            await asyncio.sleep(wait_main)

        if send_today:
            for chat_id in config.CHAT_IDS:
                try:
                    phrase = random.choice(PHRASES)
                    await bot.send_message(chat_id=chat_id, text=phrase)
                    logger.info(f"Sent main call to chat {chat_id}")
                except Exception as e:
                    logger.error(f"Failed main call to {chat_id}: {e}")
        else:
            logger.info("Skipped today (random day off)")


@dp.message(Command("братан"))
async def cmd_bratan(message: Message) -> None:
    phrase = random.choice(PHRASES)
    await message.answer(phrase)


@dp.message(Command("скоро"))
async def cmd_warmup(message: Message) -> None:
    phrase = random.choice(WARM_UP_PHRASES)
    await message.answer(phrase)


async def main() -> None:
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
