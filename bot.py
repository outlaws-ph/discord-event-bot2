import os
import json
from datetime import datetime, timezone

import asyncpg
import discord
from discord.ext import commands
from discord import app_commands

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

PRESET_EVENTS = [
    "Sindris",
    "Int FV 5F",
    "Server Battle",
    "Canyon Depth",
    "Vale of Ragnarok",
    "Crossroad of Ragnarok",
    "Guild Dungeon",
]

PRESET_EVENT_CHOICES = [
    app_commands.Choice(name=x, value=x) for x in PRESET_EVENTS
]

CATEGORY_CHOICES = [
    app_commands.Choice(name="Main Items", value="main"),
    app_commands.Choice(name="Other Items", value="other"),
]

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

db_pool = None
data_store = {
    "global_items": {
        "main": {"items": {}},
        "other": {"items": {}}
    },
    "events": {}
}

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
        try:
            data_store = json.loads(row["json"])
        except:
            pass

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

def key(x): return x.lower().strip()

def make_event_key(e, d): return key(f"{e} {d}")

def ensure_event(e, d):
    k = make_event_key(e, d)
    if k not in data_store["events"]:
        data_store["events"][k] = {
            "name": f"{e} {d}",
            "priority_order": [],
            "is_locked": False,
            "panel_channel_id": None,
            "panel_message_id": None,
            "categories": {
                "main": {"items": {}},
                "other": {"items": {}}
            }
        }
    return data_store["events"][k]

def find_global(item):
    for c in ["main", "other"]:
        for name, data in data_store["global_items"][c]["items"].items():
            if name.lower() == item.lower():
                return c, name, data
    return None, None, None

# =========================
# EMBED
# =========================
def build_embed(ev):
    e = discord.Embed(
        title=f"🎁 {ev['name']}",
        description="Select items below",
        color=discord.Color.blurple()
    )

    for c in ["main", "other"]:
        txt = ""
        for i, d in ev["categories"][c]["items"].items():
            users = ", ".join(f"<@{x['user_id']}>" for x in d["selections"]) or "—"
            txt += f"**{i}** ({len(d['selections'])}/{d['capacity']})\n{users}\n\n"

        e.add_field(
            name="Main Items" if c=="main" else "Other Items",
            value=txt or "No items",
            inline=False
        )

    return e

async def refresh(ev):
    try:
        ch = bot.get_channel(ev["panel_channel_id"])
        msg = await ch.fetch_message(ev["panel_message_id"])
        await msg.edit(embed=build_embed(ev), view=PanelView(key(ev["name"])))
    except:
        pass

# =========================
# UI SELECT
# =========================
class Select(discord.ui.Select):
    def __init__(self, ev_key, cat):
        self.ev_key = ev_key
        self.cat = cat
        ev = data_store["events"][ev_key]

        opts = [
            discord.SelectOption(label=i, value=i)
            for i in ev["categories"][cat]["items"].keys()
        ]

        super().__init__(
            placeholder=f"Select {cat}",
            options=opts[:25] or [discord.SelectOption(label="None", value="none")]
        )

    async def callback(self, interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("No items", ephemeral=True)
            return

        ev = data_store["events"][self.ev_key]
        item = ev["categories"][self.cat]["items"][self.values[0]]

        if interaction.user.id in [x["user_id"] for x in item["selections"]]:
            await interaction.response.send_message("Already selected", ephemeral=True)
            return

        if len(item["selections"]) >= item["capacity"]:
            lowest = item["selections"][-1]
            item["selections"].remove(lowest)

        item["selections"].append({"user_id": interaction.user.id, "t": now()})

        await save_data()
        await refresh(ev)
        await interaction.response.send_message("✅ Selected", ephemeral=True)

class PanelView(discord.ui.View):
    def __init__(self, ev_key):
        super().__init__(timeout=None)
        self.add_item(Select(ev_key, "main"))
        self.add_item(Select(ev_key, "other"))

# =========================
# COMMANDS
# =========================

@bot.tree.command(name="add_item")
@app_commands.choices(category=CATEGORY_CHOICES)
async def add_item(interaction, category, item_name: str, cap: int):
    await interaction.response.defer(ephemeral=True)

    data_store["global_items"][category.value]["items"][item_name] = {
        "capacity": cap
    }

    await save_data()
    await interaction.followup.send(f"✅ Added {item_name}")

# 🔥 DROPDOWN ITEM SELECTOR
@bot.tree.command(name="add_item_to_event")
@app_commands.choices(event=PRESET_EVENT_CHOICES)
async def add_item_to_event(interaction, event, run_date: str):
    await interaction.response.defer(ephemeral=True)

    ev = ensure_event(event.value, run_date)

    # BUILD DROPDOWN
    opts = []
    for c in ["main", "other"]:
        for i in data_store["global_items"][c]["items"].keys():
            opts.append(discord.SelectOption(label=i, value=i))

    class Picker(discord.ui.Select):
        def __init__(self):
            super().__init__(options=opts[:25])

        async def callback(self, i):
            cat, name, data = find_global(self.values[0])

            if name in ev["categories"][cat]["items"]:
                await i.response.send_message("Already added", ephemeral=True)
                return

            ev["categories"][cat]["items"][name] = {
                "capacity": data["capacity"],
                "selections": []
            }

            await save_data()
            await refresh(ev)
            await i.response.send_message(f"✅ Added {name}", ephemeral=True)

    view = discord.ui.View()
    view.add_item(Picker())

    await interaction.followup.send("Select item to add:", view=view, ephemeral=True)

@bot.tree.command(name="create_panel")
@app_commands.choices(event=PRESET_EVENT_CHOICES)
async def create_panel(interaction, event, run_date: str):
    await interaction.response.defer(ephemeral=True)

    ev = ensure_event(event.value, run_date)

    msg = await interaction.channel.send(
        embed=build_embed(ev),
        view=PanelView(key(ev["name"]))
    )

    ev["panel_channel_id"] = interaction.channel.id
    ev["panel_message_id"] = msg.id

    await save_data()
    await interaction.followup.send("✅ Panel created", ephemeral=True)

@bot.tree.command(name="remove_event")
@app_commands.choices(event=PRESET_EVENT_CHOICES)
async def remove_event(interaction, event, run_date: str):
    await interaction.response.defer(ephemeral=True)

    k = make_event_key(event.value, run_date)
    data_store["events"].pop(k, None)

    await save_data()
    await interaction.followup.send("✅ Event removed", ephemeral=True)

# =========================
# READY
# =========================
@bot.event
async def on_ready():
    await init_db()
    await load_data()

    for k in data_store["events"]:
        bot.add_view(PanelView(k))

    await bot.tree.sync()
    print("READY")

bot.run(TOKEN)
