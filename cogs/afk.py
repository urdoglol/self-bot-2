# cogs/afk.py | full AFK system — whitelist, blacklist, cooldown, dm-only,
# per-server, expiry, emergency
#
# behavior:
#   - ignore list fires on ANY address path (direct @, reply-to-us, raw
#     mention list, mention_everyone). anything that reaches "addressed to me"
#     is checked against the blacklist.
#   - direct @ping, reply-to-us, raw mention, and mention_everyone all
#     count as addressed — that's the trigger surface for the hook.
#   - blocked users filtered via relationship type 2
#   - all id comparisons normalized through _as_int
#   - self-message guard hardened
#   - per-call debug line (toggle DEBUG_AFK = True to trace)

import asyncio
import time
import modifyself_shim as discord
from . import state as S

DEBUG_AFK = False


# ═══════════════════════════════════════════════════════════════════
#  helpers — id normalization, ping detection, block set
# ═══════════════════════════════════════════════════════════════════

def _as_int(v):
    """Normalize any id (str / int / snowflake-like) to int. None on failure."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _author_id(message):
    a = getattr(message, "author", None)
    if a is None:
        return None
    return _as_int(getattr(a, "id", None))


def _my_id(client):
    me = getattr(client, "user", None)
    if me is None:
        return None
    return _as_int(getattr(me, "id", None))


def _mentions_me(client, message):
    """
    True if the message addresses us by ANY path:
      - direct @ in message.mentions
      - reply to one of our messages (reference / message_reference)
      - raw mention list (raw_mentions / mentioned_users)
      - @everyone / @here (mentions_everyone)
    This is the gate the ignore list runs against — anything that lands
    here is checked against the blacklist.
    """
    my_id = _my_id(client)
    if my_id is None:
        return False

    # 1) direct mention
    for u in (getattr(message, "mentions", None) or []):
        if _as_int(getattr(u, "id", None)) == my_id:
            return True

    # 2) reply-to-us
    for attr in ("reference", "message_reference"):
        ref = getattr(message, attr, None)
        if ref is None:
            continue
        resolved = getattr(ref, "resolved", None)
        candidates = [
            getattr(ref, "author_id", None),
            getattr(ref, "user_id", None),
            getattr(resolved, "author_id", None) if resolved else None,
            getattr(getattr(resolved, "author", None), "id", None) if resolved else None,
        ]
        for c in candidates:
            if _as_int(c) == my_id:
                return True
        # some forks stuff the ping list onto the reference itself
        for u in (getattr(ref, "mentions", None) or []):
            if _as_int(getattr(u, "id", None)) == my_id:
                return True

    # 3) raw mention fallback
    for attr in ("mentioned_users", "raw_mentions"):
        v = getattr(message, attr, None)
        if v is None:
            continue
        try:
            for u in v:
                if _as_int(getattr(u, "id", u)) == my_id:
                    return True
        except TypeError:
            continue

    # 4) @everyone / @here
    if getattr(message, "mentions_everyone", False):
        return True

    return False


def _blocked_ids(client):
    """
    Discord 'blocked' is a relationship state, not a message flag.
    Walk every plausible path the shim exposes.
    """
    blocked = set()
    rels = getattr(client, "relationships", None)

    if rels is None:
        st = (getattr(client, "_state", None)
              or getattr(client, "_connection", None))
        if st is not None:
            rels = (getattr(st, "_relationships", None)
                    or getattr(st, "relationships", None))

    if rels is None:
        return blocked

    for r in rels:
        rtype = getattr(r, "type", None)
        try:
            rtype = int(rtype) if rtype is not None else None
        except (TypeError, ValueError):
            rtype = None

        # 2 == BLOCKED
        if rtype == 2:
            uid = _as_int(
                getattr(getattr(r, "user", None), "id", None)
                or getattr(r, "user_id", None)
                or getattr(r, "id", None)
            )
            if uid:
                blocked.add(uid)

    return blocked


# ═══════════════════════════════════════════════════════════════════
#  pre-hook
# ═══════════════════════════════════════════════════════════════════

async def _afk_pre_hook(client, message):
    """Called on every message before the command gate."""
    a = S.afk
    if not a["enabled"]:
        return

    # expiry
    if a["expires_at"] and time.time() >= a["expires_at"]:
        a["enabled"] = False
        a["expires_at"] = 0.0
        return

    author_id = _author_id(message)
    my_id = _my_id(client)

    if DEBUG_AFK:
        print(f"[afk] hook author={author_id} me={my_id} "
              f"mentions_me={_mentions_me(client, message)} "
              f"ignored={author_id in a['blacklist']}")

    # never react to our own messages
    if author_id is None or author_id == my_id:
        return

    # addressed by any path — direct @, reply, raw mention, @everyone
    if not _mentions_me(client, message):
        return

    # ignore list — fires regardless of HOW the author reached us
    if author_id in a["blacklist"]:
        return

    # blocked users — Discord doesn't strip their events, we filter
    if author_id in _blocked_ids(client):
        return

    # whitelist gate (if populated, only these users get a response)
    if a["whitelist"] and author_id not in a["whitelist"]:
        return

    # dm-only
    if a["dm_only"] and getattr(message, "guild", None) is not None:
        return

    # per-server override
    gid = str(getattr(getattr(message, "guild", None), "id", "") or "") or None
    if gid and gid in a["per_server"]:
        msg = a["per_server"][gid].get("message") or a["message"]
    else:
        msg = a["message"]

    # custom reply per user
    if author_id in a["custom_replies"]:
        msg = a["custom_replies"][author_id]

    # cooldown per (user, channel)
    key = (author_id, getattr(message, "channel_id", 0))
    last = a["last_reply"].get(key, 0)
    if time.time() - last < a["cooldown"]:
        return
    a["last_reply"][key] = time.time()

    # ping counter
    a["ping_counter"][author_id] = a["ping_counter"].get(author_id, 0) + 1

    try:
        await message.reply(msg, mention_author=False)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
#  cog
# ═══════════════════════════════════════════════════════════════════

class AfkCog:
    COMMANDS = {"afk", "afkstop", "afkstatus", "afkignore", "afkignorelist",
                "afkwhitelist", "afkblacklist", "afkcooldown", "afkdmonly",
                "afkexpire", "afkemergency", "afkreturn", "afkcustom",
                "afkserver", "afkpingcount"}

    def register(self, client):
        S.register_pre_hook(_afk_pre_hook)

    async def handle(self, message, cmd, args):
        a = S.afk

        if cmd == "afk":
            msg = " ".join(args[1:]) if len(args) > 1 else None
            a["enabled"] = True
            if msg:
                a["message"] = msg
            tail = ""
            if a["expires_at"]:
                tail = f" (expires in {int(a['expires_at'] - time.time())}s)"
            await message.edit(content=S.ui_ok(
                f"AFK on — {a['message'][:60]}{tail}"))

        elif cmd == "afkstop":
            a["enabled"] = False
            a["expires_at"] = 0.0
            a["emergency"] = False
            await message.edit(content=S.ui_ok("AFK off"))

        elif cmd == "afkstatus":
            rows = [
                f"  {S.DIM}enabled{S.RESET}       {a['enabled']}",
                f"  {S.DIM}message{S.RESET}       {a['message'][:60]}",
                f"  {S.DIM}cooldown{S.RESET}      {a['cooldown']}s",
                f"  {S.DIM}dm only{S.RESET}       {a['dm_only']}",
                f"  {S.DIM}emergency{S.RESET}     {a['emergency']}",
                f"  {S.DIM}whitelist{S.RESET}     {len(a['whitelist'])}",
                f"  {S.DIM}blacklist{S.RESET}     {len(a['blacklist'])}",
                f"  {S.DIM}custom replies{S.RESET} {len(a['custom_replies'])}",
                f"  {S.DIM}per-server{S.RESET}    {len(a['per_server'])}",
                f"  {S.DIM}expires{S.RESET}       "
                f"{int(a['expires_at'] - time.time()) if a['expires_at'] else '—'}",
            ]
            await message.edit(content=S.ui_box("AFK status", rows))

        elif cmd == "afkignore":
            if len(args) < 2:
                return await message.edit(
                    content=S.ui_err("usage: afkignore add/remove/list/clear <uid>"))
            sub = args[1].lower()
            if sub == "add" and len(args) >= 3 and args[2].isdigit():
                a["blacklist"].add(int(args[2]))
                await message.edit(content=S.ui_ok(f"ignoring {args[2]}"))
            elif sub == "remove" and len(args) >= 3 and args[2].isdigit():
                a["blacklist"].discard(int(args[2]))
                await message.edit(content=S.ui_ok("removed"))
            elif sub == "list":
                rows = [f"  {S.GREY}•{S.RESET} <@{u}>" for u in sorted(a["blacklist"])]
                await message.edit(content=S._paginate("afk ignore", "", rows)
                                            if rows else S.ui_info("empty"))
            elif sub == "clear":
                a["blacklist"].clear()
                await message.edit(content=S.ui_ok("cleared"))
            else:
                await message.edit(content=S.ui_info(
                    "usage: afkignore add/remove/list/clear"))

        elif cmd == "afkwhitelist":
            if len(args) < 2:
                return await message.edit(content=S.ui_info(
                    "usage: afkwhitelist add/remove/list/clear <uid>"))
            sub = args[1].lower()
            if sub == "add" and len(args) >= 3 and args[2].isdigit():
                a["whitelist"].add(int(args[2]))
                await message.edit(content=S.ui_ok("added"))
            elif sub == "remove" and len(args) >= 3 and args[2].isdigit():
                a["whitelist"].discard(int(args[2]))
                await message.edit(content=S.ui_ok("removed"))
            elif sub == "list":
                rows = [f"  {S.GREY}•{S.RESET} <@{u}>" for u in sorted(a["whitelist"])]
                await message.edit(content=S._paginate("afk whitelist", "", rows)
                                            if rows else S.ui_info("empty"))
            elif sub == "clear":
                a["whitelist"].clear()
                await message.edit(content=S.ui_ok("cleared"))
            else:
                await message.edit(content=S.ui_info(
                    "usage: afkwhitelist add/remove/list/clear"))

        elif cmd == "afkblacklist":
            await self._mirror(message, args, "blacklist")

        elif cmd == "afkcooldown":
            if len(args) < 2 or not args[1].isdigit():
                return await message.edit(content=S.ui_err("usage: afkcooldown <seconds>"))
            a["cooldown"] = int(args[1])
            await message.edit(content=S.ui_ok(f"cooldown → {a['cooldown']}s"))

        elif cmd == "afkdmonly":
            val = (args[1].lower() in ("on", "enable")) if len(args) > 1 else not a["dm_only"]
            a["dm_only"] = val
            await message.edit(content=S.ui_ok(f"dm-only → {val}"))

        elif cmd == "afkexpire":
            if len(args) < 2:
                a["expires_at"] = 0.0
                return await message.edit(content=S.ui_ok("expiry cleared"))
            try:
                secs = int(args[1])
            except ValueError:
                return await message.edit(content=S.ui_err("seconds required"))
            a["expires_at"] = time.time() + secs
            await message.edit(content=S.ui_ok(f"expires in {secs}s"))

        elif cmd == "afkemergency":
            a["emergency"] = True
            a["enabled"] = True
            await message.edit(content=S.ui_ok("emergency AFK on"))

        elif cmd == "afkreturn":
            if a["enabled"]:
                a["enabled"] = False
                counts = a["ping_counter"]
                if counts:
                    lines = [f"  {S.DIM}you were pinged by {len(counts)} user(s){S.RESET}"]
                    for uid, n in sorted(counts.items(), key=lambda x: -x[1])[:10]:
                        lines.append(f"  {S.GREY}•{S.RESET} <@{uid}>  {n}x")
                    counts.clear()
                    return await message.edit(content=S.ui_box("welcome back", lines))
            await message.edit(content=S.ui_ok("AFK off"))

        elif cmd == "afkcustom":
            if len(args) < 3:
                return await message.edit(content=S.ui_err(
                    "usage: afkcustom set <uid> <text> | afkcustom remove <uid>"))
            sub = args[1].lower()
            if sub == "set" and len(args) >= 4 and args[2].isdigit():
                a["custom_replies"][int(args[2])] = " ".join(args[3:])
                await message.edit(content=S.ui_ok(f"reply for {args[2]} set"))
            elif sub == "remove" and args[2].isdigit():
                a["custom_replies"].pop(int(args[2]), None)
                await message.edit(content=S.ui_ok("removed"))
            else:
                await message.edit(content=S.ui_info("usage: afkcustom set/remove"))

        elif cmd == "afkserver":
            if not message.guild:
                return await message.edit(content=S.ui_err("server only"))
            gid = str(message.guild.id)
            if len(args) < 2:
                return await message.edit(content=S.ui_err(
                    "usage: afkserver set <text> | afkserver clear"))
            sub = args[1].lower()
            if sub == "set":
                a["per_server"][gid] = {"message": " ".join(args[2:])}
                await message.edit(content=S.ui_ok("set"))
            elif sub == "clear":
                a["per_server"].pop(gid, None)
                await message.edit(content=S.ui_ok("cleared"))
            else:
                await message.edit(content=S.ui_info("usage: afkserver set/clear"))

        elif cmd == "afkpingcount":
            rows = [f"  {S.GREY}•{S.RESET} <@{u}>  {n}x"
                    for u, n in sorted(a["ping_counter"].items(),
                                       key=lambda x: -x[1])[:20]]
            await message.edit(content=S._paginate("ping counters", "", rows)
                                        if rows else S.ui_info("no pings yet"))

    async def _mirror(self, message, args, key):
        a = S.afk
        if len(args) < 2:
            return await message.edit(
                content=S.ui_info(f"usage: afk{key} add/remove/list/clear"))
        sub = args[1].lower()
        if sub == "add" and len(args) >= 3 and args[2].isdigit():
            a[key].add(int(args[2]))
            return await message.edit(content=S.ui_ok("added"))
        if sub == "remove" and len(args) >= 3 and args[2].isdigit():
            a[key].discard(int(args[2]))
            return await message.edit(content=S.ui_ok("removed"))
        if sub == "list":
            rows = [f"  {S.GREY}•{S.RESET} <@{u}>" for u in sorted(a[key])]
            return await message.edit(content=S._paginate(f"afk {key}", "", rows)
                                                if rows else S.ui_info("empty"))
        if sub == "clear":
            a[key].clear()
            return await message.edit(content=S.ui_ok("cleared"))
        await message.edit(content=S.ui_info(f"usage: afk{key} add/remove/list/clear"))
