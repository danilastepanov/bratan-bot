import asyncio
import logging
import random
import re
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BotCommand, Message, ReactionTypeEmoji

import config
from phrases import (
    ALIBI_TEMPLATES,
    ANIME_QUIZ,
    ARCHETYPES,
    CONSPIRACY_TEMPLATES,
    HOLIDAY_PHRASES,
    MEDIA_PHRASES,
    MISSIONS,
    PRAISE_PHRASES,
    QUOTES,
    REMINDER_FIRE_PHRASES,
    REMINDER_SET_PHRASES,
    ROAST_TEMPLATES,
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

MEDIA_REACTION_CHANCE = 0.15   # 15% шанс реакции на фото/стикер
EMOJI_REACTION_CHANCE = 0.08   # 8% шанс emoji-реакции на текстовое сообщение
REACTION_EMOJIS = ["🔥", "💯", "⚡", "🏆", "🎉", "🤩", "❤️‍🔥", "👏", "😱", "💪"]

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

# Активные викторины: chat_id → {answer, hint}
_active_quizzes: dict[int, dict] = {}


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
# Аниме — Shikimori API
# ---------------------------------------------------------------------------

_SHIKI_BASE = "https://shikimori.one/api"
_SHIKI_HEADERS = {"User-Agent": "bratan-bot/1.0"}

_SHIKI_STATUS = {
    "released": "Завершено",
    "ongoing": "Выходит",
    "anons": "Анонс",
}

_SHIKI_KIND = {
    "tv": "ТВ-сериал",
    "movie": "Фильм",
    "ova": "OVA",
    "ona": "ONA",
    "special": "Спецвыпуск",
    "music": "Клип",
}


def _strip_bbcode(text: str) -> str:
    """Убирает BBCode-теги из описаний Shikimori."""
    text = re.sub(r"\[url=[^\]]*\](.*?)\[/url\]", r"\1", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\[.*?\]", "", text)
    return text.strip()


async def fetch_anime(query: str) -> str | None:
    try:
        async with aiohttp.ClientSession(headers=_SHIKI_HEADERS) as session:
            # Поиск по названию
            async with session.get(
                f"{_SHIKI_BASE}/animes",
                params={"search": query, "limit": 1, "order": "popularity"},
            ) as r:
                results = await r.json()

            if not results:
                return None

            # Детальная информация (содержит описание и жанры)
            anime_id = results[0]["id"]
            async with session.get(f"{_SHIKI_BASE}/animes/{anime_id}") as r:
                a = await r.json()

        title = a.get("russian") or a.get("name", "—")
        title_orig = a.get("name", "—")
        score = a.get("score") or "—"
        episodes = a.get("episodes") or a.get("episodes_aired") or "—"
        status = _SHIKI_STATUS.get(a.get("status", ""), a.get("status", "—"))
        kind = _SHIKI_KIND.get(a.get("kind", ""), a.get("kind", "—"))
        genres = ", ".join(
            g.get("russian") or g.get("name", "") for g in a.get("genres", [])[:3]
        ) or "—"
        description = _strip_bbcode(a.get("description") or "")
        if len(description) > 280:
            description = description[:280].rsplit(" ", 1)[0] + "..."
        shiki_url = "https://shikimori.one" + (a.get("url") or "")

        text = (
            f"🎌 <b>{title}</b> (<i>{title_orig}</i>)\n\n"
            f"🎬 Тип: <b>{kind}</b>\n"
            f"⭐ Рейтинг: <b>{score}</b>\n"
            f"📺 Эпизодов: <b>{episodes}</b>\n"
            f"📊 Статус: <b>{status}</b>\n"
            f"🎭 Жанры: {genres}\n"
        )
        if description:
            text += f"\n📖 {description}\n"
        text += f"\n🔗 <a href=\"{shiki_url}\">Shikimori</a>"
        return text

    except Exception as e:
        logger.error(f"Anime fetch error: {e}")
        return None


# ---------------------------------------------------------------------------
# Курс валют — ЦБ РФ
# ---------------------------------------------------------------------------

_CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"


async def fetch_currency() -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_CBR_URL) as r:
                data = await r.json(content_type=None)

        valutes = data.get("Valute", {})
        date = data.get("Date", "")[:10]

        def fmt(code: str, flag: str, name: str) -> str:
            v = valutes.get(code, {})
            val = v.get("Value", 0)
            prev = v.get("Previous", 0)
            diff = val - prev
            arrow = "📈" if diff > 0 else "📉" if diff < 0 else "➡️"
            return f"{flag} {name}: <b>{val:.2f} ₽</b> {arrow} <i>({diff:+.2f})</i>"

        return (
            f"💱 <b>Курсы валют ЦБ РФ</b>\n\n"
            f"{fmt('USD', '🇺🇸', 'USD')}\n"
            f"{fmt('EUR', '🇪🇺', 'EUR')}\n"
            f"{fmt('CNY', '🇨🇳', 'CNY')}\n\n"
            f"📅 {date}"
        )

    except Exception as e:
        logger.error(f"Currency fetch error: {e}")
        return None


# ---------------------------------------------------------------------------
# Мем — meme-api.com
# ---------------------------------------------------------------------------

_MEME_URL = "https://meme-api.com/gimme"


async def fetch_meme() -> tuple[str, str] | None:
    """Возвращает (заголовок, url_картинки) или None."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_MEME_URL) as r:
                data = await r.json()

        # Пропускаем NSFW и спойлеры
        if data.get("nsfw") or data.get("spoiler"):
            return None

        url = data.get("url", "")
        title = data.get("title", "")
        if not url:
            return None
        return title, url

    except Exception as e:
        logger.error(f"Meme fetch error: {e}")
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


def _allowed(message: Message) -> bool:
    """Личка — всегда разрешена. Группа — только если в CHAT_IDS."""
    return message.chat.type == "private" or message.chat.id in config.CHAT_IDS


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


# --- Планировщик цитат: 2 раза в день в случайное время между 12:00 и 17:59 ---
async def quote_scheduler() -> None:
    tz = ZoneInfo(config.TIMEZONE)

    while True:
        now = datetime.now(tz)

        # Генерируем два случайных момента в окне [12:00, 17:59] на сегодня
        times = sorted([
            now.replace(hour=random.randint(12, 17), minute=random.randint(0, 59), second=0, microsecond=0)
            for _ in range(2)
        ])

        # Оставляем только те, что ещё не прошли; остальные переносим на завтра
        candidates = []
        for t in times:
            if t <= now:
                t += timedelta(days=1)
            candidates.append(t)

        # Спим до ближайшего и отправляем
        next_time = min(candidates)
        wait = (next_time - now).total_seconds()
        logger.info(f"Next quote at {next_time.strftime('%Y-%m-%d %H:%M')} ({int(wait // 3600)}h {int(wait % 3600 // 60)}m from now)")
        await asyncio.sleep(wait)

        author, text = random.choice(QUOTES)
        quote_text = f"💬 <i>{text}</i>\n\n— <b>{author}</b>"
        for chat_id in config.CHAT_IDS:
            try:
                await bot.send_message(chat_id=chat_id, text=quote_text, parse_mode="HTML")
                logger.info(f"Quote sent to {chat_id}")
            except Exception as e:
                logger.error(f"Failed to send quote to {chat_id}: {e}")


# --- Emoji-реакции + проверка ответа на викторину ---
@dp.message(F.chat.id.in_(config.CHAT_IDS) & F.text & ~F.text.startswith("/"))
async def on_text_react(message: Message) -> None:
    chat_id = message.chat.id
    # Проверяем ответ на активную викторину
    if chat_id in _active_quizzes:
        quiz = _active_quizzes[chat_id]
        if message.text.strip().lower() == quiz["answer"]:
            del _active_quizzes[chat_id]
            name = message.from_user.mention_html()
            await message.reply(
                f"🏆 ПРАВИЛЬНО! {name} знает аниме лучше всех!!\n\n"
                f"Ответ: <b>{quiz['answer'].capitalize()}</b>",
                parse_mode="HTML",
            )
            return

    if random.random() > EMOJI_REACTION_CHANCE:
        return
    emoji = random.choice(REACTION_EMOJIS)
    try:
        await message.react([ReactionTypeEmoji(emoji=emoji)])
    except Exception as e:
        logger.debug(f"React failed: {e}")


# --- Команда /помощь ---
@dp.message(Command("помощь", "pomosh"))
async def cmd_help(message: Message) -> None:
    if not _allowed(message):
        return
    text = (
        "ХА!! БРАТАН ОБЪЯСНЯЕТ ЧТО УМЕЕТ!!\n\n"
        "⚔️ <b>Команды:</b>\n\n"
        "/братан — Братан хвалит случайного участника\n"
        "/погода <i>город</i> — Узнать текущую погоду\n"
        "/факт — Случайный факт из Википедии\n"
        "/цитата — Цитата аниме персонажа\n"
        "/аниме <i>название</i> — Инфо об аниме с Shikimori\n"
        "/обнять <i>@user</i> — Обнять кого-нибудь 🤗\n"
        "/погладить <i>@user</i> — Погладить кого-нибудь 🥺\n"
        "/потыкать <i>@user</i> — Потыкать кого-нибудь 👉\n"
        "/кто <i>вопрос</i> — Братан выбирает случайного участника 🎯\n"
        "/дуэль <i>@user</i> — Вызов на аниме-дуэль ⚔️\n"
        "/топдуэль — Топ-5 победителей дуэлей 🏆\n"
        "/курс — Курс USD, EUR, CNY от ЦБ РФ\n"
        "/мем — Случайный мем\n"
        "/напомни через <i>N ч M мин текст</i> — Поставить напоминание\n"
        "/помощь — Это сообщение\n\n"
        "🤖 <b>Автоматически:</b>\n\n"
        "• Раз в день (12:00–18:00) — похвала случайному участнику\n"
        "• В праздники — тематические поздравления\n"
        "• На фото и стикеры — реакция с шансом 15%\n"
        "• На текстовые сообщения — emoji-реакция с шансом 8%\n\n"
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
@dp.message(Command("погода", "pogoda"))
async def cmd_weather(message: Message) -> None:
    if not _allowed(message):
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
@dp.message(Command("факт", "fakt"))
async def cmd_fact(message: Message) -> None:
    if not _allowed(message):
        return
    await message.reply("БРАТАН ИДЁТ В БИБЛИОТЕКУ ЗНАНИЙ!! СЕКУНДУ!!")
    # Пробуем до 3 раз — иногда попадаются пустые статьи
    for _ in range(3):
        text = await fetch_wiki_fact()
        if text:
            await message.reply(text)
            return
    await message.reply("ХА!! БРАТАН ИСКАЛ ФАКТ НО ВИКИ МОЛЧИТ!! ПОПРОБУЙ ЕЩЁ РАЗ!! БРАТАН НЕ СДАЁТСЯ!!")


# --- Команда /цитата ---
@dp.message(Command("цитата", "quote"))
async def cmd_quote(message: Message) -> None:
    if not _allowed(message):
        return
    author, text = random.choice(QUOTES)
    await message.reply(
        f"💬 <i>{text}</i>\n\n— <b>{author}</b>",
        parse_mode="HTML",
    )


# --- Команда /аниме ---
@dp.message(Command("аниме", "anime"))
async def cmd_anime(message: Message) -> None:
    if not _allowed(message):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.reply("ХА!! Братан не знает что искать!! Напиши: /аниме Наруто")
        return
    query = args[1].strip()
    await message.reply("БРАТАН ИДЁТ НА SHIKIMORI!! СЕКУНДУ!!")
    text = await fetch_anime(query)
    if text is None:
        await message.reply(f"ХА!! Братан не нашёл аниме «{query}»!! Проверь название!!")
    else:
        await message.reply(text, parse_mode="HTML", disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# Обнимашки — nekos.best
# ---------------------------------------------------------------------------

_NEKOS_BASE = "https://nekos.best/api/v2"


async def fetch_nekos(category: str) -> str | None:
    """Возвращает URL гифки или None."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{_NEKOS_BASE}/{category}") as r:
                data = await r.json()
        results = data.get("results", [])
        if results:
            return results[0]["url"]
    except Exception as e:
        logger.error(f"nekos.best error: {e}")
    return None


