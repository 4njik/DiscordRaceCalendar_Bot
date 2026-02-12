import asyncio
import discord
import json
import os
import logging
from datetime import datetime, timedelta, timezone
from discord.ext import tasks, commands
import aiohttp
from dateutil import parser as date_parser

# ======================
# КОНФИГУРАЦИЯ
# ======================
DISCORD_TOKEN = ""
NOTIFICATION_CHANNEL_ID = 993971368964128768
CHECK_TIME_UTC = 10  # Время ежедневной проверки в UTC (10:00 UTC = 13:00 МСК)

PEPEGA_ROLE_ID = 1467435408530079866

# Разделение лиг на категории
EUROPEAN_LEAGUES = {
    "🏎️ F1": ["Formula 1"],
    "🏍️ MotoGP": ["MotoGP"],
    "🌲 WRC": ["wrc"],
    " endurance WEC": ["wec"]
}

AMERICAN_LEAGUES = {
    "🏁 IMSA": ["IMSA SportsCar Championship"],
    "🇺🇸 NASCAR": ["NASCAR Cup Series"]
}

ALL_LEAGUES = {**EUROPEAN_LEAGUES, **AMERICAN_LEAGUES}

NOTIFIED_FILE = "notified_events.json"
LOG_FILE = "bot.log"

# ======================
# ЛОГИРОВАНИЕ
# ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ======================
# ФИЛЬТРАЦИЯ СОБЫТИЙ
# ======================
def should_skip_event(event, league_name):
    """
    Определяет, нужно ли пропустить событие (практики, квалификации и т.д.)
    Возвращает (пропустить: bool, причина: str)
    """
    name = event.get("strEvent", "").lower()
    league = event.get("strLeague", "").lower()

    # F1: пропускаем практики, квалификации, тесты
    if "formula 1" in league or "f1" in league:
        skip_keywords = [
            "practice", "free practice", "fp1", "fp2", "fp3",
            "qualifying", "sprint shootout", "testing", "test",
            "launch", "pre-season", "pre season", "shakedown"
        ]
        for kw in skip_keywords:
            if kw in name:
                return True, f"пропущено (не гонка: '{kw}')"

        # Оставляем только гонки: "Grand Prix", "GP", "Sprint Race"
        keep_keywords = ["grand prix", " gp ", " gp", "sprint race", "sprint qualifying"]
        if not any(kw in name for kw in keep_keywords):
            return True, f"пропущено (не распознано как гонка)"

    # MotoGP: пропускаем практики и квалификации для некоторых событий
    if "motogp" in league:
        skip_keywords = ["practice", "qualifying", "free practice", "fp1", "fp2", "fp3", "test"]
        for kw in skip_keywords:
            if kw in name:
                return True, f"пропущено (не гонка: '{kw}')"

    # WRC: многоэтапное событие — оставляем все (начало ралли)
    if "wrc" in league:
        return False, "ралли (многоэтапное событие)"

    # Американские лиги: оставляем все события (гонки + пост-обработка)
    if any(league_name.startswith(prefix) for prefix in ["🏁", "🇺🇸"]):
        return False, "американская лига (все события)"

    return False, "гонка"


def is_zero_time(time_str):
    """Проверяет, является ли время '00:00:00' или эквивалентным"""
    if not time_str:
        return True
    time_clean = time_str.strip().split()[0].split('Z')[0].split('+')[0].split('-')[0]
    return time_clean in ["00:00:00", "00:00", "0:00:00", "0:00"]


# ======================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ======================
def load_notified_events():
    if os.path.exists(NOTIFIED_FILE):
        try:
            with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки {NOTIFIED_FILE}: {e}")
    return {
        "european_day_before": [],
        "european_event_day": [],
        "american_post_event": []
    }


def save_notified_events(data):
    try:
        with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Список уведомлённых событий сохранён")
    except Exception as e:
        logger.error(f"Ошибка сохранения {NOTIFIED_FILE}: {e}")


