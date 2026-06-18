import time as _time

import aiosqlite
import discord
from discord.ext import commands

from stuff.db import Guild, Owner, Space
from stuff.spaces import (
    build_active_threads_by_parent,
    bump_slot,
    latest_activity,
    pinned_in_order,
    reposition_pinned,
    throttled_progress,
)
from stuff.views import ConfirmView, CreateSpaceModal, ManageSpaceView, _manage_embed


def space_overwrites(owner, guild, whitelisted_role_ids):
    """
    Build Discord permission overwrites for a space channel.

    Owner gets full control. If whitelisted roles exist, @everyone is denied
    access and those roles are granted view access. Otherwise @everyone can
    view but not send messages.

    Note: `owner` may be a Member or User; resolve to Member for role checks.
    """
    member = guild.get_member(owner.id) if not isinstance(owner, discord.Member) else owner

    overwrites = {
        owner: discord.PermissionOverwrite(
            view_channel=True,
            manage_channels=True,
            manage_permissions=True,
            manage_webhooks=True,
            read_messages=True,
            send_messages=True,
        )
    }

    override_roles = [
        r
        for role_id in whitelisted_role_ids
        if (r := guild.get_role(role_id)) and member and r in member.roles
    ]

    if override_roles:
        overwrites[guild.default_role] = discord.PermissionOverwrite(
            view_channel=False, send_messages=False
        )
        for role in override_roles:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True)
    else:
        overwrites[guild.default_role] = discord.PermissionOverwrite(
            send_messages=False
        )

    return overwrites


class Cockpit(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    guild_group = discord.SlashCommandGroup("guild", "Commands to configure the guild")
    space_group = discord.SlashCommandGroup("space", "Commands related to spaces")
    config_group = space_group.create_subgroup("config")

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    async def _bump_space(self, channel, *, via_thread: bool):
        """
        Move a space to the top of the unpinned region after any activity.
        Repositions drifted pinned channels first.
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
        if channel.id in guild_db.pinned_channel_ids:
            return
        if channel.category_id != guild_db.space_category_id:
            return

        category = channel.guild.get_channel(guild_db.space_category_id)
        if category is None:
            return

        await reposition_pinned(category, guild_db.pinned_channel_ids)

        target = bump_slot(category, guild_db.pinned_channel_ids)
        if channel.position != target:
            try:
                await channel.edit(position=target)
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def _do_create_space(self, target, name, guild_db, bump_on_message, bump_on_thread_message):
        """
        Create the actual Discord channel and DB record for a space.
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
        if await guild_db.check_exists(ctx):
            greet_channel = (
                f"<#{guild_db.greet_channel_id}>" if guild_db.greet_channel_id else "None"
            )
            greet_attachments = (
                ", ".join(guild_db.greet_attachments) if guild_db.greet_attachments else "None"
            )
            space_category = (
                f"<#{guild_db.space_category_id}>" if guild_db.space_category_id else "None"
            )
            space_owner_role = (
                f"<@&{guild_db.space_owner_role_id}>" if guild_db.space_owner_role_id else "None"
            )
            pinned_channels = (
                ", ".join(f"<#{id}>" for id in guild_db.pinned_channel_ids)
                if guild_db.pinned_channel_ids
                else "None"
            )
            whitelisted_roles = (
                ", ".join(f"<@&{id}>" for id in guild_db.whitelisted_role_ids)
                if guild_db.whitelisted_role_ids
                else "None"
            )
            bump_on_message = "Yes" if guild_db.bump_on_message else "No"
            bump_on_thread_message = "Yes" if guild_db.bump_on_thread_message else "No"
            await ctx.send_followup(
                embed=discord.Embed(
                    title=f"About {ctx.guild.name}",
                    description=(
                        f"Greet channel: **{greet_channel}**\n"
                        f"Greet attachments: **{greet_attachments}**\n"
                        f"Space category: **{space_category}**\n"
                        f"Space owner role: **{space_owner_role}**\n"
                        f"Maximum spaces per owner: **{guild_db.max_spaces_per_owner}**\n"
                        f"Pinned channels: **{pinned_channels}**\n"
                        f"Whitelisted roles: **{whitelisted_roles}**\n"
                        f"Bump on message by default: **{bump_on_message}**\n"
                        f"Bump on thread message by default: **{bump_on_thread_message}**"
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
                action=f"Unset **{category.mention}** as the space category? Spaces will stop bumping until a new category is set.",
            )
            await ctx.send_followup(embed=view.embed(), view=view)
            await view.wait()
            if not view.value:
                return
            await guild_db.set_category(None)
            await ctx.send_followup(
                embed=discord.Embed(
                    description="Category for spaces unset.",
                    color=discord.Colour.green(),
                )
            )
        else:
            await guild_db.set_category(category.id)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"Category for spaces set to **{category.mention}**.",
                    color=discord.Colour.green(),
                )
            )

    @guild_group.command(
        name="set-space-owner-role",
        description="Sets or unsets the role given to space owners",
    )
    async def set_space_owner_role(
        self,
        ctx,
        role: discord.Option(discord.Role, "The role to set/unset"),
        propagate: discord.Option(
            bool,
            "Whether to add/remove the role from all current space owners",
            default=False,
        ),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return

        if guild_db.space_owner_role_id == role.id:
            if propagate:
                view = ConfirmView(
                    ctx.author.id,
                    action=f"Unset {role.mention} and remove it from all current space owners?",
                )
                await ctx.send_followup(embed=view.embed(), view=view)
                await view.wait()
                if not view.value:
                    return
            await guild_db.set_owner_role(None)
            if propagate:
                for member in ctx.guild.members:
                    owner_db = Owner()
                    await owner_db.async_init(ctx.guild.id, member.id)
                    if owner_db.exists and role in member.roles:
                        try:
                            await member.remove_roles(role)
                        except discord.Forbidden:
                            pass
                await ctx.send_followup(
                    embed=discord.Embed(
                        description="Role for space owners removed from all owners and unset.",
                        color=discord.Colour.green(),
                    )
                )
            else:
                await ctx.send_followup(
                    embed=discord.Embed(
                        description="Role for space owners unset.",
                        color=discord.Colour.green(),
                    )
                )
        else:
            old_role = ctx.guild.get_role(guild_db.space_owner_role_id) if propagate else None
            await guild_db.set_owner_role(role.id)
            if propagate:
                for member in ctx.guild.members:
                    owner_db = Owner()
                    await owner_db.async_init(ctx.guild.id, member.id)
                    if owner_db.exists:
                        if old_role and old_role in member.roles:
                            try:
                                await member.remove_roles(old_role)
                            except discord.Forbidden:
                                pass
                        if role not in member.roles:
                            try:
                                await member.add_roles(role)
                            except discord.Forbidden:
                                pass
                await ctx.send_followup(
                    embed=discord.Embed(
                        description=f"Role for space owners set to {role.mention} and applied to all owners.",
                        color=discord.Colour.green(),
                    )
                )
            else:
                await ctx.send_followup(
                    embed=discord.Embed(
                        description=f"Role for space owners set to {role.mention}.",
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
        description="Pins or unpins a channel in the spaces category",
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

        if channel.id in guild_db.pinned_channel_ids:
            await guild_db.remove_from_pinned(channel.id)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"{channel.mention} unpinned.",
                    color=discord.Colour.green(),
                ),
            )
        else:
            await guild_db.add_to_pinned(channel.id)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"{channel.mention} pinned.",
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
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"{role.mention} removed from the whitelist.",
                    color=discord.Colour.green(),
                ),
            )
        else:
            await guild_db.add_to_whitelist(role.id)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"{role.mention} added to the whitelist.",
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
        name="sort-spaces", description="Sorts spaces by activity in descending order"
    )
    async def sort_spaces(self, ctx):
        await ctx.defer(ephemeral=True)

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if not await guild_db.check_exists(ctx):
            return
        if not await guild_db.check_category(ctx):
            return

        category = ctx.guild.get_channel(guild_db.space_category_id)
        if category is None:
            await ctx.send_followup(
                embed=discord.Embed(description="Space category channel no longer exists.")
            )
            return

        spaces = []
        for channel in category.text_channels:
            if channel.id in guild_db.pinned_channel_ids:
                continue
            space_db = Space()
            await space_db.async_init(channel.id, channel.guild.id)
            if space_db.exists:
                spaces.append(channel)

        if not spaces:
            await ctx.send_followup(
                embed=discord.Embed(description="There are no spaces to sort.")
            )
            return

        await ctx.interaction.edit_original_response(
            embed=discord.Embed(description="⏳ Starting sort…")
        )

        active_map = await build_active_threads_by_parent(ctx.guild)

        activity = {}
        empty_count = 0
        last_edit = [0.0]
        total = len(spaces)

        for i, space in enumerate(spaces, start=1):
            ts = await latest_activity(space, active_map)
            activity[space.id] = ts
            if space.last_message_id is None:
                empty_count += 1
            await throttled_progress(ctx, i, total, space.mention, last_edit)

        ordered_pinned = pinned_in_order(category, guild_db.pinned_channel_ids)
        ordered_spaces = sorted(spaces, key=lambda s: activity[s.id], reverse=True)
        ordered_all = ordered_pinned + ordered_spaces

        if not category.channels:
            await ctx.interaction.edit_original_response(
                embed=discord.Embed(description="Category appears to be empty.")
            )
            return

        base = category.channels[0].position
        for idx, ch in enumerate(ordered_all):
            target = base + idx
            if ch.position != target:
                try:
                    await ch.edit(position=target)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        space_word = "space" if len(spaces) == 1 else "spaces"
        empty_note = f"  ({empty_count} empty, sorted by creation time)" if empty_count else ""
        await ctx.interaction.edit_original_response(
            embed=discord.Embed(
                description=f"✓ Sorted {len(spaces)} {space_word}.{empty_note}",
                color=discord.Colour.green(),
            )
        )

    @guild_group.command(
        name="clean-space-db",
        description="Removes deleted channels and/or departed owners from the database",
    )
    async def clean_space_db(
        self,
        ctx,
        ignore_owners: discord.Option(
            bool,
            "If true, only removes spaces whose channels are gone (not departed owners)",
            required=False,
        ),
    ):
        await ctx.defer()

        async with aiosqlite.connect("data/database.db") as db:
            async with db.execute(
                "SELECT space_id, owner_id FROM spaces WHERE guild_id = ?",
                (ctx.guild.id,),
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await ctx.send_followup(
                embed=discord.Embed(description="There are no spaces to clean.")
            )
            return

        space_ids_to_clean = [
            row[0] for row in rows if not ctx.guild.get_channel(row[0])
        ]
        owner_ids_to_clean = (
            []
            if ignore_owners
            else [row[1] for row in rows if not ctx.guild.get_member(row[1])]
        )

        total_removed = len(space_ids_to_clean) + len(owner_ids_to_clean)
        if total_removed == 0:
            await ctx.send_followup(
                embed=discord.Embed(
                    description="Nothing to clean — all spaces and owners are still present.",
                    color=discord.Colour.green(),
                )
            )
            return

        lines = []
        if space_ids_to_clean:
            lines.append(f"**{len(space_ids_to_clean)}** missing channel(s)")
        if owner_ids_to_clean:
            lines.append(f"**{len(owner_ids_to_clean)}** departed owner(s)")

        view = ConfirmView(
            ctx.author.id,
            action=f"Remove {' and '.join(lines)} from the database? This cannot be undone.",
        )
        await ctx.send_followup(embed=view.embed(), view=view)
        await view.wait()
        if not view.value:
            return

        async with aiosqlite.connect("data/database.db") as db:
            if space_ids_to_clean:
                params = ", ".join("?" * len(space_ids_to_clean))
                await db.execute(
                    f"DELETE FROM spaces WHERE space_id IN ({params})",
                    space_ids_to_clean,
                )
            if owner_ids_to_clean:
                params = ", ".join("?" * len(owner_ids_to_clean))
                await db.execute(
                    f"DELETE FROM spaces WHERE guild_id = ? AND owner_id IN ({params})",
                    [ctx.guild.id] + owner_ids_to_clean,
                )
            await db.commit()

        await ctx.send_followup(
            embed=discord.Embed(
                description=f"Cleaned {' and '.join(lines)} from the database.",
                color=discord.Colour.green(),
            )
        )

    # ------------------------------------------------------------------
    # /space commands
    # ------------------------------------------------------------------

    @space_group.command(name="info", description="Displays information about a space")
    async def space_info(
        self,
        ctx,
        space: discord.Option(discord.TextChannel, "The space to get info for"),
    ):
        await ctx.defer()

        space_db = Space()
        await space_db.async_init(space.id, ctx.guild.id)
        if await space_db.check_exists(ctx, True):
            bump_on_message = "Yes" if space_db.bump_on_message else "No"
            bump_on_thread_message = "Yes" if space_db.bump_on_thread_message else "No"
            await ctx.send_followup(
                embed=discord.Embed(
                    title=f"About {space.mention}",
                    description=(
                        f"**Owner:** <@{space_db.owner_id}>\n"
                        f"**Bump on message:** {bump_on_message}\n"
                        f"**Bump on thread message:** {bump_on_thread_message}"
                    ),
                )
            )

    @space_group.command(name="create", description="Creates a space for yourself")
    async def create_space(
        self,
        ctx,
    ):
        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)

        # Pre-flight checks before showing modal (modal can't send followups)
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
        name="manage", description="Opens a management panel for a space"
    )
    async def manage_space(
        self,
        ctx,
        space: discord.Option(discord.TextChannel, "The space to manage"),
    ):
        await ctx.defer(ephemeral=True)

        space_db = Space()
        await space_db.async_init(space.id, ctx.guild.id)
        if not await space_db.check_exists(ctx, True):
            return

        perms = ctx.author.guild_permissions
        if ctx.author.id != space_db.owner_id and not (
            perms.administrator or perms.manage_channels
        ):
            await ctx.send_followup(
                embed=discord.Embed(
                    description="You don't have permission to manage this space.",
                    color=discord.Colour.red(),
                )
            )
            return

        view = ManageSpaceView(self, space, space_db, ctx.author.id)
        view._sync_buttons()
        msg = await ctx.send_followup(embed=_manage_embed(space, space_db), view=view)
        view.message = msg

    @space_group.command(
        name="restore", description="Restores default permissions for a space"
    )
    async def restore_space(
        self,
        ctx,
        space: discord.Option(
            discord.TextChannel, "The space to restore permissions for"
        ),
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

        owner = ctx.guild.get_member(space_db.owner_id)
        if owner is None:
            await ctx.send_followup(
                embed=discord.Embed(
                    description="Space owner is not in the server.",
                    color=discord.Colour.red(),
                )
            )
            return

        await space.edit(
            overwrites=space_overwrites(owner, ctx.guild, guild_db.whitelisted_role_ids)
        )

        await ctx.send_followup(
            embed=discord.Embed(
                description=f"Permissions for {space.mention} restored.",
                color=discord.Colour.green(),
            ),
        )

    @config_group.command(
        name="bump-on-message",
        description="Configures whether new messages in a space bump it",
    )
    async def config_bump_on_message(
        self,
        ctx,
        space: discord.Option(discord.TextChannel, "The space to configure"),
        value: discord.Option(bool, "The value to set"),
    ):
        await ctx.defer()

        space_db = Space()
        await space_db.async_init(space.id, space.guild.id)
        if await space_db.check_exists(ctx, True) and await space_db.check_owner(ctx):
            await space_db.set_bump(value)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"Bump on message for {space.mention} set to `{value}`.",
                    color=discord.Colour.green(),
                )
            )

    @config_group.command(
        name="bump-on-thread-message",
        description="Configures whether thread messages in a space bump it",
    )
    async def config_bump_on_thread_message(
        self,
        ctx,
        space: discord.Option(discord.TextChannel, "The space to configure"),
        value: discord.Option(bool, "The value to set"),
    ):
        await ctx.defer()

        space_db = Space()
        await space_db.async_init(space.id, space.guild.id)
        if await space_db.check_exists(ctx, True) and await space_db.check_owner(ctx):
            await space_db.set_bump_thread(value)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"Bump on thread message for {space.mention} set to `{value}`.",
                    color=discord.Colour.green(),
                )
            )


def setup(bot):
    bot.add_cog(Cockpit(bot))
