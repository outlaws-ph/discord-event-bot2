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
    app_commands.Choice(name=name, value=name) for name in PRESET_EVENTS
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
def ensure_data_defaults(data: dict) -> dict:
    if not isinstance(data, dict):
        data = {}

    if "global_items" not in data or not isinstance(data["global_items"], dict):
        data["global_items"] = {
            "main": {"items": {}},
            "other": {"items": {}}
        }

    for cat in ["main", "other"]:
        if cat not in data["global_items"] or not isinstance(data["global_items"][cat], dict):
            data["global_items"][cat] = {"items": {}}
        if "items" not in data["global_items"][cat] or not isinstance(data["global_items"][cat]["items"], dict):
            data["global_items"][cat]["items"] = {}

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
            "categories": {
                "main": {"items": {}},
                "other": {"items": {}}
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
    for cat in ["main", "other"]:
        for existing_name in data_store["global_items"][cat]["items"].keys():
            if normalize_item_name(existing_name) == target:
                return True
    return False


def find_global_item(item_name: str):
    target = normalize_item_name(item_name)
    for cat in ["main", "other"]:
        for existing_name, item_data in data_store["global_items"][cat]["items"].items():
            if normalize_item_name(existing_name) == target:
                return cat, existing_name, item_data
    return None, None, None


def auto_assign_leftovers(event: dict):
    players = event["priority_order"]

    for cat in event["categories"].values():
        for item in cat["items"].values():
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


def build_category_text(event: dict, category_key: str) -> str:
    items = event["categories"][category_key]["items"]
    blocks = []

    for item_name in sorted(items.keys(), key=str.lower):
        item_data = items[item_name]
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
            "Select items below.\n"
            "Max per player: **Unlimited**\n"
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


def is_admin(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator


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
            view=PanelView(make_key(event["name"]))
        )
    except Exception as e:
        print(f"Failed to refresh panel: {e}")


# =========================
# PLAYER UI
# =========================
class ItemSelect(discord.ui.Select):
    def __init__(self, ev_key: str, category_key: str):
        self.ev_key = ev_key
        self.category_key = category_key

        event = data_store["events"][ev_key]
        items = event["categories"][category_key]["items"]

        options = [
            discord.SelectOption(
                label=item_name[:100],
                value=item_name,
                description=f"{len(item_data['selections'])}/{item_data['capacity']} reserved"
            )
            for item_name, item_data in sorted(items.items(), key=lambda x: x[0].lower())
        ]

        placeholder = "Choose a main item" if category_key == "main" else "Choose an other item"

        super().__init__(
            custom_id=f"pick:{ev_key}:{category_key}",
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

        selected_name = self.values[0]
        item = event["categories"][self.category_key]["items"][selected_name]

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
        for cat in ["main", "other"]:
            for item_name, item_data in sorted(event["categories"][cat]["items"].items(), key=lambda x: x[0].lower()):
                options.append(
                    discord.SelectOption(
                        label=item_name[:100],
                        value=f"{cat}|{item_name}",
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

        cat, item_name = self.values[0].split("|", 1)
        item = event["categories"][cat]["items"][item_name]

        before = len(item["selections"])
        item["selections"] = [x for x in item["selections"] if x["user_id"] != interaction.user.id]

        if len(item["selections"]) == before:
            await interaction.response.send_message("You did not select that item.", ephemeral=True)
            return

        await save_data()
        await refresh_panel_by_event(event)
        await interaction.response.send_message(f"🗑️ Removed **{item_name}**.", ephemeral=True)


# =========================
# ADMIN UI
# =========================
class EditCapItemSelect(discord.ui.Select):
    def __init__(self, ev_key: str):
        self.ev_key = ev_key
        event = data_store["events"][ev_key]

        options = []
        for cat in ["main", "other"]:
            for item_name, item_data in sorted(event["categories"][cat]["items"].items(), key=lambda x: x[0].lower()):
                options.append(
                    discord.SelectOption(
                        label=item_name[:100],
                        value=f"{cat}|{item_name}",
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

        cat, item_name = self.values[0].split("|", 1)
        await interaction.response.send_modal(EditCapModal(self.ev_key, cat, item_name))


class EditCapModal(discord.ui.Modal, title="Set New Cap"):
    new_cap = discord.ui.TextInput(label="New cap", required=True, max_length=3)

    def __init__(self, ev_key: str, category_key: str, item_name: str):
        super().__init__()
        self.ev_key = ev_key
        self.category_key = category_key
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
        item = event["categories"][self.category_key]["items"][self.item_name]
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
        super().__init__(label="✏️ Edit Cap", style=discord.ButtonStyle.primary, custom_id=f"editcap:{ev_key}")
        self.ev_key = ev_key

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        view = discord.ui.View()
        view.add_item(EditCapItemSelect(self.ev_key))
        await interaction.response.send_message("Select item to edit:", view=view, ephemeral=True)


class AddItemSingleSelect(discord.ui.Select):
    def __init__(self, ev_key: str, cap: int):
        self.ev_key = ev_key
        self.cap = cap

        options = []
        for cat in ["main", "other"]:
            for item_name in sorted(data_store["global_items"][cat]["items"].keys(), key=str.lower):
                options.append(discord.SelectOption(label=item_name[:100], value=item_name))

        super().__init__(
            placeholder=f"Select one item (cap = {cap})",
            options=options[:25] if options else [
                discord.SelectOption(label="No global items available", value="__none__")
            ],
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message("No global items available.", ephemeral=True)
            return

        event = data_store["events"][self.ev_key]
        cat, name, _ = find_global_item(self.values[0])

        if not name:
            await interaction.response.send_message("❌ Item not found.", ephemeral=True)
            return

        if name in event["categories"][cat]["items"]:
            await interaction.response.send_message("❌ Item already added to this event.", ephemeral=True)
            return

        event["categories"][cat]["items"][name] = {
            "capacity": self.cap,
            "selections": []
        }

        await save_data()
        await refresh_panel_by_event(event)
        await interaction.response.send_message(
            f"✅ Added **{name}** with cap **{self.cap}**.",
            ephemeral=True
        )


class AddItemBulkSelect(discord.ui.Select):
    def __init__(self, ev_key: str, cap: int):
        self.ev_key = ev_key
        self.cap = cap

        options = []
        for cat in ["main", "other"]:
            for item_name in sorted(data_store["global_items"][cat]["items"].keys(), key=str.lower):
                options.append(discord.SelectOption(label=item_name[:100], value=item_name))

        super().__init__(
            placeholder=f"Select multiple items (cap = {cap})",
            options=options[:25] if options else [
                discord.SelectOption(label="No global items available", value="__none__")
            ],
            min_values=1,
            max_values=min(25, len(options)) if options else 1
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message("No global items available.", ephemeral=True)
            return

        event = data_store["events"][self.ev_key]
        added = []

        for selected in self.values:
            cat, name, _ = find_global_item(selected)
            if not name:
                continue
            if name in event["categories"][cat]["items"]:
                continue

            event["categories"][cat]["items"][name] = {
                "capacity": self.cap,
                "selections": []
            }
            added.append(name)

        await save_data()
        await refresh_panel_by_event(event)

        if added:
            await interaction.response.send_message(
                f"✅ Added with cap **{self.cap}**: {', '.join(added)}",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "⚠️ All selected items were already added to this event.",
                ephemeral=True
            )


class AddItemModeView(discord.ui.View):
    def __init__(self, ev_key: str):
        super().__init__(timeout=300)
        self.ev_key = ev_key

    @discord.ui.button(label="Single Add", style=discord.ButtonStyle.primary)
    async def single_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddItemSingleCapModal(self.ev_key))

    @discord.ui.button(label="Bulk Add", style=discord.ButtonStyle.secondary)
    async def bulk_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddItemBulkCapModal(self.ev_key))


class AddItemSingleCapModal(discord.ui.Modal, title="Add One Item"):
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

        view = discord.ui.View()
        view.add_item(AddItemSingleSelect(self.ev_key, cap))
        await interaction.response.send_message("Select item to add:", view=view, ephemeral=True)


class AddItemBulkCapModal(discord.ui.Modal, title="Bulk Add Items"):
    cap_input = discord.ui.TextInput(label="Cap for all selected items", required=True, max_length=3)

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

        view = discord.ui.View()
        view.add_item(AddItemBulkSelect(self.ev_key, cap))
        await interaction.response.send_message("Select items to add:", view=view, ephemeral=True)


class AddItemButton(discord.ui.Button):
    def __init__(self, ev_key: str):
        super().__init__(label="➕ Add Item", style=discord.ButtonStyle.success, custom_id=f"additem:{ev_key}")
        self.ev_key = ev_key

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        has_global_items = any(
            data_store["global_items"][cat]["items"]
            for cat in ["main", "other"]
        )
        if not has_global_items:
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
        for cat in ["main", "other"]:
            for item_name, item_data in sorted(event["categories"][cat]["items"].items(), key=lambda x: x[0].lower()):
                options.append(
                    discord.SelectOption(
                        label=item_name[:100],
                        value=f"{cat}|{item_name}",
                        description=f"{len(item_data['selections'])}/{item_data['capacity']} reserved"
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
        cat, item_name = self.values[0].split("|", 1)

        if item_name not in event["categories"][cat]["items"]:
            await interaction.response.send_message("❌ Item not found.", ephemeral=True)
            return

        del event["categories"][cat]["items"][item_name]

        await save_data()
        await refresh_panel_by_event(event)
        await interaction.response.send_message(
            f"✅ Removed **{item_name}** from this panel.",
            ephemeral=True
        )


class RemoveItemButton(discord.ui.Button):
    def __init__(self, ev_key: str):
        super().__init__(label="🗑️ Remove Item", style=discord.ButtonStyle.danger, custom_id=f"removeitem:{ev_key}")
        self.ev_key = ev_key

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return

        view = discord.ui.View()
        view.add_item(RemoveItemFromPanelSelect(self.ev_key))
        await interaction.response.send_message("Select item to remove:", view=view, ephemeral=True)


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
        super().__init__(label="👑 Add Priority", style=discord.ButtonStyle.primary, custom_id=f"addpriority:{ev_key}")
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
        super().__init__(label="➖ Remove Priority", style=discord.ButtonStyle.danger, custom_id=f"removepriority:{ev_key}")
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
        self.add_item(ItemSelect(ev_key, "main"))
        self.add_item(ItemSelect(ev_key, "other"))
        self.add_item(RemoveSelect(ev_key))
        self.add_item(AddItemButton(ev_key))
        self.add_item(EditCapButton(ev_key))
        self.add_item(RemoveItemButton(ev_key))
        self.add_item(AddPriorityButton(ev_key))
        self.add_item(RemovePriorityButton(ev_key))


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
@app_commands.choices(category=CATEGORY_CHOICES)
async def add_item(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    item_name: str,
    cap: app_commands.Range[int, 1, 99]
):
    await interaction.response.defer(ephemeral=True)

    if item_exists_globally(item_name):
        await interaction.followup.send("❌ That item already exists in the global library.", ephemeral=True)
        return

    data_store["global_items"][category.value]["items"][item_name] = {
        "capacity": cap
    }

    await save_data()
    await interaction.followup.send(
        f"✅ Added **{item_name}** to global **{category.name}** with cap **{cap}**.",
        ephemeral=True
    )


@bot.tree.command(name="remove_global_item", description="Remove item from global library")
async def remove_global_item(
    interaction: discord.Interaction,
    item_name: str
):
    await interaction.response.defer(ephemeral=True)

    for cat in ["main", "other"]:
        for existing_name in list(data_store["global_items"][cat]["items"].keys()):
            if normalize_item_name(existing_name) == normalize_item_name(item_name):
                del data_store["global_items"][cat]["items"][existing_name]
                await save_data()
                await interaction.followup.send(
                    f"✅ Removed **{existing_name}** from global library.",
                    ephemeral=True
                )
                return

    await interaction.followup.send("❌ Item not found in global library.", ephemeral=True)


@bot.tree.command(name="add_global_items_bulk", description="Add multiple items to the global library")
@app_commands.choices(category=CATEGORY_CHOICES)
async def add_global_items_bulk(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    cap: app_commands.Range[int, 1, 99],
    item_names: str
):
    await interaction.response.defer(ephemeral=True)

    raw_items = [x.strip() for x in item_names.split(",")]
    cleaned_items = []
    seen = set()

    for item in raw_items:
        if not item:
            continue
        norm = normalize_item_name(item)
        if norm in seen:
            continue
        seen.add(norm)
        cleaned_items.append(item)

    if not cleaned_items:
        await interaction.followup.send("❌ No valid item names found.", ephemeral=True)
        return

    added = []
    skipped = []

    for item in cleaned_items:
        if item_exists_globally(item):
            skipped.append(item)
            continue

        data_store["global_items"][category.value]["items"][item] = {
            "capacity": cap
        }
        added.append(item)

    await save_data()

    parts = []
    if added:
        parts.append(f"✅ Added to global **{category.name}** with cap **{cap}**:\n" + ", ".join(added))
    if skipped:
        parts.append("⚠️ Skipped duplicates:\n" + ", ".join(skipped))

    await interaction.followup.send("\n\n".join(parts), ephemeral=True)


@bot.tree.command(name="show_items", description="Show all global library items")
async def show_items(interaction: discord.Interaction):
    main_items = sorted(data_store["global_items"]["main"]["items"].keys(), key=str.lower)
    other_items = sorted(data_store["global_items"]["other"]["items"].keys(), key=str.lower)

    main_text = "\n".join(f"• {x}" for x in main_items[:50]) if main_items else "None"
    other_text = "\n".join(f"• {x}" for x in other_items[:50]) if other_items else "None"

    embed = discord.Embed(
        title="Global Item Library",
        color=discord.Color.green()
    )
    embed.add_field(name="Main Items", value=main_text[:1024], inline=False)
    embed.add_field(name="Other Items", value=other_text[:1024], inline=False)

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
    for cat in ["main", "other"]:
        for item_name, item_data in sorted(ev["categories"][cat]["items"].items(), key=lambda x: x[0].lower()):
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
