# ONLY SHOWING THE MODIFIED CORE PART (selection logic change)

# FIND THIS FUNCTION IN YOUR CODE:
def find_user_selection_in_category(event_data: dict, category_key: str, user_id: int):
    # ❌ REMOVE THIS FUNCTION COMPLETELY
    return None


# -----------------------------
# UPDATE SELECT CALLBACK
# -----------------------------

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
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        event_data = data_store["events"].get(self.event_key)
        if not event_data:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return

        if is_event_locked(event_data):
            await interaction.response.send_message("❌ Panel is locked.", ephemeral=True)
            return

        selected_item_name = self.values[0]
        selected_item = get_item_data(event_data, self.category_key, selected_item_name)

        if not selected_item:
            await interaction.response.send_message("Item not found.", ephemeral=True)
            return

        user_id = interaction.user.id

        # ✅ NEW: check if already selected THIS item
        for entry in selected_item["selections"]:
            if entry["user_id"] == user_id:
                await interaction.response.send_message(
                    f"You already selected **{selected_item_name}**.",
                    ephemeral=True
                )
                return

        selections = selected_item["selections"]
        capacity = selected_item["capacity"]
        removed_user_id = None
        member_rank = get_priority_rank(event_data, user_id)

        # ✅ HANDLE FULL ITEM
        if len(selections) >= capacity:
            lowest_entry = max(
                selections,
                key=lambda x: (get_priority_rank(event_data, x["user_id"]), x["selected_at"])
            )

            lowest_rank = get_priority_rank(event_data, lowest_entry["user_id"])

            if member_rank < lowest_rank:
                removed_user_id = lowest_entry["user_id"]
                selections.remove(lowest_entry)
            else:
                await save_state()
                await interaction.response.send_message(
                    f"❌ **{selected_item_name}** is full and higher priority players own it.",
                    ephemeral=True
                )
                return

        # ✅ ADD USER (MULTI SELECT ENABLED)
        selections.append({
            "user_id": user_id,
            "selected_at": now_ts()
        })

        await save_state()
        await refresh_event_panel(interaction.guild, self.event_key)

        msg = f"✅ You selected **{selected_item_name}**."
        if removed_user_id:
            msg += f"\n⚠️ Removed <@{removed_user_id}> due to priority."

        await interaction.response.send_message(msg, ephemeral=True)