def parse_event_datetime(event):
    """
    Парсит время события из разных полей API.
    Возвращает кортеж: (naive_datetime, time_type, has_precise_time)
    time_type: "utc" | "local" | "date_only" | "unknown"
    has_precise_time: False если время 00:00:00 или отсутствует
    """
    # Проверяем время на 00:00:00
    time_fields = [
        event.get("strTimeUTC", ""),
        event.get("strTime", ""),
        event.get("strTimestamp", "").split('T')[1] if 'T' in event.get("strTimestamp", "") else ""
    ]

    has_zero_time = all(is_zero_time(tf) for tf in time_fields if tf.strip())
    has_any_time = any(tf.strip() for tf in time_fields)

    if has_zero_time and has_any_time:
        # Есть время, но оно 00:00:00 — считаем неточным
        logger.debug(f"Время 00:00:00 для события {event.get('idEvent')} — помечаем как неточное")
        has_precise_time = False
    elif not has_any_time:
        has_precise_time = False
    else:
        has_precise_time = True

    # 1. Пробуем strTimestamp — самый надёжный источник
    ts = event.get("strTimestamp", "").strip()
    if ts:
        try:
            # Нормализуем формат для парсинга
            ts = ts.replace(" ", "T")
            original_ts = ts

            # Обрабатываем разные форматы таймзон
            if ts.endswith("Z") and not ts.endswith("+00:00"):
                ts = ts[:-1] + "+00:00"

            # Парсим с сохранением таймзоны
            dt = date_parser.isoparse(ts)

            if dt.tzinfo:
                # Есть таймзона — определяем тип
                offset = dt.utcoffset()
                if offset and offset.total_seconds() == 0:
                    time_type = "utc"
                else:
                    time_type = "local"
                # Конвертируем в наивный datetime для единообразия
                naive_dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                logger.debug(f"Распарсено strTimestamp: {original_ts} → {naive_dt} ({time_type})")
                return naive_dt, time_type, has_precise_time
            else:
                # Нет таймзоны — это МЕСТНОЕ время трассы
                logger.debug(f"strTimestamp без таймзоны: {ts} → местное время")
                return dt.replace(tzinfo=None), "local", has_precise_time
        except Exception as e:
            logger.warning(f"Ошибка парсинга strTimestamp '{ts}': {e}")

    # 2. Резерв: дата + время из отдельных полей
    date_str = event.get("dateEvent")
    time_str = event.get("strTimeUTC") or event.get("strTime") or ""

    if date_str and time_str.strip() and not is_zero_time(time_str):
        try:
            # Очищаем время
            time_clean = time_str.split()[0].split('Z')[0].split('+')[0].split('-')[0].strip()
            dt_str = f"{date_str}T{time_clean}"
            dt = date_parser.isoparse(dt_str)

            # Определяем тип по источнику поля
            if event.get("strTimeUTC") == time_str.strip():
                time_type = "utc"
            else:
                time_type = "local"

            return dt.replace(tzinfo=None), time_type, has_precise_time
        except Exception as e:
            logger.warning(f"Ошибка парсинга даты/времени '{date_str} {time_str}': {e}")

    # 3. Только дата (или время 00:00:00)
    if date_str:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            time_type = "date_only" if not has_precise_time else "local"
            return dt.replace(tzinfo=None), time_type, has_precise_time
        except Exception as e:
            logger.warning(f"Ошибка парсинга даты '{date_str}': {e}")

    return None, "unknown", False


def create_timestamp_for_discord(naive_dt, time_type, has_precise_time):
    """
    Создаёт timestamp для Discord из наивного datetime.
    Возвращает кортеж: (timestamp, display_type)
    display_type: "precise_local" | "precise_utc" | "date_only" | "unknown"
    """
    if naive_dt is None:
        return None, "unknown"

    if not has_precise_time:
        # Время неточное (00:00:00 или отсутствует) — используем полдень для ориентира
        dt_for_ts = datetime.combine(naive_dt.date(), datetime.min.time()).replace(
            hour=12, minute=0, second=0
        )
        timestamp = int(dt_for_ts.replace(tzinfo=timezone.utc).timestamp())
        return timestamp, "date_only"

    # Для точного времени — используем как есть (без конвертации в UTC!)
    # Discord сам обработает отображение
    dt_for_ts = naive_dt
    if time_type == "utc":
        # Для истинного UTC добавляем таймзону перед конвертацией
        timestamp = int(dt_for_ts.replace(tzinfo=timezone.utc).timestamp())
        return timestamp, "precise_utc"
    else:
        # Для местного времени — используем полдень UTC как ориентир
        # (чтобы избежать смещения из-за часового пояса сервера)
        dt_for_ts = datetime.combine(naive_dt.date(), naive_dt.time() if naive_dt.time() else datetime.min.time())
        timestamp = int(dt_for_ts.replace(tzinfo=timezone.utc).timestamp())
        return timestamp, "precise_local"


