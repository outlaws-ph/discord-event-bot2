import os
import json
import copy
from datetime import datetime, timezone

import asyncpg
import discord
from discord.ext import commands
from discord import app_commands

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

MAX_ITEMS_PER_PLAYER = 2

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
    app_commands.Choice(name=name, value=name) for name in PRESET_EVENTS
]

CATEGORY_CHOICES = [
    app_commands.Choice(name="Main Items", value="main"),
    app_commands.Choice(name="Other Items", value="other"),
]

DEFAULT_GLOBAL_ITEMS = {
    "main": {
        "label": "Main Items",
        "items": {}
    },
    "other": {
        "label": "Other Items",
        "items": {}
    }
}

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

db_pool = None
data_store = {
    "global_items": copy.deepcopy(DEFAULT_GLOBAL_ITEMS),
    "events": {}
}


# =========================
# DATABASE
# =========================
def ensure_data_defaults(data: dict) -> dict:
    if not isinstance(data, dict):
        data = {}

    if "global_items" not in data or not isinstance(data["global_items"], dict):
        data["global_items"] = copy.deepcopy(DEFAULT_GLOBAL_ITEMS)

    for key in ["main", "other"]:
        if key not in data["global_items"] or not isinstance(data["global_items"][key], dict):
            data["global_items"][key] = copy.deepcopy(DEFAULT_GLOBAL_ITEMS[key])
        if "label" not in data["global_items"][key]:
            data["global_items"][key]["label"] = DEFAULT_GLOBAL_ITEMS[key]["label"]
        if "items" not in data["global_items"][key] or not isinstance(data["global_items"][key]["items"], dict):
            data["global_items"][key]["items"] = {}

    if "events" not in data or not isinstance(data["events"], dict):
        data["events"] = {}

    return data


async def init_db():
    global db_pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")

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

    raw = {}
    if row and row["json"]:
        try:
            raw = json.loads(row["json"])
        except Exception:
            raw = {}

    data_store = ensure_data_defaults(raw)
    migrate_events_to_global_items()
    await save_data()


async def save_data():
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE data SET json = $1 WHERE id = 1",
            json.dumps(data_store, ensure_ascii=False)
        )


# =========================
# HELPERS
# =========================
def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def make_key(text: str) -> str:
    return text.strip().lower()


def normalize_item_name(name: str) -> str:
    return " ".join(name.strip().split()).casefold()


def make_event_display_name(base_event: str, run_date: str) -> str:
    return f"{base_event} {run_date.strip()}"


def make_event_key(base_event: str, run_date: str) -> str:
    return make_key(make_event_display_name(base_event, run_date))


def clone_categories_from_global():
    return copy.deepcopy(data_store["global_items"])


def ensure_event(base_event: str, run_date: str):
    ev_key = make_event_key(base_event, run_date)

    if ev_key not in data_store["events"]:
        data_store["events"][ev_key] = {
            "base_event": base_event,
            "run_date": run_date.strip(),
            "name": make_event_display_name(base_event, run_date),
            "priority_order": [],
            "is_locked": False,
            "panel_channel_id": None,
            "panel_message_id": None,
            "categories": clone_categories_from_global()
        }

    return data_store["events"][ev_key]


def get_event(base_event: str, run_date: str):
    return data_store["events"].get(make_event_key(base_event, run_date))


def count_user_items(event: dict, user_id: int) -> int:
    total = 0
    for cat in event["categories"].values():
        for item in cat["items"].values():
            total += sum(1 for entry in item["selections"] if entry["user_id"] == user_id)
    return total


def get_rank(event: dict, user_id: int) -> int:
    try:
        return event["priority_order"].index(user_id) + 1
    except ValueError:
        return 999999


def item_exists_globally(item_name: str) -> bool:
    target = normalize_item_name(item_name)
    for cat in data_store["global_items"].values():
        for existing_name in cat["items"].keys():
            if normalize_item_name(existing_name) == target:
                return True
    return False


def find_global_item(item_name: str):
    target = normalize_item_name(item_name)
    for cat_key, cat in data_store["global_items"].items():
        for existing_name, item_data in cat["items"].items():
            if normalize_item_name(existing_name) == target:
                return cat_key, existing_name, item_data
    return None, None, None


