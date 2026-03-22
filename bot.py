import discord
from discord.ext import commands
from discord import app_commands
import asyncpg
import os
import json
from datetime import datetime, timezone

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

MAX_ITEMS_PER_PLAYER = 2

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
        row = await conn.fetchrow("SELECT json FROM data WHERE id = 1")
        if row and row["json"]:
            data_store = json.loads(row["json"])


async def save_data():
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE data SET json = $1 WHERE id = 1",
            json.dumps(data_store)
        )


# =========================
# HELPERS
# =========================
def now():
    return datetime.now(timezone.utc).timestamp()


def event_key(name: str) -> str:
    return name.strip().lower()


def ensure_event(name: str):
    key = event_key(name)
    if key not in data_store["events"]:
        data_store["events"][key] = {
            "name": name,
            "priority_order": [],
            "is_locked": False,
            "panel_channel_id": None,
            "panel_message_id": None,
            "categories": {
                "main": {"label": "Main Items", "items": {}},
                "other": {"label": "Other Items", "items": {}}
            }
        }
    return data_store["events"][key]


def get_event(name: str):
    return data_store["events"].get(event_key(name))


def count_user_items(event, user_id: int) -> int:
    total = 0
    for cat in event["categories"].values():
        for item in cat["items"].values():
            total += sum(1 for x in item["selections"] if x["user_id"] == user_id)
    return total


def rank(event, user_id: int) -> int:
    try:
        return event["priority_order"].index(user_id) + 1
    except ValueError:
        return 999999


def auto_assign(event):
    players = event["priority_order"]

    for cat in event["categories"].values():
        for item in cat["items"].values():
            while len(item["selections"]) < item["capacity"]:
                assigned = False
                for user_id in players:
                    if user_id not in [x["user_id"] for x in item["selections"]]:
                        if count_user_items(event, user_id) < MAX_ITEMS_PER_PLAYER:
                            item["selections"].append({
                                "user_id": user_id,
                                "selected_at": now()
                            })
                            assigned = True
                            break
                if not assigned:
                    break


def build_category_text(event, category_key: str) -> str:
    cat = event["categories"][category_key]
    parts = []

    for item_name, item_data in cat["items"].items():
        count = len(item_data["selections"])
        cap = item_data["capacity"]
        holders = [f"<@{x['user_id']}>" for x in item_data["selections"]]

        if holders:
            shown = ", ".join(holders[:6])
            if len(holders) > 6:
                shown += f" +{len(holders)-6} more"
        else:
            shown = "—"

        parts.append(f"**{item_name}** `{count}/{cap}`\n{shown}")

    text = "\n\n".join(parts)
    return text[:1024] if text else "No items yet."


