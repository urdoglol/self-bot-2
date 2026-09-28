# cogs/server.py | serverinfo, members, channels, roles, ban/kick/mute,
#                  setnick, topic, slowmode, createchannel/role, icon/banner/name
#
# ROOT CAUSE of "not in a server":
#   modifyself's Message class sets guild_id (plain int) but never sets
#   message.guild — there is no guild property on Message.  Additionally,
#   chunk_guilds_at_startup=False means the guild cache (_state._guilds) is
#   often empty even for active servers.  The old _guild() fallback chain
#   therefore always returned None.
#
# FIX:
#   serverinfo now fetches guild data directly from the REST API using
#   message.guild_id so it works regardless of whether the guild is cached.
#   Commands that need a live Guild object (ban/kick/role/channel ops) still
#   try the cache first and fall back to REST-sourced data for read operations.

import aiohttp
from datetime import timedelta
import modifyself_shim as discord
from . import state as S


# ── REST helpers ─────────────────────────────────────────────────────────────

def _headers():
    return {"Authorization": S.TOKEN, "User-Agent": S.USER_AGENT,
            "Content-Type": "application/json"}


def _sess():
    fn = getattr(S, "_get_session", None)
    if callable(fn):
        try: return fn()
        except Exception: pass
    return None


async def _rest(method: str, url: str, **kwargs):
    """Perform a REST call, returns (status, body_dict)."""
    sess = _sess()
    h = _headers()
    try:
        if sess:
            async with sess.request(method, url, headers=h, **kwargs) as r:
                try: body = await r.json(content_type=None)
                except Exception: body = {}
                return r.status, body
        async with aiohttp.ClientSession() as s:
            async with s.request(method, url, headers=h, **kwargs) as r:
                try: body = await r.json(content_type=None)
                except Exception: body = {}
                return r.status, body
    except Exception as e:
        print(f"[server] REST {method} {url} error: {e}")
        return 0, {}


async def _fetch_guild(guild_id: int):
    """Fetch full guild data from REST API."""
    _, d = await _rest("GET",
        f"https://discord.com/api/v9/guilds/{guild_id}?with_counts=true")
    return d or {}


async def _fetch_guild_channels(guild_id: int):
    _, d = await _rest("GET",
        f"https://discord.com/api/v9/guilds/{guild_id}/channels")
    return d if isinstance(d, list) else []


async def _fetch_guild_roles(guild_id: int):
    _, d = await _rest("GET",
        f"https://discord.com/api/v9/guilds/{guild_id}/roles")
    return d if isinstance(d, list) else []


def _get_guild_id(message):
    """
    Extract the guild_id from the message as a plain int.
    modifyself sets message.guild_id = int(...) directly.
    """
    gid = (getattr(message, "guild_id", None)
           or getattr(message, "_guild_id", None))
    if gid:
        try: return int(gid)
        except Exception: pass
    # Also try via channel
    try:
        ch = getattr(message, "channel", None)
        if ch:
            gid = getattr(ch, "guild_id", None)
            if gid: return int(gid)
    except Exception:
        pass
    return None


def _get_guild_obj(message):
    """
    Try to get a live Guild object from cache.
    Returns None if not cached (common with chunk_guilds_at_startup=False).
    """
    # 1. message.guild (not set by modifyself but try anyway)
    g = getattr(message, "guild", None)
    if g is not None:
        return g

    # 2. channel.guild — channel does have a guild property via _state._guilds
    try:
        ch = getattr(message, "channel", None)
        if ch:
            g = getattr(ch, "guild", None)
            if g is not None:
                return g
    except Exception:
        pass

    # 3. Walk _state._guilds with int key comparison
    gid = _get_guild_id(message)
    if not gid:
        return None

    cl = S.CLIENT
    if not cl:
        return None

    for attr_path in ("_state._guilds", "_connection._guilds", "_guilds"):
        try:
            obj = cl
            for part in attr_path.split("."):
                obj = getattr(obj, part, None)
                if obj is None: break
            if obj and isinstance(obj, dict):
                # Keys are ints in modifyself (set from int(data["guild_id"]))
                g = obj.get(gid) or obj.get(str(gid))
                if g is not None:
                    return g
                # Fallback: iterate in case keys are Snowflakes
                for k, v in obj.items():
                    try:
                        if int(k) == gid:
                            return v
                    except Exception:
                        pass
        except Exception:
            pass

    return None


