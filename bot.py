import discord
from discord.ext import commands
from discord import app_commands
import json
import os
from datetime import datetime, timezone

TOKEN = os.getenv("TOKEN")
GUILD_ID_RAW = os.getenv("GUILD_ID")
GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW and GUILD_ID_RAW.strip() else 0

DATA_FILE = "item_distribution_data.json"

DEFAULT_DATA = {
    "priority_role_id": None,
    "panel_channel_id": None,
    "panel_message_id": None,
    "categories": {
        "main_items": {
            "label": "Main Items",
            "items": {
                "Rune Bracelet": {"capacity": 1, "selections": []},
                "Rune Gloves": {"capacity": 1, "selections": []},
                "Steel Spear": {"capacity": 1, "selections": []},
                "Steel Helmet": {"capacity": 1, "selections": []},
                "Grim Helmet": {"capacity": 1, "selections": []},
                "Grim Lyra": {"capacity": 1, "selections": []},
                "Grim Spear": {"capacity": 1, "selections": []},
                "Grim Gloves": {"capacity": 1, "selections": []},
                "Grim Necklace": {"capacity": 1, "selections": []},
                "Storm Chain": {"capacity": 1, "selections": []}
            }
        },
        "other_items": {
            "label": "Other Items",
            "items": {
                "Old Silver Coin": {"capacity": 1, "selections": []},
                "Gold Key": {"capacity": 1, "selections": []},
                "Soul (UC)": {"capacity": 1, "selections": []},
                "Soul (Rare)": {"capacity": 1, "selections": []},
                "Soul (Epic)": {"capacity": 1, "selections": []}
            }
        }
    }
}

NOTICE_TEXT = (
    "Priority players have reserved access. If a priority player selects an item "
    "that’s already full, the most recent non-priority selection will be removed. "
    "Affected players may choose another item."
)

DIVIDER = "────────────────────"

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def load_data():
    if not os.path.exists(DATA_FILE):
        save_data(DEFAULT_DATA)
        return json.loads(json.dumps(DEFAULT_DATA))
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


data_store = load_data()


def now_ts():
    return datetime.now(timezone.utc).timestamp()


def is_priority(member: discord.Member) -> bool:
    priority_role_id = data_store.get("priority_role_id")
    if not priority_role_id:
        return False
    return any(role.id == priority_role_id for role in member.roles)


def find_user_selection_in_category(category_key: str, user_id: int):
    items = data_store["categories"][category_key]["items"]
    for item_name, item_data in items.items():
        for entry in item_data["selections"]:
            if entry["user_id"] == user_id:
                return item_name
    return None


def remove_user_from_category(category_key: str, user_id: int):
    items = data_store["categories"][category_key]["items"]
    removed_from = None
    for item_name, item_data in items.items():
        before = len(item_data["selections"])
        item_data["selections"] = [
            entry for entry in item_data["selections"] if entry["user_id"] != user_id
        ]
        if len(item_data["selections"]) != before:
            removed_from = item_name
    return removed_from


def get_item_data(category_key: str, item_name: str):
    return data_store["categories"][category_key]["items"].get(item_name)


def format_user_mention(user_id: int) -> str:
    return f"<@{user_id}>"


def build_category_block(category_key: str) -> str:
    category_data = data_store["categories"][category_key]
    label = category_data["label"]
    items = category_data["items"]

    lines = [
        f"## {label}",
        DIVIDER
    ]

    for item_name, item_data in items.items():
        capacity = item_data["capacity"]
        selections = sorted(item_data["selections"], key=lambda x: x["selected_at"])

        lines.append(f"**{item_name}** — `{len(selections)}/{capacity}`")

        if selections:
            for idx, entry in enumerate(selections, start=1):
                badge = "⭐ " if entry.get("priority", False) else ""
                lines.append(f"↳ {idx}. {badge}{format_user_mention(entry['user_id'])}")
        else:
            lines.append("↳ *No reservation yet*")

        lines.append("")

    return "\n".join(lines)


def build_panel_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🎁 Item Reservation Panel",
        description=(
            f"**Notice**\n"
            f"{NOTICE_TEXT}\n\n"
            f"{DIVIDER}\n"
            f"{build_category_block('main_items')}\n"
            f"{DIVIDER}\n\n"
            f"{build_category_block('other_items')}"
        ),
        color=discord.Color.blurple()
    )

    priority_role_id = data_store.get("priority_role_id")
    priority_line = f"<@&{priority_role_id}>" if priority_role_id else "Not set"
    embed.set_footer(text=f"Priority role: {priority_line}")
    return embed


class ItemSelect(discord.ui.Select):
    def __init__(self, category_key: str):
        self.category_key = category_key
        category_data = data_store["categories"][category_key]

        options = []
        for item_name, item_data in category_data["items"].items():
            count = len(item_data["selections"])
            capacity = item_data["capacity"]
            options.append(
                discord.SelectOption(
                    label=item_name[:100],
                    description=f"{count}/{capacity} reserved",
                    value=item_name
                )
            )

        super().__init__(
            placeholder=f"Choose from {category_data['label']}",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"select_{category_key}"
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True
            )
            return

        category_key = self.category_key
        selected_item_name = self.values[0]
        selected_item = get_item_data(category_key, selected_item_name)

        if not selected_item:
            await interaction.response.send_message(
                "That item no longer exists.",
                ephemeral=True
            )
            return

        member = interaction.user
        member_is_priority = is_priority(member)

        previous_item = find_user_selection_in_category(category_key, member.id)
        if previous_item == selected_item_name:
            await interaction.response.send_message(
                f"You already selected **{selected_item_name}**.",
                ephemeral=True
            )
            return

        if previous_item:
            remove_user_from_category(category_key, member.id)

        selections = selected_item["selections"]
        capacity = selected_item["capacity"]
        kicked_user_id = None

        if len(selections) >= capacity:
            if member_is_priority:
                non_priority_entries = [e for e in selections if not e.get("priority", False)]
                if non_priority_entries:
                    latest_non_priority = max(non_priority_entries, key=lambda x: x["selected_at"])
                    kicked_user_id = latest_non_priority["user_id"]
                    selections.remove(latest_non_priority)
                else:
                    if previous_item:
                        old_item = get_item_data(category_key, previous_item)
                        if old_item and len(old_item["selections"]) < old_item["capacity"]:
                            old_item["selections"].append({
                                "user_id": member.id,
                                "selected_at": now_ts(),
                                "priority": member_is_priority
                            })

                    await interaction.response.send_message(
                        "That item is full and all current reservations are priority players.",
                        ephemeral=True
                    )
                    save_data(data_store)
                    return
            else:
                if previous_item:
                    old_item = get_item_data(category_key, previous_item)
                    if old_item and len(old_item["selections"]) < old_item["capacity"]:
                        old_item["selections"].append({
                            "user_id": member.id,
                            "selected_at": now_ts(),
                            "priority": member_is_priority
                        })

                await interaction.response.send_message(
                    f"**{selected_item_name}** is already full.",
                    ephemeral=True
                )
                save_data(data_store)
                return

        selected_item["selections"].append({
            "user_id": member.id,
            "selected_at": now_ts(),
            "priority": member_is_priority
        })

        save_data(data_store)
        await refresh_panel(interaction.guild)

        msg = f"✅ You selected **{selected_item_name}**."
        if previous_item:
            msg += f"\nYour previous choice **{previous_item}** was removed."
        if kicked_user_id:
            msg += f"\nPriority override applied. Removed most recent non-priority user: <@{kicked_user_id}>."

        await interaction.response.send_message(msg, ephemeral=True)


