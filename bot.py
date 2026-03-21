import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
from datetime import datetime, timezone

TOKEN = os.getenv("TOKEN")
GUILD_ID_RAW = os.getenv("GUILD_ID")
GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW and GUILD_ID_RAW.strip() else 0

DATA_FILE = "item_distribution_data.json"

DEFAULT_MAIN_ITEMS = [
    "Rune Bracelet",
    "Rune Gloves",
    "Steel Spear",
    "Steel Helmet",
    "Grim Helmet",
    "Grim Lyra",
    "Grim Spear",
    "Grim Gloves",
    "Grim Necklace",
    "Storm Chain"
]

DEFAULT_OTHER_ITEMS = [
    "Old Silver Coin",
    "Gold Key",
    "Soul (UC)",
    "Soul (Rare)",
    "Soul (Epic)"
]

NOTICE_TEXT = (
    "Priority players have reserved access. If a higher-priority player selects an item "
    "that’s already full, the lowest-priority current selection will be removed. "
    "Affected players may choose another item."
)

DIVIDER = "────────────────────"

DEFAULT_DATA = {
    "global_priority_order": [],
    "events": {}
}

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def load_data():
    if not os.path.exists(DATA_FILE):
        save_data(DEFAULT_DATA)
        return json.loads(json.dumps(DEFAULT_DATA))
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "global_priority_order" not in data:
        data["global_priority_order"] = []
    if "events" not in data:
        data["events"] = {}

    return data


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


data_store = load_data()


def now_ts():
    return datetime.now(timezone.utc).timestamp()


def normalize_event_key(name: str) -> str:
    return name.strip().lower()


def make_default_event(event_name: str):
    return {
        "name": event_name,
        "priority_mode": "event",  # event | global
        "priority_order": [],
        "panel_channel_id": None,
        "panel_message_id": None,
        "is_locked": False,
        "lock_time": None,
        "reset_time": None,
        "winners_sent": False,
        "categories": {
            "main_items": {
                "label": "Main Items",
                "items": {
                    item: {"capacity": 1, "selections": []}
                    for item in DEFAULT_MAIN_ITEMS
                }
            },
            "other_items": {
                "label": "Other Items",
                "items": {
                    item: {"capacity": 1, "selections": []}
                    for item in DEFAULT_OTHER_ITEMS
                }
            }
        }
    }


def get_event(event_name: str):
    return data_store["events"].get(normalize_event_key(event_name))


def ensure_event(event_name: str):
    key = normalize_event_key(event_name)
    if key not in data_store["events"]:
        data_store["events"][key] = make_default_event(event_name)
    return data_store["events"][key]


def get_priority_order_for_event(event_data: dict):
    if event_data.get("priority_mode", "event") == "global":
        return data_store.setdefault("global_priority_order", [])
    return event_data.setdefault("priority_order", [])


def get_priority_rank(event_data: dict, user_id: int) -> int:
    priority_order = get_priority_order_for_event(event_data)
    try:
        return priority_order.index(user_id) + 1
    except ValueError:
        return 999999


def format_user_mention(user_id: int) -> str:
    return f"<@{user_id}>"


def format_rank_badge(rank: int) -> str:
    if rank == 999999:
        return ""
    return f"`#{rank}` "


def find_user_selection_in_category(event_data: dict, category_key: str, user_id: int):
    items = event_data["categories"][category_key]["items"]
    for item_name, item_data in items.items():
        for entry in item_data["selections"]:
            if entry["user_id"] == user_id:
                return item_name
    return None


def remove_user_from_category(event_data: dict, category_key: str, user_id: int):
    items = event_data["categories"][category_key]["items"]
    removed_from = None
    for item_name, item_data in items.items():
        before = len(item_data["selections"])
        item_data["selections"] = [
            entry for entry in item_data["selections"] if entry["user_id"] != user_id
        ]
        if len(item_data["selections"]) != before:
            removed_from = item_name
    return removed_from


