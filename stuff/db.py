import json
import discord

import aiosqlite
from loguru import logger


# Initialize database
async def initialize_db():
    async with aiosqlite.connect("data/database.db") as db:
        await db.execute(
            """
                CREATE TABLE IF NOT EXISTS spaces (
                    space_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    owner_id INTEGER,
                    bump_on_message INTEGER,
                    bump_on_thread_message INTEGER
                );
            """
        )
        await db.execute(
            """
                CREATE TABLE IF NOT EXISTS guilds (
                    guild_id INTEGER PRIMARY KEY,
                    greet_channel_id INTEGER,
                    greet_message TEXT,
                    greet_attachments TEXT,
                    space_category_id INTEGER,
                    space_owner_role_id INTEGER,
                    max_spaces_per_owner INTEGER,
                    pinned_channel_ids TEXT,
                    whitelisted_role_ids TEXT,
                    bump_on_message INTEGER,
                    bump_on_thread_message INTEGER,
                    dead_space_category_id INTEGER,
                    dead_pinned_channel_ids TEXT DEFAULT '[]',
                    auto_housekeeping INTEGER DEFAULT 1,
                    housekeeping_hour INTEGER DEFAULT 0
                );
            """
        )

        # Idempotent migration: add any columns missing from older databases.
        async with db.execute("PRAGMA table_info(guilds)") as cursor:
            existing_columns = {row[1] for row in await cursor.fetchall()}
        migrations = {
            "dead_space_category_id": "INTEGER",
            "dead_pinned_channel_ids": "TEXT DEFAULT '[]'",
            "auto_housekeeping": "INTEGER DEFAULT 1",
            "housekeeping_hour": "INTEGER DEFAULT 0",
        }
        for column, definition in migrations.items():
            if column not in existing_columns:
                await db.execute(
                    f"ALTER TABLE guilds ADD COLUMN {column} {definition}"
                )
                logger.info(f"Migrated guilds table: added column {column}.")

        await db.commit()