def build_embed(event):
    embed = discord.Embed(
        title=f"🎁 {event['name']}",
        description=(
            f"Select items below.\n"
            f"Max per player: **{MAX_ITEMS_PER_PLAYER}**\n"
            f"Status: **{'Locked' if event['is_locked'] else 'Open'}**"
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="Main Items",
        value=build_category_text(event, "main"),
        inline=False
    )
    embed.add_field(
        name="Other Items",
        value=build_category_text(event, "other"),
        inline=False
    )

    if event["priority_order"]:
        priority_preview = "\n".join(
            f"{i}. <@{user_id}>"
            for i, user_id in enumerate(event["priority_order"][:10], start=1)
        )
        if len(event["priority_order"]) > 10:
            priority_preview += f"\n+{len(event['priority_order']) - 10} more"
    else:
        priority_preview = "No priority players set."

    embed.add_field(
        name="Priority Order",
        value=priority_preview[:1024],
        inline=False
    )

    return embed


async def refresh_panel_by_event(event):
    channel_id = event.get("panel_channel_id")
    message_id = event.get("panel_message_id")

    if not channel_id or not message_id:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return

    try:
        message = await channel.fetch_message(message_id)
        await message.edit(embed=build_embed(event), view=EventView(event_key(event["name"])))
    except Exception as e:
        print(f"Failed to refresh panel: {e}")


# =========================
# UI
# =========================
class ItemSelect(discord.ui.Select):
    def __init__(self, ev_key: str, cat_key: str):
        self.ev_key = ev_key
        self.cat_key = cat_key

        event = data_store["events"][ev_key]
        items = event["categories"][cat_key]["items"]

        options = [
            discord.SelectOption(
                label=item_name[:100],
                value=item_name,
                description=f"{len(item_data['selections'])}/{item_data['capacity']} reserved"
            )
            for item_name, item_data in items.items()
        ]

        placeholder = "Choose a main item" if cat_key == "main" else "Choose an other item"
        super().__init__(
            placeholder=placeholder,
            options=options[:25],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        event = data_store["events"][self.ev_key]

        if event["is_locked"]:
            await interaction.response.send_message("❌ This panel is locked.", ephemeral=True)
            return

        if count_user_items(event, interaction.user.id) >= MAX_ITEMS_PER_PLAYER:
            await interaction.response.send_message(
                f"❌ You can only choose up to {MAX_ITEMS_PER_PLAYER} item(s).",
                ephemeral=True
            )
            return

        selected_name = self.values[0]
        item = event["categories"][self.cat_key]["items"][selected_name]

        if interaction.user.id in [x["user_id"] for x in item["selections"]]:
            await interaction.response.send_message("You already selected this item.", ephemeral=True)
            return

        if len(item["selections"]) >= item["capacity"]:
            lowest = max(item["selections"], key=lambda x: rank(event, x["user_id"]))
            if rank(event, interaction.user.id) < rank(event, lowest["user_id"]):
                item["selections"].remove(lowest)
            else:
                await interaction.response.send_message("❌ This item is full.", ephemeral=True)
                return

        item["selections"].append({
            "user_id": interaction.user.id,
            "selected_at": now()
        })

        await save_data()
        await refresh_panel_by_event(event)
        await interaction.response.send_message(f"✅ You selected **{selected_name}**.", ephemeral=True)


class RemoveSelect(discord.ui.Select):
    def __init__(self, ev_key: str):
        self.ev_key = ev_key
        event = data_store["events"][ev_key]

        options = []
        for cat_key in ["main", "other"]:
            for item_name, item_data in event["categories"][cat_key]["items"].items():
                options.append(
                    discord.SelectOption(
                        label=item_name[:100],
                        value=f"{cat_key}|{item_name}",
                        description=f"{len(item_data['selections'])}/{item_data['capacity']} reserved"
                    )
                )

        if not options:
            options = [discord.SelectOption(label="No items available", value="none")]

        super().__init__(
            placeholder="Remove one of your selected items",
            options=options[:25],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("No items available.", ephemeral=True)
            return

        event = data_store["events"][self.ev_key]

        if event["is_locked"]:
            await interaction.response.send_message("❌ This panel is locked.", ephemeral=True)
            return

        cat_key, item_name = self.values[0].split("|", 1)
        item = event["categories"][cat_key]["items"][item_name]

        before = len(item["selections"])
        item["selections"] = [x for x in item["selections"] if x["user_id"] != interaction.user.id]

        if len(item["selections"]) == before:
            await interaction.response.send_message("You did not select that item.", ephemeral=True)
            return

        await save_data()
        await refresh_panel_by_event(event)
        await interaction.response.send_message(f"🗑️ Removed **{item_name}**.", ephemeral=True)


class EventView(discord.ui.View):
    def __init__(self, ev_key: str):
        super().__init__(timeout=None)
        self.add_item(ItemSelect(ev_key, "main"))
        self.add_item(ItemSelect(ev_key, "other"))
        self.add_item(RemoveSelect(ev_key))


# =========================
# COMMANDS
# =========================
@bot.tree.command(name="create_event", description="Create a new event")
async def create_event(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    ensure_event(name)
    await save_data()
    await interaction.followup.send(f"✅ Event **{name}** created.", ephemeral=True)


@bot.tree.command(name="create_panel", description="Create a panel for an event")
async def create_panel(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)

    event = ensure_event(name)
    msg = await interaction.channel.send(
        embed=build_embed(event),
        view=EventView(event_key(name))
    )

    event["panel_channel_id"] = interaction.channel.id
    event["panel_message_id"] = msg.id

    await save_data()
    await interaction.followup.send("✅ Panel created.", ephemeral=True)


@app_commands.describe(category="Use main or other")
@bot.tree.command(name="add_item", description="Add an item to an event")
async def add_item(
    interaction: discord.Interaction,
    name: str,
    category: str,
    item: str,
    cap: int
):
    await interaction.response.defer(ephemeral=True)

    category = category.lower()
    if category not in ["main", "other"]:
        await interaction.followup.send("❌ Category must be `main` or `other`.", ephemeral=True)
        return

    event = ensure_event(name)
    event["categories"][category]["items"][item] = {
        "capacity": cap,
        "selections": []
    }

    await save_data()
    await refresh_panel_by_event(event)
    await interaction.followup.send("✅ Item added.", ephemeral=True)


@bot.tree.command(name="add_priority", description="Add a priority player to an event")
async def add_priority(interaction: discord.Interaction, name: str, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    event = ensure_event(name)
    if user.id not in event["priority_order"]:
        event["priority_order"].append(user.id)

    await save_data()
    await refresh_panel_by_event(event)
    await interaction.followup.send("✅ Priority added.", ephemeral=True)


@bot.tree.command(name="lock_event", description="Lock event, auto-assign leftovers, and announce winners")
async def lock_event(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)

    event = ensure_event(name)
    auto_assign(event)
    event["is_locked"] = True

    winners = ["🏆 **Winners**", ""]
    for cat in event["categories"].values():
        for item_name, item_data in cat["items"].items():
            if item_data["selections"]:
                users = ", ".join([f"<@{x['user_id']}>" for x in item_data["selections"]])
                winners.append(f"**{item_name}**: {users}")

    await save_data()
    await refresh_panel_by_event(event)

    await interaction.channel.send("\n".join(winners))
    await interaction.followup.send(f"✅ Event **{name}** locked.", ephemeral=True)


@bot.tree.command(name="unlock_event", description="Unlock an event")
async def unlock_event(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)

    event = get_event(name)
    if not event:
        await interaction.followup.send("❌ Event not found.", ephemeral=True)
        return

    event["is_locked"] = False
    await save_data()
    await refresh_panel_by_event(event)
    await interaction.followup.send(f"✅ Event **{name}** unlocked.", ephemeral=True)


@bot.tree.command(name="show_events", description="Show all events")
async def show_events(interaction: discord.Interaction):
    if not data_store["events"]:
        await interaction.response.send_message("No events yet.", ephemeral=True)
        return

    names = [f"• {ev['name']}" for ev in data_store["events"].values()]
    await interaction.response.send_message("\n".join(names), ephemeral=True)


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