class RemoveSelectionButton(discord.ui.Button):
    def __init__(self, category_key: str, label: str):
        self.category_key = category_key
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=f"remove_{category_key}"
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        removed = remove_user_from_category(self.category_key, interaction.user.id)
        if not removed:
            await interaction.response.send_message(
                "You do not have a selection in this category.",
                ephemeral=True
            )
            return

        save_data(data_store)
        await refresh_panel(interaction.guild)

        await interaction.response.send_message(
            f"🗑️ Removed your selection from **{removed}**.",
            ephemeral=True
        )


class ItemPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ItemSelect("main_items"))
        self.add_item(RemoveSelectionButton("main_items", "Remove Main Item"))
        self.add_item(ItemSelect("other_items"))
        self.add_item(RemoveSelectionButton("other_items", "Remove Other Item"))


async def get_panel_message(guild: discord.Guild) -> discord.Message | None:
    channel_id = data_store.get("panel_channel_id")
    message_id = data_store.get("panel_message_id")

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


async def refresh_panel(guild: discord.Guild):
    message = await get_panel_message(guild)
    if not message:
        return

    embed = build_panel_embed(guild)
    view = ItemPanelView()
    try:
        await message.edit(embed=embed, view=view)
    except Exception as e:
        print(f"Failed to refresh panel: {e}")


@bot.event
async def on_ready():
    bot.add_view(ItemPanelView())

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

    print(f"Logged in as {bot.user}")


def admin_only():
    async def predicate(interaction: discord.Interaction):
        if not interaction.user or not isinstance(interaction.user, discord.Member):
            return False
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)


