import os
import json
import math
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
    app_commands.Choice(name=name, value=name) for name in PRESET_EVENTS
]

ITEMS_PER_PAGE = 25

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

db_pool = None
data_store = {
    "global_items": {},
    "events": {}
}


# =========================
# DATABASE
# =========================
def ensure_data_defaults(data: dict) -> dict:
    if not isinstance(data, dict):
        data = {}

    if "global_items" not in data or not isinstance(data["global_items"], dict):
        data["global_items"] = {}

    # migrate old top-level category format -> flat global_items
    old_categories = data.get("categories")
    if isinstance(old_categories, dict):
        for cat_data in old_categories.values():
            if not isinstance(cat_data, dict):
                continue
            items = cat_data.get("items", {})
            if not isinstance(items, dict):
                continue
            for item_name, item_data in items.items():
                if item_name not in data["global_items"]:
                    data["global_items"][item_name] = {
                        "capacity": int(item_data.get("capacity", 1))
                    }

    if "events" not in data or not isinstance(data["events"], dict):
        data["events"] = {}

    for ev in data["events"].values():
        if "items" not in ev or not isinstance(ev["items"], dict):
            ev["items"] = {}

        # migrate old event category format -> flat items
        old_ev_categories = ev.get("categories")
        if isinstance(old_ev_categories, dict):
            for cat_data in old_ev_categories.values():
                if not isinstance(cat_data, dict):
                    continue
                items = cat_data.get("items", {})
                if not isinstance(items, dict):
                    continue
                for item_name, item_data in items.items():
                    if item_name not in ev["items"]:
                        ev["items"][item_name] = {
                            "capacity": int(item_data.get("capacity", 1)),
                            "selections": item_data.get("selections", [])
                        }

        if "priority_order" not in ev or not isinstance(ev["priority_order"], list):
            ev["priority_order"] = []

        if "is_locked" not in ev or not isinstance(ev["is_locked"], bool):
            ev["is_locked"] = False

        if "panel_channel_id" not in ev:
            ev["panel_channel_id"] = None

        if "panel_message_id" not in ev:
            ev["panel_message_id"] = None

        if "ui_state" not in ev or not isinstance(ev["ui_state"], dict):
            ev["ui_state"] = {}

        if not isinstance(ev["ui_state"].get("page"), int) or ev["ui_state"]["page"] < 0:
            ev["ui_state"]["page"] = 0

        if "categories" in ev:
            del ev["categories"]

    if "categories" in data:
        del data["categories"]

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
            "items": {},
            "ui_state": {
                "page": 0
            }
        }

    return data_store["events"][ev_key]


def get_event(base_event: str, run_date: str):
    return data_store["events"].get(make_event_key(base_event, run_date))


def get_rank(event: dict, user_id: int) -> int:
    try:
        return event["priority_order"].index(user_id) + 1
    except ValueError:
        return 999999


def item_exists_globally(item_name: str) -> bool:
    target = normalize_item_name(item_name)
    for existing_name in data_store["global_items"].keys():
        if normalize_item_name(existing_name) == target:
            return True
    return False


def find_global_item(item_name: str):
    target = normalize_item_name(item_name)
    for existing_name, item_data in data_store["global_items"].items():
        if normalize_item_name(existing_name) == target:
            return existing_name, item_data
    return None, None


def split_bulk_item_input(text: str):
    if not text:
        return []

    normalized = text.replace("\r", "\n").replace(",", "\n").replace(";", "\n")
    parts = [x.strip() for x in normalized.split("\n")]

    cleaned = []
    seen = set()

    for item in parts:
        if not item:
            continue
        key = normalize_item_name(item)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)

    return cleaned


def get_sorted_event_items(event: dict):
    return sorted(
        event["items"].items(),
        key=lambda x: x[0].lower()
    )


def get_page_count(event: dict) -> int:
    total = len(event["items"])
    return max(1, math.ceil(total / ITEMS_PER_PAGE))


def clamp_page(event: dict):
    max_pages = get_page_count(event)
    page = event["ui_state"].get("page", 0)
    if not isinstance(page, int):
        page = 0

    page = max(0, min(page, max_pages - 1))
    event["ui_state"]["page"] = page


def get_current_page_items(event: dict):
    clamp_page(event)
    page = event["ui_state"]["page"]

    all_items = get_sorted_event_items(event)
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE

    return page, all_items[start:end]


def get_user_selected_items(event: dict, user_id: int):
    results = []
    for item_name, item_data in get_sorted_event_items(event):
        if any(x["user_id"] == user_id for x in item_data["selections"]):
            results.append((item_name, item_data))
    return results


