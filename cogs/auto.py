# cogs/auto.py | giveaway, nitrosniper, autoreact, multireact, vsniper, superreact
#
# NEW: per-user react targeting
#   .autoreact <emoji>               — react to ALL messages (global)
#   .autoreact <@user|userid> <emoji>— react only to that user's messages
#   .autoreact list                  — show all active targets
#   .autoreact off                   — disable global autoreact
#   .autoreactstop [<@user|uid>|all] — stop global, per-user, or everything
#
#   .superreact <emoji>              — react to YOUR OWN messages (unchanged)
#   .superreact <@user|userid> <emoji>— react to a specific user's messages
#   .superreact list                 — show all targets
#   .superreactstop [<@user|uid>|all]— stop own, per-user, or everything
#
#   .multireact add <emoji>              — add to global pool
#   .multireact add <@user|uid> <emoji>  — add to user's personal pool
#   .multireact remove <emoji>           — remove from global pool
#   .multireact remove <@user|uid> <emoji>
#   .multireact on/off [<@user|uid>]     — enable/disable global or per-user
#   .multireact list                     — show all pools + user pools
#   .multireact clear [<@user|uid>]      — clear global or user pool
#
# HOW IT WORKS:
#   - Per-user maps (_autoreact_users, _superreact_users, _multireact_users)
#     live on the cog instance.
#   - A pre-hook registered via S.register_pre_hook() fires on every incoming
#     message and dispatches per-user reactions + global reactions for
#     OTHER users' messages.
#   - selfbot.py already handles own-message reactions via _sync() → no
#     double-react on own messages.
#
# BUGS CARRIED FORWARD FROM PREVIOUS VERSION (all still fixed):
#   [BUG-1] _react_once always returns int status tuple (never str)
#   [BUG-2] _emoji_url_part strips angle-brackets correctly for custom emoji
#   [BUG-3] _parse_emoji_arg re-joins split tokens and uses raw_content
#   [BUG-4] multireact duplicate check uses normalised emoji string
#   [BUG-5] superreact control-word check before emoji parse (no lowercasing)
#   [BUG-6] vsniper uses one session per tick, not per entry
#   [BUG-7] react patch raises descriptive error on non-204

import asyncio
import re
import sys
import urllib.parse

import aiohttp
import modifyself_shim as discord
from . import state as S

# ─────────────────────────────────────────────────────────────────────────────
# Regex
# ─────────────────────────────────────────────────────────────────────────────

_RE_CUSTOM    = re.compile(r"<(a?):([A-Za-z0-9_]+):(\d+)>")
_RE_SHORTCODE = re.compile(r":([A-Za-z0-9_]+):")
_RE_MENTION   = re.compile(r"<@!?(\d+)>")
_RE_UID       = re.compile(r"^\d{15,20}$")
_RE_NITRO     = re.compile(r"discord\.gift/([A-Za-z0-9]+)")
_RE_GIVEAWAY  = re.compile(r"🎉|giveaway", re.IGNORECASE)

# ─────────────────────────────────────────────────────────────────────────────
# User-ID parsing
# ─────────────────────────────────────────────────────────────────────────────

def _parse_user_id(token: str):
    """
    Extract a Discord user ID from a <@mention> or a raw 15-20 digit string.
    Returns int or None.
    """
    m = _RE_MENTION.match(token.strip())
    if m:
        return int(m.group(1))
    if _RE_UID.match(token.strip()):
        return int(token.strip())
    return None


def _parse_user_and_emoji(args: list, start: int = 1,
                           raw_content: str = ""):
    """
    Parse (user_id_or_None, emoji_str_or_None) from args[start:].

    If args[start] looks like a user mention or numeric ID, treat it as the
    user target and parse the emoji from args[start+1:].
    Otherwise parse the emoji starting at args[start] with no user.

    Returns:
        (uid: "int | None", emoji: "str | None")
    """
    if len(args) <= start:
        return None, None

    uid = _parse_user_id(args[start])

    if uid is not None:
        # User found at args[start]; emoji follows at args[start+1]
        if len(args) <= start + 1:
            return uid, None  # user given but no emoji
        emoji = _parse_emoji_arg(args, start + 1, raw_content)
        return uid, emoji or None
    else:
        # No user; emoji starts at args[start]
        emoji = _parse_emoji_arg(args, start, raw_content)
        return None, emoji or None


# ─────────────────────────────────────────────────────────────────────────────
# Emoji helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sync(**kw):
    """Mirror state values onto __main__ so selfbot.py sees them."""
    main = sys.modules.get("__main__")
    if main is None:
        return
    for k, v in kw.items():
        try:
            setattr(main, k, v)
        except Exception:
            pass


def _emoji_to_str(emoji):
    if isinstance(emoji, str):
        return emoji
    name     = getattr(emoji, "name",     None)
    eid      = getattr(emoji, "id",       None)
    animated = getattr(emoji, "animated", False)
    if name and eid:
        return f"<{'a' if animated else ''}:{name}:{eid}>"
    return str(name or emoji)


def _emoji_url_part(emoji_str: str):
    """
    Encode emoji for Discord's PUT reaction URL.
    Custom <a:name:id> / <:name:id>  →  name%3Aid  (no angle brackets)
    Unicode                           →  percent-encoded codepoints
    """
    m = _RE_CUSTOM.match(emoji_str.strip())
    if m:
        return urllib.parse.quote(f"{m.group(2)}:{m.group(3)}", safe="")
    return urllib.parse.quote(emoji_str, safe="")


def _parse_emoji_arg(args: list, start: int = 1,
                     raw_content: str = ""):
    """
    Extract the first emoji from args[start:], using raw_content for split
    tokens (e.g. <:name:id> split into ["<:name", "id>"] by the parser).

    Priority:
      1. raw_content  — search original message text past `start` whitespace-
                        tokens; most reliable for custom emoji
      2. re-joined args — catches <:name:id> split across args
      3. first token  — unicode emoji or plain word
    """
    # 1. raw_content: skip past `start` whitespace tokens, search the rest
    if raw_content:
        parts = raw_content.split(None, start)
        remainder = parts[-1] if len(parts) > start else ""
        m = _RE_CUSTOM.search(remainder)
        if m:
            return m.group(0)
        m = _RE_SHORTCODE.search(remainder)
        if m:
            return m.group(0)

    # 2. Re-joined args
    joined = " ".join(args[start:]).strip()
    m = _RE_CUSTOM.search(joined)
    if m:
        return m.group(0)
    m = _RE_SHORTCODE.search(joined)
    if m:
        return m.group(0)

    # 3. First token
    return joined.split()[0] if joined.split() else joined


def _resolve_emoji(client, raw: str):
    """Resolve :shortcode: / bare name → <:name:id> by searching joined guilds."""
    if _RE_CUSTOM.match(raw.strip()):
        return raw
    target = raw.strip(": ").lower()
    for guild in (getattr(client, "guilds", None) or []):
        for e in getattr(guild, "emojis", []):
            if getattr(e, "name", "").lower() == target:
                prefix = "a" if getattr(e, "animated", False) else ""
                return f"<{prefix}:{e.name}:{e.id}>"
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# REST reaction
# ─────────────────────────────────────────────────────────────────────────────

async def _react_once(session, channel_id, message_id, emoji):
    emoji_enc = _emoji_url_part(_emoji_to_str(emoji))
    url = (f"https://discord.com/api/v9/channels/{channel_id}"
           f"/messages/{message_id}/reactions/{emoji_enc}/@me")
    headers = {
        "Authorization":  S.TOKEN,
        "User-Agent":     S.USER_AGENT,
        "Content-Length": "0",
    }
    try:
        async with session.put(url, headers=headers) as r:
            if r.status == 429:
                ra = 1.0
                try:
                    body = await r.json(content_type=None)
                    ra = float(body.get("retry_after", 1.0))
                except Exception:
                    pass
                return 429, ra
            return r.status, None
    except Exception as e:
        print(f"[auto] react network error: {e}")
        return -1, None


async def _react(channel_id, message_id, emoji, session=None):
    own = session is None
    if own:
        session = aiohttp.ClientSession()
    try:
        for attempt in range(4):
            status, retry_after = await _react_once(
                session, channel_id, message_id, emoji)
            if status == 429:
                await asyncio.sleep(max(float(retry_after or 1.0), 0.5))
                continue
            if status == -1:
                await asyncio.sleep(0.5)
                continue
            return status
        return 429
    finally:
        if own:
            await session.close()


