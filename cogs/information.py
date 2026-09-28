# cogs/information.py | userinfo, avatar, checkname, whois, channelinfo
#
# ROOT CAUSE of 403 on userinfo:
#   GET /users/{uid} returns 403 for user tokens on non-friends/non-mutuals.
#   GET /users/{uid}/profile also 403s without valid shared-server context.
#
# FIX:
#   When in a server, try GET /guilds/{guild_id}/members/{uid} FIRST.
#   This endpoint returns full user data for any member in the same guild
#   without needing friend status or the profile endpoint.  It only 403s
#   if the target user isn't in the guild — in that case we fall back to
#   the profile endpoint (with guild_id param) then the basic user endpoint.

import aiohttp
import modifyself_shim as discord
from . import state as S


# ── helpers ──────────────────────────────────────────────────────────────────

def _headers():
    return {"Authorization": S.TOKEN, "User-Agent": S.USER_AGENT,
            "Content-Type": "application/json"}


def _sess():
    fn = getattr(S, "_get_session", None)
    if callable(fn):
        try: return fn()
        except Exception: pass
    return None


async def _get(url: str, params: dict | None = None) -> tuple[int, dict]:
    h = _headers()
    sess = _sess()
    try:
        if sess:
            async with sess.get(url, headers=h, params=params) as r:
                try: body = await r.json(content_type=None)
                except Exception: body = {}
                return r.status, body
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=h, params=params) as r:
                try: body = await r.json(content_type=None)
                except Exception: body = {}
                return r.status, body
    except Exception as e:
        print(f"[information] GET {url} error: {e}")
        return 0, {}


def _guild_id(message) -> int | None:
    gid = getattr(message, "guild_id", None)
    if gid:
        try: return int(gid)
        except Exception: pass
    try:
        ch = getattr(message, "channel", None)
        if ch:
            gid = getattr(ch, "guild_id", None)
            if gid: return int(gid)
    except Exception:
        pass
    return None


def _avatar_url(uid: int, u: dict) -> str:
    av = u.get("avatar")
    if not av:
        return "no avatar"
    ext = "gif" if str(av).startswith("a_") else "png"
    return f"https://cdn.discordapp.com/avatars/{uid}/{av}.{ext}?size=512"


def _banner_url(uid: int, u: dict) -> str:
    bn = u.get("banner")
    if not bn:
        return "none"
    ext = "gif" if str(bn).startswith("a_") else "png"
    return f"https://cdn.discordapp.com/banners/{uid}/{bn}.{ext}?size=600"


def _snowflake_time(uid: int) -> str:
    try:
        ts = ((uid >> 22) + 1420070400000) / 1000
        from datetime import datetime
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


async def _fetch_member(guild_id: int, uid: int) -> dict | None:
    """
    GET /guilds/{guild_id}/members/{uid}
    Returns the user sub-dict from the member object.
    Works for any member in the same guild — no friend/mutual requirement.
    This is the most reliable way to get user info without 403.
    """
    status, d = await _get(
        f"https://discord.com/api/v9/guilds/{guild_id}/members/{uid}")
    if status == 200 and isinstance(d, dict):
        u = d.get("user") or {}
        # Also carry through nick so we can display it
        u["_nick"] = d.get("nick")
        u["_roles"] = d.get("roles", [])
        u["_joined_at"] = d.get("joined_at", "")
        return u
    return None


async def _fetch_user_profile(uid: int, guild_id: int | None = None) -> dict | None:
    """
    Fallback: GET /users/{uid}/profile
    Needs shared server context (guild_id param) — works for mutual-server users.
    """
    params = {"with_mutual_guilds": "true", "with_mutual_friends_count": "true"}
    if guild_id:
        params["guild_id"] = str(guild_id)
    status, p = await _get(
        f"https://discord.com/api/v9/users/{uid}/profile", params=params)
    if status == 200 and isinstance(p, dict):
        u = p.get("user") or {}
        u["_profile_data"] = p   # stash full profile for whois
        return u
    # Last attempt: without guild_id
    if guild_id:
        status2, p2 = await _get(
            f"https://discord.com/api/v9/users/{uid}/profile",
            params={"with_mutual_guilds": "true"})
        if status2 == 200 and isinstance(p2, dict):
            u = p2.get("user") or {}
            u["_profile_data"] = p2
            return u
    return None


async def _fetch_user_basic(uid: int) -> dict | None:
    """
    GET /users/{uid}  — works for own user and sometimes bots.
    Often 403 for other user tokens.
    """
    status, d = await _get(f"https://discord.com/api/v9/users/{uid}")
    if status == 200 and isinstance(d, dict):
        return d
    return None