def format_time_display(timestamp, display_type, event_name=""):
    """Форматирует отображение времени для embed"""
    if timestamp is None:
        return "⏰ Время неизвестно"

    if display_type == "precise_local":
        return f"⏰ <t:{timestamp}:t> *(местное время трассы)*\n🌍 <t:{timestamp}:R>"
    elif display_type == "precise_utc":
        return f"⏰ <t:{timestamp}:t> *(по Гринвичу)*\n🌍 <t:{timestamp}:R>"
    elif display_type == "date_only":
        # Проверяем, ралли ли это (обычно многоэтапное)
        if "rally" in event_name.lower() or "wrc" in event_name.lower():
            return f"🗓️ <t:{timestamp}:D>\nℹ️ Многоэтапное событие (точное время старта не указано)"
        else:
            return f"🗓️ <t:{timestamp}:D>\n⚠️ Время неизвестно"
    else:
        return "⏰ Время неизвестно"


def format_european_event_message(event, league_name, notif_type):
    """Уведомление для европейских лиг (до/в день события)"""
    name = event.get("strEvent", "Без названия")
    circuit = event.get("strVenue", "").strip()
    country = event.get("strCountry", "").strip()
    thumb = event.get("strThumb", "").strip()

    location = f"{circuit}, {country}" if circuit and country else (circuit or country or "Локация не указана")

    # Парсим время
    naive_dt, time_type, has_precise_time = parse_event_datetime(event)
    timestamp, display_type = create_timestamp_for_discord(naive_dt, time_type, has_precise_time)

    if notif_type == "day_before":
        title = "🔔 УВЕДОМЛЕНИЕ ЗА ДЕНЬ ДО СОБЫТИЯ"
        color = 0xFFA500
    else:
        title = "🏁 СЕГОДНЯ СОСТОИТСЯ СОБЫТИЕ!"
        color = 0xFF0000

    embed = discord.Embed(
        title=title,
        description=f"**{league_name}**",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Название", value=f"**{name}**", inline=False)
    embed.add_field(name="Локация", value=f"📍 {location}", inline=False)

    # Отображаем время
    time_display = format_time_display(timestamp, display_type, name)
    embed.add_field(name="Время старта", value=time_display, inline=False)

    if thumb:
        embed.set_thumbnail(url=thumb)

    footer_parts = []
    if display_type == "precise_local":
        footer_parts.append("ℹ️ Время указано в местном поясе трассы")
    elif display_type == "date_only" and "rally" not in name.lower():
        footer_parts.append("⚠️ Точное время старта неизвестно")

    footer_parts.append("Discord автоматически адаптирует отображение под ваш часовой пояс")
    embed.set_footer(text=" | ".join(footer_parts))
    return embed


def format_american_post_event_message(event, league_name):
    """Уведомление для американских лиг (через день после — для хайлайтов)"""
    name = event.get("strEvent", "Без названия")
    circuit = event.get("strVenue", "").strip()
    country = event.get("strCountry", "").strip()
    video_url = event.get("strVideo", "").strip()
    result = event.get("strResult", "").strip()

    location = f"{circuit}, {country}" if circuit and country else (circuit or country or "Локация не указана")

    # Парсим время проведения (для reference)
    naive_dt, time_type, has_precise_time = parse_event_datetime(event)
    timestamp, display_type = create_timestamp_for_discord(naive_dt, time_type, has_precise_time)

    embed = discord.Embed(
        title="🎬 ДОСТУПНЫ ХАЙЛАЙТЫ И РЕЗУЛЬТАТЫ!",
        description=f"**{league_name}**",
        color=0x00FF00,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Событие", value=f"**{name}**", inline=False)

    if timestamp:
        if display_type == "precise_local":
            date_display = f"<t:{timestamp}:D>\n*(местное время трассы)*"
        elif display_type == "date_only":
            date_display = f"<t:{timestamp}:D>\n*(время неизвестно)*"
        else:
            date_display = f"<t:{timestamp}:D>"
        embed.add_field(name="Дата проведения", value=date_display, inline=True)

    embed.add_field(name="Трасса", value=f"📍 {location}", inline=True)

    if video_url:
        embed.add_field(
            name="Видеозапись",
            value=f"[Смотреть на YouTube]({video_url})",
            inline=False
        )
    if result:
        embed.add_field(
            name="Результаты",
            value=result[:200] + "..." if len(result) > 200 else result,
            inline=False
        )

    embed.set_footer(
        text="🇺🇸 Гонка прошла в Северной Америке. Время проведения — местное время трассы. "
             "Рекомендуем смотреть запись в удобное для вас время!"
    )
    return embed


def format_week_schedule(events_by_date):
    """Форматирует расписание на неделю со ВСЕМИ лигами и временем"""
    embed = discord.Embed(
        title="📅 Расписание автоспорта на ближайшие 7 дней",
        color=0x1E90FF,
        timestamp=datetime.now(timezone.utc)
    )

    today = datetime.now(timezone.utc).date()
    total_events = 0

    for i in range(7):
        check_date = today + timedelta(days=i)
        date_str = check_date.strftime("%Y-%m-%d")
        day_name = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][check_date.weekday()]
        date_display = f"{day_name}, {check_date.strftime('%d.%m')}"

        events = events_by_date.get(date_str, [])
        total_events += len(events)

        if events:
            event_list = []
            for ev in events[:7]:  # Максимум 7 событий на день для компактности
                league = ev["league"]
                name = ev["name"]
                raw_event = ev["raw_event"]

                # Парсим время для отображения
                naive_dt, time_type, has_precise_time = parse_event_datetime(raw_event)
                timestamp, display_type = create_timestamp_for_discord(naive_dt, time_type, has_precise_time)

                # Форматируем строку события
                if display_type == "precise_local":
                    time_icon = "⏰"
                    time_text = f"<t:{timestamp}:t>" if timestamp else "местное"
                elif display_type == "precise_utc":
                    time_icon = "🌍"
                    time_text = f"<t:{timestamp}:t>" if timestamp else "UTC"
                elif display_type == "date_only":
                    time_icon = "🗓️"
                    time_text = "время неизвестно"
                else:
                    time_icon = "❓"
                    time_text = "время неизвестно"

                event_list.append(f"{time_icon} {league} — **{name}** ({time_text})")

            if len(events) > 7:
                event_list.append(f"\n+ ещё {len(events) - 7} событий")

            embed.add_field(
                name=f"🗓️ {date_display}",
                value="\n".join(event_list),
                inline=False
            )
        else:
            embed.add_field(
                name=f"🗓️ {date_display}",
                value="— Нет запланированных событий —",
                inline=False
            )

    if total_events == 0:
        embed.description = "На ближайшие 7 дней события не найдены."

    embed.set_footer(
        text="⏰ = местное время трассы | 🌍 = по Гринвичу (UTC) | 🗓️ = время неизвестно | "
             "Данные: TheSportsDB.com"
    )
    return embed, total_events


# ======================
# DISCORD БОТ
# ======================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    logger.info(f"✅ Бот запущен как {bot.user} (ID: {bot.user.id})")
    logger.info(f"🌍 Серверное время: {datetime.now()}")
    logger.info(f"⏰ UTC время: {datetime.now(timezone.utc)}")
    logger.info(f"🇪🇺 Евро-лиги: {', '.join(EUROPEAN_LEAGUES.keys())}")
    logger.info(f"🇺🇸 Американские лиги: {', '.join(AMERICAN_LEAGUES.keys())}")

    # Проверка часового пояса сервера
    try:
        import time
        tz_offset = -time.timezone if time.daylight == 0 else -time.altzone
        tz_hours = tz_offset // 3600
        logger.info(f"🔧 Часовой пояс сервера: UTC{tz_hours:+d}:00")
        if tz_hours != 0:
            logger.warning(
                f"⚠️ ВНИМАНИЕ: Сервер в поясе UTC{tz_hours:+d}:00. "
                "Все datetime используют явный UTC для избежания смещения!"
            )
    except:
        pass

    if not os.path.exists(NOTIFIED_FILE):
        save_notified_events({
            "european_day_before": [],
            "european_event_day": [],
            "american_post_event": []
        })
    check_events.start()


@tasks.loop(hours=24)
async def check_events():
    # ✅ ВСЕГДА используем UTC для расчётов времени
    now = datetime.now(timezone.utc)
    target = now.replace(hour=CHECK_TIME_UTC, minute=0, second=0, microsecond=0)

    if now < target:
        sleep_seconds = (target - now).total_seconds()
        logger.info(f"⏳ Первый запуск через {sleep_seconds:.0f} секунд...")
        await asyncio.sleep(sleep_seconds)

    logger.info(f"🔄 Запуск ежедневной проверки событий (UTC: {now.strftime('%Y-%m-%d %H:%M:%S')})")

    notified = load_notified_events()
    today_utc = datetime.now(timezone.utc).date()
    tomorrow_utc = today_utc + timedelta(days=1)
    yesterday_utc = today_utc - timedelta(days=1)  # Для американских лиг

    channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if not channel:
        logger.error(f"❌ Канал с ID {NOTIFICATION_CHANNEL_ID} не найден!")
        return

    new_notifications = 0
    async with aiohttp.ClientSession() as session:
        # === ЕВРОПЕЙСКИЕ ЛИГИ: уведомления за день до и в день события ===
        for league_name, league_names in EUROPEAN_LEAGUES.items():
            for lname in league_names:
                for check_date, notif_type, notified_key in [
                    (tomorrow_utc, "day_before", "european_day_before"),
                    (today_utc, "event_day", "european_event_day")
                ]:
                    date_str = check_date.strftime("%Y-%m-%d")
                    base_url = "https://www.thesportsdb.com/api/v1/json/123/eventsday.php"
                    params = {"d": date_str, "l": lname}

                    try:
                        async with session.get(base_url, params=params, timeout=10) as resp:
                            if resp.status != 200:
                                logger.warning(f"⚠️ HTTP {resp.status} для лиги '{lname}', дата {date_str}")
                                continue
                            data = await resp.json()
                    except Exception as e:
                        logger.error(f"❌ Ошибка запроса для '{lname}' ({date_str}): {e}")
                        continue

                    events = data.get("events") or []
                    logger.info(f"📥 Получено {len(events)} событий для '{lname}' на {date_str}")

                    for event in events:
                        eid = event.get("idEvent")
                        if not eid or eid in notified[notified_key]:
                            continue

                        # Фильтрация практик и квалификаций
                        skip, reason = should_skip_event(event, league_name)
                        if skip:
                            logger.debug(f"⏭️ Событие {eid} пропущено: {reason}")
                            continue

                        # Дополнительная проверка даты
                        if event.get("dateEvent") != date_str:
                            continue

                        try:
                            embed = format_european_event_message(event, league_name, notif_type)
                            await channel.send(embed=embed)
                            notified[notified_key].append(eid)
                            new_notifications += 1
                            logger.info(
                                f"✅ Евро-уведомление ({notif_type}): {league_name} | {event.get('strEvent', 'Без названия')}"
                            )
                            await asyncio.sleep(1.5)  # Rate limiting
                        except Exception as e:
                            logger.error(f"❌ Ошибка отправки уведомления для события {eid}: {e}")

        # === АМЕРИКАНСКИЕ ЛИГИ: уведомления через день ПОСЛЕ события (для хайлайтов) ===
        for league_name, league_names in AMERICAN_LEAGUES.items():
            for lname in league_names:
                date_str = yesterday_utc.strftime("%Y-%m-%d")
                base_url = "https://www.thesportsdb.com/api/v1/json/123/eventsday.php"
                params = {"d": date_str, "l": lname}

                try:
                    async with session.get(base_url, params=params, timeout=10) as resp:
                        if resp.status != 200:
                            logger.warning(f"⚠️ HTTP {resp.status} для лиги '{lname}', дата {date_str}")
                            continue
                        data = await resp.json()
                except Exception as e:
                    logger.error(f"❌ Ошибка запроса для '{lname}' ({date_str}): {e}")
                    continue

                events = data.get("events") or []
                logger.info(f"📥 Получено {len(events)} событий для '{lname}' на {date_str} (пост-гонка)")

                for event in events:
                    eid = event.get("idEvent")
                    if not eid or eid in notified["american_post_event"]:
                        continue

                    # Проверяем наличие видео или результатов
                    if not (event.get("strVideo") or event.get("strResult")):
                        logger.debug(f"⏭️ Пропускаем событие {eid} — нет видео/результатов")
                        continue

                    try:
                        embed = format_american_post_event_message(event, league_name)
                        await channel.send(embed=embed)
                        notified["american_post_event"].append(eid)
                        new_notifications += 1
                        logger.info(
                            f"✅ Американское пост-уведомление: {league_name} | {event.get('strEvent', 'Без названия')}"
                        )
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        logger.error(f"❌ Ошибка отправки пост-уведомления для события {eid}: {e}")

    save_notified_events(notified)
    logger.info(f"✅ Проверка завершена. Новых уведомлений: {new_notifications}")


@check_events.before_loop
async def before_check():
    await bot.wait_until_ready()


# ======================
# КОМАНДЫ
# ======================
@bot.command(name="week")
async def week_schedule(ctx):
    """Показать расписание на ближайшие 7 дней со ВСЕМИ лигами"""
    await ctx.send("🔍 Загружаю расписание на неделю...")

    events_by_date = {}
    today = datetime.now(timezone.utc).date()

    async with aiohttp.ClientSession() as session:
        total_api_calls = 0
        total_events_found = 0

        # Собираем события из ВСЕХ лиг за 7 дней
        for league_name, league_names in ALL_LEAGUES.items():
            for lname in league_names:
                for i in range(7):
                    check_date = today + timedelta(days=i)
                    date_str = check_date.strftime("%Y-%m-%d")

                    try:
                        base_url = "https://www.thesportsdb.com/api/v1/json/123/eventsday.php"
                        params = {"d": date_str, "l": lname}
                        async with session.get(base_url, params=params, timeout=8) as resp:
                            total_api_calls += 1
                            if resp.status == 200:
                                data = await resp.json()
                                events = data.get("events") or []

                                for event in events:
                                    # Пропускаем практики только для отображения в списке
                                    # (но не фильтруем строго, чтобы пользователь видел всё)
                                    eid = event.get("idEvent")
                                    if not eid:
                                        continue

                                    if date_str not in events_by_date:
                                        events_by_date[date_str] = []

                                    events_by_date[date_str].append({
                                        "name": event.get("strEvent", "Без названия"),
                                        "league": league_name,
                                        "raw_event": event
                                    })
                                    total_events_found += 1

                        await asyncio.sleep(0.25)  # Rate limiting
                    except Exception as e:
                        logger.warning(f"⚠️ Ошибка при загрузке '{lname}' на {date_str}: {e}")

    embed, displayed_events = format_week_schedule(events_by_date)
    await ctx.send(embed=embed)
    await ctx.send(
        f"ℹ️ Найдено {displayed_events} событий на ближайшую неделю "
        f"(запросов к API: {total_api_calls}).\n"
        f"⚠️ Некоторые события могут отсутствовать — данные зависят от источника."
    )


@bot.command(name="timecheck")
@commands.has_permissions(administrator=True)
async def timecheck(ctx, *, event_id: str = None):
    """
    Отладочная команда: проверить парсинг времени для события
    Использование: !timecheck <idEvent> или !timecheck (проверит ближайшее событие)
    """
    if event_id:
        # Запросить конкретное событие по ID
        url = f"https://www.thesportsdb.com/api/v1/json/123/lookupevent.php?id={event_id}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        await ctx.send(f"❌ Ошибка запроса: HTTP {resp.status}")
                        return
                    data = await resp.json()
                    events = data.get("events", [])
                    if not events:
                        await ctx.send(f"❌ Событие с ID {event_id} не найдено")
                        return
                    event = events[0]
            except Exception as e:
                await ctx.send(f"❌ Ошибка: {e}")
                return
    else:
        # Найти ближайшее событие из всех лиг
        today = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
        url = "https://www.thesportsdb.com/api/v1/json/123/eventsday.php"
        params = {"d": today, "l": "Formula 1"}

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status != 200:
                        await ctx.send(f"❌ Ошибка запроса: HTTP {resp.status}")
                        return
                    data = await resp.json()
                    events = data.get("events", [])
                    if not events:
                        await ctx.send("❌ Событий на сегодня не найдено")
                        return
                    event = events[0]
            except Exception as e:
                await ctx.send(f"❌ Ошибка: {e}")
                return

    # Анализ времени
    naive_dt, time_type, has_precise_time = parse_event_datetime(event)
    timestamp, display_type = create_timestamp_for_discord(naive_dt, time_type, has_precise_time)

    # Формируем отладочное сообщение
    name = event.get("strEvent", "Без названия")
    debug_lines = [
        f"**ID события:** {event.get('idEvent')}",
        f"**Название:** {name}",
        f"**Лига:** {event.get('strLeague')}",
        f"**strTimestamp:** `{event.get('strTimestamp', 'N/A')}`",
        f"**dateEvent:** `{event.get('dateEvent', 'N/A')}`",
        f"**strTimeUTC:** `{event.get('strTimeUTC', 'N/A')}`",
        f"**strTime:** `{event.get('strTime', 'N/A')}`",
        f"\n**Анализ времени:**",
        f"• Тип времени: `{time_type}`",
        f"• Точное время: `{has_precise_time}`",
        f"• Распарсено: `{naive_dt}`" if naive_dt else "• Распарсено: `None`",
        f"• Timestamp: `{timestamp}`" if timestamp else "• Timestamp: `None`",
        f"• Отображение: `{display_type}`",
    ]

    if timestamp:
        debug_lines.append(f"\n**Предпросмотр в Discord:**")
        debug_lines.append(f"• Короткое: <t:{timestamp}:t>")
        debug_lines.append(f"• Полное: <t:{timestamp}:F>")
        debug_lines.append(f"• Относительное: <t:{timestamp}:R>")

    debug_lines.append("\n**Диагностика:**")
    if not has_precise_time:
        debug_lines.append("⚠️ Время 00:00:00 или отсутствует → показывается как 'время неизвестно'")
    if time_type == "local":
        debug_lines.append("ℹ️ Время в местном поясе трассы (не конвертируется в UTC)")
    elif time_type == "utc":
        debug_lines.append("✅ Время в UTC (по Гринвичу)")

    await ctx.send("\n".join(debug_lines))


@bot.command(name="checknow")
@commands.has_permissions(administrator=True)
async def manual_check(ctx):
    """Запустить немедленную проверку событий"""
    await ctx.send("🔍 Запускаю немедленную проверку событий...")
    check_events.restart()
    await ctx.send("✅ Проверка инициирована.")


@bot.command(name="stats")
@commands.has_permissions(administrator=True)
async def stats(ctx):
    """Показать статистику уведомлений"""
    notified = load_notified_events()
    total_euro_before = len(notified["european_day_before"])
    total_euro_day = len(notified["european_event_day"])
    total_american = len(notified["american_post_event"])
    await ctx.send(
        f"📊 **Статистика уведомлений:**\n"
        f"Евро (за день до): {total_euro_before}\n"
        f"Евро (в день): {total_euro_day}\n"
        f"Американские (пост-гонка): {total_american}\n"
        f"**Всего:** {total_euro_before + total_euro_day + total_american}"
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ У вас недостаточно прав для этой команды.")
    elif isinstance(error, commands.CommandNotFound):
        await ctx.send("❓ Неизвестная команда. Доступные команды: `!week`, `!checknow`, `!stats`, `!timecheck`")


# ======================
# ЗАПУСК
# ======================
if __name__ == "__main__":
    # Базовые проверки конфигурации
    if not DISCORD_TOKEN or DISCORD_TOKEN == "ВАШ_ТОКЕН_БОТА":
        logger.critical("❌ ОШИБКА: Укажите корректный DISCORD_TOKEN в коде!")
        exit(1)

    if not isinstance(NOTIFICATION_CHANNEL_ID, int):
        logger.critical(
            f"❌ ОШИБКА: NOTIFICATION_CHANNEL_ID должен быть целым числом, а не {type(NOTIFICATION_CHANNEL_ID)}")
        exit(1)

    # Проверка часового пояса перед запуском
    now_naive = datetime.now()
    now_utc = datetime.now(timezone.utc)
    logger.info(f"🔧 Проверка времени перед запуском:")
    logger.info(f"   Локальное время сервера: {now_naive}")
    logger.info(f"   UTC время: {now_utc}")

    if now_naive.hour != now_utc.hour:
        logger.warning(
            "⚠️ Часовой пояс сервера НЕ UTC! Все операции используют явный timezone.utc."
        )

    # Запуск бота
    try:
        logger.info("🚀 Запуск бота...")
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        logger.critical("❌ Неверный токен Discord. Проверьте DISCORD_TOKEN в конфигурации.")
    except Exception as e:
        logger.critical(f"❌ Критическая ошибка при запуске: {e}")
        raise