async def _react_pool(channel_id, message_id, pool: list):
    """React with every emoji in `pool`, one session, 0.3s gap."""
    async with aiohttp.ClientSession() as sess:
        for emoji in pool:
            status = await _react(channel_id, message_id, emoji, session=sess)
            if status not in (200, 204):
                print(f"[auto] multireact failed {emoji!r} → HTTP {status}")
            await asyncio.sleep(0.3)


# ─────────────────────────────────────────────────────────────────────────────
# Message.add_reaction shim patch
# ─────────────────────────────────────────────────────────────────────────────

def _install_react_patch():
    try:
        from modifyself.models.message import Message as M
    except ImportError as e:
        print(f"[auto] react patch import failed: {e}")
        return False

    if getattr(M, "_raw_react_patched", False):
        return True

    _orig = M.add_reaction

    async def _patched(self, emoji, *a, **kw):
        try:
            return await _orig(self, emoji, *a, **kw)
        except Exception as err:
            ch_id = getattr(self, "channel_id", None)
            if ch_id is None:
                ch    = getattr(self, "channel", None)
                ch_id = getattr(ch, "id", None) if ch else None
            msg_id = getattr(self, "id", None)
            if not ch_id or not msg_id:
                raise err
            status = await _react(ch_id, msg_id, emoji)
            if isinstance(status, int) and status in (200, 204):
                return None
            raise RuntimeError(
                f"[auto] react failed HTTP {status} "
                f"ch={ch_id} msg={msg_id} emoji={_emoji_to_str(emoji)!r}")

    M.add_reaction       = _patched
    M._raw_react_patched = True
    print("[auto] Message.add_reaction patched")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Nitro sniper
# ─────────────────────────────────────────────────────────────────────────────

