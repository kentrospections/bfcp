import discord

from stuff.db import Guild, Space
from stuff.spaces import space_overwrites


def user_may_manage(member, space_db):
    """True if the member owns the space or has manage_channels / admin."""
    if member.id == space_db.owner_id:
        return True
    perms = member.guild_permissions
    return perms.administrator or perms.manage_channels


# ---------------------------------------------------------------------------
# ConfirmView — classic View, used before destructive actions
# ---------------------------------------------------------------------------

class ConfirmView(discord.ui.View):
    """
    A two-button (Confirm / Cancel) confirmation dialog.

    Usage::

        view = ConfirmView(ctx.author.id, action="delete 3 spaces from the database")
        await ctx.send_followup(embed=view.embed(), view=view)
        await view.wait()
        if view.value:
            ...  # confirmed
    """

    def __init__(self, author_id, *, action: str, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.action = action
        self.value = None

    def embed(self):
        return discord.Embed(
            title="Are you sure?",
            description=self.action,
            color=discord.Colour.yellow(),
        )

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This confirmation isn't for you.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, button, interaction):
        self.value = True
        self.disable_all_items()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button, interaction):
        self.value = False
        self.disable_all_items()
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self):
        self.disable_all_items()


# ---------------------------------------------------------------------------
# CreateSpaceModal — DesignerModal with Checkbox support (py-cord 2.8)
# ---------------------------------------------------------------------------

class CreateSpaceModal(discord.ui.DesignerModal):
    """
    Modal for /space create. Collects name + bump preferences in one dialog.
    After submit, the cog's _do_create_space is called.
    """

    def __init__(self, cog, guild_db):
        self._cog = cog
        self._guild_db = guild_db

        self.name_input = discord.ui.InputText(
            placeholder='Leave blank for "yourname-space"',
            required=False,
            max_length=100,
        )
        self.bump_checkbox = discord.ui.Checkbox(
            default=bool(guild_db.bump_on_message),
            custom_id="create_bump_msg",
        )
        self.bump_thread_checkbox = discord.ui.Checkbox(
            default=bool(guild_db.bump_on_thread_message),
            custom_id="create_bump_thread",
        )

        super().__init__(
            discord.ui.Label("Name (optional)", self.name_input),
            discord.ui.Label("Bump on new message", self.bump_checkbox),
            discord.ui.Label("Bump on thread message", self.bump_thread_checkbox),
            title="Create Space",
        )

    async def callback(self, interaction):
        name = self.name_input.value or None
        bump = (
            self.bump_checkbox.value
            if self.bump_checkbox.value is not None
            else bool(self._guild_db.bump_on_message)
        )
        bump_thread = (
            self.bump_thread_checkbox.value
            if self.bump_thread_checkbox.value is not None
            else bool(self._guild_db.bump_on_thread_message)
        )
        await self._cog._do_create_space(
            interaction, name, self._guild_db, bump, bump_thread
        )


# ---------------------------------------------------------------------------
# ManageSpaceModal — single-form management for a space
# ---------------------------------------------------------------------------

class ManageSpaceModal(discord.ui.DesignerModal):
    """
    Single modal form to manage a space: rename, bump toggles, reset permissions,
    and ownership transfer — all applied atomically on submit.
    """

    def __init__(self, channel, space_db, guild_db):
        self.channel = channel
        self.space_db = space_db
        self.guild_db = guild_db

        self.name_input = discord.ui.InputText(
            required=False,
            max_length=100,
            value=channel.name,
        )
        self.settings = discord.ui.CheckboxGroup(
            options=[
                discord.CheckboxGroupOption(
                    label="Bump on new message",
                    value="bump_msg",
                    default=bool(space_db.bump_on_message),
                ),
                discord.CheckboxGroupOption(
                    label="Bump on thread message",
                    value="bump_thread",
                    default=bool(space_db.bump_on_thread_message),
                ),
                discord.CheckboxGroupOption(
                    label="Reset permissions to default",
                    value="reset",
                    description="Re-applies the default owner / whitelist permissions",
                    default=False,
                ),
            ],
            required=False,
            min_values=0,
            max_values=3,
        )
        self.transfer = discord.ui.UserSelect(required=False, max_values=1)

        super().__init__(
            discord.ui.Label("Name", self.name_input),
            discord.ui.Label("Settings", self.settings),
            discord.ui.Label("Transfer ownership (optional)", self.transfer),
            title=f"Manage Space",
        )

    async def callback(self, interaction):
        guild = interaction.guild
        changes = []

        selected = set(self.settings.values or [])

        # Rename
        new_name = (self.name_input.value or "").strip()
        if new_name and new_name != self.channel.name:
            try:
                await self.channel.edit(name=new_name)
                changes.append(f"renamed to **{new_name}**")
            except discord.Forbidden:
                changes.append("⚠️ failed to rename (missing permissions)")

        # Bump toggles
        bump_msg = "bump_msg" in selected
        bump_thread = "bump_thread" in selected
        if bump_msg != bool(self.space_db.bump_on_message):
            await self.space_db.set_bump(bump_msg)
            changes.append(f"bump on message → **{'on' if bump_msg else 'off'}**")
        if bump_thread != bool(self.space_db.bump_on_thread_message):
            await self.space_db.set_bump_thread(bump_thread)
            changes.append(
                f"bump on thread message → **{'on' if bump_thread else 'off'}**"
            )

        # Ownership transfer
        new_owner = self.transfer.values[0] if self.transfer.values else None
        if new_owner is not None:
            if new_owner.bot:
                changes.append("⚠️ cannot transfer ownership to a bot")
            elif new_owner.id != self.space_db.owner_id:
                await self.space_db.set_owner(new_owner.id)
                changes.append(f"ownership transferred to {new_owner.mention}")
                # Apply default permissions for the new owner unless reset will run anyway
                if "reset" not in selected:
                    member = guild.get_member(new_owner.id)
                    if member:
                        try:
                            await self.channel.edit(
                                overwrites=space_overwrites(
                                    member, guild, self.guild_db.whitelisted_role_ids
                                )
                            )
                        except discord.Forbidden:
                            pass

        # Reset permissions
        if "reset" in selected:
            owner = guild.get_member(self.space_db.owner_id)
            if owner:
                try:
                    await self.channel.edit(
                        overwrites=space_overwrites(
                            owner, guild, self.guild_db.whitelisted_role_ids
                        )
                    )
                    changes.append("permissions reset to default")
                except discord.Forbidden:
                    changes.append("⚠️ failed to reset permissions")
            else:
                changes.append("⚠️ owner not in server; permissions not reset")

        if not changes:
            description = "No changes were made."
            color = discord.Colour.blurple()
        else:
            description = f"Updated {self.channel.mention}:\n" + "\n".join(
                f"• {c}" for c in changes
            )
            color = discord.Colour.green()

        await interaction.response.send_message(
            embed=discord.Embed(description=description, color=color),
            ephemeral=True,
        )