def get_item_data(event_data: dict, category_key: str, item_name: str):
    return event_data["categories"][category_key]["items"].get(item_name)


def is_event_locked(event_data: dict) -> bool:
    return event_data.get("is_locked", False)


def build_priority_block(event_data: dict) -> str:
    priority_order = get_priority_order_for_event(event_data)
    mode = event_data.get("priority_mode", "event").upper()

    lines = [
        f"## Priority Order ({mode})",
        DIVIDER
    ]

    if not priority_order:
        lines.append("No priority players set.")
        return "\n".join(lines)

    for idx, user_id in enumerate(priority_order, start=1):
        lines.append(f"{idx}. {format_user_mention(user_id)}")

    return "\n".join(lines)


def build_category_block(event_data: dict, category_key: str) -> str:
    category_data = event_data["categories"][category_key]
    label = category_data["label"]
    items = category_data["items"]

    lines = [
        f"## {label}",
        DIVIDER
    ]

    for item_name, item_data in items.items():
        capacity = item_data["capacity"]
        selections = sorted(
            item_data["selections"],
            key=lambda x: (get_priority_rank(event_data, x["user_id"]), x["selected_at"])
        )

        lines.append(f"**{item_name}** — `{len(selections)}/{capacity}`")

        if selections:
            for idx, entry in enumerate(selections, start=1):
                rank = get_priority_rank(event_data, entry["user_id"])
                lines.append(f"↳ {idx}. {format_rank_badge(rank)}{format_user_mention(entry['user_id'])}")
        else:
            lines.append("↳ *No reservation yet*")

        lines.append("")

    return "\n".join(lines)


def build_panel_embed(event_data: dict) -> discord.Embed:
    title = f"🎁 {event_data['name']} Distribution Panel"
    if is_event_locked(event_data):
        title += " 🔒"

    embed = discord.Embed(
        title=title,
        description=(
            f"**Notice**\n"
            f"{NOTICE_TEXT}\n\n"
            f"{build_priority_block(event_data)}\n\n"
            f"{DIVIDER}\n\n"
            f"{build_category_block(event_data, 'main_items')}\n"
            f"{DIVIDER}\n\n"
            f"{build_category_block(event_data, 'other_items')}"
        ),
        color=discord.Color.blurple()
    )

    lock_text = "Locked" if event_data.get("is_locked") else "Open"
    embed.set_footer(text=f"Status: {lock_text} | Lower rank number = higher priority")
    return embed


def build_winner_message(event_data: dict) -> str:
    lines = [
        f"🏆 **{event_data['name']} Distribution Winners**",
        ""
    ]

    found_any = False

    for category_key in ["main_items", "other_items"]:
        category = event_data["categories"][category_key]
        lines.append(f"**{category['label']}**")

        for item_name, item_data in category["items"].items():
            selections = sorted(
                item_data["selections"],
                key=lambda x: (get_priority_rank(event_data, x["user_id"]), x["selected_at"])
            )
            if selections:
                found_any = True
                winners = ", ".join(format_user_mention(x["user_id"]) for x in selections)
                lines.append(f"• **{item_name}**: {winners}")

        lines.append("")

    if not found_any:
        lines.append("No winners recorded.")

    return "\n".join(lines)