def _snowflake_time(snowflake_id: int):
    """Convert a Discord snowflake to a human-readable date."""
    try:
        ts = ((snowflake_id >> 22) + 1420070400000) / 1000
        from datetime import datetime
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return "?"


# ── Cog ──────────────────────────────────────────────────────────────────────

class ServerCog:
    COMMANDS = {
        "serverinfo", "members", "channels", "roles",
        "ban", "kick", "mute", "unmute",
        "createrole", "delrole",
        "createchannel", "deletechannel",
        "setnick", "topic", "slowmode",
        "servericon", "serverbanner", "servername",
    }

    async def handle(self, message, cmd, args):

        # ── serverinfo ────────────────────────────────────────────────────────
        if cmd == "serverinfo":
            gid = _get_guild_id(message)
            if not gid:
                return await message.edit(content=S.ui_err(
                    "could not resolve server ID from this message"))

            # Always fetch via REST — reliable regardless of cache state
            await message.edit(content=S.ui_info("fetching server info…"))
            d = await _fetch_guild(gid)
            if not d or "id" not in d:
                return await message.edit(content=S.ui_err(
                    f"failed to fetch guild {gid} from API"))

            name        = d.get("name", "?")
            owner_id    = d.get("owner_id", "?")
            member_count= d.get("approximate_member_count") or d.get("member_count", "?")
            online_count= d.get("approximate_presence_count", "?")
            desc        = d.get("description") or ""
            boost_level = d.get("premium_tier", 0)
            boosts      = d.get("premium_subscription_count", 0)
            features    = len(d.get("features", []))
            roles_raw   = await _fetch_guild_roles(gid)
            channels_raw= await _fetch_guild_channels(gid)
            emojis_count= len(d.get("emojis", []))
            created     = _snowflake_time(int(d["id"]))

            rows = [
                f"  {S.DIM}id{S.RESET}          {d['id']}",
                f"  {S.DIM}name{S.RESET}        {name}",
                f"  {S.DIM}owner{S.RESET}       <@{owner_id}> ({owner_id})",
                f"  {S.DIM}members{S.RESET}     {member_count}"
                + (f"  ({online_count} online)" if online_count != "?" else ""),
                f"  {S.DIM}channels{S.RESET}    {len(channels_raw)}",
                f"  {S.DIM}roles{S.RESET}       {len(roles_raw)}",
                f"  {S.DIM}emojis{S.RESET}      {emojis_count}",
                f"  {S.DIM}boost tier{S.RESET}  {boost_level}  ({boosts} boosts)",
                f"  {S.DIM}features{S.RESET}    {features}",
                f"  {S.DIM}created{S.RESET}     {created}",
            ]
            if desc:
                rows.append(f"  {S.DIM}desc{S.RESET}        {desc[:80]}")
            await message.edit(content=S.ui_box(name, rows))

        # ── members ───────────────────────────────────────────────────────────
        elif cmd == "members":
            gid = _get_guild_id(message)
            if not gid:
                return await message.edit(content=S.ui_err("could not resolve server ID"))
            n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
            # Try cache first
            g = _get_guild_obj(message)
            member_list = list(getattr(g, "members", None) or []) if g else []
            if member_list:
                rows = [
                    f"  {S.GREY}•{S.RESET} {getattr(m,'display_name',str(m))}  "
                    f"{S.DIM}({m.id}){S.RESET}"
                    for m in member_list[:n]
                ]
                return await message.edit(content=S._paginate("members", "cached", rows))
            # REST fallback
            _, d = await _rest("GET",
                f"https://discord.com/api/v9/guilds/{gid}/members",
                params={"limit": min(n, 100)})
            if isinstance(d, list):
                rows = [
                    f"  {S.GREY}•{S.RESET} "
                    f"{m.get('nick') or m.get('user',{}).get('username','?')}  "
                    f"{S.DIM}({m.get('user',{}).get('id','?')}){S.RESET}"
                    for m in d[:n]
                ]
                return await message.edit(content=S._paginate("members", "REST", rows))
            await message.edit(content=S.ui_err("could not fetch members"))

        # ── channels ──────────────────────────────────────────────────────────
        elif cmd == "channels":
            gid = _get_guild_id(message)
            if not gid:
                return await message.edit(content=S.ui_err("could not resolve server ID"))
            channels = await _fetch_guild_channels(gid)
            rows = [
                f"  {S.GREY}•{S.RESET} #{c.get('name','?')}  "
                f"{S.DIM}({c.get('id','?')}){S.RESET}"
                for c in sorted(channels, key=lambda c: c.get("position", 0))
            ]
            guild_d = await _fetch_guild(gid)
            await message.edit(content=S._paginate("channels", guild_d.get("name","?"), rows))

        # ── roles ─────────────────────────────────────────────────────────────
        elif cmd == "roles":
            gid = _get_guild_id(message)
            if not gid:
                return await message.edit(content=S.ui_err("could not resolve server ID"))
            roles = await _fetch_guild_roles(gid)
            rows = [
                f"  {S.GREY}•{S.RESET} {r.get('name','?')}  "
                f"{S.DIM}({r.get('id','?')}){S.RESET}"
                for r in sorted(roles, key=lambda r: -int(r.get("position",0)))
            ]
            guild_d = await _fetch_guild(gid)
            await message.edit(content=S._paginate("roles", guild_d.get("name","?"), rows))

        # ── ban / kick ────────────────────────────────────────────────────────
        elif cmd in ("ban", "kick"):
            gid = _get_guild_id(message)
            if not gid or len(args) < 2 or not args[1].isdigit():
                return await message.edit(
                    content=S.ui_err(f"usage: {cmd} <user_id> [reason]"))
            uid    = int(args[1])
            reason = " ".join(args[2:]) or "no reason"
            try:
                if cmd == "ban":
                    s, _ = await _rest("PUT",
                        f"https://discord.com/api/v9/guilds/{gid}/bans/{uid}",
                        json={"delete_message_days": 0, "reason": reason})
                    ok = s in (200, 204)
                else:
                    s, _ = await _rest("DELETE",
                        f"https://discord.com/api/v9/guilds/{gid}/members/{uid}",
                        json={"reason": reason})
                    ok = s in (200, 204)
                await message.edit(content=(
                    S.ui_ok(f"{cmd} → <@{uid}>")
                    if ok else S.ui_err(f"{cmd} failed (HTTP {s})")))
            except Exception as e:
                await message.edit(content=S.ui_err(str(e)))

        # ── mute / unmute ─────────────────────────────────────────────────────
        elif cmd in ("mute", "unmute"):
            gid = _get_guild_id(message)
            if not gid or len(args) < 2 or not args[1].isdigit():
                return await message.edit(
                    content=S.ui_err(f"usage: {cmd} <user_id>"))
            uid  = int(args[1])
            from datetime import datetime, timezone
            if cmd == "mute":
                until = (datetime.now(timezone.utc) +
                         timedelta(minutes=10)).isoformat()
                s, _ = await _rest("PATCH",
                    f"https://discord.com/api/v9/guilds/{gid}/members/{uid}",
                    json={"communication_disabled_until": until})
            else:
                s, _ = await _rest("PATCH",
                    f"https://discord.com/api/v9/guilds/{gid}/members/{uid}",
                    json={"communication_disabled_until": None})
            await message.edit(content=(
                S.ui_ok(f"{cmd} → <@{uid}>") if s in (200,204)
                else S.ui_err(f"{cmd} failed (HTTP {s})")))

        # ── createrole ────────────────────────────────────────────────────────
        elif cmd == "createrole":
            gid = _get_guild_id(message)
            if not gid or len(args) < 2:
                return await message.edit(
                    content=S.ui_err("usage: createrole <name>"))
            name = " ".join(args[1:])
            g = _get_guild_obj(message)
            try:
                if g:
                    r = await g.create_role(name=name)
                    await message.edit(content=S.ui_ok(f"role created: {r.name} ({r.id})"))
                else:
                    s, d = await _rest("POST",
                        f"https://discord.com/api/v9/guilds/{gid}/roles",
                        json={"name": name})
                    if s in (200, 201):
                        await message.edit(content=S.ui_ok(
                            f"role created: {d.get('name','?')} ({d.get('id','?')})"))
                    else:
                        await message.edit(content=S.ui_err(f"failed (HTTP {s})"))
            except Exception as e:
                await message.edit(content=S.ui_err(str(e)))

        # ── delrole ───────────────────────────────────────────────────────────
        elif cmd == "delrole":
            gid = _get_guild_id(message)
            if not gid or len(args) < 2 or not args[1].isdigit():
                return await message.edit(
                    content=S.ui_err("usage: delrole <role_id>"))
            rid = int(args[1])
            try:
                g = _get_guild_obj(message)
                if g:
                    role = g.get_role(rid)
                    if role:
                        await role.delete()
                        return await message.edit(content=S.ui_ok(f"role `{role.name}` deleted"))
                # REST fallback
                s, _ = await _rest("DELETE",
                    f"https://discord.com/api/v9/guilds/{gid}/roles/{rid}")
                await message.edit(content=(
                    S.ui_ok("role deleted") if s in (200,204)
                    else S.ui_err(f"failed (HTTP {s})")))
            except Exception as e:
                await message.edit(content=S.ui_err(str(e)))

        # ── createchannel ─────────────────────────────────────────────────────
        elif cmd == "createchannel":
            gid = _get_guild_id(message)
            if not gid or len(args) < 2:
                return await message.edit(
                    content=S.ui_err("usage: createchannel <name>"))
            name = " ".join(args[1:])
            try:
                g = _get_guild_obj(message)
                if g:
                    ch = await g.create_text_channel(name=name)
                    return await message.edit(content=S.ui_ok(
                        f"channel created: #{ch.name} ({ch.id})"))
                s, d = await _rest("POST",
                    f"https://discord.com/api/v9/guilds/{gid}/channels",
                    json={"name": name, "type": 0})
                if s in (200,201):
                    await message.edit(content=S.ui_ok(
                        f"channel created: #{d.get('name','?')} ({d.get('id','?')})"))
                else:
                    await message.edit(content=S.ui_err(f"failed (HTTP {s})"))
            except Exception as e:
                await message.edit(content=S.ui_err(str(e)))

        # ── deletechannel ─────────────────────────────────────────────────────
        elif cmd == "deletechannel":
            if len(args) < 2 or not args[1].isdigit():
                return await message.edit(
                    content=S.ui_err("usage: deletechannel <channel_id>"))
            cid = int(args[1])
            try:
                cl = S.CLIENT
                ch = cl.get_channel(cid) if cl else None
                if ch:
                    await ch.delete()
                    return await message.edit(content=S.ui_ok("channel deleted"))
                s, _ = await _rest("DELETE",
                    f"https://discord.com/api/v9/channels/{cid}")
                await message.edit(content=(
                    S.ui_ok("channel deleted") if s in (200,204)
                    else S.ui_err(f"failed (HTTP {s})")))
            except Exception as e:
                await message.edit(content=S.ui_err(str(e)))

        # ── setnick ───────────────────────────────────────────────────────────
        elif cmd == "setnick":
            gid = _get_guild_id(message)
            if not gid or len(args) < 3:
                return await message.edit(
                    content=S.ui_err("usage: setnick <user_id> <nick>"))
            uid  = int(args[1]) if args[1].isdigit() else None
            nick = " ".join(args[2:])
            if uid is None:
                return await message.edit(content=S.ui_err("invalid user_id"))
            try:
                g = _get_guild_obj(message)
                if g:
                    m = g.get_member(uid)
                    if m:
                        await m.edit(nick=nick)
                        return await message.edit(content=S.ui_ok(f"nick set: {nick}"))
                s, _ = await _rest("PATCH",
                    f"https://discord.com/api/v9/guilds/{gid}/members/{uid}",
                    json={"nick": nick})
                await message.edit(content=(
                    S.ui_ok(f"nick set: {nick}") if s in (200,204)
                    else S.ui_err(f"failed (HTTP {s})")))
            except Exception as e:
                await message.edit(content=S.ui_err(str(e)))

        # ── topic ─────────────────────────────────────────────────────────────
        elif cmd == "topic":
            if len(args) < 2:
                return await message.edit(content=S.ui_err("usage: topic <text>"))
            topic = " ".join(args[1:])
            try:
                ch = getattr(message, "channel", None)
                if ch:
                    await ch.edit(topic=topic)
                else:
                    cid = int(message.channel_id)
                    await _rest("PATCH",
                        f"https://discord.com/api/v9/channels/{cid}",
                        json={"topic": topic})
                await message.edit(content=S.ui_ok("topic set"))
            except Exception as e:
                await message.edit(content=S.ui_err(str(e)))

        # ── slowmode ──────────────────────────────────────────────────────────
        elif cmd == "slowmode":
            if len(args) < 2 or not args[1].isdigit():
                return await message.edit(
                    content=S.ui_err("usage: slowmode <seconds>"))
            secs = int(args[1])
            try:
                ch = getattr(message, "channel", None)
                if ch:
                    await ch.edit(slowmode_delay=secs)
                else:
                    cid = int(message.channel_id)
                    await _rest("PATCH",
                        f"https://discord.com/api/v9/channels/{cid}",
                        json={"rate_limit_per_user": secs})
                await message.edit(content=S.ui_ok(f"slowmode → {secs}s"))
            except Exception as e:
                await message.edit(content=S.ui_err(str(e)))

        # ── servericon / serverbanner / servername ────────────────────────────
        elif cmd in ("servericon", "serverbanner", "servername"):
            gid = _get_guild_id(message)
            if not gid or len(args) < 2:
                return await message.edit(
                    content=S.ui_err(f"usage: {cmd} <url|name>"))
            try:
                if cmd == "servername":
                    s, _ = await _rest("PATCH",
                        f"https://discord.com/api/v9/guilds/{gid}",
                        json={"name": " ".join(args[1:])})
                    await message.edit(content=(
                        S.ui_ok("server renamed") if s in (200,204)
                        else S.ui_err(f"failed (HTTP {s})")))
                else:
                    await message.edit(content=S.ui_info("downloading image…"))
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(args[1]) as r:
                            img_bytes = await r.read()
                    import base64
                    ext   = args[1].rsplit(".", 1)[-1].split("?")[0].lower()
                    mime  = "image/gif" if ext=="gif" else "image/png" if ext=="png" else "image/jpeg"
                    b64   = base64.b64encode(img_bytes).decode()
                    field = "icon" if cmd == "servericon" else "banner"
                    s, _  = await _rest("PATCH",
                        f"https://discord.com/api/v9/guilds/{gid}",
                        json={field: f"data:{mime};base64,{b64}"})
                    await message.edit(content=(
                        S.ui_ok(f"{field} updated") if s in (200,204)
                        else S.ui_err(f"failed (HTTP {s})")))
            except Exception as e:
                await message.edit(content=S.ui_err(str(e)))
