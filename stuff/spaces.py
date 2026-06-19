from datetime import timedelta

import discord

ARCHIVED_THREAD_LIMIT = 50
ACTIVITY_LOOKBACK = timedelta(days=30)


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


def is_dead(owner_member, activity_ts, now):
    """
    A space is dead if its owner is no longer in the server, or it has had no
    activity within ACTIVITY_LOOKBACK.
    """
    if owner_member is None:
        return True
    return activity_ts < now - ACTIVITY_LOOKBACK


def _msg_time(last_message_id):
    """Convert a snowflake message ID to its creation datetime, or None."""
    if last_message_id is None:
        return None
    return discord.utils.snowflake_time(last_message_id)


async def build_active_threads_by_parent(guild):
    """One API call → {parent_channel_id: [Thread, ...]} for all active threads."""
    mapping = {}
    for thread in await guild.active_threads():
        mapping.setdefault(thread.parent_id, []).append(thread)
    return mapping


async def latest_activity(channel, active_threads_by_parent):
    """
    Return the most recent activity datetime for a space channel.

    Considers (in descending priority):
      - channel's last message time (via snowflake, no fetch)
      - each active thread's last message time + creation time
      - each archived thread's last message time + creation time (bounded to 30 days)
      - channel.created_at as the fallback for completely empty channels
    """
    candidates = [channel.created_at]

    t = _msg_time(channel.last_message_id)
    if t:
        candidates.append(t)

    cutoff = discord.utils.utcnow() - ACTIVITY_LOOKBACK

    for thread in active_threads_by_parent.get(channel.id, ()):
        candidates.append(thread.created_at)
        tm = _msg_time(thread.last_message_id)
        if tm:
            candidates.append(tm)

    try:
        async for thread in channel.archived_threads(limit=ARCHIVED_THREAD_LIMIT):
            candidates.append(thread.created_at)
            tm = _msg_time(thread.last_message_id)
            if tm:
                candidates.append(tm)
            # archived_threads returns newest-first; exit early once past lookback window
            if thread.created_at < cutoff and (tm is None or tm < cutoff):
                break
    except (discord.Forbidden, discord.HTTPException):
        pass

    return max(candidates)


def pinned_in_order(category, pinned_ids):
    """
    Return pinned channels that actually exist in the category, sorted by their
    current position ascending (preserves whatever order the admin has set).
    """
    pinned_set = set(pinned_ids)
    return sorted(
        [ch for ch in category.text_channels if ch.id in pinned_set],
        key=lambda c: c.position,
    )


async def reposition_pinned(category, pinned_ids):
    """
    Move any pinned channels that have drifted back to the top of the category,
    preserving their current relative order. Only edits channels that need moving.
    """
    if not pinned_ids:
        return
    ordered = pinned_in_order(category, pinned_ids)
    if not ordered:
        return
    base = category.channels[0].position
    for idx, ch in enumerate(ordered):
        target = base + idx
        if ch.position != target:
            try:
                await ch.edit(position=target)
            except (discord.Forbidden, discord.HTTPException):
                pass


def bump_slot(category, pinned_ids):
    """
    Absolute position for the bumped space: directly below all pinned channels
    in the category, regardless of where they currently sit.
    """
    pinned_set = set(pinned_ids)
    pinned_count = sum(
        1 for ch in category.text_channels if ch.id in pinned_set
    )
    if not category.channels:
        return 0
    return category.channels[0].position + pinned_count