class ItemSelect(discord.ui.Select):
    def __init__(self, event_key: str, category_key: str):
        self.event_key = event_key
        self.category_key = category_key
        event_data = data_store["events"][event_key]
        category_data = event_data["categories"][category_key]

        options = []
        for item_name, item_data in category_data["items"].items():
            options.append(
                discord.SelectOption(
                    label=item_name[:100],
                    description=f"{len(item_data['selections'])}/{item_data['capacity']} reserved",
                    value=item_name
                )
            )

        super().__init__(
            placeholder=f"{event_data['name']} • {category_data['label']}",
            min_values=1,
            max_values=1,
            options=options[:25],
            custom_id=f"select:{event_key}:{category_key}"
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
            return

        event_data = data_store["events"].get(self.event_key)
        if not event_data:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return

        if is_event_locked(event_data):
            await interaction.response.send_message("❌ This event panel is locked.", ephemeral=True)
            return

        selected_item_name = self.values[0]
        selected_item = get_item_data(event_data, self.category_key, selected_item_name)

        if not selected_item:
            await interaction.response.send_message("That item no longer exists.", ephemeral=True)
            return

        previous_item = find_user_selection_in_category(event_data, self.category_key, interaction.user.id)
        if previous_item == selected_item_name:
            await interaction.response.send_message(
                f"You already selected **{selected_item_name}**.",
                ephemeral=True
            )
            return

        if previous_item:
            remove_user_from_category(event_data, self.category_key, interaction.user.id)

        selections = selected_item["selections"]
        capacity = selected_item["capacity"]
        removed_user_id = None
        member_rank = get_priority_rank(event_data, interaction.user.id)

        if len(selections) >= capacity:
            lowest_priority_entry = max(
                selections,
                key=lambda x: (get_priority_rank(event_data, x["user_id"]), x["selected_at"])
            )
            lowest_rank = get_priority_rank(event_data, lowest_priority_entry["user_id"])

            if member_rank < lowest_rank:
                removed_user_id = lowest_priority_entry["user_id"]
                selections.remove(lowest_priority_entry)
            else:
                if previous_item:
                    old_item = get_item_data(event_data, self.category_key, previous_item)
                    if old_item and len(old_item["selections"]) < old_item["capacity"]:
                        old_item["selections"].append({
                            "user_id": interaction.user.id,
                            "selected_at": now_ts()
                        })

                save_data(data_store)
                await interaction.response.send_message(
                    f"**{selected_item_name}** is already full, and the current holder(s) have equal or higher priority than you.",
                    ephemeral=True
                )
                return

        selected_item["selections"].append({
            "user_id": interaction.user.id,
            "selected_at": now_ts()
        })

        save_data(data_store)
        await refresh_event_panel(interaction.guild, self.event_key)

        msg = f"✅ You selected **{selected_item_name}** for **{event_data['name']}**."
        if previous_item:
            msg += f"\nYour previous choice **{previous_item}** was removed."
        if removed_user_id:
            msg += f"\nPriority override applied. Removed: <@{removed_user_id}>."

        await interaction.response.send_message(msg, ephemeral=True)


class RemoveSelectionButton(discord.ui.Button):
    def __init__(self, event_key: str, category_key: str, label: str):
        self.event_key = event_key
        self.category_key = category_key
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=f"remove:{event_key}:{category_key}"
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        event_data = data_store["events"].get(self.event_key)
        if not event_data:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return

        if is_event_locked(event_data):
            await interaction.response.send_message("❌ This event panel is locked.", ephemeral=True)
            return

        removed = remove_user_from_category(event_data, self.category_key, interaction.user.id)
        if not removed:
            await interaction.response.send_message(
                "You do not have a selection in this category.",
                ephemeral=True
            )
            return

        save_data(data_store)
        await refresh_event_panel(interaction.guild, self.event_key)

        await interaction.response.send_message(
            f"🗑️ Removed your selection from **{removed}** in **{event_data['name']}**.",
            ephemeral=True
        )


class EventPanelView(discord.ui.View):
    def __init__(self, event_key: str):
        super().__init__(timeout=None)
        self.add_item(ItemSelect(event_key, "main_items"))
        self.add_item(RemoveSelectionButton(event_key, "main_items", "Remove Main Item"))
        self.add_item(ItemSelect(event_key, "other_items"))
        self.add_item(RemoveSelectionButton(event_key, "other_items", "Remove Other Item"))


async def get_event_panel_message(guild: discord.Guild, event_key: str):
    event_data = data_store["events"].get(event_key)
    if not event_data:
        return None

    channel_id = event_data.get("panel_channel_id")
    message_id = event_data.get("panel_message_id")

    if not channel_id or not message_id:
        return None

    channel = guild.get_channel(channel_id)
    if not channel:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            return None

    try:
        return await channel.fetch_message(message_id)
    except Exception:
        return None


async def refresh_event_panel(guild: discord.Guild, event_key: str):
    event_data = data_store["events"].get(event_key)
    if not event_data:
        return

    message = await get_event_panel_message(guild, event_key)
    if not message:
        return

    try:
        await message.edit(embed=build_panel_embed(event_data), view=EventPanelView(event_key))
    except Exception as e:
        print(f"Failed to refresh event panel {event_key}: {e}")


async def announce_winners_if_needed(guild: discord.Guild, event_key: str):
    event_data = data_store["events"].get(event_key)
    if not event_data or event_data.get("winners_sent"):
        return

    channel_id = event_data.get("panel_channel_id")
    if not channel_id:
        return

    channel = guild.get_channel(channel_id)
    if not channel:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            return

    try:
        await channel.send(build_winner_message(event_data))
        event_data["winners_sent"] = True
        save_data(data_store)
    except Exception as e:
        print(f"Failed to send winner message for {event_key}: {e}")


@tasks.loop(seconds=10)
async def event_scheduler():
    for guild in bot.guilds:
        updated_any = False

        for event_key, event_data in list(data_store["events"].items()):
            now = now_ts()

            lock_time = event_data.get("lock_time")
            if lock_time and now >= lock_time and not event_data.get("is_locked"):
                event_data["is_locked"] = True
                event_data["lock_time"] = None
                updated_any = True
                await refresh_event_panel(guild, event_key)
                await announce_winners_if_needed(guild, event_key)

            reset_time = event_data.get("reset_time")
            if reset_time and now >= reset_time:
                for category in event_data["categories"].values():
                    for item_data in category["items"].values():
                        item_data["selections"] = []

                event_data["reset_time"] = None
                event_data["is_locked"] = False
                event_data["winners_sent"] = False
                updated_any = True
                await refresh_event_panel(guild, event_key)

        if updated_any:
            save_data(data_store)


@event_scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    for event_key in data_store["events"].keys():
        bot.add_view(EventPanelView(event_key))

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} guild commands.")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} global commands.")
    except Exception as e:
        print(f"Sync failed: {e}")

    if not event_scheduler.is_running():
        event_scheduler.start()

    print(f"Logged in as {bot.user}")


