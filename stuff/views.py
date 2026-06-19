import discord

from stuff.db import Guild, Space


async def _user_may_manage(interaction, space_db):
    """True if the interaction user owns the space or has manage_channels / admin."""
    if interaction.user.id == space_db.owner_id:
        return True
    perms = interaction.user.guild_permissions
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
        bump = self.bump_checkbox.value if self.bump_checkbox.value is not None else bool(self._guild_db.bump_on_message)
        bump_thread = self.bump_thread_checkbox.value if self.bump_thread_checkbox.value is not None else bool(self._guild_db.bump_on_thread_message)
        await self._cog._do_create_space(interaction, name, self._guild_db, bump, bump_thread)


# ---------------------------------------------------------------------------
# RenameModal — DesignerModal for renaming a space
# ---------------------------------------------------------------------------

class RenameModal(discord.ui.DesignerModal):
    def __init__(self, channel):
        self._channel = channel
        self.name_input = discord.ui.InputText(
            placeholder="Enter a new name for your space",
            required=True,
            max_length=100,
            value=channel.name,
        )
        super().__init__(
            discord.ui.Label("New name", self.name_input),
            title="Rename Space",
        )

    async def callback(self, interaction):
        new_name = self.name_input.value
        try:
            await self._channel.edit(name=new_name)
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=f"Space renamed to **{new_name}**.",
                    color=discord.Colour.green(),
                ),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="Missing permissions to rename this channel.",
                    color=discord.Colour.red(),
                ),
                ephemeral=True,
            )


# ---------------------------------------------------------------------------
# ManageSpaceView — interactive management panel for a space
# ---------------------------------------------------------------------------

def _manage_embed(space_channel, space_db):
    bump_msg = "✓ Yes" if space_db.bump_on_message else "✗ No"
    bump_thread = "✓ Yes" if space_db.bump_on_thread_message else "✗ No"
    return discord.Embed(
        title=f"Manage: #{space_channel.name}",
        description=(
            f"**Owner:** <@{space_db.owner_id}>\n"
            f"**Bump on message:** {bump_msg}\n"
            f"**Bump on threads:** {bump_thread}"
        ),
    ).set_footer(text="Changes are saved immediately · Panel expires in 3 minutes")


class ManageSpaceView(discord.ui.View):
    """
    Stateful ephemeral panel for space owners / admins to manage a space.

    Supports:
      - Toggle bump-on-message
      - Toggle bump-on-thread-message
      - Restore default permissions
      - Rename (opens RenameModal)
      - Transfer ownership (UserSelect)
    """

    def __init__(self, cog, space_channel, space_db, invoker_id):
        super().__init__(timeout=180)
        self._cog = cog
        self.channel = space_channel
        self.space_db = space_db
        self.invoker_id = invoker_id
        self._sync_buttons()

    def _sync_buttons(self):
        """Update bump toggle button labels/styles to reflect current DB state."""
        for item in self.children:
            if hasattr(item, "custom_id"):
                if item.custom_id == "toggle_bump_msg":
                    if self.space_db.bump_on_message:
                        item.label = "✓ Bump: messages ON"
                        item.style = discord.ButtonStyle.success
                    else:
                        item.label = "✗ Bump: messages OFF"
                        item.style = discord.ButtonStyle.secondary
                elif item.custom_id == "toggle_bump_thread":
                    if self.space_db.bump_on_thread_message:
                        item.label = "✓ Bump: threads ON"
                        item.style = discord.ButtonStyle.success
                    else:
                        item.label = "✗ Bump: threads OFF"
                        item.style = discord.ButtonStyle.secondary

    async def interaction_check(self, interaction):
        if not await _user_may_manage(interaction, self.space_db):
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="You don't have permission to manage this space.",
                    color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="✓ Bump: messages ON",
        style=discord.ButtonStyle.success,
        custom_id="toggle_bump_msg",
        row=0,
    )
    async def toggle_bump_msg(self, button, interaction):
        await self.space_db.set_bump(not self.space_db.bump_on_message)
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=_manage_embed(self.channel, self.space_db), view=self
        )

    @discord.ui.button(
        label="✓ Bump: threads ON",
        style=discord.ButtonStyle.success,
        custom_id="toggle_bump_thread",
        row=0,
    )
    async def toggle_bump_thread(self, button, interaction):
        await self.space_db.set_bump_thread(not self.space_db.bump_on_thread_message)
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=_manage_embed(self.channel, self.space_db), view=self
        )

    @discord.ui.button(
        label="Restore Permissions",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def restore_permissions(self, button, interaction):
        guild_db = Guild()
        await guild_db.async_init(self.channel.guild.id)
        if not guild_db.exists:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="Server config not found.",
                    color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return
        owner = self.channel.guild.get_member(self.space_db.owner_id)
        if owner is None:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="Space owner is not in the server.",
                    color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return
        from cogs.space import space_overwrites
        try:
            await self.channel.edit(
                overwrites=space_overwrites(owner, self.channel.guild, guild_db.whitelisted_role_ids)
            )
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=f"Permissions restored for {self.channel.mention}.",
                    color=discord.Colour.green(),
                ),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="Missing permissions to edit this channel.",
                    color=discord.Colour.red(),
                ),
                ephemeral=True,
            )

    @discord.ui.button(
        label="Rename",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def rename(self, button, interaction):
        await interaction.response.send_modal(RenameModal(self.channel))

    @discord.ui.user_select(
        placeholder="Transfer ownership to…",
        min_values=1,
        max_values=1,
        row=2,
    )
    async def transfer_ownership(self, select, interaction):
        new_owner = select.values[0]
        if new_owner.bot:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="Cannot transfer ownership to a bot.",
                    color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return
        await self.space_db.set_owner(new_owner.id)

        guild_db = Guild()
        await guild_db.async_init(self.channel.guild.id)
        if guild_db.exists:
            from cogs.space import space_overwrites
            try:
                await self.channel.edit(
                    overwrites=space_overwrites(new_owner, self.channel.guild, guild_db.whitelisted_role_ids)
                )
            except discord.Forbidden:
                pass

        await interaction.response.edit_message(
            embed=_manage_embed(self.channel, self.space_db), view=self
        )

    async def on_timeout(self):
        self.disable_all_items()
        if self.message:
            try:
                await self.message.edit(
                    embed=discord.Embed(
                        description="Panel expired. Run `/space manage` again to reopen."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass
