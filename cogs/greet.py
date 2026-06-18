import random
from io import BytesIO

import discord
import requests
from discord.ext import commands

from stuff.db import autocomplete_greet_attachment, Guild


class Greet(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    greet_group = discord.SlashCommandGroup("greet", "Commands related to greeting")

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        guild_db = Guild()
        await guild_db.async_init(after.guild.id)
        if not (
            guild_db.exists and guild_db.greet_channel_id and guild_db.greet_attachments
        ):
            return

        if before.pending and not after.pending:
            greet_channel = after.guild.get_channel(guild_db.greet_channel_id)
            if greet_channel:
                greet_attachment = requests.get(
                    random.choice(guild_db.greet_attachments)
                )

                await greet_channel.send(
                    f"{guild_db.greet_message}",
                    file=discord.File(
                        BytesIO(greet_attachment.content), filename="greetings.mp4"
                    ),
                    allowed_mentions=discord.AllowedMentions.all(),
                )

    @greet_group.command(name="set-channel", description="Sets the greet channel")
    async def set_greet_channel(
        self,
        ctx,
        channel: discord.Option(
            discord.TextChannel, "The channel to set as the greet channel"
        ),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if await guild_db.check_exists(ctx):
            await guild_db.set_greet_channel(channel.id)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"Greetings channel set to {channel.mention}.",
                    color=discord.Colour.green(),
                )
            )

    @greet_group.command(name="set-message", description="Sets the greet message")
    async def set_greet_message(
        self,
        ctx,
        message: discord.Option(str, "The greet message"),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if await guild_db.check_exists(ctx):
            await guild_db.set_greet_message(message)
            await ctx.send_followup(
                embed=discord.Embed(
                    description="Greetings message set.",
                    color=discord.Colour.green(),
                )
            )

    @greet_group.command(
        name="add-attachment",
        description="Adds a greet attachment URL to the list of options to pick from randomly",
    )
    async def add_greet_attachment(
        self,
        ctx,
        url: discord.Option(str, "The greet attachment URL to add"),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if await guild_db.check_exists(ctx):
            await guild_db.add_to_greet_attachments(url)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"Greetings [attachment]({url}) added.",
                    color=discord.Colour.green(),
                )
            )

    @greet_group.command(
        name="remove-attachment",
        description="Removes a greet attachment URL from the list of options to pick from randomly",
    )
    async def remove_greet_attachment(
        self,
        ctx,
        url: discord.Option(
            str,
            "The greet attachment URL to remove",
            autocomplete=autocomplete_greet_attachment,
        ),
    ):
        await ctx.defer()

        guild_db = Guild()
        await guild_db.async_init(ctx.guild.id)
        if await guild_db.check_exists(ctx) and await guild_db.check_greet_attachment(
            ctx, url
        ):
            await guild_db.remove_from_greet_attachments(url)
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"Greetings [attachment]({url}) removed.",
                    color=discord.Colour.green(),
                )
            )


def setup(bot):
    bot.add_cog(Greet(bot))