def admin_only():
    async def predicate(interaction: discord.Interaction):
        return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)


@app_commands.choices(mode=[
    app_commands.Choice(name="Event", value="event"),
    app_commands.Choice(name="Global", value="global")
])
@bot.tree.command(name="create_event_panel", description="Create a distribution panel for an event.")
@admin_only()
async def create_event_panel(
    interaction: discord.Interaction,
    event_name: str,
    mode: app_commands.Choice[str] | None = None
):
    if not interaction.guild:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    event_data = ensure_event(event_name)

    if mode:
        event_data["priority_mode"] = mode.value

    await interaction.response.send_message(f"Creating panel for **{event_data['name']}**...", ephemeral=True)
    message = await interaction.channel.send(
        embed=build_panel_embed(event_data),
        view=EventPanelView(normalize_event_key(event_data["name"]))
    )

    event_data["panel_channel_id"] = interaction.channel.id
    event_data["panel_message_id"] = message.id
    save_data(data_store)


@bot.tree.command(name="delete_event", description="Delete an event and its saved data.")
@admin_only()
async def delete_event(interaction: discord.Interaction, event_name: str):
    event_key = normalize_event_key(event_name)
    event_data = data_store["events"].get(event_key)

    if not event_data:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    del data_store["events"][event_key]
    save_data(data_store)

    await interaction.response.send_message(f"Deleted event **{event_name}**.", ephemeral=True)