def auto_assign_leftovers(event: dict):
    players = event["priority_order"]

    for item in event["items"].values():
        while len(item["selections"]) < item["capacity"]:
            assigned = False

            for user_id in players:
                if user_id in [x["user_id"] for x in item["selections"]]:
                    continue

                item["selections"].append({
                    "user_id": user_id,
                    "selected_at": now_ts()
                })
                assigned = True
                break

            if not assigned:
                break


def build_priority_preview(event: dict) -> str:
    if not event["priority_order"]:
        return "No priority players set."

    lines = []
    for i, user_id in enumerate(event["priority_order"], start=1):
        lines.append(f"{i}. <@{user_id}>")

    text = "\n".join(lines)
    if len(text) > 1024:
        text = text[:1020] + "..."
    return text


def build_current_page_details(event: dict) -> str:
    clamp_page(event)
    page, page_items = get_current_page_items(event)
    page_count = get_page_count(event)

    if not page_items:
        return f"No items on this page.\nPage **{page + 1}/{page_count}**"

    blocks = [f"Page **{page + 1}/{page_count}**", ""]

    for item_name, item_data in page_items:
        count = len(item_data["selections"])
        cap = item_data["capacity"]
        users = [f"<@{x['user_id']}>" for x in item_data["selections"]]

        display_users = ", ".join(users) if users else "—"

        blocks.append(f"**{item_name}** `{count}/{cap}`")
        blocks.append(display_users)
        blocks.append("")

    text = "\n".join(blocks).strip()
    if len(text) > 4096:
        text = text[:4092] + "..."
    return text