@bot.tree.command(name="create_item_panel", description="Create the item reservation panel.")
@admin_only()
async def create_item_panel(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    embed = build_panel_embed(interaction.guild)
    view = ItemPanelView()

    await interaction.response.send_message("Creating item panel...", ephemeral=True)
    message = await interaction.channel.send(embed=embed, view=view)

    data_store["panel_channel_id"] = interaction.channel.id
    data_store["panel_message_id"] = message.id
    save_data(data_store)


@bot.tree.command(name="set_priority_role", description="Set the role considered as priority.")
@admin_only()
async def set_priority_role(interaction: discord.Interaction, role: discord.Role):
    data_store["priority_role_id"] = role.id
    save_data(data_store)

    if interaction.guild:
        await refresh_panel(interaction.guild)

    await interaction.response.send_message(
        f"Priority role set to {role.mention}.",
        ephemeral=True
    )


@bot.tree.command(name="set_item_cap", description="Set reservation cap for an item.")
@admin_only()
@app_commands.describe(
    category="Choose the category",
    item_name="Exact item name",
    capacity="Maximum number of reservations"
)
@app_commands.choices(category=[
    app_commands.Choice(name="Main Items", value="main_items"),
    app_commands.Choice(name="Other Items", value="other_items")
])
async def set_item_cap(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    item_name: str,
    capacity: app_commands.Range[int, 1, 99]
):
    items = data_store["categories"][category.value]["items"]
    if item_name not in items:
        await interaction.response.send_message(
            f"Item not found in {category.name}: **{item_name}**",
            ephemeral=True
        )
        return

    items[item_name]["capacity"] = capacity
    save_data(data_store)

    if interaction.guild:
        await refresh_panel(interaction.guild)

    await interaction.response.send_message(
        f"Set **{item_name}** cap to **{capacity}**.",
        ephemeral=True
    )


@bot.tree.command(name="add_item", description="Add a new item to a category.")
@admin_only()
@app_commands.describe(
    category="Choose the category",
    item_name="New item name",
    capacity="Maximum number of reservations"
)
@app_commands.choices(category=[
    app_commands.Choice(name="Main Items", value="main_items"),
    app_commands.Choice(name="Other Items", value="other_items")
])
async def add_item(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    item_name: str,
    capacity: app_commands.Range[int, 1, 99] = 1
):
    items = data_store["categories"][category.value]["items"]

    if item_name in items:
        await interaction.response.send_message(
            f"**{item_name}** already exists.",
            ephemeral=True
        )
        return

    if len(items) >= 25:
        await interaction.response.send_message(
            "That dropdown already has 25 items, which is Discord's select menu limit.",
            ephemeral=True
        )
        return

    items[item_name] = {
        "capacity": capacity,
        "selections": []
    }
    save_data(data_store)

    if interaction.guild:
        await refresh_panel(interaction.guild)

    await interaction.response.send_message(
        f"Added **{item_name}** to **{category.name}** with cap **{capacity}**.",
        ephemeral=True
    )


@bot.tree.command(name="remove_item", description="Remove an item from a category.")
@admin_only()
@app_commands.describe(
    category="Choose the category",
    item_name="Exact item name"
)
@app_commands.choices(category=[
    app_commands.Choice(name="Main Items", value="main_items"),
    app_commands.Choice(name="Other Items", value="other_items")
])
async def remove_item(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    item_name: str
):
    items = data_store["categories"][category.value]["items"]
    if item_name not in items:
        await interaction.response.send_message(
            f"Item not found: **{item_name}**",
            ephemeral=True
        )
        return

    del items[item_name]
    save_data(data_store)

    if interaction.guild:
        await refresh_panel(interaction.guild)

    await interaction.response.send_message(
        f"Removed **{item_name}** from **{category.name}**.",
        ephemeral=True
    )


@bot.tree.command(name="clear_item", description="Clear all reservations from one item.")
@admin_only()
@app_commands.describe(
    category="Choose the category",
    item_name="Exact item name"
)
@app_commands.choices(category=[
    app_commands.Choice(name="Main Items", value="main_items"),
    app_commands.Choice(name="Other Items", value="other_items")
])
async def clear_item(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    item_name: str
):
    items = data_store["categories"][category.value]["items"]
    if item_name not in items:
        await interaction.response.send_message(
            f"Item not found: **{item_name}**",
            ephemeral=True
        )
        return

    items[item_name]["selections"] = []
    save_data(data_store)

    if interaction.guild:
        await refresh_panel(interaction.guild)

    await interaction.response.send_message(
        f"Cleared reservations for **{item_name}**.",
        ephemeral=True
    )


@bot.tree.command(name="clear_all_items", description="Clear all reservations from all items.")
@admin_only()
async def clear_all_items(interaction: discord.Interaction):
    for category_data in data_store["categories"].values():
        for item_data in category_data["items"].values():
            item_data["selections"] = []

    save_data(data_store)

    if interaction.guild:
        await refresh_panel(interaction.guild)

    await interaction.response.send_message(
        "Cleared all reservations.",
        ephemeral=True
    )


@bot.tree.command(name="remove_my_choice", description="Remove your reservation from a category.")
@app_commands.describe(category="Choose the category")
@app_commands.choices(category=[
    app_commands.Choice(name="Main Items", value="main_items"),
    app_commands.Choice(name="Other Items", value="other_items")
])
async def remove_my_choice(
    interaction: discord.Interaction,
    category: app_commands.Choice[str]
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    removed = remove_user_from_category(category.value, interaction.user.id)
    if not removed:
        await interaction.response.send_message(
            "You have no reservation in that category.",
            ephemeral=True
        )
        return

    save_data(data_store)
    await refresh_panel(interaction.guild)

    await interaction.response.send_message(
        f"Removed your reservation from **{removed}**.",
        ephemeral=True
    )


@bot.tree.command(name="refresh_item_panel", description="Refresh the reservation panel manually.")
@admin_only()
async def refresh_item_panel(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    await refresh_panel(interaction.guild)
    await interaction.response.send_message("Panel refreshed.", ephemeral=True)


if TOKEN:
    bot.run(TOKEN)
else:
    print("TOKEN environment variable is missing.")
