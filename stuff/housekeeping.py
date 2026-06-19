import discord

from stuff.db import Space, all_guild_spaces
from stuff.spaces import (
    build_active_threads_by_parent,
    is_dead,
    latest_activity,
    overwrites_match,
    pinned_in_order,
    space_overwrites,
)


async def _sort_category(category, pinned_ids, channels, activity):
    """Sort the given space channels within a category by activity desc, under pinned."""
    if category is None or not channels:
        return 0
    ordered_pinned = pinned_in_order(category, pinned_ids)
    ordered_spaces = sorted(channels, key=lambda c: activity[c.id], reverse=True)
    ordered_all = ordered_pinned + ordered_spaces

    if not category.channels:
        return 0

    base = category.channels[0].position
    moved = 0
    for idx, ch in enumerate(ordered_all):
        target = base + idx
        if ch.position != target:
            try:
                await ch.edit(position=target)
                moved += 1
            except (discord.Forbidden, discord.HTTPException):
                pass
    return moved


async def run_housekeeping(bot, guild, guild_db, progress=None):
    """
    Non-destructive maintenance pass shared by /guild sort-spaces and the daily task.

    `progress` is an optional async callback (stage: str, current: int, total: int).
    Returns a summary dict of counts.
    """
    summary = {
        "purged": 0,
        "archived": 0,
        "revived": 0,
        "sorted": 0,
        "roles_fixed": 0,
        "perms_fixed": 0,
    }

    main_category = guild.get_channel(guild_db.space_category_id)
    dead_category = (
        guild.get_channel(guild_db.dead_space_category_id)
        if guild_db.dead_space_category_id
        else None
    )

    rows = await all_guild_spaces(guild.id)
    if not rows:
        return summary

    # 1. Purge orphaned rows (channel no longer exists).
    orphaned = [sid for sid, _ in rows if guild.get_channel(sid) is None]
    if orphaned:
        await Space.delete_many(orphaned)
        summary["purged"] = len(orphaned)

    live = [(sid, oid) for sid, oid in rows if guild.get_channel(sid) is not None]
    if not live:
        return summary

    active_map = await build_active_threads_by_parent(guild)
    now = discord.utils.utcnow()

    # 2. Compute activity + ownership; archive/revive as needed.
    activity = {}
    total = len(live)
    for i, (sid, oid) in enumerate(live, start=1):
        channel = guild.get_channel(sid)
        ts = await latest_activity(channel, active_map)
        activity[sid] = ts
        owner_member = guild.get_member(oid)

        if dead_category is not None:
            dead = is_dead(owner_member, ts, now)
            if dead and channel.category_id != dead_category.id:
                try:
                    await channel.edit(category=dead_category)
                    summary["archived"] += 1
                except (discord.Forbidden, discord.HTTPException):
                    pass
            elif not dead and channel.category_id == dead_category.id and main_category:
                try:
                    await channel.edit(category=main_category)
                    summary["revived"] += 1
                except (discord.Forbidden, discord.HTTPException):
                    pass

        if progress:
            await progress("Scanning activity", i, total)

    # 3. Sort both categories.
    main_spaces = [
        guild.get_channel(sid)
        for sid, _ in live
        if main_category and guild.get_channel(sid).category_id == main_category.id
    ]
    dead_spaces = [
        guild.get_channel(sid)
        for sid, _ in live
        if dead_category and guild.get_channel(sid).category_id == dead_category.id
    ]
    summary["sorted"] += await _sort_category(
        main_category, guild_db.pinned_channel_ids, main_spaces, activity
    )
    summary["sorted"] += await _sort_category(
        dead_category, guild_db.dead_pinned_channel_ids, dead_spaces, activity
    )

    # 4. Repair owner roles.
    if guild_db.space_owner_role_id:
        role = guild.get_role(guild_db.space_owner_role_id)
        if role:
            owner_ids = {oid for _, oid in live}
            for member in guild.members:
                has_role = role in member.roles
                should_have = member.id in owner_ids
                if should_have and not has_role:
                    try:
                        await member.add_roles(role)
                        summary["roles_fixed"] += 1
                    except discord.Forbidden:
                        pass
                elif has_role and not should_have:
                    try:
                        await member.remove_roles(role)
                        summary["roles_fixed"] += 1
                    except discord.Forbidden:
                        pass

    # 5. Repair permissions (re-apply only where they have drifted).
    for i, (sid, oid) in enumerate(live, start=1):
        channel = guild.get_channel(sid)
        owner = guild.get_member(oid)
        if owner is None:
            continue
        expected = space_overwrites(owner, guild, guild_db.whitelisted_role_ids)
        if not overwrites_match(channel.overwrites, expected):
            try:
                await channel.edit(overwrites=expected)
                summary["perms_fixed"] += 1
            except (discord.Forbidden, discord.HTTPException):
                pass
        if progress:
            await progress("Repairing permissions", i, total)

    return summary
