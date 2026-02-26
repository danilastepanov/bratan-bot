import asyncio
import logging
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

PHRASES = [
    "БРАТАНЫ! Кто со мной?! Моя лучшая подруга — победа, и сегодня я её навещу. Вы идёте или как?",
    "Эй! Я только что подумал о вас. Знаете почему? Потому что без нормальной команды победа — пустышка. Погнали!",
    "СЛУШАЙТЕ МЕНЯ ВНИМАТЕЛЬНО. Сейчас самое время. Я уже готов. Осталось только дождаться вас.",
    "Братья! Настоящий воин не сидит дома в одиночестве. Он зовёт своих людей. Я вас зову. Идёте?",
    "Знаете что меня бесит? Когда все онлайн, но никто ничего не предлагает. Я предлагаю. ПОГНАЛИ!",
    "ХА! Думаете я буду ждать особого случая? Сегодняшний вечер — и есть особый случай. Все ко мне!",
    "Моя лучшая подруга говорит что пора. А я всегда её слушаю. Погнали, братаны!",
    "Вы знаете кто я? Я тот, кто никогда не отступает. И сейчас я не отступлю — зову вас играть!",
    "ВНИМАНИЕ! Это не просьба. Это призыв. Братан зовёт — братаны отзываются. Кто в деле?",
    "Эй! Пока вы тут сидите — время уходит. Я мог бы промолчать, но я не такой. ПОГНАЛИ!",
    "Сегодня я чувствую силу. Знаете что усиливает силу? Правильная компания. Все сюда!",
    "Братаны, я был терпелив весь день. Но терпение закончилось. Пора действовать. Кто со мной?",
    "ВЫ ТОЛЬКО ПОСМОТРИТЕ НА ВРЕМЯ! Это знак. Я в это верю. Погнали пока знак не пропал!",
    "Настоящая дружба проверяется в бою. Сегодня проверим. Все онлайн — жду!",
    "Я мог бы сидеть тихо. Но зачем? Жизнь слишком коротка для тишины. БРАТАНЫ, ПОГНАЛИ!",
    "Знаете что объединяет великих людей? Они не теряют время. Я не теряю. Зову вас. Идёмте!",
    "ХА-ХА-ХА! Отличный вечер чтобы показать на что мы способны. Кто не с нами — тот против нас!",
    "Братаны! Моя интуиция редко ошибается. А она говорит — сегодня наш вечер. ПОГНАЛИ!",
    "Эй вы там! Да-да, именно вы. Хватит делать вид что заняты. Я знаю правду. Погнали играть!",
    "СЛЫШИТЕ?! Это зов. Мой зов. Братан зовёт раз в день — и этот раз сейчас. Все сюда!",
    "Некоторые ждут подходящего момента. Я создаю подходящий момент. Момент создан. Погнали!",
    "Братаны, я долго думал. И пришёл к выводу — нечего думать. Надо действовать. КТО В ДЕЛЕ?!",
    "Вы моя команда или нет?! Команда не бросает своего братана. Все онлайн — жду вас!",
    "ЭТО НЕ УЧЕНИЯ! Повторяю — это не учения. Братан зовёт по-настоящему. Погнали!",
    "Знаете что я думаю о тех кто игнорирует этот призыв? Думаю они ещё пожалеют. ПОГНАЛИ БРАТАНЫ!",
    "Сегодня особый день. Почему? Потому что я так решил. А я обычно прав. Все ко мне!",
    "Братаны! Жизнь — это не спектакль. Это битва. И в битву идут вместе. ПОГНАЛИ!",
    "ХА! Думали я не позову? Всегда зову. Традиция такая. Кто нарушит традицию — не братан!",
    "Эй! Я смотрю на часы и вижу знак. Знак говорит — время. Я говорю — ПОГНАЛИ!",
    "Братаны, без вас победа не та. Совсем не та. Так что идите сюда и сделаем её настоящей!",
]


async def send_daily_call(chat_id: int) -> None:
    phrase = random.choice(PHRASES)
    await bot.send_message(chat_id=chat_id, text=phrase)
    logger.info(f"Sent daily call to chat {chat_id}")


async def scheduler() -> None:
    tz = ZoneInfo(config.TIMEZONE)

    while True:
        now = datetime.now(tz)
        target_hour = 21
        target_minute = random.randint(0, 59)

        target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
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