def migrate_events_to_global_items():
    global_items = data_store["global_items"]

    # pull any existing event-only items into global list
    for event in data_store["events"].values():
        categories = event.get("categories", {})
        for cat_key in ["main", "other"]:
            cat = categories.get(cat_key, {})
            items = cat.get("items", {})
            for item_name, item_data in items.items():
                if not item_exists_globally(item_name):
                    global_items[cat_key]["items"][item_name] = {
                        "capacity": item_data.get("capacity", 1),
                        "selections": []
                    }

    # then make sure every event contains every global item
    for event in data_store["events"].values():
        sync_global_items_to_event(event)


def sync_global_items_to_event(event: dict):
    for cat_key in ["main", "other"]:
        if cat_key not in event["categories"]:
            event["categories"][cat_key] = copy.deepcopy(DEFAULT_GLOBAL_ITEMS[cat_key])

        if "items" not in event["categories"][cat_key]:
            event["categories"][cat_key]["items"] = {}

        event_items = event["categories"][cat_key]["items"]
        global_items = data_store["global_items"][cat_key]["items"]

        # add missing global items to event
        for item_name, global_item in global_items.items():
            if item_name not in event_items:
                event_items[item_name] = {
                    "capacity": global_item["capacity"],
                    "selections": []
                }

        # remove items that no longer exist globally
        to_remove = [name for name in event_items.keys() if name not in global_items]
        for item_name in to_remove:
            del event_items[item_name]


def sync_global_items_to_all_events():
    for event in data_store["events"].values():
        sync_global_items_to_event(event)


def auto_assign_leftovers(event: dict):
    players = event["priority_order"]

    for cat in event["categories"].values():
        for item in cat["items"].values():
            while len(item["selections"]) < item["capacity"]:
                assigned = False

                for user_id in players:
                    already_on_item = user_id in [x["user_id"] for x in item["selections"]]
                    if already_on_item:
                        continue

                    if count_user_items(event, user_id) >= MAX_ITEMS_PER_PLAYER:
                        continue

                    item["selections"].append({
                        "user_id": user_id,
                        "selected_at": now_ts()
                    })
                    assigned = True
                    break

                if not assigned:
                    break


def build_category_text(event: dict, category_key: str) -> str:
    cat = event["categories"][category_key]
    blocks = []

    for item_name, item_data in cat["items"].items():
        count = len(item_data["selections"])
        cap = item_data["capacity"]
        users = [f"<@{x['user_id']}>" for x in item_data["selections"]]

        if users:
            display_users = ", ".join(users[:6])
            if len(users) > 6:
                display_users += f" +{len(users) - 6} more"
        else:
            display_users = "—"

        blocks.append(f"**{item_name}** `{count}/{cap}`\n{display_users}")

    text = "\n\n".join(blocks)
    return text[:1024] if text else "No items yet."


def build_priority_preview(event: dict) -> str:
    if not event["priority_order"]:
        return "No priority players set."

    lines = []
    for i, user_id in enumerate(event["priority_order"][:10], start=1):
        lines.append(f"{i}. <@{user_id}>")

    if len(event["priority_order"]) > 10:
        lines.append(f"+{len(event['priority_order']) - 10} more")

    return "\n".join(lines)[:1024]


def build_embed(event: dict) -> discord.Embed:
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
    embed.add_field(
        name="Priority Order",
        value=build_priority_preview(event),
        inline=False
    )

    return embed


async def refresh_panel_by_event(event: dict):
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
        await message.edit(
            embed=build_embed(event),
            view=EventView(make_key(event["name"]))
        )
    except Exception as e:
        print(f"Failed to refresh panel: {e}")


