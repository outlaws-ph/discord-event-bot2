import discord
from discord.ext import commands
from discord import app_commands
import asyncpg  # ✅ FIXED (correct import)
import os
import json
from datetime import datetime, timezone

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

MAX_ITEMS_PER_PLAYER = 2  # 🔥 you can change this anytime

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

db_pool = None
data_store = {"events": {}}

# =========================
# DATABASE
# =========================
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS data (
            id INT PRIMARY KEY,
            json TEXT
        )
        """)

        await conn.execute("""
        INSERT INTO data (id, json)
        VALUES (1, '{}')
        ON CONFLICT (id) DO NOTHING
        """)

async def load_data():
    global data_store
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT json FROM data WHERE id=1")
        if row and row["json"]:
            data_store = json.loads(row["json"])

async def save_data():
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE data SET json=$1 WHERE id=1",
            json.dumps(data_store)
        )

# =========================
# HELPERS
# =========================
def now():
    return datetime.now(timezone.utc).timestamp()

def ensure_event(name):
    key = name.lower()
    if key not in data_store["events"]:
        data_store["events"][key] = {
            "name": name,
            "priority_order": [],
            "is_locked": False,
            "categories": {
                "main": {"label": "Main Items", "items": {}},
                "other": {"label": "Other Items", "items": {}}
            }
        }
    return data_store["events"][key]

def count_user_items(event, user_id):
    total = 0
    for cat in event["categories"].values():
        for item in cat["items"].values():
            total += sum(1 for x in item["selections"] if x["user_id"] == user_id)
    return total

def rank(event, user):
    try:
        return event["priority_order"].index(user) + 1
    except:
        return 999999

# =========================
# AUTO ASSIGN LEFTOVERS
# =========================
def auto_assign(event):
    players = event["priority_order"]

    for cat in event["categories"].values():
        for item in cat["items"].values():
            while len(item["selections"]) < item["capacity"]:
                for user in players:
                    if user not in [x["user_id"] for x in item["selections"]]:
                        if count_user_items(event, user) < MAX_ITEMS_PER_PLAYER:
                            item["selections"].append({
                                "user_id": user,
                                "selected_at": now()
                            })
                            break
                else:
                    break

# =========================
# EMBED UI
# =========================
def build_embed(event):
    embed = discord.Embed(
        title=f"🎁 {event['name']} Distribution",
        color=discord.Color.blurple()
    )

    for cat in event["categories"].values():
        text = ""
        for item, data in cat["items"].items():
            users = ", ".join([f"<@{x['user_id']}>" for x in data["selections"]])
            text += f"**{item}** ({len(data['selections'])}/{data['capacity']})\n{users or '—'}\n\n"

        embed.add_field(name=cat["label"], value=text or "No items", inline=False)

    return embed

# =========================
# SELECT UI
# =========================
class Select(discord.ui.Select):
    def __init__(self, event_key, cat_key):
        self.event_key = event_key
        self.cat_key = cat_key

        event = data_store["events"][event_key]
        items = event["categories"][cat_key]["items"]

        options = [
            discord.SelectOption(label=i, value=i)
            for i in items.keys()
        ]

        super().__init__(options=options[:25])

    async def callback(self, interaction):
        event = data_store["events"][self.event_key]

        if event["is_locked"]:
            await interaction.response.send_message("❌ Panel is locked", ephemeral=True)
            return

        if count_user_items(event, interaction.user.id) >= MAX_ITEMS_PER_PLAYER:
            await interaction.response.send_message(
                f"❌ Max {MAX_ITEMS_PER_PLAYER} items only.",
                ephemeral=True
            )
            return

        item = event["categories"][self.cat_key]["items"][self.values[0]]

        if interaction.user.id in [x["user_id"] for x in item["selections"]]:
            await interaction.response.send_message("Already selected", ephemeral=True)
            return

        if len(item["selections"]) >= item["capacity"]:
            lowest = max(item["selections"], key=lambda x: rank(event, x["user_id"]))
            if rank(event, interaction.user.id) < rank(event, lowest["user_id"]):
                item["selections"].remove(lowest)
            else:
                await interaction.response.send_message("Item full", ephemeral=True)
                return

        item["selections"].append({
            "user_id": interaction.user.id,
            "selected_at": now()
        })

        await save_data()
        await interaction.message.edit(embed=build_embed(event))
        await interaction.response.send_message("✅ Selected!", ephemeral=True)

# =========================
# VIEW
# =========================
class View(discord.ui.View):
    def __init__(self, event_key):
        super().__init__(timeout=None)
        self.add_item(Select(event_key, "main"))
        self.add_item(Select(event_key, "other"))

# =========================
# COMMANDS
# =========================

@bot.tree.command(name="create_event")
async def create_event(interaction, name: str):
    ensure_event(name)
    await save_data()
    await interaction.response.send_message(f"✅ Event **{name}** created")

@bot.tree.command(name="create_panel")
async def create_panel(interaction, name: str):
    event = ensure_event(name)

    msg = await interaction.channel.send(
        embed=build_embed(event),
        view=View(name.lower())
    )

    event["panel_channel_id"] = interaction.channel.id
    event["panel_message_id"] = msg.id

    await save_data()
    await interaction.response.send_message("✅ Panel created", ephemeral=True)

@bot.tree.command(name="add_item")
async def add_item(interaction, name: str, category: str, item: str, cap: int):
    event = ensure_event(name)

    event["categories"][category]["items"][item] = {
        "capacity": cap,
        "selections": []
    }

    await save_data()
    await interaction.response.send_message("✅ Item added", ephemeral=True)

@bot.tree.command(name="add_priority")
async def add_priority(interaction, name: str, user: discord.Member):
    event = ensure_event(name)
    event["priority_order"].append(user.id)

    await save_data()
    await interaction.response.send_message("✅ Priority added", ephemeral=True)

@bot.tree.command(name="lock_event")
async def lock_event(interaction, name: str):
    event = ensure_event(name)

    auto_assign(event)
    event["is_locked"] = True

    msg = "🏆 **Winners**\n\n"
    for cat in event["categories"].values():
        for item, data in cat["items"].items():
            if data["selections"]:
                users = ", ".join([f"<@{x['user_id']}>" for x in data["selections"]])
                msg += f"**{item}**: {users}\n"

    await interaction.channel.send(msg)
    await save_data()

# =========================
# READY
# =========================
@bot.event
async def on_ready():
    await init_db()
    await load_data()
    await bot.tree.sync()
    print("READY")

bot.run(TOKEN)