async def _resolve_user(uid: int, guild_id: int | None) -> dict | None:
    """
    Try all endpoints in order of reliability:
    1. Guild member endpoint  (best — works for all guild members, no 403)
    2. Profile endpoint       (needs shared server — works for mutual-server users)
    3. Basic user endpoint    (works for own user, sometimes bots)
    """
    # 1. Guild member (most reliable when in a server)
    if guild_id:
        u = await _fetch_member(guild_id, uid)
        if u:
            return u

    # 2. Profile endpoint
    u = await _fetch_user_profile(uid, guild_id)
    if u:
        return u

    # 3. Basic endpoint (last resort — likely 403 but worth trying)
    u = await _fetch_user_basic(uid)
    if u:
        return u

    return None


# ── cog ──────────────────────────────────────────────────────────────────────

class InformationCog:
    COMMANDS = {"userinfo", "avatar", "checkname", "whois", "channelinfo"}

    async def handle(self, message, cmd, args):

        # ── userinfo ──────────────────────────────────────────────────────────
        if cmd == "userinfo":
            raw_uid = args[1] if len(args) > 1 else None
            uid = int(raw_uid) if raw_uid and raw_uid.isdigit() else None

            try:
                if uid is None:
                    # Own profile — always works
                    status, u = await _get("https://discord.com/api/v9/users/@me")
                    if status != 200:
                        return await message.edit(
                            content=S.ui_err(f"own profile failed ({status})"))
                    uid = int(u.get("id", 0))
                else:
                    gid = _guild_id(message)
                    u   = await _resolve_user(uid, gid)
                    if not u:
                        return await message.edit(content=S.ui_err(
                            "user not found — they may not be in this server\n"
                            f"  try .whois {uid}  for profile info"))

                uid_int = int(u.get("id", uid))
                rows = [
                    f"  {S.DIM}username{S.RESET}  {u.get('username','?')}",
                    f"  {S.DIM}global{S.RESET}    {u.get('global_name') or '-'}",
                    f"  {S.DIM}id{S.RESET}        {uid_int}",
                    f"  {S.DIM}created{S.RESET}   {_snowflake_time(uid_int)}",
                    f"  {S.DIM}avatar{S.RESET}    {_avatar_url(uid_int, u)}",
                    f"  {S.DIM}banner{S.RESET}    {_banner_url(uid_int, u)}",
                    f"  {S.DIM}bot{S.RESET}       {'yes' if u.get('bot') else 'no'}",
                    f"  {S.DIM}flags{S.RESET}     {u.get('public_flags', 0)}",
                ]
                # If fetched from member endpoint, show extra fields
                if u.get("_nick"):
                    rows.append(f"  {S.DIM}nick{S.RESET}      {u['_nick']}")
                if u.get("_joined_at"):
                    rows.append(f"  {S.DIM}joined{S.RESET}    {u['_joined_at'][:10]}")
                await message.edit(content=S.ui_box("user info", rows))

            except Exception as e:
                import traceback; traceback.print_exc()
                await message.edit(content=S.ui_err(str(e)))

        # ── avatar ────────────────────────────────────────────────────────────
        elif cmd == "avatar":
            raw_uid = args[1] if len(args) > 1 else None
            uid = int(raw_uid) if raw_uid and raw_uid.isdigit() else None
            try:
                if uid is None:
                    status, u = await _get("https://discord.com/api/v9/users/@me")
                    if status != 200:
                        return await message.edit(content=S.ui_err(f"failed ({status})"))
                    uid = int(u.get("id", 0))
                else:
                    gid = _guild_id(message)
                    u   = await _resolve_user(uid, gid)
                    if not u:
                        return await message.edit(content=S.ui_err(
                            "user not found — they may not be in this server"))
                uid_int = int(u.get("id", uid))
                url = _avatar_url(uid_int, u)
                if url == "no avatar":
                    return await message.edit(content=S.ui_info("user has no avatar"))
                await message.edit(content=url)
            except Exception as e:
                await message.edit(content=S.ui_err(str(e)))

        # ── checkname ─────────────────────────────────────────────────────────
        elif cmd == "checkname":
            if len(args) < 2:
                return await message.edit(
                    content=S.ui_err("usage: checkname <username>"))
            username = args[1].lower().strip()
            try:
                h    = _headers()
                sess = _sess()
                url  = "https://discord.com/api/v9/users/@me/pomelo-attempt"
                body = {}
                status = 0
                if sess:
                    async with sess.post(url, headers=h,
                                         json={"username": username}) as r:
                        status = r.status
                        if status == 200:
                            body = await r.json(content_type=None)
                else:
                    async with aiohttp.ClientSession() as s:
                        async with s.post(url, headers=h,
                                          json={"username": username}) as r:
                            status = r.status
                            if status == 200:
                                body = await r.json(content_type=None)
                if status == 200:
                    taken = body.get("taken", True)
                    await message.edit(content=(
                        S.ui_ok(f"`{username}` is available") if not taken
                        else S.ui_err(f"`{username}` is taken")))
                else:
                    await message.edit(content=S.ui_err(f"check failed ({status})"))
            except Exception as e:
                await message.edit(content=S.ui_err(str(e)))

        # ── whois ─────────────────────────────────────────────────────────────
        elif cmd == "whois":
            raw_uid = args[1] if len(args) > 1 else None
            uid = int(raw_uid) if raw_uid and raw_uid.isdigit() else None
            if uid is None:
                uid = int(message.author.id)
            try:
                gid    = _guild_id(message)
                params = {"with_mutual_guilds": "true",
                          "with_mutual_friends_count": "true"}
                if gid:
                    params["guild_id"] = str(gid)

                status, p = await _get(
                    f"https://discord.com/api/v9/users/{uid}/profile",
                    params=params)

                if status != 200 and gid:
                    # Try without guild_id
                    status, p = await _get(
                        f"https://discord.com/api/v9/users/{uid}/profile",
                        params={"with_mutual_guilds": "true"})

                if status != 200:
                    return await message.edit(content=S.ui_err(
                        f"profile not found ({status}) — "
                        "user may not share a server with you"))

                u     = p.get("user", {}) or {}
                prof  = p.get("user_profile", {}) or {}
                badges    = [b.get("id","") for b in p.get("badges",[])
                             if isinstance(b, dict)]
                connected = p.get("connected_accounts", []) or []
                uid_int   = int(u.get("id", uid))
                rows = [
                    f"  {S.DIM}username{S.RESET}       {u.get('username','?')}",
                    f"  {S.DIM}id{S.RESET}             {uid_int}",
                    f"  {S.DIM}created{S.RESET}        {_snowflake_time(uid_int)}",
                    f"  {S.DIM}bio{S.RESET}            {prof.get('bio','') or '-'}",
                    f"  {S.DIM}pronouns{S.RESET}       {prof.get('pronouns','') or '-'}",
                    f"  {S.DIM}badges{S.RESET}         {', '.join(badges) or 'none'}",
                    f"  {S.DIM}connected{S.RESET}      {len(connected)}",
                    f"  {S.DIM}nitro{S.RESET}          {'yes' if p.get('premium_since') else 'no'}",
                    f"  {S.DIM}mutual servers{S.RESET} {len(p.get('mutual_guilds',[]) or [])}",
                    f"  {S.DIM}mutual friends{S.RESET} {len(p.get('mutual_friends',[]) or [])}",
                ]
                await message.edit(content=S.ui_box("whois", rows))
            except Exception as e:
                await message.edit(content=S.ui_err(str(e)))

        # ── channelinfo ───────────────────────────────────────────────────────
        elif cmd == "channelinfo":
            cid = (int(args[1]) if len(args) > 1 and args[1].isdigit()
                   else getattr(message, "channel_id", None))
            if not cid:
                return await message.edit(content=S.ui_err("could not resolve channel ID"))
            # Try cache first
            cl = S.CLIENT
            ch = cl.get_channel(int(cid)) if cl and hasattr(cl,"get_channel") else None
            if ch:
                g = getattr(ch, "guild", None)
                rows = [
                    f"  {S.DIM}name{S.RESET}     #{getattr(ch,'name',cid)}",
                    f"  {S.DIM}id{S.RESET}       {ch.id}",
                    f"  {S.DIM}guild{S.RESET}    {getattr(g,'name','DM') if g else 'DM'}",
                ]
                try:
                    rows.append(
                        f"  {S.DIM}created{S.RESET}  {_snowflake_time(int(ch.id))}")
                except Exception:
                    pass
                return await message.edit(content=S.ui_box("channel info", rows))
            # REST fallback
            status, d = await _get(
                f"https://discord.com/api/v9/channels/{cid}")
            if status != 200:
                return await message.edit(
                    content=S.ui_err(f"channel not found ({status})"))
            rows = [
                f"  {S.DIM}name{S.RESET}     #{d.get('name', cid)}",
                f"  {S.DIM}id{S.RESET}       {d.get('id','?')}",
                f"  {S.DIM}type{S.RESET}     {d.get('type','?')}",
                f"  {S.DIM}created{S.RESET}  {_snowflake_time(int(d.get('id',0)))}",
            ]
            if d.get("topic"):
                rows.append(f"  {S.DIM}topic{S.RESET}    {d['topic'][:80]}")
            await message.edit(content=S.ui_box("channel info", rows))