@app_commands.choices(mode=[
    app_commands.Choice(name="Event", value="event"),
    app_commands.Choice(name="Global", value="global")
])
@bot.tree.command(name="set_priority_mode", description="Set priority mode for an event.")
@admin_only()
async def set_priority_mode(interaction: discord.Interaction, event_name: str, mode: app_commands.Choice[str]):
    event_data = get_event(event_name)
    if not event_data:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    event_data["priority_mode"] = mode.value
    save_data(data_store)

    if interaction.guild:
        await refresh_event_panel(interaction.guild, normalize_event_key(event_name))

    await interaction.response.send_message(
        f"Priority mode for **{event_data['name']}** set to **{mode.value}**.",
        ephemeral=True
    )


@app_commands.choices(category=[
    app_commands.Choice(name="Main Items", value="main_items"),
    app_commands.Choice(name="Other Items", value="other_items")
])
@bot.tree.command(name="add_item", description="Add an item to an event.")
@admin_only()
async def add_item(
    interaction: discord.Interaction,
    event_name: str,
    category: app_commands.Choice[str],
    item_name: str,
    capacity: app_commands.Range[int, 1, 99] = 1
):
    event_data = get_event(event_name)
    if not event_data:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    items = event_data["categories"][category.value]["items"]

    if item_name in items:
        await interaction.response.send_message("Item already exists.", ephemeral=True)
        return

    if len(items) >= 25:
        await interaction.response.send_message("This dropdown already has 25 items.", ephemeral=True)
        return

    items[item_name] = {"capacity": capacity, "selections": []}
    save_data(data_store)

    if interaction.guild:
        await refresh_event_panel(interaction.guild, normalize_event_key(event_name))

    await interaction.response.send_message(
        f"Added **{item_name}** to **{event_data['name']}** ({category.name}) with cap **{capacity}**.",
        ephemeral=True
    )


@app_commands.choices(category=[
    app_commands.Choice(name="Main Items", value="main_items"),
    app_commands.Choice(name="Other Items", value="other_items")
])
@bot.tree.command(name="remove_item", description="Remove an item from an event.")
@admin_only()
async def remove_item(interaction: discord.Interaction, event_name: str, category: app_commands.Choice[str], item_name: str):
    event_data = get_event(event_name)
    if not event_data:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    items = event_data["categories"][category.value]["items"]
    if item_name not in items:
        await interaction.response.send_message("Item not found.", ephemeral=True)
        return

    del items[item_name]
    save_data(data_store)

    if interaction.guild:
        await refresh_event_panel(interaction.guild, normalize_event_key(event_name))

    await interaction.response.send_message(
        f"Removed **{item_name}** from **{event_data['name']}**.",
        ephemeral=True
    )


@app_commands.choices(category=[
    app_commands.Choice(name="Main Items", value="main_items"),
    app_commands.Choice(name="Other Items", value="other_items")
])
@bot.tree.command(name="set_item_cap", description="Set cap for an event item.")
@admin_only()
async def set_item_cap(
    interaction: discord.Interaction,
    event_name: str,
    category: app_commands.Choice[str],
    item_name: str,
    capacity: app_commands.Range[int, 1, 99]
):
    event_data = get_event(event_name)
    if not event_data:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    items = event_data["categories"][category.value]["items"]
    if item_name not in items:
        await interaction.response.send_message("Item not found.", ephemeral=True)
        return

    items[item_name]["capacity"] = capacity
    save_data(data_store)

    if interaction.guild:
        await refresh_event_panel(interaction.guild, normalize_event_key(event_name))

    await interaction.response.send_message(
        f"Set **{item_name}** cap to **{capacity}** for **{event_data['name']}**.",
        ephemeral=True
    )


