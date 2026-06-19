import time
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from loguru import logger

from stuff.db import Guild, Owner, Space, all_guild_spaces
from stuff.spaces import (
    bump_slot,
    reposition_pinned,
    space_overwrites,
)
from stuff.housekeeping import run_housekeeping
from stuff.views import (
    ConfirmView,
    CreateSpaceModal,
    ManageSpaceModal,
    user_may_manage,
)


async def manageable_spaces(ctx: discord.AutocompleteContext):
    """Autocomplete: list spaces the invoking user is allowed to manage."""
    interaction = ctx.interaction
    guild = interaction.guild
    if guild is None:
        return []

    member = interaction.user
    perms = member.guild_permissions
    can_manage_all = perms.administrator or perms.manage_channels

    typed = (ctx.value or "").lower()
    choices = []
    for space_id, owner_id in await all_guild_spaces(guild.id):
        channel = guild.get_channel(space_id)
        if channel is None:
            continue
        if not can_manage_all and owner_id != member.id:
            continue
        if typed and typed not in channel.name.lower():
            continue
        choices.append(discord.OptionChoice(name=f"#{channel.name}", value=str(space_id)))
        if len(choices) >= 25:
            break
    return choices


class Cockpit(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def cog_unload(self):
        self.daily_housekeeping.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        # Start the scheduled loop here (not in __init__) so it binds to the bot's
        # running event loop rather than a stale one captured at import time.
        if not self.daily_housekeeping.is_running():
            self.daily_housekeeping.start()

    guild_group = discord.SlashCommandGroup("guild", "Commands to configure the guild")
    space_group = discord.SlashCommandGroup("space", "Commands related to spaces")

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    async def _bump_space(self, channel, *, via_thread: bool):
        """
        Move a space to the top of the unpinned region after activity.
        Revives a space from the archive category if it was there.
        """
        space_db = Space()
        await space_db.async_init(channel.id, channel.guild.id)
        if not space_db.exists:
            return
        if via_thread and not space_db.bump_on_thread_message:
            return
        if not via_thread and not space_db.bump_on_message:
            return

        guild_db = Guild()
        await guild_db.async_init(channel.guild.id)
        if not guild_db.exists:
            return

        main_category = channel.guild.get_channel(guild_db.space_category_id)
        if main_category is None:
            return

        in_dead = (
            guild_db.dead_space_category_id
            and channel.category_id == guild_db.dead_space_category_id
        )
        if in_dead:
            try:
                await channel.edit(category=main_category)
            except (discord.Forbidden, discord.HTTPException):
                return
        elif channel.category_id != guild_db.space_category_id:
            return

        if channel.id in guild_db.pinned_channel_ids:
            return

        await reposition_pinned(main_category, guild_db.pinned_channel_ids)
        target = bump_slot(main_category, guild_db.pinned_channel_ids)
        if channel.position != target:
            try:
                await channel.edit(position=target)
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def _do_create_space(
        self, target, name, guild_db, bump_on_message, bump_on_thread_message
    ):
        """
        Create the Discord channel and DB record for a space.
        `target` is either an ApplicationContext or an Interaction.
        """
        is_interaction = isinstance(target, discord.Interaction)
        guild = target.guild
        author = target.user if is_interaction else target.author

        name = name or f"{author.display_name}-space"
        category = guild.get_channel(guild_db.space_category_id)
        space = await category.create_text_channel(
            name,
            overwrites=space_overwrites(author, guild, guild_db.whitelisted_role_ids),
        )

        await Space.add(
            (
                space.id,
                space.guild.id,
                author.id,
                bump_on_message,
                bump_on_thread_message,
            )
        )

        space_owner_role = guild.get_role(guild_db.space_owner_role_id)
        if space_owner_role:
            try:
                member = guild.get_member(author.id)
                if member:
                    await member.add_roles(space_owner_role)
            except discord.Forbidden:
                pass

        success_embed = discord.Embed(
            description=f"Your space is ready at {space.mention}!",
            color=discord.Colour.green(),
        )
        if is_interaction:
            await target.response.send_message(embed=success_embed, ephemeral=True)
        else:
            await target.send_followup(embed=success_embed)

    # ------------------------------------------------------------------
    # Scheduled task
    # ------------------------------------------------------------------

    @tasks.loop(hours=1)
    async def daily_housekeeping(self):
        current_hour = datetime.now(timezone.utc).hour
        for guild in self.bot.guilds:
            guild_db = Guild()
            await guild_db.async_init(guild.id)
            if not guild_db.exists or not guild_db.auto_housekeeping:
                continue
            if (guild_db.housekeeping_hour or 0) != current_hour:
                continue
            if not guild_db.space_category_id:
                continue
            try:
                summary = await run_housekeeping(self.bot, guild, guild_db)
                logger.info(f"Housekeeping for {guild.name} ({guild.id}): {summary}")
            except Exception as e:
                logger.error(f"Housekeeping failed for guild {guild.id}: {e}")

    @daily_housekeeping.before_loop
    async def before_daily_housekeeping(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Bump listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return

        channel = message.channel
        if channel.type not in (
            discord.ChannelType.text,
            discord.ChannelType.public_thread,
        ):
            return

        if channel.type == discord.ChannelType.public_thread:
            await self._bump_space(channel.parent, via_thread=True)
        else:
            await self._bump_space(channel, via_thread=False)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        if channel.type == discord.ChannelType.public_thread:
            await self._bump_space(channel.parent, via_thread=True)
        elif channel.type == discord.ChannelType.text:
            await self._bump_space(channel, via_thread=False)

    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        if thread.type == discord.ChannelType.public_thread and thread.parent:
            await self._bump_space(thread.parent, via_thread=True)

    # ------------------------------------------------------------------
    # /guild commands
    # ------------------------------------------------------------------

    @guild_group.command(name="info", description="Displays information about this server")
    async def guild_info(self, ctx):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return

        greet_channel = (
            f"<#{guild_db.greet_channel_id}>" if guild_db.greet_channel_id else "None"
        )
        greet_attachments = (
            ", ".join(guild_db.greet_attachments) if guild_db.greet_attachments else "None"
        )
        space_category = (
            f"<#{guild_db.space_category_id}>" if guild_db.space_category_id else "None"
        )
        dead_category = (
            f"<#{guild_db.dead_space_category_id}>"
            if guild_db.dead_space_category_id
            else "None"
        )
        space_owner_role = (
            f"<@&{guild_db.space_owner_role_id}>" if guild_db.space_owner_role_id else "None"
        )
        pinned_channels = (
            ", ".join(f"<#{id}>" for id in guild_db.pinned_channel_ids)
            if guild_db.pinned_channel_ids
            else "None"
        )
        dead_pinned_channels = (
            ", ".join(f"<#{id}>" for id in guild_db.dead_pinned_channel_ids)
            if guild_db.dead_pinned_channel_ids
            else "None"
        )
        whitelisted_roles = (
            ", ".join(f"<@&{id}>" for id in guild_db.whitelisted_role_ids)
            if guild_db.whitelisted_role_ids
            else "None"
        )
        bump_on_message = "Yes" if guild_db.bump_on_message else "No"
        bump_on_thread_message = "Yes" if guild_db.bump_on_thread_message else "No"
        housekeeping = (
            f"Yes (at {(guild_db.housekeeping_hour or 0):02d}:00 UTC)"
            if guild_db.auto_housekeeping
            else "No"
        )
        await ctx.send_followup(
            embed=discord.Embed(
                title=f"About {ctx.guild.name}",
                description=(
                    f"Greet channel: **{greet_channel}**\n"
                    f"Greet attachments: **{greet_attachments}**\n"
                    f"Space category: **{space_category}**\n"
                    f"Dead-space category: **{dead_category}**\n"
                    f"Space owner role: **{space_owner_role}**\n"
                    f"Maximum spaces per owner: **{guild_db.max_spaces_per_owner}**\n"
                    f"Pinned channels: **{pinned_channels}**\n"
                    f"Dead-space pinned channels: **{dead_pinned_channels}**\n"
                    f"Whitelisted roles: **{whitelisted_roles}**\n"
                    f"Bump on message by default: **{bump_on_message}**\n"
                    f"Bump on thread message by default: **{bump_on_thread_message}**\n"
                    f"Daily housekeeping: **{housekeeping}**"
                ),
            )
        )

    @guild_group.command(
        name="create-space", description="Creates a space given an owner"
    )
    async def create_space_for_owner(
        self,
        ctx,
        owner: discord.Option(discord.User, "The owner of the space", required=False),
        name: discord.Option(
            str, "The name of the space", required=False, min_length=1
        ),
    ):
        await ctx.defer(ephemeral=True)

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return
        if not await guild_db.check_category(ctx):
            return

        owner = owner or ctx.author

        owner_db = Owner()
        await owner_db.async_init(ctx.guild.id, owner.id)
        if not await owner_db.check_max_spaces(ctx, guild_db.max_spaces_per_owner):
            return

        name = name or f"{owner.display_name}-space"
        category = ctx.guild.get_channel(guild_db.space_category_id)
        space = await category.create_text_channel(
            name,
            overwrites=space_overwrites(owner, ctx.guild, guild_db.whitelisted_role_ids),
        )

        await Space.add(
            (
                space.id,
                space.guild.id,
                owner.id,
                guild_db.bump_on_message,
                guild_db.bump_on_thread_message,
            )
        )

        space_owner_role = ctx.guild.get_role(guild_db.space_owner_role_id)
        if space_owner_role:
            try:
                member = ctx.guild.get_member(owner.id)
                if member:
                    await member.add_roles(space_owner_role)
            except discord.Forbidden:
                await ctx.send_followup(
                    embed=discord.Embed(
                        description=f"Failed to assign {space_owner_role.mention} to {owner.mention}.",
                        color=discord.Colour.red(),
                    )
                )
                return

        await ctx.send_followup(
            embed=discord.Embed(
                description=f"{space.mention} was successfully created for {owner.mention}.",
                color=discord.Colour.green(),
            ),
        )
        await ctx.channel.send(
            owner.mention,
            embed=discord.Embed(
                description=f"Check your space out at {space.mention}!",
            ),
            allowed_mentions=discord.AllowedMentions.all(),
        )

    @guild_group.command(
        name="add-space", description="Adds an existing channel to the database as a space"
    )
    async def add_space(
        self,
        ctx,
        space: discord.Option(discord.TextChannel, "The channel to register as a space"),
        owner: discord.Option(discord.User, "The owner of the space"),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return
        if not await guild_db.check_category(ctx):
            return

        space_db = Space()
        await space_db.async_init(space.id, space.guild.id)
        if not await space_db.check_exists(ctx, False):
            return

        owner_db = Owner()
        await owner_db.async_init(ctx.guild.id, owner.id)
        if not await owner_db.check_max_spaces(ctx, guild_db.max_spaces_per_owner):
            return

        await Space.add(
            (
                space.id,
                space.guild.id,
                owner.id,
                guild_db.bump_on_message,
                guild_db.bump_on_thread_message,
            )
        )

        space_owner_role = ctx.guild.get_role(guild_db.space_owner_role_id)
        if space_owner_role:
            try:
                member = ctx.guild.get_member(owner.id)
                if member:
                    await member.add_roles(space_owner_role)
            except discord.Forbidden:
                await ctx.send_followup(
                    embed=discord.Embed(
                        description=f"Failed to assign {space_owner_role.mention} to {owner.mention}.",
                        color=discord.Colour.red(),
                    )
                )
                return

        await ctx.send_followup(
            embed=discord.Embed(
                description=f"{space.mention} registered and now owned by {owner.mention}.",
                color=discord.Colour.green(),
            )
        )

    @guild_group.command(
        name="set-space-owner",
        description="Sets the owner of a space",
    )
    async def set_space_owner(
        self,
        ctx,
        space: discord.Option(discord.TextChannel, "The space to set the owner for"),
        owner: discord.Option(discord.User, "The new owner of the space"),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return
        if not await guild_db.check_category(ctx):
            return

        space_db = Space()
        await space_db.async_init(space.id, space.guild.id)
        if not await space_db.check_exists(ctx, True):
            return

        await space_db.set_owner(owner.id)

        await ctx.send_followup(
            embed=discord.Embed(
                description=f"{space.mention} now owned by {owner.mention}.",
                color=discord.Colour.green(),
            )
        )

    @guild_group.command(
        name="set-space-category",
        description="Sets or unsets the category for spaces",
    )
    async def set_space_category(
        self,
        ctx,
        category: discord.Option(discord.CategoryChannel, "The category to set/unset"),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return

        if guild_db.space_category_id == category.id:
            view = ConfirmView(
                ctx.author.id,
                action=f"Unset {category.mention} as the space category? Spaces will stop bumping until a new one is set.",
            )
            await ctx.send_followup(embed=view.embed(), view=view)
            await view.wait()
            if not view.value:
                return
            await guild_db.set_category(None)
            await ctx.send_followup(
                embed=discord.Embed(
                    description="Space category unset.",
                    color=discord.Colour.green(),
                )
            )
            return

        old_category_id = guild_db.space_category_id
        to_move = []
        if old_category_id:
            for space_id, _ in await all_guild_spaces(ctx.guild.id):
                channel = ctx.guild.get_channel(space_id)
                if channel and channel.category_id == old_category_id:
                    to_move.append(channel)

        if to_move:
            view = ConfirmView(
                ctx.author.id,
                action=f"Set the space category to {category.mention} and move **{len(to_move)}** existing space(s) into it?",
            )
            await ctx.send_followup(embed=view.embed(), view=view)
            await view.wait()
            if not view.value:
                return

        await guild_db.set_category(category.id)
        moved = 0
        for channel in to_move:
            try:
                await channel.edit(category=category)
                moved += 1
            except (discord.Forbidden, discord.HTTPException):
                pass

        suffix = f" Moved {moved} space(s)." if moved else ""
        await ctx.send_followup(
            embed=discord.Embed(
                description=f"Space category set to {category.mention}.{suffix}",
                color=discord.Colour.green(),
            )
        )

    @guild_group.command(
        name="set-dead-space-category",
        description="Sets or unsets the archive category for dead spaces",
    )
    async def set_dead_space_category(
        self,
        ctx,
        category: discord.Option(discord.CategoryChannel, "The category to set/unset"),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return

        if guild_db.dead_space_category_id == category.id:
            await guild_db.set_dead_category(None)
            await ctx.send_followup(
                embed=discord.Embed(
                    description="Archive category for dead spaces unset. Dead spaces will stay in the main category.",
                    color=discord.Colour.green(),
                )
            )
        else:
            await guild_db.set_dead_category(category.id)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"Archive category for dead spaces set to {category.mention}.",
                    color=discord.Colour.green(),
                )
            )

    @guild_group.command(
        name="set-space-owner-role",
        description="Sets or unsets the role given to space owners (synced to all owners)",
    )
    async def set_space_owner_role(
        self,
        ctx,
        role: discord.Option(discord.Role, "The role to set/unset"),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return

        owner_ids = {oid for _, oid in await all_guild_spaces(ctx.guild.id)}

        if guild_db.space_owner_role_id == role.id:
            await guild_db.set_owner_role(None)
            for member in ctx.guild.members:
                if role in member.roles:
                    try:
                        await member.remove_roles(role)
                    except discord.Forbidden:
                        pass
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"Space owner role unset and {role.mention} removed from all members.",
                    color=discord.Colour.green(),
                )
            )
            return

        old_role = ctx.guild.get_role(guild_db.space_owner_role_id)
        await guild_db.set_owner_role(role.id)
        for member in ctx.guild.members:
            is_owner = member.id in owner_ids
            if old_role and old_role in member.roles:
                try:
                    await member.remove_roles(old_role)
                except discord.Forbidden:
                    pass
            if is_owner and role not in member.roles:
                try:
                    await member.add_roles(role)
                except discord.Forbidden:
                    pass
            elif not is_owner and role in member.roles:
                try:
                    await member.remove_roles(role)
                except discord.Forbidden:
                    pass

        await ctx.send_followup(
            embed=discord.Embed(
                description=f"Space owner role set to {role.mention} and synced to all current owners.",
                color=discord.Colour.green(),
            )
        )

    @guild_group.command(
        name="set-max-spaces-per-owner",
        description="Sets the maximum number of spaces per owner",
    )
    async def set_max_spaces(
        self,
        ctx,
        value: discord.Option(int, min_value=1),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if await guild_db.check_exists(ctx):
            await guild_db.set_max_spaces(value)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"Maximum spaces per owner set to `{value}`.",
                    color=discord.Colour.green(),
                ),
            )

    @guild_group.command(
        name="pin-channel",
        description="Pins or unpins a channel in its space category",
    )
    async def pin_channel(
        self,
        ctx,
        channel: discord.Option(discord.TextChannel, "The channel to pin/unpin"),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return

        is_dead_category = (
            guild_db.dead_space_category_id
            and channel.category_id == guild_db.dead_space_category_id
        )
        if is_dead_category:
            pinned = guild_db.dead_pinned_channel_ids
            add = guild_db.add_to_dead_pinned
            remove = guild_db.remove_from_dead_pinned
            where = "the archive category"
        else:
            pinned = guild_db.pinned_channel_ids
            add = guild_db.add_to_pinned
            remove = guild_db.remove_from_pinned
            where = "the space category"

        if channel.id in pinned:
            await remove(channel.id)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"{channel.mention} unpinned.",
                    color=discord.Colour.green(),
                ),
            )
        else:
            await add(channel.id)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"{channel.mention} pinned in {where}.",
                    color=discord.Colour.green(),
                ),
            )

    @guild_group.command(
        name="modify-whitelist",
        description="Adds or removes a role from the default whitelist",
    )
    async def modify_whitelist(
        self,
        ctx,
        role: discord.Option(discord.Role, "The role to add/remove"),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return

        if role.id in guild_db.whitelisted_role_ids:
            await guild_db.remove_from_whitelist(role.id)
            action = "removed from"
        else:
            await guild_db.add_to_whitelist(role.id)
            action = "added to"

        # Re-apply permissions to every space so whitelist access updates immediately.
        fixed = 0
        for space_id, owner_id in await all_guild_spaces(ctx.guild.id):
            channel = ctx.guild.get_channel(space_id)
            owner = ctx.guild.get_member(owner_id)
            if channel is None or owner is None:
                continue
            expected = space_overwrites(owner, ctx.guild, guild_db.whitelisted_role_ids)
            if channel.overwrites != expected:
                try:
                    await channel.edit(overwrites=expected)
                    fixed += 1
                except (discord.Forbidden, discord.HTTPException):
                    pass

        suffix = f" Updated {fixed} space(s)." if fixed else ""
        await ctx.send_followup(
            embed=discord.Embed(
                description=f"{role.mention} {action} the whitelist.{suffix}",
                color=discord.Colour.green(),
            ),
        )

    @guild_group.command(
        name="set-bump-on-message",
        description="Sets the default for whether new messages in a space bump it",
    )
    async def set_bump_on_message(
        self,
        ctx,
        value: discord.Option(bool, "The value to set"),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if await guild_db.check_exists(ctx):
            await guild_db.set_bump(value)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"Default bump on message set to `{value}`.",
                    color=discord.Colour.green(),
                )
            )

    @guild_group.command(
        name="set-bump-on-thread-message",
        description="Sets the default for whether thread messages in a space bump it",
    )
    async def set_bump_on_thread_message(
        self,
        ctx,
        value: discord.Option(bool, "The value to set"),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if await guild_db.check_exists(ctx):
            await guild_db.set_bump_thread(value)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"Default bump on thread message set to `{value}`.",
                    color=discord.Colour.green(),
                )
            )

    @guild_group.command(
        name="set-housekeeping",
        description="Configures the daily automatic housekeeping run",
    )
    async def set_housekeeping(
        self,
        ctx,
        enabled: discord.Option(bool, "Enable or disable the daily run"),
        hour: discord.Option(
            int, "UTC hour (0-23) to run at", required=False, min_value=0, max_value=23
        ),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return

        await guild_db.set_auto_housekeeping(enabled)
        if hour is not None:
            await guild_db.set_housekeeping_hour(hour)

        run_hour = hour if hour is not None else (guild_db.housekeeping_hour or 0)
        state = "enabled" if enabled else "disabled"
        await ctx.send_followup(
            embed=discord.Embed(
                description=f"Daily housekeeping **{state}** (runs at {run_hour:02d}:00 UTC).",
                color=discord.Colour.green(),
            )
        )

    @guild_group.command(
        name="sort-spaces",
        description="Sorts spaces and runs full housekeeping (roles, permissions, archiving)",
    )
    async def sort_spaces(self, ctx):
        await ctx.defer(ephemeral=True)

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return
        if not await guild_db.check_category(ctx):
            return

        await ctx.interaction.edit_original_response(
            embed=discord.Embed(description="⏳ Starting housekeeping…")
        )

        last_edit = [0.0]

        async def progress(stage, current, total):
            now = time.monotonic()
            if current == 1 or current == total or now - last_edit[0] > 1.5:
                last_edit[0] = now
                try:
                    await ctx.interaction.edit_original_response(
                        embed=discord.Embed(
                            description=f"⏳ {stage}… {current}/{total}"
                        )
                    )
                except discord.HTTPException:
                    pass

        summary = await run_housekeeping(
            self.bot, ctx.guild, guild_db, progress=progress
        )

        await ctx.interaction.edit_original_response(
            embed=discord.Embed(
                description=(
                    "✓ Housekeeping complete.\n"
                    f"• Channels reordered: **{summary['sorted']}**\n"
                    f"• Archived dead spaces: **{summary['archived']}**\n"
                    f"• Revived spaces: **{summary['revived']}**\n"
                    f"• Owner roles fixed: **{summary['roles_fixed']}**\n"
                    f"• Permissions repaired: **{summary['perms_fixed']}**\n"
                    f"• Orphaned DB rows purged: **{summary['purged']}**"
                ),
                color=discord.Colour.green(),
            )
        )

    @guild_group.command(
        name="prune",
        description="Deletes empty space channels whose owners have left the server",
    )
    async def prune(self, ctx):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return

        targets = []
        for space_id, owner_id in await all_guild_spaces(ctx.guild.id):
            channel = ctx.guild.get_channel(space_id)
            if channel is None:
                continue
            empty = channel.last_message_id is None
            ownerless = ctx.guild.get_member(owner_id) is None
            if empty and ownerless:
                targets.append(channel)

        if not targets:
            await ctx.send_followup(
                embed=discord.Embed(
                    description="No empty, ownerless spaces to prune.",
                    color=discord.Colour.green(),
                )
            )
            return

        view = ConfirmView(
            ctx.author.id,
            action=(
                f"Delete **{len(targets)}** empty, ownerless space channel(s)? "
                "This permanently deletes the channels and cannot be undone."
            ),
        )
        await ctx.send_followup(embed=view.embed(), view=view)
        await view.wait()
        if not view.value:
            return

        deleted_ids = []
        for channel in targets:
            try:
                await channel.delete(reason="Pruned: empty and ownerless space")
                deleted_ids.append(channel.id)
            except (discord.Forbidden, discord.HTTPException):
                pass

        await Space.delete_many(deleted_ids)
        await ctx.send_followup(
            embed=discord.Embed(
                description=f"Pruned {len(deleted_ids)} space(s).",
                color=discord.Colour.green(),
            )
        )

    # ------------------------------------------------------------------
    # /space commands
    # ------------------------------------------------------------------

    @space_group.command(name="create", description="Creates a space for yourself")
    async def create_space(self, ctx):
        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)

        if not guild_db.exists:
            await ctx.respond(
                embed=discord.Embed(description="This server is not in the database."),
                ephemeral=True,
            )
            return
        if not guild_db.space_category_id:
            await ctx.respond(
                embed=discord.Embed(
                    description="Category for spaces not set for this server."
                ),
                ephemeral=True,
            )
            return

        owner_db = Owner()
        await owner_db.async_init(ctx.guild.id, ctx.author.id)
        if len(owner_db.spaces) >= guild_db.max_spaces_per_owner:
            await ctx.respond(
                embed=discord.Embed(
                    description="You have reached the maximum number of spaces for this server."
                ),
                ephemeral=True,
            )
            return

        await ctx.send_modal(CreateSpaceModal(self, guild_db))

    @space_group.command(
        name="manage", description="Opens a form to manage one of your spaces"
    )
    async def manage_space(
        self,
        ctx,
        space: discord.Option(
            str, "The space to manage", autocomplete=manageable_spaces
        ),
    ):
        try:
            space_id = int(space)
        except (TypeError, ValueError):
            await ctx.respond(
                embed=discord.Embed(
                    description="Please pick a space from the list.",
                    color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        channel = ctx.guild.get_channel(space_id)
        if channel is None:
            await ctx.respond(
                embed=discord.Embed(
                    description="That space no longer exists.",
                    color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        space_db = Space()
        await space_db.async_init(space_id, ctx.guild.id)
        if not space_db.exists:
            await ctx.respond(
                embed=discord.Embed(
                    description="That channel is not a space.",
                    color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        if not user_may_manage(ctx.author, space_db):
            await ctx.respond(
                embed=discord.Embed(
                    description="You don't have permission to manage this space.",
                    color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        await ctx.send_modal(ManageSpaceModal(channel, space_db, guild_db))


def setup(bot):
    bot.add_cog(Cockpit(bot))
