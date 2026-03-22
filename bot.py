import os
import re
import json
import pytz
import discord
from datetime import datetime, timedelta
from discord.ext import commands
from discord import app_commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# =========================================
# CONFIG
# =========================================
TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

# Change this if you want a different timezone
TZ = pytz.timezone("Asia/Manila")

EVENTS_FILE = "events.json"

DEFAULT_EVENTS = {
    "canyon": {
        "name": "Canyon of World Tree Depth",
        "days": ["sun", "wed", "sat"],
        "time": "16:00"
    },
    "vale": {
        "name": "Vale of Ragnarok",
        "days": ["mon", "sat"],
        "time": "14:25"
    },
    "crossroads": {
        "name": "Crossroads of Ragnarok",
        "days": ["wed", "fri"],
        "time": "16:20"
    },
    "inter": {
        "name": "Inter FV 5f",
        "days": ["sun", "mon", "fri"],
        "time": "16:00"
    },
    "sindris": {
        "name": "Sindris",
        "days": ["thu"],
        "time": "20:00"
    },
    "serverbattle": {
        "name": "Server Battle",
        "days": ["tue"],
        "time": "20:00"
    }
}

DAY_MAP = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6
}

VALID_DAYS = set(DAY_MAP.keys())

# =========================================
# BOT SETUP
# =========================================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
scheduler = AsyncIOScheduler(timezone=TZ)

# =========================================
# STORAGE
# =========================================
def normalize_event_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())

def save_events(data):
    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def load_events():
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data:
                    return data
        except Exception as e:
            print(f"Failed to load {EVENTS_FILE}: {e}")

    save_events(DEFAULT_EVENTS)
    return DEFAULT_EVENTS.copy()

events = load_events()

# =========================================
# HELPERS
# =========================================
def is_valid_time(value: str) -> bool:
    return re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value) is not None

def parse_days(days_str: str):
    parts = [d.strip().lower() for d in days_str.split(",") if d.strip()]
    unique_days = []
    seen = set()

    for d in parts:
        if d not in VALID_DAYS:
            return None
        if d not in seen:
            unique_days.append(d)
            seen.add(d)

    return unique_days if unique_days else None

def next_event_datetime(day_code: str, time_str: str):
    now = datetime.now(TZ)
    target_weekday = DAY_MAP[day_code]
    hour, minute = map(int, time_str.split(":"))

    days_ahead = (target_weekday - now.weekday()) % 7
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)

    if candidate <= now:
        candidate += timedelta(days=7)

    return candidate

def build_reminder_message(event_name: str, event_dt: datetime, reminder_label: str):
    unix_ts = int(event_dt.timestamp())
    return (
        f"@everyone\n"
        f"⏰ **{event_name}** starts **{reminder_label}**\n"
        f"🕒 Countdown: <t:{unix_ts}:R>\n"
        f"📅 Event Time: <t:{unix_ts}:F>"
    )

async def get_target_channel():
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(CHANNEL_ID)
    return channel

async def send_event_reminder(event_name: str, event_day: str, event_time: str, reminder_label: str):
    try:
        channel = await get_target_channel()
        event_dt = next_event_datetime(event_day, event_time)
        message = build_reminder_message(event_name, event_dt, reminder_label)

        await channel.send(
            content=message,
            allowed_mentions=discord.AllowedMentions(everyone=True)
        )

        print(f"Reminder sent: {event_name} - {reminder_label}")

    except Exception as e:
        print(f"Failed to send reminder for {event_name}: {e}")

def schedule_events():
    scheduler.remove_all_jobs()

    for event_key, event_data in events.items():
        event_name = event_data["name"]
        event_time = event_data["time"]
        hour, minute = map(int, event_time.split(":"))

        for day_code in event_data["days"]:
            # 15-minute reminder
            reminder_15_hour = hour
            reminder_15_minute = minute - 15
            if reminder_15_minute < 0:
                reminder_15_minute += 60
                reminder_15_hour = (hour - 1) % 24

            scheduler.add_job(
                send_event_reminder,
                trigger="cron",
                day_of_week=day_code,
                hour=reminder_15_hour,
                minute=reminder_15_minute,
                args=[event_name, day_code, event_time, "in 15 minutes"],
                id=f"{event_key}_{day_code}_15m",
                replace_existing=True
            )

            # 10-hour reminder
            scheduler.add_job(
                send_event_reminder,
                trigger="cron",
                day_of_week=day_code,
                hour=(hour - 10) % 24,
                minute=minute,
                args=[event_name, day_code, event_time, "in 10 hours"],
                id=f"{event_key}_{day_code}_10h",
                replace_existing=True
            )

    print(f"Scheduled {len(scheduler.get_jobs())} jobs.")
    for job in scheduler.get_jobs():
        print(f"Job scheduled: {job.id} | next run: {job.next_run_time}")

async def event_name_autocomplete(interaction: discord.Interaction, current: str):
    current = current.lower().strip()
    choices = []

    for key, data in events.items():
        name = data["name"]
        if current in key.lower() or current in name.lower():
            choices.append(app_commands.Choice(name=name, value=key))

    return choices[:25]

# =========================================
# SLASH COMMANDS
# =========================================
@tree.command(name="addevent", description="Add a new event")
@app_commands.describe(
    name="Event name",
    days="Comma-separated days: mon,tue,wed,thu,fri,sat,sun",
    time="Time in HH:MM (24-hour)"
)
async def addevent(interaction: discord.Interaction, name: str, days: str, time: str):
    if not is_valid_time(time):
        await interaction.response.send_message(
            "❌ Invalid time format. Use HH:MM in 24-hour format, example: 16:25",
            ephemeral=True
        )
        return

    parsed_days = parse_days(days)
    if not parsed_days:
        await interaction.response.send_message(
            "❌ Invalid days. Use comma-separated values like: mon,wed,sat",
            ephemeral=True
        )
        return

    event_key = normalize_event_key(name)
    if not event_key:
        await interaction.response.send_message("❌ Invalid event name.", ephemeral=True)
        return

    events[event_key] = {
        "name": name,
        "days": parsed_days,
        "time": time
    }

    save_events(events)
    schedule_events()

    await interaction.response.send_message(
        f"✅ Event added.\n"
        f"**Name:** {name}\n"
        f"**Days:** {', '.join(parsed_days)}\n"
        f"**Time:** {time} ({TZ.zone})"
    )

@tree.command(name="settime", description="Change event time")
@app_commands.describe(
    event="Choose the event",
    time="New time in HH:MM (24-hour)"
)
@app_commands.autocomplete(event=event_name_autocomplete)
async def settime(interaction: discord.Interaction, event: str, time: str):
    if event not in events:
        await interaction.response.send_message("❌ Event not found.", ephemeral=True)
        return

    if not is_valid_time(time):
        await interaction.response.send_message(
            "❌ Invalid time format. Use HH:MM in 24-hour format.",
            ephemeral=True
        )
        return

    events[event]["time"] = time
    save_events(events)
    schedule_events()

    await interaction.response.send_message(
        f"✅ Updated **{events[event]['name']}** to **{time}** ({TZ.zone})"
    )

@tree.command(name="setdays", description="Change event days")
@app_commands.describe(
    event="Choose the event",
    days="Comma-separated days: mon,tue,wed,thu,fri,sat,sun"
)
@app_commands.autocomplete(event=event_name_autocomplete)
async def setdays(interaction: discord.Interaction, event: str, days: str):
    if event not in events:
        await interaction.response.send_message("❌ Event not found.", ephemeral=True)
        return

    parsed_days = parse_days(days)
    if not parsed_days:
        await interaction.response.send_message(
            "❌ Invalid days. Use comma-separated values like: mon,wed,sat",
            ephemeral=True
        )
        return

    events[event]["days"] = parsed_days
    save_events(events)
    schedule_events()

    await interaction.response.send_message(
        f"✅ Updated **{events[event]['name']}** days to **{', '.join(parsed_days)}**"
    )

@tree.command(name="removeevent", description="Remove an event")
@app_commands.describe(event="Choose the event")
@app_commands.autocomplete(event=event_name_autocomplete)
async def removeevent(interaction: discord.Interaction, event: str):
    if event not in events:
        await interaction.response.send_message("❌ Event not found.", ephemeral=True)
        return

    removed_name = events[event]["name"]
    del events[event]
    save_events(events)
    schedule_events()

    await interaction.response.send_message(f"✅ Removed **{removed_name}**")

@tree.command(name="listevents", description="Show all scheduled events")
async def listevents(interaction: discord.Interaction):
    if not events:
        await interaction.response.send_message("No events found.")
        return

    lines = []
    for _, event_data in sorted(events.items(), key=lambda x: x[1]["name"].lower()):
        lines.append(
            f"**{event_data['name']}** — {event_data['time']} ({TZ.zone}) — {', '.join(event_data['days'])}"
        )

    await interaction.response.send_message("\n".join(lines))

@tree.command(name="pingevent", description="Send a test reminder now")
@app_commands.describe(event="Choose the event")
@app_commands.autocomplete(event=event_name_autocomplete)
async def pingevent(interaction: discord.Interaction, event: str):
    await interaction.response.defer(ephemeral=True)

    try:
        if event not in events:
            await interaction.followup.send("❌ Event not found.", ephemeral=True)
            return

        event_data = events[event]
        next_day = event_data["days"][0]
        event_dt = next_event_datetime(next_day, event_data["time"])
        message = build_reminder_message(event_data["name"], event_dt, "soon")

        channel = await get_target_channel()

        await channel.send(
            content=message,
            allowed_mentions=discord.AllowedMentions(everyone=True)
        )

        await interaction.followup.send(
            f"✅ Test reminder sent for **{event_data['name']}**",
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(
            f"❌ pingevent failed: {e}",
            ephemeral=True
        )

@tree.command(name="testhere", description="Send a test message in this channel")
async def testhere(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        await interaction.channel.send(
            "@everyone ✅ Test message from this channel\n🕒 Countdown: <t:9999999999:R>",
            allowed_mentions=discord.AllowedMentions(everyone=True)
        )
        await interaction.followup.send(
            f"✅ Sent successfully in this channel.\nChannel ID: {interaction.channel_id}",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(
            f"❌ Failed in this channel: {type(e).__name__}: {e}",
            ephemeral=True
        )

# =========================================
# BOT EVENTS
# =========================================
@bot.event
async def on_ready():
    try:
        await tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Slash command sync failed: {e}")

    if not scheduler.running:
        schedule_events()
        scheduler.start()

    print(f"Bot ready as {bot.user}")
    print(f"Timezone: {TZ.zone}")
    print(f"Channel ID: {CHANNEL_ID}")

# =========================================
# START
# =========================================
if not TOKEN:
    raise ValueError("TOKEN environment variable is missing.")

if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID environment variable is missing or invalid.")

bot.run(TOKEN)
