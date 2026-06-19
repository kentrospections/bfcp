import time
from datetime import timedelta

import discord

ARCHIVED_THREAD_LIMIT = 50
ACTIVITY_LOOKBACK = timedelta(days=30)


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


async def throttled_progress(ctx, current, total, channel_name, last_edit_ref):
    """
    Edit the deferred interaction response with sort progress.
    `last_edit_ref` is a single-element list [float] used as a mutable reference
    to track the last edit timestamp.
    Rate-limits edits to ~1 per 1.5 seconds, always emitting first and last.
    """
    now = time.monotonic()
    if current == 1 or current == total or now - last_edit_ref[0] > 1.5:
        last_edit_ref[0] = now
        try:
            await ctx.interaction.edit_original_response(
                embed=discord.Embed(
                    description=f"⏳ Processing {current}/{total}  ·  {channel_name}",
                )
            )
        except discord.HTTPException:
            pass
