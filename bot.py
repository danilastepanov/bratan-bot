import asyncio
import logging
import random
import re
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BotCommand, Message

import config
from phrases import (
    HOLIDAY_PHRASES,
    MEDIA_PHRASES,
    PRAISE_PHRASES,
    REMINDER_FIRE_PHRASES,
    REMINDER_SET_PHRASES,
    WEATHER_BAD_PHRASES,
    WEATHER_GOOD_PHRASES,
    WEATHER_PHRASES,
    WIKI_PHRASES,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# --- Shuffle-деки: гарантируют отсутствие повторов подряд ---
_phrase_deck: deque[str] = deque()
_member_deck: deque[str] = deque()
_media_deck: deque[str] = deque()

MEDIA_REACTION_CHANCE = 0.15  # 15% шанс реакции на фото/стикер

# WMO weather codes → русское описание + тип (good/bad/neutral)
_WMO: dict[int, tuple[str, str]] = {
    0:  ("ясно",                  "good"),
    1:  ("преимущественно ясно",  "good"),
    2:  ("переменная облачность", "neutral"),
    3:  ("пасмурно",              "neutral"),
    45: ("туман",                 "bad"),
    48: ("туман с инеем",         "bad"),
    51: ("лёгкая морось",         "bad"),
    53: ("морось",                "bad"),
    55: ("сильная морось",        "bad"),
    61: ("небольшой дождь",       "bad"),
    63: ("дождь",                 "bad"),
    65: ("сильный дождь",         "bad"),
    71: ("небольшой снег",        "bad"),
    73: ("снег",                  "bad"),
    75: ("сильный снег",          "bad"),
    77: ("снежная крупа",         "bad"),
    80: ("ливень",                "bad"),
    81: ("сильный ливень",        "bad"),
    82: ("очень сильный ливень",  "bad"),
    85: ("снегопад",              "bad"),
    86: ("сильный снегопад",      "bad"),
    95: ("гроза",                 "bad"),
    96: ("гроза с градом",        "bad"),
    99: ("гроза с сильным градом","bad"),
}

# Хранилище активных напоминаний: task_id → asyncio.Task
_reminders: dict[str, asyncio.Task] = {}


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


# ---------------------------------------------------------------------------
# Погода
# ---------------------------------------------------------------------------

async def fetch_weather(city: str) -> str | None:
    """Возвращает готовый текст ответа или None если город не найден."""
    try:
        async with aiohttp.ClientSession() as session:
            # 1. Геокодинг: название → координаты
            geo_url = "https://geocoding-api.open-meteo.com/v1/search"
            async with session.get(geo_url, params={"name": city, "count": 1, "language": "ru"}) as r:
                geo = await r.json()
            if not geo.get("results"):
                return None
            result = geo["results"][0]
            lat, lon = result["latitude"], result["longitude"]
            city_name = result.get("name", city)

            # 2. Погода по координатам
            weather_url = "https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,wind_speed_10m,weather_code",
                "wind_speed_unit": "ms",
            }
            async with session.get(weather_url, params=params) as r:
                data = await r.json()

        current = data["current"]
        temp = round(current["temperature_2m"])
        wind = round(current["wind_speed_10m"])
        code = current["weather_code"]
        desc, kind = _WMO.get(code, ("неизвестно", "neutral"))

        pool = (
            WEATHER_GOOD_PHRASES if kind == "good"
            else WEATHER_BAD_PHRASES if kind == "bad"
            else WEATHER_PHRASES
        )
        phrase = random.choice(pool)
        return phrase.format(city=city_name, temp=temp, desc=desc, wind=wind)

    except Exception as e:
        logger.error(f"Weather fetch error: {e}")
        return None


# ---------------------------------------------------------------------------
# Случайный факт из Википедии
# ---------------------------------------------------------------------------

WIKI_API = "https://ru.wikipedia.org/api/rest_v1/page/random/summary"
MAX_FACT_LEN = 500  # макс. символов из описания статьи


async def fetch_wiki_fact() -> str | None:
    """Возвращает готовый текст с фактом или None при ошибке."""
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"User-Agent": "bratan-bot/1.0"}
            async with session.get(WIKI_API, headers=headers) as r:
                data = await r.json()

        title = data.get("title", "Неизвестно")
        extract = data.get("extract", "").strip()
        url = data.get("content_urls", {}).get("desktop", {}).get("page", "")

        # Если это страница-дизамбиг или нет описания — пропускаем
        if not extract or data.get("type") == "disambiguation":
            return None

        # Обрезаем до MAX_FACT_LEN, не разрывая слово
        if len(extract) > MAX_FACT_LEN:
            extract = extract[:MAX_FACT_LEN].rsplit(" ", 1)[0] + "..."

        phrase = random.choice(WIKI_PHRASES)
        return phrase.format(title=title, fact=extract, url=url)

    except Exception as e:
        logger.error(f"Wiki fetch error: {e}")
        return None


# ---------------------------------------------------------------------------
# Напоминания
# ---------------------------------------------------------------------------

# Парсит строки вида: "через 2 часа 30 минут", "через 1 час", "через 45 минут", "через 3 дня"
_TIME_RE = re.compile(
    r"(?:через\s+)?"
    r"(?:(\d+)\s*(?:д(?:ень|ня|ней)|d))?"
    r"\s*(?:(\d+)\s*(?:ч(?:ас(?:а|ов)?)?|h))?"
    r"\s*(?:(\d+)\s*(?:м(?:ин(?:уты?|ут)?)?|m))?",
    re.IGNORECASE,
)