@dp.message(Command("обнять", "hug"))
async def cmd_hug(message: Message) -> None:
    if not _allowed(message):
        return

    args = (message.text or "").split(maxsplit=1)
    target = args[1].strip() if len(args) > 1 and args[1].strip() else "всех братанов"

    sender_name = (
        f"@{message.from_user.username}"
        if message.from_user and message.from_user.username
        else (message.from_user.full_name if message.from_user else "Братан")
    )

    gif_url = await fetch_nekos("hug")
    caption = f"🤗 <b>{sender_name}</b> обнимает <b>{target}</b>!!"

    if gif_url:
        try:
            await message.reply_animation(gif_url, caption=caption, parse_mode="HTML")
            return
        except Exception as e:
            logger.debug(f"reply_animation failed: {e}")
    await message.reply(caption, parse_mode="HTML")


# --- Команда /погладить ---
@dp.message(Command("погладить", "pat"))
async def cmd_pat(message: Message) -> None:
    if not _allowed(message):
        return

    args = (message.text or "").split(maxsplit=1)
    target = args[1].strip() if len(args) > 1 and args[1].strip() else "кого-то"

    sender_name = (
        f"@{message.from_user.username}"
        if message.from_user and message.from_user.username
        else (message.from_user.full_name if message.from_user else "Братан")
    )

    gif_url = await fetch_nekos("pat")
    caption = f"🥺 <b>{sender_name}</b> гладит <b>{target}</b>!!"

    if gif_url:
        try:
            await message.reply_animation(gif_url, caption=caption, parse_mode="HTML")
            return
        except Exception as e:
            logger.debug(f"reply_animation failed: {e}")
    await message.reply(caption, parse_mode="HTML")