# Add guild to database
async def initialize_guild(guild):
    async with aiosqlite.connect("data/database.db") as db:
        async with db.execute(
            "SELECT * FROM guilds WHERE guild_id = ?", (guild.id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                await db.execute(
                    """
                        INSERT INTO guilds (
                            guild_id,
                            greet_channel_id,
                            greet_message,
                            greet_attachments,
                            space_category_id,
                            space_owner_role_id,
                            max_spaces_per_owner,
                            pinned_channel_ids,
                            whitelisted_role_ids,
                            bump_on_message,
                            bump_on_thread_message,
                            dead_space_category_id,
                            dead_pinned_channel_ids,
                            auto_housekeeping,
                            housekeeping_hour
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild.id,
                        None,
                        "",
                        json.dumps([]),
                        None,
                        None,
                        1,
                        json.dumps([]),
                        json.dumps([]),
                        True,
                        True,
                        None,
                        json.dumps([]),
                        True,
                        0,
                    ),
                )
                await db.commit()
                logger.info(f"Added {guild.name} (ID {guild.id}) to database.")


# Get all (space_id, owner_id) pairs for a guild
async def all_guild_spaces(guild_id):
    async with aiosqlite.connect("data/database.db") as db:
        async with db.execute(
            "SELECT space_id, owner_id FROM spaces WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            return await cursor.fetchall()


# Get greetings attachments for autocomplete
async def autocomplete_greet_attachment(ctx: discord.commands.AutocompleteContext):
    guild_db = Guild()
    await guild_db.async_init(ctx.interaction.guild.id)
    return guild_db.greet_attachments


class Guild:
    def __init__(self):
        self.guild_id = None
        self.greet_channel_id = None
        self.greet_message = None
        self.greet_attachments = []
        self.space_category_id = None
        self.space_owner_role_id = None
        self.max_spaces_per_owner = None
        self.pinned_channel_ids = []
        self.whitelisted_role_ids = []
        self.bump_on_message = None
        self.bump_on_thread_message = None
        self.dead_space_category_id = None
        self.dead_pinned_channel_ids = []
        self.auto_housekeeping = None
        self.housekeeping_hour = None
        self.exists = False

    async def async_init(self, guild_id):
        self.guild_id = guild_id
        async with aiosqlite.connect("data/database.db") as db:
            async with db.execute(
                "SELECT * FROM guilds WHERE guild_id = ?",
                (guild_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row is not None:
                    self.greet_channel_id = row[1]
                    self.greet_message = row[2]
                    self.greet_attachments = json.loads(row[3])
                    self.space_category_id = row[4]
                    self.space_owner_role_id = row[5]
                    self.max_spaces_per_owner = row[6]
                    self.pinned_channel_ids = json.loads(row[7])
                    self.whitelisted_role_ids = json.loads(row[8])
                    self.bump_on_message = row[9]
                    self.bump_on_thread_message = row[10]
                    self.dead_space_category_id = row[11]
                    self.dead_pinned_channel_ids = json.loads(row[12] or "[]")
                    self.auto_housekeeping = row[13]
                    self.housekeeping_hour = row[14]
                    self.exists = True

    async def set_greet_channel(self, channel_id):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE guilds SET greet_channel_id = ? WHERE guild_id = ?",
                (channel_id, self.guild_id),
            )
            await db.commit()
            self.greet_channel_id = channel_id

    async def set_greet_message(self, message):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE guilds SET greet_message = ? WHERE guild_id = ?",
                (message, self.guild_id),
            )
            await db.commit()
            self.greet_message = message

    async def add_to_greet_attachments(self, url):
        if url not in self.greet_attachments:
            self.greet_attachments.append(url)
            async with aiosqlite.connect("data/database.db") as db:
                await db.execute(
                    "UPDATE guilds SET greet_attachments = ? WHERE guild_id = ?",
                    (json.dumps(self.greet_attachments), self.guild_id),
                )
                await db.commit()

    async def remove_from_greet_attachments(self, url):
        if url in self.greet_attachments:
            self.greet_attachments.remove(url)
            async with aiosqlite.connect("data/database.db") as db:
                await db.execute(
                    "UPDATE guilds SET greet_attachments = ? WHERE guild_id = ?",
                    (json.dumps(self.greet_attachments), self.guild_id),
                )
                await db.commit()

    async def set_category(self, category_id):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE guilds SET space_category_id = ? WHERE guild_id = ?",
                (category_id, self.guild_id),
            )
            await db.commit()
            self.space_category_id = category_id

    async def set_dead_category(self, category_id):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE guilds SET dead_space_category_id = ? WHERE guild_id = ?",
                (category_id, self.guild_id),
            )
            await db.commit()
            self.dead_space_category_id = category_id

    async def add_to_dead_pinned(self, channel_id):
        if channel_id not in self.dead_pinned_channel_ids:
            self.dead_pinned_channel_ids.append(channel_id)
            async with aiosqlite.connect("data/database.db") as db:
                await db.execute(
                    "UPDATE guilds SET dead_pinned_channel_ids = ? WHERE guild_id = ?",
                    (json.dumps(self.dead_pinned_channel_ids), self.guild_id),
                )
                await db.commit()

    async def remove_from_dead_pinned(self, channel_id):
        if channel_id in self.dead_pinned_channel_ids:
            self.dead_pinned_channel_ids.remove(channel_id)
            async with aiosqlite.connect("data/database.db") as db:
                await db.execute(
                    "UPDATE guilds SET dead_pinned_channel_ids = ? WHERE guild_id = ?",
                    (json.dumps(self.dead_pinned_channel_ids), self.guild_id),
                )
                await db.commit()

    async def set_auto_housekeeping(self, value):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE guilds SET auto_housekeeping = ? WHERE guild_id = ?",
                (value, self.guild_id),
            )
            await db.commit()
            self.auto_housekeeping = value

    async def set_housekeeping_hour(self, value):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE guilds SET housekeeping_hour = ? WHERE guild_id = ?",
                (value, self.guild_id),
            )
            await db.commit()
            self.housekeeping_hour = value

    async def set_owner_role(self, role_id):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE guilds SET space_owner_role_id = ? WHERE guild_id = ?",
                (role_id, self.guild_id),
            )
            await db.commit()
            self.space_owner_role_id = role_id

    async def set_max_spaces(self, value):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE guilds SET max_spaces_per_owner = ? WHERE guild_id = ?",
                (value, self.guild_id),
            )
            await db.commit()
            self.max_spaces_per_owner = value

    async def add_to_pinned(self, channel_id):
        if channel_id not in self.pinned_channel_ids:
            self.pinned_channel_ids.append(channel_id)
            async with aiosqlite.connect("data/database.db") as db:
                await db.execute(
                    "UPDATE guilds SET pinned_channel_ids = ? WHERE guild_id = ?",
                    (json.dumps(self.pinned_channel_ids), self.guild_id),
                )
                await db.commit()

    async def remove_from_pinned(self, channel_id):
        if channel_id in self.pinned_channel_ids:
            self.pinned_channel_ids.remove(channel_id)
            async with aiosqlite.connect("data/database.db") as db:
                await db.execute(
                    "UPDATE guilds SET pinned_channel_ids = ? WHERE guild_id = ?",
                    (json.dumps(self.pinned_channel_ids), self.guild_id),
                )
                await db.commit()

    async def add_to_whitelist(self, role_id):
        if role_id not in self.whitelisted_role_ids:
            self.whitelisted_role_ids.append(role_id)
            async with aiosqlite.connect("data/database.db") as db:
                await db.execute(
                    "UPDATE guilds SET whitelisted_role_ids = ? WHERE guild_id = ?",
                    (json.dumps(self.whitelisted_role_ids), self.guild_id),
                )
                await db.commit()

    async def remove_from_whitelist(self, role_id):
        if role_id in self.whitelisted_role_ids:
            self.whitelisted_role_ids.remove(role_id)
            async with aiosqlite.connect("data/database.db") as db:
                await db.execute(
                    "UPDATE guilds SET whitelisted_role_ids = ? WHERE guild_id = ?",
                    (json.dumps(self.whitelisted_role_ids), self.guild_id),
                )
                await db.commit()

    async def set_bump(self, value):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE guilds SET bump_on_message = ? WHERE guild_id = ?",
                (value, self.guild_id),
            )
            await db.commit()
            self.bump_on_message = value

    async def set_bump_thread(self, value):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE guilds SET bump_on_thread_message = ? WHERE guild_id = ?",
                (value, self.guild_id),
            )
            await db.commit()
            self.bump_on_thread_message = value

    async def check_exists(self, ctx):
        if self.exists:
            return True
        else:
            await ctx.send_followup(
                embed=discord.Embed(
                    description="This server is not in the database.",
                )
            )
            return False

    async def check_category(self, ctx):
        if self.exists and self.space_category_id:
            return True
        else:
            await ctx.send_followup(
                embed=discord.Embed(
                    description="Category for spaces not set for this server.",
                )
            )
            return False

    async def check_greet_attachment(self, ctx, url):
        if self.exists and url in self.greet_attachments:
            return True
        else:
            await ctx.send_followup(
                embed=discord.Embed(
                    description="Greetings attachment URL not in the database.",
                )
            )
            return False


class Space:
    def __init__(self):
        self.space_id = None
        self.guild_id = None
        self.owner_id = None
        self.bump_on_message = None
        self.bump_on_thread_message = None
        self.exists = False

    @staticmethod
    async def add(data):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "INSERT INTO spaces VALUES (?, ?, ?, ?, ?)",
                data,
            )
            await db.commit()

    @staticmethod
    async def delete_many(space_ids):
        if not space_ids:
            return
        async with aiosqlite.connect("data/database.db") as db:
            params = ", ".join("?" * len(space_ids))
            await db.execute(
                f"DELETE FROM spaces WHERE space_id IN ({params})",
                tuple(space_ids),
            )
            await db.commit()

    async def async_init(self, space_id, guild_id):
        self.space_id = space_id
        self.guild_id = guild_id
        async with aiosqlite.connect("data/database.db") as db:
            async with db.execute(
                "SELECT * FROM spaces WHERE guild_id = ? AND space_id = ?",
                (guild_id, space_id),
            ) as cursor:
                row = await cursor.fetchone()
                if row is not None:
                    self.space_id = row[0]
                    self.guild_id = row[1]
                    self.owner_id = row[2]
                    self.bump_on_message = row[3]
                    self.bump_on_thread_message = row[4]
                    self.exists = True

    async def set_owner(self, owner_id):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE spaces SET owner_id = ? WHERE space_id = ?",
                (owner_id, self.space_id),
            )
            await db.commit()
            self.owner_id = owner_id

    async def set_bump(self, value):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE spaces SET bump_on_message = ? WHERE space_id = ?",
                (value, self.space_id),
            )
            await db.commit()
            self.bump_on_message = value

    async def set_bump_thread(self, value):
        async with aiosqlite.connect("data/database.db") as db:
            await db.execute(
                "UPDATE spaces SET bump_on_thread_message = ? WHERE space_id = ?",
                (value, self.space_id),
            )
            await db.commit()
            self.bump_on_thread_message = value

    async def check_exists(self, ctx, should_exist):
        if self.exists:
            if should_exist:
                return True
            else:
                await ctx.send_followup(
                    embed=discord.Embed(
                        description=f"<#{self.space_id}> already exists in the database.",
                    )
                )
                return False
        else:
            if should_exist:
                await ctx.send_followup(
                    embed=discord.Embed(
                        description=f"<#{self.space_id}> is not a space.",
                    )
                )
                return False
            else:
                return True

    async def check_owner(self, ctx):
        if self.exists and ctx.author.id == self.owner_id:
            return True
        else:
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"You do not own <#{self.space_id}>.",
                    color=discord.Colour.red(),
                )
            )
            return False


class Owner:
    def __init__(self):
        self.guild_id = None
        self.owner_id = None
        self.spaces = []
        self.exists = False

    async def async_init(self, guild_id, owner_id):
        self.guild_id = guild_id
        self.owner_id = owner_id
        async with aiosqlite.connect("data/database.db") as db:
            async with db.execute(
                "SELECT * FROM spaces WHERE guild_id = ? AND owner_id = ?",
                (guild_id, owner_id),
            ) as cursor:
                rows = await cursor.fetchall()
                if rows:
                    for row in rows:
                        self.spaces.append(
                            {
                                "space_id": row[0],
                                "bump_on_message": row[3],
                                "bump_on_thread_message": row[4],
                            }
                        )
                    self.exists = True

    async def check_max_spaces(self, ctx, max_spaces_per_owner):
        if len(self.spaces) < max_spaces_per_owner:
            return True
        else:
            await ctx.send_followup(
                embed=discord.Embed(
                    description=f"<@{self.owner_id}> has reached the maximum amount of spaces for this server.",
                )
            )
            return False