@bot.tree.command(name="add_priority_player", description="Add a player to an event or global priority list.")
@admin_only()
async def add_priority_player(
    interaction: discord.Interaction,
    user: discord.Member,
    event_name: str | None = None,
    position: app_commands.Range[int, 1, 100] | None = None
):
    if event_name:
        event_data = get_event(event_name)
        if not event_data:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        target_list = get_priority_order_for_event(event_data)
        label = event_data["name"]
    else:
        target_list = data_store["global_priority_order"]
        label = "Global"

    if user.id in target_list:
        await interaction.response.send_message(f"{user.mention} is already in the priority list.", ephemeral=True)
        return

    if position is None or position > len(target_list) + 1:
        target_list.append(user.id)
        actual_pos = len(target_list)
    else:
        target_list.insert(position - 1, user.id)
        actual_pos = position

    save_data(data_store)

    if interaction.guild and event_name:
        await refresh_event_panel(interaction.guild, normalize_event_key(event_name))

    await interaction.response.send_message(
        f"Added {user.mention} to **{label}** priority position **{actual_pos}**.",
        ephemeral=True
    )


@bot.tree.command(name="remove_priority_player", description="Remove a player from an event or global priority list.")
@admin_only()
async def remove_priority_player(
    interaction: discord.Interaction,
    user: discord.Member,
    event_name: str | None = None
):
    if event_name:
        event_data = get_event(event_name)
        if not event_data:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        target_list = get_priority_order_for_event(event_data)
        label = event_data["name"]
    else:
        target_list = data_store["global_priority_order"]
        label = "Global"

    if user.id not in target_list:
        await interaction.response.send_message(f"{user.mention} is not in the priority list.", ephemeral=True)
        return

    old_pos = target_list.index(user.id) + 1
    target_list.remove(user.id)
    save_data(data_store)

    if interaction.guild and event_name:
        await refresh_event_panel(interaction.guild, normalize_event_key(event_name))

    await interaction.response.send_message(
        f"Removed {user.mention} from **{label}** priority position **{old_pos}**.",
        ephemeral=True
    )


@bot.tree.command(name="move_priority_player", description="Move a player in an event or global priority list.")
@admin_only()
async def move_priority_player(
    interaction: discord.Interaction,
    user: discord.Member,
    position: app_commands.Range[int, 1, 100],
    event_name: str | None = None
):
    if event_name:
        event_data = get_event(event_name)
        if not event_data:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        target_list = get_priority_order_for_event(event_data)
        label = event_data["name"]
    else:
        target_list = data_store["global_priority_order"]
        label = "Global"

    if user.id not in target_list:
        await interaction.response.send_message(f"{user.mention} is not in the priority list.", ephemeral=True)
        return

    target_list.remove(user.id)
    if position > len(target_list) + 1:
        position = len(target_list) + 1
    target_list.insert(position - 1, user.id)

    save_data(data_store)

    if interaction.guild and event_name:
        await refresh_event_panel(interaction.guild, normalize_event_key(event_name))

    await interaction.response.send_message(
        f"Moved {user.mention} to **{label}** priority position **{position}**.",
        ephemeral=True
    )


@bot.tree.command(name="show_priority_list", description="Show event or global priority list.")
async def show_priority_list(interaction: discord.Interaction, event_name: str | None = None):
    if event_name:
        event_data = get_event(event_name)
        if not event_data:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        target_list = get_priority_order_for_event(event_data)
        label = event_data["name"]
    else:
        target_list = data_store["global_priority_order"]
        label = "Global"

    if not target_list:
        await interaction.response.send_message("No priority players set.", ephemeral=True)
        return

    lines = [f"**{label} Priority Order**"]
    for idx, user_id in enumerate(target_list, start=1):
        lines.append(f"{idx}. <@{user_id}>")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="lock_panel_in", description="Lock an event panel after X minutes and announce winners.")