# --- Команда /потыкать ---
@dp.message(Command("потыкать", "poke"))
async def cmd_poke(message: Message) -> None:
    if not _allowed(message):
        return

    args = (message.text or "").split(maxsplit=1)
    target = args[1].strip() if len(args) > 1 and args[1].strip() else "кого-то"

    sender_name = (
        f"@{message.from_user.username}"
        if message.from_user and message.from_user.username
        else (message.from_user.full_name if message.from_user else "Братан")
    )

    gif_url = await fetch_nekos("poke")
    caption = f"👉 <b>{sender_name}</b> тыкает <b>{target}</b>!!"

    if gif_url:
        try:
            await message.reply_animation(gif_url, caption=caption, parse_mode="HTML")
            return
        except Exception as e:
            logger.debug(f"reply_animation failed: {e}")
    await message.reply(caption, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /кто — выбирает случайного участника
# ---------------------------------------------------------------------------

_KTO_FALLBACKS = [
    "самый умный",
    "главный братан",
    "настоящий герой",
    "лучший в команде",
    "избранный",
]


@dp.message(Command("кто", "kto"))
async def cmd_kto(message: Message) -> None:
    if not _allowed(message):
        return

    args = (message.text or "").split(maxsplit=1)
    question = args[1].strip() if len(args) > 1 and args[1].strip() else None

    if not config.MEMBERS:
        await message.reply("ХА!! БРАТАН НЕ ЗНАЕТ УЧАСТНИКОВ!! ЗАДАЙ MEMBERS В .ENV!!")
        return

    chosen = random.choice(config.MEMBERS)

    if question:
        answer = f"🎯 ХА!! БРАТАН РЕШИЛ!! {question.upper()} — это <b>{chosen}</b>!!"
    else:
        role = random.choice(_KTO_FALLBACKS)
        answer = f"🎯 ХА!! БРАТАН ВЫБРАЛ!! <b>{chosen}</b> — {role}!!"

    await message.reply(answer, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /дуэль — аниме-дуэль + статистика побед (SQLite)
# ---------------------------------------------------------------------------

DB_PATH = "duel_stats.db"


async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS duel_wins ("
            "  username TEXT PRIMARY KEY,"
            "  wins     INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        await db.commit()


async def db_add_win(username: str) -> int:
    """Добавляет победу и возвращает новое суммарное кол-во побед."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO duel_wins(username, wins) VALUES(?, 1)"
            " ON CONFLICT(username) DO UPDATE SET wins = wins + 1",
            (username,),
        )
        await db.commit()
        async with db.execute(
            "SELECT wins FROM duel_wins WHERE username = ?", (username,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 1


async def db_top(limit: int = 5) -> list[tuple[str, int]]:
    """Возвращает топ-N победителей."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, wins FROM duel_wins ORDER BY wins DESC LIMIT ?",
            (limit,),
        ) as cur:
            return await cur.fetchall()


_DUEL_WIN_PHRASES = [
    "{winner} СНЁС {loser} ОДНИМ УДАРОМ!! КАК ГИТАРАКРА!!",
    "{winner} ПОБЕДИЛ!! {loser} ДАЖЕ НЕ УСПЕЛ ДОСТАТЬ КАТАНУ!!",
    "БРАТАН ВИДЕЛ — {winner} АКТИВИРОВАЛ ЧИТКАЙТ И УНИЧТОЖИЛ {loser}!!",
    "{loser} ЛЕЖИТ В НОКАУТЕ!! {winner} СТОИТ НА ВЕРШИНЕ!!",
    "{winner} ИСПОЛЬЗОВАЛ ЗАПРЕЩЁННЫЙ ПРИЁМ!! {loser} ПОБЕЖДЁН!!",
    "ПОСЛЕ ДОЛГОЙ БИТВЫ {winner} ВЫШЕЛ ПОБЕДИТЕЛЕМ!! {loser} УВАЖАЕТ СИЛУ!!",
    "{winner} ПРОБУДИЛ СКРЫТУЮ СИЛУ И СМЁЛ {loser} С АРЕНЫ!!",
    "{loser} НЕДООЦЕНИЛ {winner}!! КЛАССИЧЕСКАЯ ОШИБКА ЗЛОДЕЯ!!",
    "{winner} ПРОЧИТАЛ ВСЕ ХОДЫ {loser} НАПЕРЁД!! ЧИТАТЕЛЬ МАНГИ ИМЕЕТ ПРЕИМУЩЕСТВО!!",
    "НИЧЬЯ?? НЕТ!! В ПОСЛЕДНИЙ МОМЕНТ {winner} НАНЁС ФИНАЛЬНЫЙ УДАР!!",
]


@dp.message(Command("дуэль", "duel"))
async def cmd_duel(message: Message) -> None:
    if not _allowed(message):
        return

    args = (message.text or "").split(maxsplit=1)
    sender = (
        f"@{message.from_user.username}"
        if message.from_user and message.from_user.username
        else (message.from_user.full_name if message.from_user else "Братан")
    )

    if len(args) > 1 and args[1].strip():
        # Конкретный противник указан — вызов от отправителя
        opponent = args[1].strip()
        if opponent == sender:
            await message.reply("ХА!! БРАТАН НЕ ДЕРЁТСЯ САМ С СОБОЙ!! ВЫЗОВИ КОГО-ТО ДРУГОГО!!")
            return
        challenger, opponent = sender, opponent
    elif len(config.MEMBERS) >= 2:
        # Без аргумента — два случайных участника из MEMBERS
        challenger, opponent = random.sample(config.MEMBERS, 2)
    else:
        await message.reply("ХА!! НАПИШИ КОГО ВЫЗЫВАЕШЬ: /дуэль @user!!")
        return

    # Случайный победитель
    winner, loser = random.choice([(challenger, opponent), (opponent, challenger)])
    phrase = random.choice(_DUEL_WIN_PHRASES).format(winner=f"<b>{winner}</b>", loser=f"<b>{loser}</b>")

    total_wins = await db_add_win(winner)

    await message.reply(
        f"⚔️ <b>{challenger}</b> vs <b>{opponent}</b>!!\n\n"
        f"<i>...бой начался...</i>\n\n"
        f"🏆 {phrase}\n\n"
        f"📊 Побед у <b>{winner}</b>: <b>{total_wins}</b>",
        parse_mode="HTML",
    )


# --- Команда /топдуэль ---
@dp.message(Command("топдуэль", "topduel"))
async def cmd_topduel(message: Message) -> None:
    if not _allowed(message):
        return
    rows = await db_top(5)
    if not rows:
        await message.reply("ХА!! БРАТАН СМОТРИТ — ЕЩЁ НИ ОДНОЙ ДУЭЛИ НЕ БЫЛО!! /дуэль @user ЧТОБЫ НАЧАТЬ!!")
        return

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = [f"{medals[i]} <b>{name}</b> — {wins} побед{'а' if 2 <= wins % 10 <= 4 and wins % 100 not in range(11, 15) else ('а' if wins % 10 == 1 and wins % 100 != 11 else '')}"
             for i, (name, wins) in enumerate(rows)]
    text = "⚔️ <b>ТОП ДУЭЛЯНТОВ:</b>\n\n" + "\n".join(lines) + "\n\nБРАТАН УВАЖАЕТ СИЛЬНЕЙШИХ!!"
    await message.reply(text, parse_mode="HTML")


# --- Команда /курс ---
@dp.message(Command("курс", "kurs"))
async def cmd_currency(message: Message) -> None:
    if not _allowed(message):
        return
    text = await fetch_currency()
    if text is None:
        await message.reply("ХА!! БРАТАН НЕ СМОГ ПОЛУЧИТЬ КУРС!! ЦБ РФ МОЛЧИТ!!")
    else:
        await message.reply(text, parse_mode="HTML")


# --- Команда /мем ---
@dp.message(Command("мем", "mem"))
async def cmd_meme(message: Message) -> None:
    if not _allowed(message):
        return
    # Пробуем до 3 раз — иногда попадаются NSFW
    for _ in range(3):
        result = await fetch_meme()
        if result:
            title, url = result
            try:
                await message.reply_photo(url, caption=f"😂 {title}" if title else "😂")
            except Exception:
                await message.reply(f"😂 {title}\n{url}" if title else f"😂 {url}")
            return
    await message.reply("ХА!! БРАТАН НЕ НАШЁЛ МЕМ!! ИНТЕРНЕТ ПОДВЁЛ!!")


# --- Команда /напомни ---
@dp.message(Command("напомни", "napomni"))
async def cmd_remind(message: Message) -> None:
    if not _allowed(message):
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


# --- Команда /роастани ---
@dp.message(Command("роастани", "roast"))
async def cmd_roast(message: Message) -> None:
    if not _allowed(message):
        return
    target = None
    if message.entities:
        for e in message.entities:
            if e.type == "mention":
                target = message.text[e.offset:e.offset + e.length]
                break
    if not target and message.reply_to_message:
        u = message.reply_to_message.from_user
        target = f"@{u.username}" if u.username else u.first_name
    if not target:
        await message.reply("Укажи кого роастить — /роастани @user или ответь на сообщение")
        return
    roast = random.choice(ROAST_TEMPLATES).format(target)
    await message.reply(roast)


# --- Команда /заговор ---
@dp.message(Command("заговор", "conspiracy"))
async def cmd_conspiracy(message: Message) -> None:
    if not _allowed(message):
        return
    mentions = []
    if message.entities:
        for e in message.entities:
            if e.type == "mention":
                mentions.append(message.text[e.offset:e.offset + e.length])
    if len(mentions) < 2:
        await message.reply("Укажи двух участников — /заговор @user1 @user2")
        return
    text = random.choice(CONSPIRACY_TEMPLATES).format(user1=mentions[0], user2=mentions[1])
    await message.reply(text)


# --- Команда /алиби ---
@dp.message(Command("алиби", "alibi"))
async def cmd_alibi(message: Message) -> None:
    if not _allowed(message):
        return
    target = None
    if message.entities:
        for e in message.entities:
            if e.type == "mention":
                target = message.text[e.offset:e.offset + e.length]
                break
    if not target and message.reply_to_message:
        u = message.reply_to_message.from_user
        target = f"@{u.username}" if u.username else u.first_name
    if not target:
        await message.reply("Укажи кому нужно алиби — /алиби @user или ответь на сообщение")
        return
    text = random.choice(ALIBI_TEMPLATES).format(user=target)
    await message.reply(text)


# --- Команда /миссия ---
@dp.message(Command("миссия", "mission"))
async def cmd_mission(message: Message) -> None:
    if not _allowed(message):
        return
    await message.reply(random.choice(MISSIONS))


# --- Команда /характер ---
@dp.message(Command("характер", "character"))
async def cmd_character(message: Message) -> None:
    if not _allowed(message):
        return
    target = None
    target_name = None
    if message.entities:
        for e in message.entities:
            if e.type == "mention":
                target = message.text[e.offset:e.offset + e.length]
                target_name = target
                break
    if not target:
        u = message.from_user
        target_name = f"@{u.username}" if u.username else u.first_name
    archetype, description = random.choice(ARCHETYPES)
    await message.reply(
        f"🎭 <b>{target_name}</b> — это <b>{archetype}</b>\n\n{description}",
        parse_mode="HTML",
    )


# --- Команда /аниме_викторина ---
@dp.message(Command("аниме_викторина", "animequiz"))
async def cmd_anime_quiz(message: Message) -> None:
    if not _allowed(message):
        return
    chat_id = message.chat.id
    if chat_id in _active_quizzes:
        await message.reply("⚡ Викторина уже идёт! Ответь на текущий вопрос.")
        return
    question, answer, hint = random.choice(ANIME_QUIZ)
    _active_quizzes[chat_id] = {"answer": answer, "hint": hint}
    await message.reply(
        f"🎌 <b>АНИМЕ-ВИКТОРИНА!</b>\n\n"
        f"❓ {question}\n\n"
        f"💡 Подсказка: <i>{hint}</i>",
        parse_mode="HTML",
    )


# --- Команда /братан: только в разрешённых чатах ---
@dp.message(Command("братан", "bratan"))
async def cmd_bratan(message: Message) -> None:
    if not _allowed(message):
        return
    tz = ZoneInfo(config.TIMEZONE)
    member = next_member()
    await message.answer(get_praise(member, tz))


# --- Настройка меню команд ---
async def setup_bot_commands() -> None:
    commands = [
        BotCommand(command="bratan", description="Братан хвалит случайного участника 💪"),
        BotCommand(command="pogoda", description="Погода в городе — /pogoda Москва 🌤"),
        BotCommand(command="fakt", description="Случайный факт из Википедии 🧠"),
        BotCommand(command="napomni", description="Напоминание — /napomni через 2ч встреча ⏰"),
        BotCommand(command="quote", description="Случайная цитата аниме персонажа 💬"),
        BotCommand(command="anime", description="Инфо об аниме — /anime Наруто 🎌"),
        BotCommand(command="hug", description="Обнять кого-нибудь — /hug @user 🤗"),
        BotCommand(command="pat", description="Погладить кого-нибудь — /pat @user 🥺"),
        BotCommand(command="poke", description="Потыкать кого-нибудь — /poke @user 👉"),
        BotCommand(command="kto", description="Кто тут самый X? — /kto вопрос 🎯"),
        BotCommand(command="duel", description="Аниме-дуэль — /duel @user ⚔️"),
        BotCommand(command="topduel", description="Топ-5 победителей дуэлей 🏆"),
        BotCommand(command="kurs", description="Курс валют ЦБ РФ 💱"),
        BotCommand(command="mem", description="Случайный мем 😂"),
        BotCommand(command="roast", description="Зароастить участника — /roast @user 🔥"),
        BotCommand(command="conspiracy", description="Раскрыть заговор — /conspiracy @u1 @u2 🕵️"),
        BotCommand(command="alibi", description="Железное алиби — /alibi @user ⚖️"),
        BotCommand(command="mission", description="Случайная миссия на день 🎯"),
        BotCommand(command="character", description="Какой ты аниме-персонаж — /character @user 🎭"),
        BotCommand(command="animequiz", description="Аниме-викторина 🎌"),
        BotCommand(command="pomosh", description="Список всех команд и возможностей ⚔️"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Bot commands menu set")


# --- Запуск с graceful shutdown ---
async def main() -> None:
    await db_init()
    await setup_bot_commands()
    task_praise = asyncio.create_task(scheduler())
    task_quotes = asyncio.create_task(quote_scheduler())
    try:
        await dp.start_polling(bot)
    finally:
        task_praise.cancel()
        task_quotes.cancel()
        for t in _reminders.values():
            t.cancel()
        try:
            await asyncio.gather(task_praise, task_quotes, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        logger.info("Bot stopped gracefully")


if __name__ == "__main__":
    asyncio.run(main())