async def refresh_all_event_panels():
    for event in data_store["events"].values():
        await refresh_panel_by_event(event)


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
            custom_id=f"select:{ev_key}:{cat_key}",
            placeholder=placeholder,
            options=options[:25] if options else [
                discord.SelectOption(label="No items available", value="__none__")
            ],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message("No items available.", ephemeral=True)
            return

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
            lowest = max(
                item["selections"],
                key=lambda x: (get_rank(event, x["user_id"]), x["selected_at"])
            )
            if get_rank(event, interaction.user.id) < get_rank(event, lowest["user_id"]):
                item["selections"].remove(lowest)
            else:
                await interaction.response.send_message("❌ This item is full.", ephemeral=True)
                return

        item["selections"].append({
            "user_id": interaction.user.id,
            "selected_at": now_ts()
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

        super().__init__(
            custom_id=f"remove:{ev_key}",
            placeholder="Remove one of your selected items",
            options=options[:25] if options else [
                discord.SelectOption(label="No items available", value="__none__")
            ],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
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
@bot.tree.command(name="create_event", description="Create a dated event from preset list")
@app_commands.choices(event=PRESET_EVENT_CHOICES)
async def create_event(
    interaction: discord.Interaction,
    event: app_commands.Choice[str],
    run_date: str
):
    await interaction.response.defer(ephemeral=True)

    ev = ensure_event(event.value, run_date)
    await save_data()

    await interaction.followup.send(
        f"✅ Event **{ev['name']}** created.",
        ephemeral=True
    )


@bot.tree.command(name="create_panel", description="Create a panel from preset event list")
@app_commands.choices(event=PRESET_EVENT_CHOICES)
async def create_panel(
    interaction: discord.Interaction,
    event: app_commands.Choice[str],
    run_date: str
):
    await interaction.response.defer(ephemeral=True)

    ev = ensure_event(event.value, run_date)
    msg = await interaction.channel.send(
        embed=build_embed(ev),
        view=EventView(make_key(ev["name"]))
    )

    ev["panel_channel_id"] = interaction.channel.id
    ev["panel_message_id"] = msg.id

    await save_data()
    await interaction.followup.send(
        f"✅ Panel created for **{ev['name']}**.",
        ephemeral=True
    )


@bot.tree.command(name="add_item", description="Add a global item for all events")
@app_commands.choices(category=CATEGORY_CHOICES)
async def add_item(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    item_name: str,
    cap: app_commands.Range[int, 1, 99]
):
    await interaction.response.defer(ephemeral=True)

    if item_exists_globally(item_name):
        await interaction.followup.send(
            "❌ That item name already exists globally.",
            ephemeral=True
        )
        return

    data_store["global_items"][category.value]["items"][item_name] = {
        "capacity": cap,
        "selections": []
    }

    sync_global_items_to_all_events()
    await save_data()
    await refresh_all_event_panels()

    await interaction.followup.send(
        f"✅ Added **{item_name}** under **{category.name}**.\n"
        f"It is now available across all events.",
        ephemeral=True
    )


@bot.tree.command(name="edit_item", description="Edit a global item")
@app_commands.choices(category=CATEGORY_CHOICES)
async def edit_item(
    interaction: discord.Interaction,
    current_item_name: str,
    new_item_name: str,
    category: app_commands.Choice[str],
    cap: app_commands.Range[int, 1, 99]
):
    await interaction.response.defer(ephemeral=True)

    old_cat_key, old_name, old_item = find_global_item(current_item_name)
    if not old_name:
        await interaction.followup.send("❌ Item not found.", ephemeral=True)
        return

    if normalize_item_name(new_item_name) != normalize_item_name(old_name):
        if item_exists_globally(new_item_name):
            await interaction.followup.send(
                "❌ The new item name already exists globally.",
                ephemeral=True
            )
            return

    # rebuild global item
    del data_store["global_items"][old_cat_key]["items"][old_name]
    data_store["global_items"][category.value]["items"][new_item_name] = {
        "capacity": cap,
        "selections": []
    }

    # update all events, preserving selections when possible
    for event in data_store["events"].values():
        old_event_item = None
        if old_name in event["categories"][old_cat_key]["items"]:
            old_event_item = event["categories"][old_cat_key]["items"][old_name]

        if old_event_item:
            selections = old_event_item.get("selections", [])
            del event["categories"][old_cat_key]["items"][old_name]
        else:
            selections = []

        event["categories"][category.value]["items"][new_item_name] = {
            "capacity": cap,
            "selections": selections
        }

    await save_data()
    await refresh_all_event_panels()

    await interaction.followup.send(
        f"✅ Updated **{current_item_name}** → **{new_item_name}**.\n"
        f"Category: **{category.name}** | Cap: **{cap}**",
        ephemeral=True
    )


@bot.tree.command(name="remove_item", description="Remove a global item from all events")
async def remove_item(
    interaction: discord.Interaction,
    item_name: str
):
    await interaction.response.defer(ephemeral=True)

    cat_key, existing_name, _ = find_global_item(item_name)
    if not existing_name:
        await interaction.followup.send("❌ Item not found.", ephemeral=True)
        return

    del data_store["global_items"][cat_key]["items"][existing_name]

    for event in data_store["events"].values():
        if existing_name in event["categories"][cat_key]["items"]:
            del event["categories"][cat_key]["items"][existing_name]

    await save_data()
    await refresh_all_event_panels()

    await interaction.followup.send(
        f"✅ Removed **{existing_name}** from all events.",
        ephemeral=True
    )


@bot.tree.command(name="add_priority", description="Add a priority player to a dated event")
@app_commands.choices(event=PRESET_EVENT_CHOICES)
async def add_priority(
    interaction: discord.Interaction,
    event: app_commands.Choice[str],
    run_date: str,
    user: discord.Member
):
    await interaction.response.defer(ephemeral=True)

    ev = ensure_event(event.value, run_date)
    if user.id not in ev["priority_order"]:
        ev["priority_order"].append(user.id)

    await save_data()
    await refresh_panel_by_event(ev)
    await interaction.followup.send(
        f"✅ Added {user.mention} to priority for **{ev['name']}**.",
        ephemeral=True
    )


@bot.tree.command(name="lock_event", description="Lock event, auto-assign leftovers, and announce winners")
@app_commands.choices(event=PRESET_EVENT_CHOICES)
async def lock_event(
    interaction: discord.Interaction,
    event: app_commands.Choice[str],
    run_date: str
):
    await interaction.response.defer(ephemeral=True)

    ev = ensure_event(event.value, run_date)
    auto_assign_leftovers(ev)
    ev["is_locked"] = True

    winners = [f"🏆 **Winners — {ev['name']}**", ""]
    for cat in ev["categories"].values():
        for item_name, item_data in cat["items"].items():
            if item_data["selections"]:
                users = ", ".join([f"<@{x['user_id']}>" for x in item_data["selections"]])
                winners.append(f"**{item_name}**: {users}")

    await save_data()
    await refresh_panel_by_event(ev)

    await interaction.channel.send("\n".join(winners))
    await interaction.followup.send(f"✅ Event **{ev['name']}** locked.", ephemeral=True)


@bot.tree.command(name="unlock_event", description="Unlock a dated event")
@app_commands.choices(event=PRESET_EVENT_CHOICES)
async def unlock_event(
    interaction: discord.Interaction,
    event: app_commands.Choice[str],
    run_date: str
):
    await interaction.response.defer(ephemeral=True)

    ev = get_event(event.value, run_date)
    if not ev:
        await interaction.followup.send("❌ Event not found.", ephemeral=True)
        return

    ev["is_locked"] = False
    await save_data()
    await refresh_panel_by_event(ev)

    await interaction.followup.send(f"✅ Event **{ev['name']}** unlocked.", ephemeral=True)


@bot.tree.command(name="show_events", description="Show all created dated events")
async def show_events(interaction: discord.Interaction):
    events = data_store.get("events", {})
    if not events:
        await interaction.response.send_message("No events yet.", ephemeral=True)
        return

    names = [f"• {ev['name']}" for ev in events.values()]
    await interaction.response.send_message("\n".join(names[:100]), ephemeral=True)


@bot.tree.command(name="show_items", description="Show all global items")
async def show_items(interaction: discord.Interaction):
    main_items = list(data_store["global_items"]["main"]["items"].keys())
    other_items = list(data_store["global_items"]["other"]["items"].keys())

    main_text = "\n".join(f"• {x}" for x in main_items[:50]) if main_items else "None"
    other_text = "\n".join(f"• {x}" for x in other_items[:50]) if other_items else "None"

    embed = discord.Embed(
        title="Global Item List",
        color=discord.Color.green()
    )
    embed.add_field(name="Main Items", value=main_text[:1024], inline=False)
    embed.add_field(name="Other Items", value=other_text[:1024], inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# =========================
# READY
# =========================
@bot.event
async def on_ready():
    await init_db()
    await load_data()

    for ev_key in data_store["events"].keys():
        bot.add_view(EventView(ev_key))

    await bot.tree.sync()
    print("READY")


bot.run(TOKEN)
