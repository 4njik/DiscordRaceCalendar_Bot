import asyncio

import discord
import json
import os
import logging
from datetime import datetime, timedelta, timezone
from discord.ext import tasks, commands
import aiohttp

# ======================
# КОНФИГУРАЦИЯ
# ======================
DISCORD_TOKEN = ""
NOTIFICATION_CHANNEL_ID = 
CHECK_TIME_UTC = 8  # Время ежедневной проверки в UTC
PEPEGA_ROLE_ID = 1467435408530079866  # ← сюда вставьте скопированный ID

# League NAMES (не ID!) — точные названия из TheSportsDB
LEAGUES = {
    "🏎️ F1": ["Formula 1"],
    "🏍️ MotoGP": ["MotoGP"],
    "🌲 WRC": ["World Rally Championship"],
    " endurance WEC": ["FIA World Endurance Championship"],
    "🏁 IMSA": ["IMSA SportsCar Championship"],
    "🇺🇸 NASCAR": ["NASCAR Cup Series"]
}

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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ======================
def load_notified_events():
    if os.path.exists(NOTIFIED_FILE):
        try:
            with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки {NOTIFIED_FILE}: {e}")
    return {"day_before": [], "event_day": []}


def save_notified_events(data):
    try:
        with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Список уведомлённых событий сохранён")
    except Exception as e:
        logger.error(f"Ошибка сохранения {NOTIFIED_FILE}: {e}")


def parse_event_utc_date(event):
    try:
        if event.get("strTimestamp"):
            ts = event["strTimestamp"].replace(" ", "T")
            return datetime.fromisoformat(ts).date()
    except Exception:
        pass

    try:
        if event.get("dateEvent") and event.get("strTimeUTC"):
            dt_str = f"{event['dateEvent']}T{event['strTimeUTC']}"
            return datetime.fromisoformat(dt_str).date()
    except Exception:
        pass

    try:
        return datetime.strptime(event["dateEvent"], "%Y-%m-%d").date()
    except Exception:
        return None


def format_event_message(event, league_name, notif_type):
    name = event.get("strEvent", "Без названия")
    date_local = event.get("dateEvent", "??")
    time_local = event.get("strTime", "??")
    circuit = event.get("strVenue", "Не указан")
    thumb = event.get("strThumb", "")

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
    embed.add_field(name="Дата (местная)", value=f"📅 {date_local}", inline=True)
    embed.add_field(name="Время (местное)", value=f"⏰ {time_local}", inline=True)
    embed.add_field(name="Трасса/Локация", value=f"📍 {circuit}", inline=False)

    if thumb:
        embed.set_thumbnail(url=thumb)

    if event.get("strVideo"):
        embed.add_field(name="Видео", value=f"[Обзор на YouTube]({event['strVideo']})", inline=False)

    embed.set_footer(text="Источник: TheSportsDB.com | Автоспорт-бот")
    return embed


# ======================
# DISCORD БОТ
# ======================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)


@bot.event
async def on_ready():
    logger.info(f"Бот запущен как {bot.user} (ID: {bot.user.id})")
    logger.info(f"Отслеживаемые лиги: {', '.join(LEAGUES.keys())}")
    if not os.path.exists(NOTIFIED_FILE):
        save_notified_events({"day_before": [], "event_day": []})
    check_events.start()


@tasks.loop(hours=24)
async def check_events():
    now = datetime.now(timezone.utc)
    target = now.replace(hour=CHECK_TIME_UTC, minute=0, second=0, microsecond=0)
    if now < target:
        await asyncio.sleep((target - now).total_seconds())

    notified = load_notified_events()
    today_utc = datetime.now(timezone.utc).date()
    tomorrow_utc = today_utc + timedelta(days=1)

    channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if not channel:
        logger.error(f"Канал с ID {NOTIFICATION_CHANNEL_ID} не найден!")
        return

    new_notifications = 0
    async with aiohttp.ClientSession() as session:
        for league_name, league_names in LEAGUES.items():
            for lname in league_names:
                for check_date, notif_type in [(tomorrow_utc, "day_before"), (today_utc, "event_day")]:
                    date_str = check_date.strftime("%Y-%m-%d")
                    base_url = "https://www.thesportsdb.com/api/v1/json/123/eventsday.php"
                    params = {"d": date_str, "l": lname}

                    try:
                        async with session.get(base_url, params=params, timeout=10) as resp:
                            if resp.status != 200:
                                logger.warning(f"HTTP {resp.status} для лиги {lname}, дата {date_str}")
                                continue

                            content_type = resp.headers.get('Content-Type', '')
                            if 'application/json' not in content_type:
                                logger.error(f"Получен не JSON (Content-Type: {content_type})")
                                continue

                            data = await resp.json()

                    except Exception as e:
                        logger.error(f"Исключение при запросе к {base_url} с {params}: {e}")
                        continue

                    events = data.get("events") or []
                    for event in events:
                        eid = event.get("idEvent")
                        if not eid or (eid in notified[notif_type]):
                            continue

                        event_utc_date = parse_event_utc_date(event)
                        if event_utc_date != check_date:
                            continue

                        try:
                            embed = format_event_message(event, league_name, notif_type)
                            role_mention = f"<@&{PEPEGA_ROLE_ID}>"
                            await channel.send(content=role_mention, embed=embed)
                            notified[notif_type].append(eid)
                            new_notifications += 1
                            logger.info(
                                f"Отправлено уведомление: {notif_type} | {league_name} | {event.get('strEvent')}"
                            )
                            await asyncio.sleep(1)
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления для события {eid}: {e}")

    save_notified_events(notified)
    logger.info(f"Проверка завершена. Новых уведомлений: {new_notifications}")


@check_events.before_loop
async def before_check():
    await bot.wait_until_ready()


# ======================
# КОМАНДЫ
# ======================
@bot.command(name="checknow")
@commands.has_permissions(administrator=True)
async def manual_check(ctx):
    await ctx.send("🔍 Запускаю немедленную проверку событий...")
    check_events.restart()
    await ctx.send("✅ Проверка инициирована.")


@bot.command(name="stats")
@commands.has_permissions(administrator=True)
async def stats(ctx):
    notified = load_notified_events()
    total_day_before = len(notified["day_before"])
    total_event_day = len(notified["event_day"])
    await ctx.send(
        f"📊 Статистика уведомлений:\n"
        f"За день до: {total_day_before}\n"
        f"В день события: {total_event_day}\n"
        f"Всего: {total_day_before + total_event_day}"
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ У вас недостаточно прав для этой команды.")


# ======================
# ЗАПУСК
# ======================
if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "ВАШ_ТОКЕН_БОТА":
        logger.critical("ОШИБКА: Укажите корректный DISCORD_TOKEN!")
        exit(1)
    if not isinstance(NOTIFICATION_CHANNEL_ID, int):
        logger.critical("ОШИБКА: NOTIFICATION_CHANNEL_ID должен быть целым числом!")
        exit(1)

    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        logger.critical("Неверный токен Discord.")
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")