@admin_only()
async def lock_panel_in(interaction: discord.Interaction, event_name: str, minutes: app_commands.Range[int, 1, 10080]):
    event_data = get_event(event_name)
    if not event_data:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    event_data["lock_time"] = now_ts() + (minutes * 60)
    event_data["is_locked"] = False
    event_data["winners_sent"] = False
    save_data(data_store)

    await interaction.response.send_message(
        f"🔒 **{event_data['name']}** will lock in **{minutes}** minute(s).",
        ephemeral=True
    )


@bot.tree.command(name="unlock_panel", description="Unlock an event panel.")
@admin_only()
async def unlock_panel(interaction: discord.Interaction, event_name: str):
    event_data = get_event(event_name)
    if not event_data:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    event_data["is_locked"] = False
    event_data["lock_time"] = None
    event_data["winners_sent"] = False
    save_data(data_store)

    if interaction.guild:
        await refresh_event_panel(interaction.guild, normalize_event_key(event_name))

    await interaction.response.send_message(
        f"🔓 **{event_data['name']}** unlocked.",
        ephemeral=True
    )


@bot.tree.command(name="reset_panel_in", description="Reset an event panel after X minutes.")
@admin_only()
async def reset_panel_in(interaction: discord.Interaction, event_name: str, minutes: app_commands.Range[int, 1, 10080]):
    event_data = get_event(event_name)
    if not event_data:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    event_data["reset_time"] = now_ts() + (minutes * 60)
    save_data(data_store)

    await interaction.response.send_message(
        f"🔄 **{event_data['name']}** will reset in **{minutes}** minute(s).",
        ephemeral=True
    )


@app_commands.choices(category=[
    app_commands.Choice(name="Main Items", value="main_items"),
    app_commands.Choice(name="Other Items", value="other_items")
])
@bot.tree.command(name="remove_my_choice", description="Remove your reservation from an event category.")
async def remove_my_choice(interaction: discord.Interaction, event_name: str, category: app_commands.Choice[str]):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    event_data = get_event(event_name)
    if not event_data:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    if is_event_locked(event_data):
        await interaction.response.send_message("❌ This event panel is locked.", ephemeral=True)
        return

    removed = remove_user_from_category(event_data, category.value, interaction.user.id)
    if not removed:
        await interaction.response.send_message("You have no reservation in that category.", ephemeral=True)
        return

    save_data(data_store)
    await refresh_event_panel(interaction.guild, normalize_event_key(event_name))

    await interaction.response.send_message(
        f"Removed your reservation from **{removed}** in **{event_data['name']}**.",
        ephemeral=True
    )


@bot.tree.command(name="refresh_event_panel", description="Refresh one event panel.")
@admin_only()
async def refresh_event_panel_command(interaction: discord.Interaction, event_name: str):
    event_data = get_event(event_name)
    if not event_data:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    if interaction.guild:
        await refresh_event_panel(interaction.guild, normalize_event_key(event_name))

    await interaction.response.send_message(
        f"Refreshed **{event_data['name']}** panel.",
        ephemeral=True
    )


@bot.tree.command(name="announce_winners", description="Announce winners now for an event.")
@admin_only()
async def announce_winners(interaction: discord.Interaction, event_name: str):
    event_data = get_event(event_name)
    if not event_data:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    event_data["winners_sent"] = False
    save_data(data_store)
    await announce_winners_if_needed(interaction.guild, normalize_event_key(event_name))

    await interaction.response.send_message(
        f"Winner announcement sent for **{event_data['name']}**.",
        ephemeral=True
    )


@bot.tree.command(name="list_events", description="Show all created events.")
async def list_events(interaction: discord.Interaction):
    if not data_store["events"]:
        await interaction.response.send_message("No events created yet.", ephemeral=True)
        return

    lines = ["**Events**"]
    for event_data in data_store["events"].values():
        lines.append(f"• {event_data['name']}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


if TOKEN:
    bot.run(TOKEN)
else:
    print("TOKEN environment variable is missing.")