def build_embed(event: dict) -> discord.Embed:
    clamp_page(event)
    page_count = get_page_count(event)
    current_page = event["ui_state"]["page"] + 1

    embed = discord.Embed(
        title=f"🎁 {event['name']}",
        description=(
            f"**Page:** {current_page}/{page_count}\n"
            f"**Max per player:** Unlimited\n"
            f"**Status:** {'Locked' if event['is_locked'] else 'Open'}"
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="Items",
        value=build_current_page_details(event),
        inline=False
    )

    embed.add_field(
        name="Priority Order",
        value=build_priority_preview(event),
        inline=False
    )

    return embed


def is_admin(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator


async def refresh_panel_by_event(event: dict):
    clamp_page(event)

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
            view=PanelView(make_key(event["name"]))
        )
    except Exception as e:
        print(f"Failed to refresh panel: {e}")


# =========================
# PLAYER UI
# =========================
class ItemSelect(discord.ui.Select):
    def __init__(self, ev_key: str):
        self.ev_key = ev_key
        event = data_store["events"][ev_key]

        _, page_items = get_current_page_items(event)

        options = [
            discord.SelectOption(
                label=item_name[:100],
                value=item_name,
                description=f"{len(item_data['selections'])}/{item_data['capacity']} reserved"
            )
            for item_name, item_data in page_items
        ]

        super().__init__(
            custom_id=f"pick:{ev_key}",
            placeholder="Choose the item",
            options=options if options else [
                discord.SelectOption(label="No items available", value="__none__")
            ],
            min_values=1,
            max_values=1,
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message("No items available.", ephemeral=True)
            return

        event = data_store["events"][self.ev_key]

        if event["is_locked"]:
            await interaction.response.send_message("❌ This panel is locked.", ephemeral=True)
            return

        selected_name = self.values[0]
        item = event["items"][selected_name]

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


# =========================
# REMOVE MY ITEM UI
# =========================
class RemoveMyItemSelect(discord.ui.Select):
    def __init__(self, ev_key: str, user_id: int, page: int = 0, query: str | None = None):
        self.ev_key = ev_key
        self.user_id = user_id
        self.page = page
        self.query = query or ""

        event = data_store["events"][ev_key]
        items = get_user_selected_items(event, user_id)

        if self.query:
            q = normalize_item_name(self.query)
            items = [
                (item_name, item_data)
                for item_name, item_data in items
                if q in normalize_item_name(item_name)
            ]

        self.filtered_items = items
        self.total_pages = max(1, math.ceil(len(items) / ITEMS_PER_PAGE))
        self.page = max(0, min(self.page, self.total_pages - 1))

        start = self.page * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]

        options = [
            discord.SelectOption(
                label=item_name[:100],
                value=item_name,
                description=f"{len(item_data['selections'])}/{item_data['capacity']}"
            )
            for item_name, item_data in page_items
        ]

        super().__init__(
            placeholder=f"Your selected items • Page {self.page + 1}/{self.total_pages}",
            options=options if options else [
                discord.SelectOption(label="No matching selected items", value="__none__")
            ],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message("No removable item found.", ephemeral=True)
            return

        event = data_store["events"][self.ev_key]

        if event["is_locked"]:
            await interaction.response.send_message("❌ This panel is locked.", ephemeral=True)
            return

        item_name = self.values[0]
        item = event["items"][item_name]

        before = len(item["selections"])
        item["selections"] = [x for x in item["selections"] if x["user_id"] != interaction.user.id]

        if len(item["selections"]) == before:
            await interaction.response.send_message("You did not select that item.", ephemeral=True)
            return

        await save_data()
        await refresh_panel_by_event(event)

        new_view = RemoveMyItemView(self.ev_key, interaction.user.id, page=self.page, query=self.query)
        await interaction.response.edit_message(
            content=f"🗑️ Removed **{item_name}**.",
            view=new_view
        )


class RemoveMyItemSearchModal(discord.ui.Modal, title="Search My Selected Items"):
    query = discord.ui.TextInput(label="Search item name", required=True, max_length=100)

    def __init__(self, ev_key: str, user_id: int):
        super().__init__()
        self.ev_key = ev_key
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        view = RemoveMyItemView(self.ev_key, self.user_id, page=0, query=self.query.value)
        await interaction.response.send_message(
            f"Search results for: **{self.query.value}**",
            view=view,
            ephemeral=True
        )


class RemoveMyItemPrevButton(discord.ui.Button):
    def __init__(self, ev_key: str, user_id: int, page: int, query: str | None):
        super().__init__(label="⬅️ Prev", style=discord.ButtonStyle.secondary)
        self.ev_key = ev_key
        self.user_id = user_id
        self.page = page
        self.query = query or ""

    async def callback(self, interaction: discord.Interaction):
        new_view = RemoveMyItemView(self.ev_key, self.user_id, page=max(0, self.page - 1), query=self.query)
        await interaction.response.edit_message(view=new_view)


class RemoveMyItemPageInfoButton(discord.ui.Button):
    def __init__(self, total_pages: int, page: int):
        super().__init__(
            label=f"Page {page + 1}/{total_pages}",
            style=discord.ButtonStyle.primary,
            disabled=True
        )


class RemoveMyItemNextButton(discord.ui.Button):
    def __init__(self, ev_key: str, user_id: int, page: int, total_pages: int, query: str | None):
        super().__init__(label="Next ➡️", style=discord.ButtonStyle.secondary)
        self.ev_key = ev_key
        self.user_id = user_id
        self.page = page
        self.total_pages = total_pages
        self.query = query or ""

    async def callback(self, interaction: discord.Interaction):
        new_view = RemoveMyItemView(
            self.ev_key,
            self.user_id,
            page=min(self.total_pages - 1, self.page + 1),
            query=self.query
        )
        await interaction.response.edit_message(view=new_view)


class RemoveMyItemSearchButton(discord.ui.Button):
    def __init__(self, ev_key: str, user_id: int):
        super().__init__(label="🔍 Search My Items", style=discord.ButtonStyle.secondary)
        self.ev_key = ev_key
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RemoveMyItemSearchModal(self.ev_key, self.user_id))


class ClearRemoveMyItemSearchButton(discord.ui.Button):
    def __init__(self, ev_key: str, user_id: int):
        super().__init__(label="♻️ Clear Search", style=discord.ButtonStyle.secondary)
        self.ev_key = ev_key
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        new_view = RemoveMyItemView(self.ev_key, self.user_id, page=0, query="")
        await interaction.response.edit_message(content="Your selected items:", view=new_view)


class RemoveMyItemView(discord.ui.View):
    def __init__(self, ev_key: str, user_id: int, page: int = 0, query: str | None = None):
        super().__init__(timeout=300)
        self.ev_key = ev_key
        self.user_id = user_id
        self.page = page
        self.query = query or ""

        select = RemoveMyItemSelect(ev_key, user_id, page=page, query=self.query)
        self.page = select.page
        total_pages = select.total_pages

        self.add_item(select)
        self.add_item(RemoveMyItemPrevButton(ev_key, user_id, self.page, self.query))
        self.add_item(RemoveMyItemPageInfoButton(total_pages, self.page))
        self.add_item(RemoveMyItemNextButton(ev_key, user_id, self.page, total_pages, self.query))
        self.add_item(RemoveMyItemSearchButton(ev_key, user_id))

        if self.query:
            self.add_item(ClearRemoveMyItemSearchButton(ev_key, user_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This remover is only for the user who opened it.", ephemeral=True)
            return False
        return True


class RemoveMyItemButton(discord.ui.Button):
    def __init__(self, ev_key: str):
        super().__init__(
            label="Remove my Item",
            style=discord.ButtonStyle.danger,
            custom_id=f"removemyitem:{ev_key}",
            row=1
        )
        self.ev_key = ev_key

    async def callback(self, interaction: discord.Interaction):
        event = data_store["events"][self.ev_key]

        if event["is_locked"]:
            await interaction.response.send_message("❌ This panel is locked.", ephemeral=True)
            return

        selected = get_user_selected_items(event, interaction.user.id)
        if not selected:
            await interaction.response.send_message("You have no selected items to remove.", ephemeral=True)
            return

        view = RemoveMyItemView(self.ev_key, interaction.user.id)
        await interaction.response.send_message("Your selected items:", view=view, ephemeral=True)


# =========================
# PAGE CONTROLS
# =========================
class PrevPageButton(discord.ui.Button):
    def __init__(self, ev_key: str):
        super().__init__(
            label="⬅️ Prev",
            style=discord.ButtonStyle.secondary,
            custom_id=f"prev:{ev_key}",
            row=2
        )
        self.ev_key = ev_key

    async def callback(self, interaction: discord.Interaction):
        event = data_store["events"][self.ev_key]
        clamp_page(event)
        event["ui_state"]["page"] = max(0, event["ui_state"]["page"] - 1)

        await save_data()
        await interaction.response.edit_message(
            embed=build_embed(event),
            view=PanelView(self.ev_key)
        )


class PageInfoButton(discord.ui.Button):
    def __init__(self, ev_key: str):
        event = data_store["events"][ev_key]
        clamp_page(event)
        page = event["ui_state"]["page"]
        total_pages = get_page_count(event)

        super().__init__(
            label=f"Page {page + 1}/{total_pages}",
            style=discord.ButtonStyle.primary,
            custom_id=f"pageinfo:{ev_key}",
            disabled=True,
            row=2
        )


class NextPageButton(discord.ui.Button):
    def __init__(self, ev_key: str):
        super().__init__(
            label="Next ➡️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"next:{ev_key}",
            row=2
        )
        self.ev_key = ev_key

    async def callback(self, interaction: discord.Interaction):
        event = data_store["events"][self.ev_key]
        clamp_page(event)
        max_pages = get_page_count(event)
        event["ui_state"]["page"] = min(max_pages - 1, event["ui_state"]["page"] + 1)

        await save_data()
        await interaction.response.edit_message(
            embed=build_embed(event),
            view=PanelView(self.ev_key)
        )


# =========================
# ADMIN UI
# =========================
class EditCapItemSelect(discord.ui.Select):
    def __init__(self, ev_key: str):
        self.ev_key = ev_key
        event = data_store["events"][ev_key]

        options = []
        for item_name, item_data in get_sorted_event_items(event):
            options.append(
                discord.SelectOption(
                    label=item_name[:100],
                    value=item_name,
                    description=f"Current cap: {item_data['capacity']}"
                )
            )

        super().__init__(
            placeholder="Select item to edit cap",
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

        item_name = self.values[0]
        await interaction.response.send_modal(EditCapModal(self.ev_key, item_name))


class EditCapModal(discord.ui.Modal, title="Set New Cap"):
    new_cap = discord.ui.TextInput(label="New cap", required=True, max_length=3)

    def __init__(self, ev_key: str, item_name: str):
        super().__init__()
        self.ev_key = ev_key
        self.item_name = item_name

    async def on_submit(self, interaction: discord.Interaction):
        try:
            cap = int(self.new_cap.value)
            if cap < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Cap must be a whole number greater than 0.", ephemeral=True)
            return

        event = data_store["events"][self.ev_key]
        item = event["items"][self.item_name]
        item["capacity"] = cap

        if len(item["selections"]) > cap:
            item["selections"].sort(key=lambda x: (get_rank(event, x["user_id"]), x["selected_at"]))
            item["selections"] = item["selections"][:cap]

        await save_data()
        await refresh_panel_by_event(event)
        await interaction.response.send_message(
            f"✅ Updated **{self.item_name}** cap to **{cap}**.",
            ephemeral=True
        )


class EditCapButton(discord.ui.Button):
    def __init__(self, ev_key: str):
        super().__init__(
            label="Edit Cap",
            style=discord.ButtonStyle.primary,
            custom_id=f"editcap:{ev_key}",
            row=3
        )
        self.ev_key = ev_key

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        view = discord.ui.View()
        view.add_item(EditCapItemSelect(self.ev_key))
        await interaction.response.send_message("Select item to edit:", view=view, ephemeral=True)


class SearchItemSelect(discord.ui.Select):
    def __init__(self, ev_key: str, cap: int, results):
        self.ev_key = ev_key
        self.cap = cap

        options = [
            discord.SelectOption(
                label=name[:100],
                value=name,
                description="Global item"
            )
            for name in results[:25]
        ]

        super().__init__(
            placeholder=f"Search results (cap = {cap})",
            options=options if options else [
                discord.SelectOption(label="No results found", value="__none__")
            ],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message("No matching items found.", ephemeral=True)
            return

        event = data_store["events"][self.ev_key]
        name = self.values[0]

        if name in event["items"]:
            await interaction.response.send_message("❌ Item already added to this event.", ephemeral=True)
            return

        event["items"][name] = {
            "capacity": self.cap,
            "selections": []
        }

        await save_data()
        await refresh_panel_by_event(event)
        await interaction.response.send_message(
            f"✅ Added **{name}** with cap **{self.cap}**.",
            ephemeral=True
        )


class SearchItemModal(discord.ui.Modal, title="Search Global Item"):
    query = discord.ui.TextInput(label="Search item name", required=True, max_length=100)
    cap_input = discord.ui.TextInput(label="Cap for this item", required=True, max_length=3)

    def __init__(self, ev_key: str):
        super().__init__()
        self.ev_key = ev_key

    async def on_submit(self, interaction: discord.Interaction):
        try:
            cap = int(self.cap_input.value)
            if cap < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Cap must be a whole number greater than 0.", ephemeral=True)
            return

        q = normalize_item_name(self.query.value)
        results = []

        for item_name in sorted(data_store["global_items"].keys(), key=str.lower):
            if q in normalize_item_name(item_name):
                results.append(item_name)

        if not results:
            await interaction.response.send_message("❌ No matching global items found.", ephemeral=True)
            return

        view = discord.ui.View()
        view.add_item(SearchItemSelect(self.ev_key, cap, results))
        await interaction.response.send_message("Select search result:", view=view, ephemeral=True)


class BulkAddTextModal(discord.ui.Modal, title="Bulk Add by Text"):
    cap_input = discord.ui.TextInput(
        label="Cap for all matched items",
        required=True,
        max_length=3
    )
    items_input = discord.ui.TextInput(
        label="Item names",
        placeholder="One per line, or separated by commas",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=4000
    )

    def __init__(self, ev_key: str):
        super().__init__()
        self.ev_key = ev_key

    async def on_submit(self, interaction: discord.Interaction):
        try:
            cap = int(self.cap_input.value)
            if cap < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Cap must be a whole number greater than 0.",
                ephemeral=True
            )
            return

        raw_items = split_bulk_item_input(self.items_input.value)
        if not raw_items:
            await interaction.response.send_message("❌ No valid item names found.", ephemeral=True)
            return

        event = data_store["events"][self.ev_key]
        added = []
        already_added = []
        not_found = []

        for raw_name in raw_items:
            actual_name, _ = find_global_item(raw_name)

            if not actual_name:
                not_found.append(raw_name)
                continue

            if actual_name in event["items"]:
                already_added.append(actual_name)
                continue

            event["items"][actual_name] = {
                "capacity": cap,
                "selections": []
            }
            added.append(actual_name)

        await save_data()
        await refresh_panel_by_event(event)

        parts = []

        if added:
            parts.append("✅ **Added:**\n" + "\n".join(f"• {name}" for name in added[:50]))
            if len(added) > 50:
                parts.append(f"…and {len(added) - 50} more added.")

        if already_added:
            parts.append("⚠️ **Already in panel:**\n" + "\n".join(f"• {name}" for name in already_added[:30]))
            if len(already_added) > 30:
                parts.append(f"…and {len(already_added) - 30} more already in panel.")

        if not_found:
            parts.append("❌ **Not found in global list:**\n" + "\n".join(f"• {name}" for name in not_found[:30]))
            if len(not_found) > 30:
                parts.append(f"…and {len(not_found) - 30} more not found.")

        if not parts:
            parts.append("Nothing was added.")

        message = "\n\n".join(parts)
        if len(message) > 1900:
            message = message[:1900] + "\n\n…message trimmed."

        await interaction.response.send_message(message, ephemeral=True)


class AddItemModeView(discord.ui.View):
    def __init__(self, ev_key: str):
        super().__init__(timeout=300)
        self.ev_key = ev_key

    @discord.ui.button(label="Search Item", style=discord.ButtonStyle.secondary)
    async def search_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        if not data_store["global_items"]:
            await interaction.response.send_message("❌ No global items available yet.", ephemeral=True)
            return

        await interaction.response.send_modal(SearchItemModal(self.ev_key))

    @discord.ui.button(label="Bulk Add by Text", style=discord.ButtonStyle.success)
    async def bulk_add_text(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        if not data_store["global_items"]:
            await interaction.response.send_message("❌ No global items available yet.", ephemeral=True)
            return

        await interaction.response.send_modal(BulkAddTextModal(self.ev_key))


class AddItemButton(discord.ui.Button):
    def __init__(self, ev_key: str):
        super().__init__(
            label="Add Item",
            style=discord.ButtonStyle.success,
            custom_id=f"additem:{ev_key}",
            row=3
        )
        self.ev_key = ev_key

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        if not data_store["global_items"]:
            await interaction.response.send_message("❌ No global items available yet.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Choose how to add items to this panel:",
            view=AddItemModeView(self.ev_key),
            ephemeral=True
        )


class RemoveItemFromPanelSelect(discord.ui.Select):
    def __init__(self, ev_key: str):
        self.ev_key = ev_key
        event = data_store["events"][ev_key]

        options = []
        for item_name, item_data in get_sorted_event_items(event):
            options.append(
                discord.SelectOption(
                    label=item_name[:100],
                    value=item_name,
                    description=f"{len(item_data['selections'])}/{item_data['capacity']}"
                )
            )

        super().__init__(
            placeholder="Select item to remove from panel",
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
        item_name = self.values[0]

        if item_name not in event["items"]:
            await interaction.response.send_message("❌ Item not found.", ephemeral=True)
            return

        del event["items"][item_name]
        clamp_page(event)

        await save_data()
        await refresh_panel_by_event(event)
        await interaction.response.send_message(
            f"✅ Removed **{item_name}** from this panel.",
            ephemeral=True
        )


class RemoveItemButton(discord.ui.Button):
    def __init__(self, ev_key: str):
        super().__init__(
            label="Remove Item",
            style=discord.ButtonStyle.danger,
            custom_id=f"removeitem:{ev_key}",
            row=3
        )
        self.ev_key = ev_key

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        view = discord.ui.View()
        view.add_item(RemoveItemFromPanelSelect(self.ev_key))
        await interaction.response.send_message("Select item to remove:", view=view, ephemeral=True)


class RemovePlayerItemSelect(discord.ui.Select):
    def __init__(self, ev_key: str):
        self.ev_key = ev_key
        event = data_store["events"][ev_key]

        options = []
        for item_name, item_data in get_sorted_event_items(event):
            if item_data["selections"]:
                options.append(
                    discord.SelectOption(
                        label=item_name[:100],
                        value=item_name,
                        description=f"{len(item_data['selections'])}/{item_data['capacity']} selected"
                    )
                )

        super().__init__(
            placeholder="Select item",
            options=options[:25] if options else [
                discord.SelectOption(label="No items with players", value="__none__")
            ],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message("No items with selected players.", ephemeral=True)
            return

        item_name = self.values[0]
        view = discord.ui.View()
        view.add_item(RemovePlayerUserSelect(self.ev_key, item_name))
        await interaction.response.send_message(
            f"Select player to remove from **{item_name}**:",
            view=view,
            ephemeral=True
        )


class RemovePlayerUserSelect(discord.ui.Select):
    def __init__(self, ev_key: str, item_name: str):
        self.ev_key = ev_key
        self.item_name = item_name

        event = data_store["events"][ev_key]
        item = event["items"][item_name]

        options = []
        for entry in item["selections"]:
            user_id = entry["user_id"]
            options.append(
                discord.SelectOption(
                    label=str(user_id),
                    value=str(user_id),
                    description=f"Remove user {user_id} from this item"[:100]
                )
            )

        super().__init__(
            placeholder="Select player to remove",
            options=options[:25] if options else [
                discord.SelectOption(label="No players found", value="__none__")
            ],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message("No players found.", ephemeral=True)
            return

        event = data_store["events"][self.ev_key]
        item = event["items"][self.item_name]
        target_user_id = int(self.values[0])

        before = len(item["selections"])
        item["selections"] = [
            x for x in item["selections"]
            if x["user_id"] != target_user_id
        ]

        if len(item["selections"]) == before:
            await interaction.response.send_message("Player was not found in that item.", ephemeral=True)
            return

        await save_data()
        await refresh_panel_by_event(event)
        await interaction.response.send_message(
            f"✅ Removed <@{target_user_id}> from **{self.item_name}**.",
            ephemeral=True
        )


class RemovePlayerButton(discord.ui.Button):
    def __init__(self, ev_key: str):
        super().__init__(
            label="Remove Player",
            style=discord.ButtonStyle.danger,
            custom_id=f"removeplayer:{ev_key}",
            row=4
        )
        self.ev_key = ev_key

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        view = discord.ui.View()
        view.add_item(RemovePlayerItemSelect(self.ev_key))
        await interaction.response.send_message(
            "Select the item first:",
            view=view,
            ephemeral=True
        )


class AddPriorityUserSelect(discord.ui.UserSelect):
    def __init__(self, ev_key: str):
        self.ev_key = ev_key
        super().__init__(placeholder="Select user to add to priority", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        event = data_store["events"][self.ev_key]
        user = self.values[0]

        if user.id in event["priority_order"]:
            await interaction.response.send_message("❌ User is already in the priority list.", ephemeral=True)
            return

        event["priority_order"].append(user.id)
        await save_data()
        await refresh_panel_by_event(event)
        await interaction.response.send_message(f"✅ Added {user.mention} to priority.", ephemeral=True)


class AddPriorityButton(discord.ui.Button):
    def __init__(self, ev_key: str):
        super().__init__(
            label="Add Priority",
            style=discord.ButtonStyle.primary,
            custom_id=f"addpriority:{ev_key}",
            row=4
        )
        self.ev_key = ev_key

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        view = discord.ui.View()
        view.add_item(AddPriorityUserSelect(self.ev_key))
        await interaction.response.send_message("Select user to add to priority:", view=view, ephemeral=True)


class RemovePrioritySelect(discord.ui.Select):
    def __init__(self, ev_key: str):
        self.ev_key = ev_key
        event = data_store["events"][ev_key]

        options = [
            discord.SelectOption(label=f"{idx}. {user_id}", value=str(user_id))
            for idx, user_id in enumerate(event["priority_order"], start=1)
        ]

        super().__init__(
            placeholder="Select priority user to remove",
            options=options[:25] if options else [
                discord.SelectOption(label="No priority users", value="__none__")
            ],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message("No priority users.", ephemeral=True)
            return

        event = data_store["events"][self.ev_key]
        user_id = int(self.values[0])

        if user_id not in event["priority_order"]:
            await interaction.response.send_message("❌ User not found in priority list.", ephemeral=True)
            return

        event["priority_order"].remove(user_id)
        await save_data()
        await refresh_panel_by_event(event)
        await interaction.response.send_message(f"✅ Removed <@{user_id}> from priority.", ephemeral=True)


class RemovePriorityButton(discord.ui.Button):
    def __init__(self, ev_key: str):
        super().__init__(
            label="Remove Priority",
            style=discord.ButtonStyle.danger,
            custom_id=f"removepriority:{ev_key}",
            row=4
        )
        self.ev_key = ev_key

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        view = discord.ui.View()
        view.add_item(RemovePrioritySelect(self.ev_key))
        await interaction.response.send_message("Select priority user to remove:", view=view, ephemeral=True)


class PanelView(discord.ui.View):
    def __init__(self, ev_key: str):
        super().__init__(timeout=None)

        # For everyone
        self.add_item(ItemSelect(ev_key))
        self.add_item(RemoveMyItemButton(ev_key))

        # Page controls
        self.add_item(PrevPageButton(ev_key))
        self.add_item(PageInfoButton(ev_key))
        self.add_item(NextPageButton(ev_key))

        # Admin only
        self.add_item(AddItemButton(ev_key))
        self.add_item(RemoveItemButton(ev_key))
        self.add_item(EditCapButton(ev_key))
        self.add_item(AddPriorityButton(ev_key))
        self.add_item(RemovePriorityButton(ev_key))
        self.add_item(RemovePlayerButton(ev_key))


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

    await interaction.followup.send(f"✅ Event **{ev['name']}** created.", ephemeral=True)


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
        view=PanelView(make_key(ev["name"]))
    )

    ev["panel_channel_id"] = interaction.channel.id
    ev["panel_message_id"] = msg.id

    await save_data()
    await interaction.followup.send(f"✅ Panel created for **{ev['name']}**.", ephemeral=True)


@bot.tree.command(name="remove_event", description="Remove a dated event")
@app_commands.choices(event=PRESET_EVENT_CHOICES)
async def remove_event(
    interaction: discord.Interaction,
    event: app_commands.Choice[str],
    run_date: str
):
    await interaction.response.defer(ephemeral=True)

    ev_key = make_event_key(event.value, run_date)
    ev = data_store["events"].get(ev_key)

    if not ev:
        await interaction.followup.send("❌ Event not found.", ephemeral=True)
        return

    del data_store["events"][ev_key]
    await save_data()

    await interaction.followup.send(f"✅ Removed **{event.value} {run_date}**.", ephemeral=True)


@bot.tree.command(name="add_item", description="Add an item to the global item library")
async def add_item(
    interaction: discord.Interaction,
    item_name: str,
    cap: app_commands.Range[int, 1, 99]
):
    await interaction.response.defer(ephemeral=True)

    if item_exists_globally(item_name):
        await interaction.followup.send("❌ That item already exists in the global library.", ephemeral=True)
        return

    data_store["global_items"][item_name] = {
        "capacity": cap
    }

    await save_data()
    await interaction.followup.send(
        f"✅ Added **{item_name}** to global library with cap **{cap}**.",
        ephemeral=True
    )


@bot.tree.command(name="remove_global_item", description="Remove item from global library")
async def remove_global_item(
    interaction: discord.Interaction,
    item_name: str
):
    await interaction.response.defer(ephemeral=True)

    for existing_name in list(data_store["global_items"].keys()):
        if normalize_item_name(existing_name) == normalize_item_name(item_name):
            del data_store["global_items"][existing_name]
            await save_data()
            await interaction.followup.send(
                f"✅ Removed **{existing_name}** from global library.",
                ephemeral=True
            )
            return

    await interaction.followup.send("❌ Item not found in global library.", ephemeral=True)


@bot.tree.command(name="add_global_items_bulk", description="Add multiple items to the global library")
async def add_global_items_bulk(
    interaction: discord.Interaction,
    cap: app_commands.Range[int, 1, 99],
    item_names: str
):
    await interaction.response.defer(ephemeral=True)

    raw_items = split_bulk_item_input(item_names)

    if not raw_items:
        await interaction.followup.send("❌ No valid item names found.", ephemeral=True)
        return

    added = []
    skipped = []

    for item in raw_items:
        if item_exists_globally(item):
            skipped.append(item)
            continue

        data_store["global_items"][item] = {
            "capacity": cap
        }
        added.append(item)

    await save_data()

    parts = []
    if added:
        parts.append(
            f"✅ Added to global library with cap **{cap}**:\n" +
            "\n".join(f"• {x}" for x in added[:50])
        )
        if len(added) > 50:
            parts.append(f"…and {len(added) - 50} more added.")

    if skipped:
        parts.append(
            "⚠️ Skipped duplicates:\n" +
            "\n".join(f"• {x}" for x in skipped[:50])
        )
        if len(skipped) > 50:
            parts.append(f"…and {len(skipped) - 50} more skipped.")

    await interaction.followup.send("\n\n".join(parts), ephemeral=True)


@bot.tree.command(name="show_items", description="Show all global library items")
async def show_items(interaction: discord.Interaction):
    items = sorted(data_store["global_items"].keys(), key=str.lower)
    text = "\n".join(f"• {x}" for x in items[:100]) if items else "None"

    embed = discord.Embed(
        title="Global Item Library",
        description=text[:4000],
        color=discord.Color.green()
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


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
    for item_name, item_data in sorted(ev["items"].items(), key=lambda x: x[0].lower()):
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
    if not data_store["events"]:
        await interaction.response.send_message("No events yet.", ephemeral=True)
        return

    names = [f"• {ev['name']}" for ev in sorted(data_store["events"].values(), key=lambda x: x["name"].lower())]
    await interaction.response.send_message("\n".join(names[:100]), ephemeral=True)


@bot.tree.command(name="show_item_picks", description="Show all players who picked a specific item")
@app_commands.choices(event=PRESET_EVENT_CHOICES)
async def show_item_picks(
    interaction: discord.Interaction,
    event: app_commands.Choice[str],
    run_date: str,
    item_name: str
):
    await interaction.response.defer(ephemeral=True)

    ev = get_event(event.value, run_date)
    if not ev:
        await interaction.followup.send("❌ Event not found.", ephemeral=True)
        return

    actual_name = None
    for existing_name in ev["items"].keys():
        if normalize_item_name(existing_name) == normalize_item_name(item_name):
            actual_name = existing_name
            break

    if not actual_name:
        await interaction.followup.send("❌ Item not found in this event.", ephemeral=True)
        return

    item = ev["items"][actual_name]
    selections = item["selections"]

    if not selections:
        await interaction.followup.send(
            f"**{actual_name}** has no players yet.",
            ephemeral=True
        )
        return

    lines = []
    for i, entry in enumerate(selections, start=1):
        uid = entry["user_id"]
        rank = get_rank(ev, uid)
        priority_text = f" (Priority #{rank})" if rank != 999999 else ""
        lines.append(f"{i}. <@{uid}>{priority_text}")

    text = "\n".join(lines)

    if len(text) > 1900:
        chunks = []
        current = ""

        for line in lines:
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}".strip()

        if current:
            chunks.append(current)

        await interaction.followup.send(
            f"**{actual_name}** — players who picked this item:",
            ephemeral=True
        )

        for chunk in chunks:
            await interaction.followup.send(chunk, ephemeral=True)
        return

    await interaction.followup.send(
        f"**{actual_name}** — players who picked this item:\n\n{text}",
        ephemeral=True
    )


# =========================
# READY
# =========================
@bot.event
async def on_ready():
    await init_db()
    await load_data()

    for ev_key in data_store["events"].keys():
        bot.add_view(PanelView(ev_key))

    await bot.tree.sync()
    print("READY")


bot.run(TOKEN)