async def _snipe_nitro(code: str):
    url = f"https://discord.com/api/v9/entitlements/gift-codes/{code}/redeem"
    headers = {"Authorization": S.TOKEN, "User-Agent": S.USER_AGENT,
               "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=headers, json={}) as r:
                body = {}
                try:
                    body = await r.json(content_type=None)
                except Exception:
                    pass
                result = "CLAIMED" if r.status == 200 else f"FAILED ({r.status})"
                if S.log_msg:
                    S.log_msg("NITRO", f"{result} discord.gift/{code} → {body}")
    except Exception as e:
        if S.log_msg:
            S.log_msg("NITRO", f"error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Vanity sniper
# ─────────────────────────────────────────────────────────────────────────────

async def _vsniper_loop():
    while True:
        entries = list(S._vsniper_list)
        if entries:
            h = {"Authorization": S.TOKEN, "Content-Type": "application/json",
                 "User-Agent": S.USER_AGENT}
            async with aiohttp.ClientSession() as s:   # one session per tick
                for entry in entries:
                    code, guild_id = entry["code"], entry["guild_id"]
                    try:
                        async with s.get(
                            f"https://discord.com/api/v9/invites/{code}", headers=h
                        ) as r:
                            if r.status == 404:
                                async with s.patch(
                                    f"https://discord.com/api/v9/guilds/{guild_id}/vanity-url",
                                    headers=h, json={"code": code}
                                ) as r2:
                                    if r2.status in (200, 204) and S.log_msg:
                                        S.log_msg("VSNIPER",
                                                  f"CLAIMED {code} guild={guild_id}")
                    except Exception as e:
                        print(f"[auto] vsniper error {code}: {e}")
        await asyncio.sleep(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# Pre-hook factory  (per-user reactions + global-other-users reactions)
# ─────────────────────────────────────────────────────────────────────────────

def _make_pre_hook(cog: "AutoCog"):
    """
    Build the per-message pre-hook that is registered via S.register_pre_hook().

    Responsibilities:
      • Global autoreact   — react to OTHER users' messages (selfbot.py handles own)
      • Global multireact  — same
      • Per-user autoreact — react to specific users' messages
      • Per-user superreact— react to specific users' messages (second emoji slot)
      • Per-user multireact— react with a per-user pool
      • Nitro sniper       — scan other-user messages for gift links
      • Giveaway auto-enter
    """
    async def _hook(client, message):
        ch_id = getattr(message, "channel_id", None)
        if ch_id is None:
            ch    = getattr(message, "channel", None)
            ch_id = getattr(ch, "id", None) if ch else None
        msg_id = getattr(message, "id", None)
        if not ch_id or not msg_id:
            return

        try:
            author_id = int(getattr(getattr(message, "author", None), "id", 0) or 0)
        except Exception:
            return
        if author_id == 0:
            return

        try:
            my_id = int(getattr(getattr(client, "user", None), "id", 0) or 0)
        except Exception:
            my_id = 0

        content = getattr(message, "content", "") or ""

        # ── nitro sniper ─────────────────────────────────────────────────────
        if S._nitrosniper_enabled and author_id != my_id:
            for code in _RE_NITRO.findall(content):
                asyncio.create_task(_snipe_nitro(code))

        # ── giveaway ─────────────────────────────────────────────────────────
        if S._giveaway_enabled and _RE_GIVEAWAY.search(content):
            asyncio.create_task(_react(ch_id, msg_id, "🎉"))

        # ── global autoreact — other users only (selfbot.py does own) ────────
        if S._autoreact_emoji and author_id != my_id:
            asyncio.create_task(_react(ch_id, msg_id, S._autoreact_emoji))

        # ── global multireact — other users only ─────────────────────────────
        if S._multireact_enabled and S._multireact_pool and author_id != my_id:
            asyncio.create_task(
                _react_pool(ch_id, msg_id, list(S._multireact_pool)))

        # ── per-user autoreact ────────────────────────────────────────────────
        if author_id in cog._autoreact_users:
            asyncio.create_task(
                _react(ch_id, msg_id, cog._autoreact_users[author_id]))

        # ── per-user superreact ───────────────────────────────────────────────
        if author_id in cog._superreact_users:
            asyncio.create_task(
                _react(ch_id, msg_id, cog._superreact_users[author_id]))

        # ── per-user multireact ───────────────────────────────────────────────
        if (author_id in cog._multireact_users
                and cog._multireact_users_on.get(author_id, False)):
            pool = list(cog._multireact_users[author_id])
            if pool:
                asyncio.create_task(_react_pool(ch_id, msg_id, pool))

    return _hook


# ─────────────────────────────────────────────────────────────────────────────
# AutoCog
# ─────────────────────────────────────────────────────────────────────────────

class AutoCog:
    COMMANDS = {
        "giveaway", "nitrosniper",
        "autoreact", "autoreactstop",
        "superreact", "superreactstop",
        "multireact", "multiautoreact",
        "vsniper",
        "reactdiag",
    }

    def __init__(self):
        _install_react_patch()
        # Per-user react targets — keyed by int user ID
        self._autoreact_users:     dict = {}   # {uid: emoji}
        self._superreact_users:    dict = {}   # {uid: emoji}
        self._multireact_users:    dict = {}   # {uid: [emoji, ...]}
        self._multireact_users_on: dict = {}   # {uid: bool}

    def register(self, client):
        """Wire the per-message pre-hook into the selfbot's event stream."""
        S.register_pre_hook(_make_pre_hook(self))

    # ── internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _client():
        main = sys.modules.get("__main__")
        return getattr(main, "client", None) if main else None

    def _resolve(self, raw: str):
        if _RE_CUSTOM.match(raw.strip()):
            return raw
        client = self._client()
        return _resolve_emoji(client, raw) if client else raw

    def _ulabel(self, uid: int):
        return f"<@{uid}> ({uid})"

    # ── dispatch ──────────────────────────────────────────────────────────────

    async def handle(self, message, cmd, args):
        rc = getattr(message, "content", "") or ""   # raw content for emoji parse

        if cmd == "giveaway":
            S._giveaway_enabled = len(args) < 2 or args[1].lower() in ("on", "enable")
            _sync(_giveaway_enabled=S._giveaway_enabled)
            await message.edit(content=S.ui_ok(
                f"giveaway → {'on' if S._giveaway_enabled else 'off'}"))

        elif cmd == "nitrosniper":
            S._nitrosniper_enabled = len(args) < 2 or args[1].lower() in ("on", "enable")
            _sync(_nitrosniper_enabled=S._nitrosniper_enabled)
            await message.edit(content=S.ui_ok(
                f"nitrosniper → {'on' if S._nitrosniper_enabled else 'off'}"))

        elif cmd == "autoreact":
            await self._autoreact(message, args, rc)

        elif cmd == "autoreactstop":
            await self._autoreactstop(message, args)

        elif cmd == "superreact":
            await self._superreact(message, args, rc)

        elif cmd == "superreactstop":
            await self._superreactstop(message, args)

        elif cmd in ("multireact", "multiautoreact"):
            await self._multireact(message, args, rc)

        elif cmd == "vsniper":
            await self._vsniper(message, args)

        elif cmd == "reactdiag":
            await self._reactdiag(message)

    # ─────────────────────────────────────────────────────────────────────────
    # .autoreact
    # ─────────────────────────────────────────────────────────────────────────

    async def _autoreact(self, message, args, rc):
        """
        .autoreact <emoji>               — react to ALL messages (global)
        .autoreact <@user|uid> <emoji>   — react to that user's messages only
        .autoreact list                  — show every active target
        .autoreact off                   — disable global autoreact
        """
        if len(args) < 2:
            return await message.edit(content=S.ui_err(
                "usage: autoreact <emoji>"
                "  |  autoreact <@user|uid> <emoji>"
                "  |  autoreact list  |  autoreact off"))

        sub = args[1].lower()

        if sub == "list":
            rows = [f"  {S.DIM}global{S.RESET}  "
                    f"{'off' if not S._autoreact_emoji else S._autoreact_emoji}"]
            for uid, emoji in sorted(self._autoreact_users.items()):
                rows.append(f"  {S.GREY}•{S.RESET} {self._ulabel(uid)}  →  {emoji}")
            return await message.edit(content=S.ui_box("autoreact targets", rows))

        if sub in ("off", "stop", "disable"):
            S._autoreact_emoji = None
            _sync(_autoreact_emoji=None)
            return await message.edit(content=S.ui_ok("autoreact → off (global)"))

        uid, raw_emoji = _parse_user_and_emoji(args, 1, rc)

        if uid is not None and raw_emoji is None:
            return await message.edit(content=S.ui_err(
                f"usage: autoreact {self._ulabel(uid)} <emoji>"))

        if raw_emoji is None:
            return await message.edit(content=S.ui_err(
                "couldn't read emoji — paste it directly after the command"))

        emoji = self._resolve(raw_emoji)

        if uid is not None:
            self._autoreact_users[uid] = emoji
            await message.edit(content=S.ui_ok(
                f"autoreact → {emoji}  (messages from {self._ulabel(uid)})"))
        else:
            S._autoreact_emoji = emoji
            _sync(_autoreact_emoji=emoji)
            await message.edit(content=S.ui_ok(
                f"autoreact → {emoji}  (all messages)"))

    # ─────────────────────────────────────────────────────────────────────────
    # .autoreactstop
    # ─────────────────────────────────────────────────────────────────────────

    async def _autoreactstop(self, message, args):
        """
        .autoreactstop              — stop global autoreact
        .autoreactstop <@user|uid>  — remove per-user target
        .autoreactstop all          — stop global + every per-user target
        """
        if len(args) >= 2:
            sub = args[1].lower()
            if sub == "all":
                S._autoreact_emoji = None
                _sync(_autoreact_emoji=None)
                n = len(self._autoreact_users)
                self._autoreact_users.clear()
                return await message.edit(content=S.ui_ok(
                    f"autoreact → off  (global + {n} user target(s) cleared)"))
            uid = _parse_user_id(args[1])
            if uid is not None:
                if uid in self._autoreact_users:
                    del self._autoreact_users[uid]
                    return await message.edit(content=S.ui_ok(
                        f"autoreact → off for {self._ulabel(uid)}"))
                return await message.edit(content=S.ui_info(
                    f"no autoreact target set for {self._ulabel(uid)}"))

        S._autoreact_emoji = None
        _sync(_autoreact_emoji=None)
        await message.edit(content=S.ui_ok("autoreact → off (global)"))

    # ─────────────────────────────────────────────────────────────────────────
    # .superreact
    # ─────────────────────────────────────────────────────────────────────────

    async def _superreact(self, message, args, rc):
        """
        .superreact <emoji>              — react to YOUR OWN messages
        .superreact <@user|uid> <emoji>  — react to that user's messages
        .superreact list                 — show every active target
        .superreact stop / off           — disable own-message superreact
        """
        if len(args) < 2:
            return await message.edit(content=S.ui_err(
                "usage: superreact <emoji>"
                "  |  superreact <@user|uid> <emoji>"
                "  |  superreact list  |  superreact stop"))

        sub = args[1].lower()

        if sub == "list":
            rows = [f"  {S.DIM}self{S.RESET}  "
                    f"{'off' if not S._superreact_emoji else S._superreact_emoji}"]
            for uid, emoji in sorted(self._superreact_users.items()):
                rows.append(f"  {S.GREY}•{S.RESET} {self._ulabel(uid)}  →  {emoji}")
            return await message.edit(content=S.ui_box("superreact targets", rows))

        if sub in ("stop", "off", "disable"):
            S._superreact_emoji = None
            _sync(_superreact_emoji=None)
            return await message.edit(content=S.ui_ok(
                "superreact → off (own messages)"))

        uid, raw_emoji = _parse_user_and_emoji(args, 1, rc)

        if uid is not None and raw_emoji is None:
            return await message.edit(content=S.ui_err(
                f"usage: superreact {self._ulabel(uid)} <emoji>"))

        if raw_emoji is None:
            return await message.edit(content=S.ui_err(
                "couldn't read emoji — paste it directly after the command"))

        emoji = self._resolve(raw_emoji)

        if uid is not None:
            self._superreact_users[uid] = emoji
            await message.edit(content=S.ui_ok(
                f"superreact → {emoji}  (messages from {self._ulabel(uid)})"))
        else:
            S._superreact_emoji = emoji
            _sync(_superreact_emoji=emoji)
            await message.edit(content=S.ui_ok(
                f"superreact → {emoji}  (your own messages)"))

    # ─────────────────────────────────────────────────────────────────────────
    # .superreactstop
    # ─────────────────────────────────────────────────────────────────────────

    async def _superreactstop(self, message, args):
        """
        .superreactstop              — stop own-message superreact
        .superreactstop <@user|uid>  — remove per-user target
        .superreactstop all          — stop everything
        """
        if len(args) >= 2:
            sub = args[1].lower()
            if sub == "all":
                S._superreact_emoji = None
                _sync(_superreact_emoji=None)
                n = len(self._superreact_users)
                self._superreact_users.clear()
                return await message.edit(content=S.ui_ok(
                    f"superreact → off  (self + {n} user target(s) cleared)"))
            uid = _parse_user_id(args[1])
            if uid is not None:
                if uid in self._superreact_users:
                    del self._superreact_users[uid]
                    return await message.edit(content=S.ui_ok(
                        f"superreact → off for {self._ulabel(uid)}"))
                return await message.edit(content=S.ui_info(
                    f"no superreact target set for {self._ulabel(uid)}"))

        S._superreact_emoji = None
        _sync(_superreact_emoji=None)
        await message.edit(content=S.ui_ok("superreact → off (own messages)"))

    # ─────────────────────────────────────────────────────────────────────────
    # .multireact
    # ─────────────────────────────────────────────────────────────────────────

    async def _multireact(self, message, args, rc):
        """
        .multireact add <emoji>                 — add to global pool
        .multireact add <@user|uid> <emoji>     — add to user pool
        .multireact remove <emoji>              — remove from global pool
        .multireact remove <@user|uid> <emoji>  — remove from user pool
        .multireact on  [<@user|uid>]           — enable global or per-user
        .multireact off [<@user|uid>]           — disable global or per-user
        .multireact list                        — show all pools
        .multireact clear [<@user|uid>]         — clear global or user pool
        """
        sub = args[1].lower() if len(args) > 1 else ""

        # ── add ──────────────────────────────────────────────────────────────
        if sub == "add":
            if len(args) < 3:
                return await message.edit(content=S.ui_err(
                    "usage: multireact add <emoji>"
                    "  |  multireact add <@user|uid> <emoji>"))
            uid, raw_emoji = _parse_user_and_emoji(args, 2, rc)
            if raw_emoji is None:
                return await message.edit(content=S.ui_err(
                    "couldn't read emoji"))
            emoji = self._resolve(raw_emoji)

            if uid is not None:
                pool = self._multireact_users.setdefault(uid, [])
                if emoji in pool:
                    return await message.edit(content=S.ui_info(
                        f"{emoji} already in {self._ulabel(uid)}'s pool"))
                pool.append(emoji)
                return await message.edit(content=S.ui_ok(
                    f"added {emoji} → {self._ulabel(uid)}'s pool  "
                    f"({len(pool)} total)"))
            else:
                if emoji in S._multireact_pool:
                    return await message.edit(content=S.ui_info(
                        "already in global pool"))
                S._multireact_pool.append(emoji)
                return await message.edit(content=S.ui_ok(
                    f"added {emoji} → global pool  "
                    f"({len(S._multireact_pool)} total)"))

        # ── remove ───────────────────────────────────────────────────────────
        elif sub in ("remove", "rem", "del"):
            if len(args) < 3:
                return await message.edit(content=S.ui_err(
                    "usage: multireact remove <emoji>"
                    "  |  multireact remove <@user|uid> <emoji>"))
            uid, raw_emoji = _parse_user_and_emoji(args, 2, rc)
            if raw_emoji is None:
                return await message.edit(content=S.ui_err("couldn't read emoji"))
            emoji = self._resolve(raw_emoji)

            if uid is not None:
                pool = self._multireact_users.get(uid, [])
                if emoji not in pool:
                    return await message.edit(content=S.ui_err(
                        f"{emoji} not in {self._ulabel(uid)}'s pool"))
                pool.remove(emoji)
                return await message.edit(content=S.ui_ok(
                    f"removed {emoji} from {self._ulabel(uid)}'s pool"))
            else:
                if emoji not in S._multireact_pool:
                    return await message.edit(content=S.ui_err(
                        "not in global pool"))
                S._multireact_pool.remove(emoji)
                return await message.edit(content=S.ui_ok(
                    f"removed {emoji}  ({len(S._multireact_pool)} remaining)"))

        # ── on ───────────────────────────────────────────────────────────────
        elif sub in ("on", "enable"):
            uid = _parse_user_id(args[2]) if len(args) > 2 else None
            if uid is not None:
                if not self._multireact_users.get(uid):
                    return await message.edit(content=S.ui_err(
                        f"no pool for {self._ulabel(uid)} — add emojis first"))
                self._multireact_users_on[uid] = True
                return await message.edit(content=S.ui_ok(
                    f"multireact → on for {self._ulabel(uid)}"))
            else:
                if not S._multireact_pool:
                    return await message.edit(content=S.ui_err(
                        "global pool is empty — add emojis first"))
                S._multireact_enabled = True
                _sync(_multireact_enabled=True)
                return await message.edit(content=S.ui_ok(
                    "multireact → on (global)"))

        # ── off ──────────────────────────────────────────────────────────────
        elif sub in ("off", "disable"):
            uid = _parse_user_id(args[2]) if len(args) > 2 else None
            if uid is not None:
                self._multireact_users_on[uid] = False
                return await message.edit(content=S.ui_ok(
                    f"multireact → off for {self._ulabel(uid)}"))
            else:
                S._multireact_enabled = False
                _sync(_multireact_enabled=False)
                return await message.edit(content=S.ui_ok(
                    "multireact → off (global)"))

        # ── list ─────────────────────────────────────────────────────────────
        elif sub == "list":
            rows = []
            g_status = "ON" if S._multireact_enabled else "OFF"
            if S._multireact_pool:
                rows.append(f"  {S.DIM}global [{g_status}]{S.RESET}")
                for i, e in enumerate(S._multireact_pool, 1):
                    rows.append(f"    {S.GREY}{i}.{S.RESET}  {e}")
            else:
                rows.append(f"  {S.DIM}global [{g_status}]{S.RESET}  (empty)")
            for uid, pool in sorted(self._multireact_users.items()):
                on = self._multireact_users_on.get(uid, False)
                rows.append(
                    f"  {S.GREY}•{S.RESET} {self._ulabel(uid)}  "
                    f"[{'ON' if on else 'OFF'}]")
                for i, e in enumerate(pool, 1):
                    rows.append(f"    {S.GREY}{i}.{S.RESET}  {e}")
            return await message.edit(
                content=S.ui_box("multireact pools", rows)
                if rows else S.ui_info("no pools configured"))

        # ── clear ─────────────────────────────────────────────────────────────
        elif sub == "clear":
            uid = _parse_user_id(args[2]) if len(args) > 2 else None
            if uid is not None:
                self._multireact_users.pop(uid, None)
                self._multireact_users_on.pop(uid, None)
                return await message.edit(content=S.ui_ok(
                    f"multireact pool cleared for {self._ulabel(uid)}"))
            else:
                S._multireact_pool.clear()
                S._multireact_enabled = False
                _sync(_multireact_enabled=False)
                return await message.edit(content=S.ui_ok(
                    "global multireact pool cleared"))

        else:
            await message.edit(content=S.ui_info(
                "multireact add/remove/on/off/list/clear  <emoji>"
                "  |  <@user|uid> <emoji>"))

    # ─────────────────────────────────────────────────────────────────────────
    # .vsniper
    # ─────────────────────────────────────────────────────────────────────────

    async def _vsniper(self, message, args):
        sub = args[1].lower() if len(args) > 1 else ""

        if sub == "add" and len(args) >= 4:
            S._vsniper_list.append({"code": args[2], "guild_id": args[3]})
            await message.edit(content=S.ui_ok(f"watching: {args[2]}"))
        elif sub == "start":
            if S._vsniper_task and not S._vsniper_task.done():
                return await message.edit(content=S.ui_info("already running"))
            S._vsniper_task = asyncio.create_task(_vsniper_loop())
            _sync(_vsniper_task=S._vsniper_task)
            await message.edit(content=S.ui_ok("vsniper → started"))
        elif sub == "stop":
            if S._vsniper_task:
                S._vsniper_task.cancel()
                S._vsniper_task = None
                _sync(_vsniper_task=None)
            await message.edit(content=S.ui_ok("vsniper → stopped"))
        elif sub == "list":
            rows = [f"  {S.GREY}•{S.RESET} {e['code']}  "
                    f"{S.DIM}guild {e['guild_id']}{S.RESET}"
                    for e in S._vsniper_list]
            await message.edit(
                content=S._paginate("vsniper", "watch list", rows)
                if rows else S.ui_info("empty"))
        else:
            await message.edit(content=S.ui_info(
                "vsniper add <code> <guild_id> | start | stop | list"))

    # ─────────────────────────────────────────────────────────────────────────
    # .reactdiag
    # ─────────────────────────────────────────────────────────────────────────

    async def _reactdiag(self, message):
        try:
            from modifyself.models.message import Message as _M
            patched = bool(getattr(_M, "_raw_react_patched", False))
        except ImportError:
            patched = "import-failed"

        rows = [
            f"  {S.DIM}patch installed{S.RESET}      {patched}",
            f"  {S.DIM}global autoreact{S.RESET}     "
            f"{S._autoreact_emoji or 'off'}",
            f"  {S.DIM}global superreact{S.RESET}    "
            f"{S._superreact_emoji or 'off'}",
            f"  {S.DIM}global multi{S.RESET}         "
            f"{'ON' if S._multireact_enabled else 'OFF'}  "
            f"pool={S._multireact_pool}",
            "",
            f"  {S.DIM}per-user autoreact{S.RESET}   "
            f"{len(self._autoreact_users)} target(s)",
        ]
        for uid, emoji in sorted(self._autoreact_users.items()):
            rows.append(f"    {S.GREY}•{S.RESET} <@{uid}>  →  {emoji}")

        rows.append(f"  {S.DIM}per-user superreact{S.RESET}  "
                    f"{len(self._superreact_users)} target(s)")
        for uid, emoji in sorted(self._superreact_users.items()):
            rows.append(f"    {S.GREY}•{S.RESET} <@{uid}>  →  {emoji}")

        rows.append(f"  {S.DIM}per-user multireact{S.RESET}  "
                    f"{len(self._multireact_users)} pool(s)")
        for uid, pool in sorted(self._multireact_users.items()):
            on = self._multireact_users_on.get(uid, False)
            rows.append(f"    {S.GREY}•{S.RESET} <@{uid}>  "
                        f"[{'ON' if on else 'OFF'}]  {pool}")

        await message.edit(content=S._ansi_block(rows))