def parse_reminder(text: str) -> tuple[int, str] | None:
    """
    Парсит '/напомни через 2 часа 30 минут сделать что-то'
    Возвращает (секунды, текст_напоминания) или None.
    """
    # Убираем команду
    text = re.sub(r"^/напомни\s*", "", text, flags=re.IGNORECASE).strip()

    m = _TIME_RE.match(text)
    if not m or not any(m.groups()):
        return None

    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    seconds = days * 86400 + hours * 3600 + minutes * 60
    if seconds <= 0:
        return None

    reminder_text = text[m.end():].strip().lstrip(",").lstrip("-").strip()
    if not reminder_text:
        reminder_text = "кое-что важное"
    return seconds, reminder_text


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


# --- Команда /помощь ---
@dp.message(Command("помощь"))
async def cmd_help(message: Message) -> None:
    if message.chat.id not in config.CHAT_IDS:
        return
    text = (
        "ХА!! БРАТАН ОБЪЯСНЯЕТ ЧТО УМЕЕТ!!\n\n"
        "⚔️ <b>Команды:</b>\n\n"
        "/братан — Братан хвалит случайного участника\n"
        "/погода <i>город</i> — Узнать текущую погоду\n"
        "/факт — Случайный факт из Википедии\n"
        "/напомни через <i>N ч M мин текст</i> — Поставить напоминание\n"
        "/помощь — Это сообщение\n\n"
        "🤖 <b>Автоматически:</b>\n\n"
        "• Раз в день (12:00–18:00) — похвала случайному участнику\n"
        "• В праздники — тематические поздравления\n"
        "• На фото и стикеры — реакция с шансом 15%\n\n"
        "БРАТАН ВСЕГДА НА СТРАЖЕ!! БУГИ ВУГИ!!"
    )
    await message.reply(text, parse_mode="HTML")


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


# --- Команда /погода ---
@dp.message(Command("погода"))
async def cmd_weather(message: Message) -> None:
    if message.chat.id not in config.CHAT_IDS:
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.reply("ХА!! Братан не знает где смотреть!! Напиши: /погода Москва")
        return
    city = args[1].strip()
    await message.reply("Братан идёт на разведку погоды!! Секунду!! ХА!!")
    text = await fetch_weather(city)
    if text is None:
        await message.reply(f"ХА!! Братан не нашёл такой город — «{city}»!! Проверь название!!")
    else:
        await message.reply(text)


# --- Команда /факт ---
@dp.message(Command("факт"))
async def cmd_fact(message: Message) -> None:
    if message.chat.id not in config.CHAT_IDS:
        return
    await message.reply("БРАТАН ИДЁТ В БИБЛИОТЕКУ ЗНАНИЙ!! СЕКУНДУ!!")
    # Пробуем до 3 раз — иногда попадаются пустые статьи
    for _ in range(3):
        text = await fetch_wiki_fact()
        if text:
            await message.reply(text)
            return
    await message.reply("ХА!! БРАТАН ИСКАЛ ФАКТ НО ВИКИ МОЛЧИТ!! ПОПРОБУЙ ЕЩЁ РАЗ!! БРАТАН НЕ СДАЁТСЯ!!")


# --- Команда /напомни ---
@dp.message(Command("напомни"))
async def cmd_remind(message: Message) -> None:
    if message.chat.id not in config.CHAT_IDS:
        return
    parsed = parse_reminder(message.text or "")
    if parsed is None:
        await message.reply(
            "ХА!! Братан не понял!! Пиши так:\n"
            "/напомни через 2 часа 30 минут встреча\n"
            "/напомни через 1 час позвонить\n"
            "/напомни через 45 минут обед"
        )
        return

    seconds, reminder_text = parsed
    chat_id = message.chat.id
    username = message.from_user.username
    name = f"@{username}" if username else message.from_user.first_name

    # Подтверждение
    set_phrase = random.choice(REMINDER_SET_PHRASES).format(text=reminder_text)
    await message.reply(set_phrase)

    # Создаём задачу напоминания
    async def _fire() -> None:
        await asyncio.sleep(seconds)
        fire_phrase = random.choice(REMINDER_FIRE_PHRASES).format(name=name, text=reminder_text)
        try:
            await bot.send_message(chat_id=chat_id, text=fire_phrase)
        except Exception as e:
            logger.error(f"Reminder send error: {e}")

    task_key = f"{chat_id}:{message.message_id}"
    task = asyncio.create_task(_fire())
    _reminders[task_key] = task
    task.add_done_callback(lambda t: _reminders.pop(task_key, None))
    logger.info(f"Reminder set for {name} in {chat_id}: '{reminder_text}' in {seconds}s")


# --- Команда /братан: только в разрешённых чатах ---
@dp.message(Command("братан"))
async def cmd_bratan(message: Message) -> None:
    if message.chat.id not in config.CHAT_IDS:
        return
    tz = ZoneInfo(config.TIMEZONE)
    member = next_member()
    await message.answer(get_praise(member, tz))


# --- Настройка меню команд ---
async def setup_bot_commands() -> None:
    commands = [
        BotCommand(command="братан", description="Братан хвалит случайного участника 💪"),
        BotCommand(command="погода", description="Погода в городе — /погода Москва 🌤"),
        BotCommand(command="факт", description="Случайный факт из Википедии 🧠"),
        BotCommand(command="напомни", description="Напоминание — /напомни через 2ч встреча ⏰"),
        BotCommand(command="помощь", description="Список всех команд и возможностей ⚔️"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Bot commands menu set")


# --- Запуск с graceful shutdown ---
async def main() -> None:
    await setup_bot_commands()
    task = asyncio.create_task(scheduler())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        for t in _reminders.values():
            t.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        logger.info("Bot stopped gracefully")


if __name__ == "__main__":
    asyncio.run(main())
