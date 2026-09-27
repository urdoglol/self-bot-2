# cogs/auto.py | giveaway, nitrosniper, autoreact, multireact, vsniper, superreact
#
# ROOT CAUSE FIX (why autoreact never fired):
#   The cog had NO on_message listener — commands stored the emoji in state
#   but nothing ever read state and called _react() on incoming messages.
#   Added on_message() which handles autoreact, multireact, superreact,
#   giveaway joins, and nitro sniping all in one place.
#
# ADDITIONAL FIXES:
#   [BUG-1] _react_once returned str on error → int compare always failed
#   [BUG-2] Custom emoji URL encoding was wrong (<:name:id> → 400 from Discord)
#   [BUG-3] args[1] grabbed wrong token when emoji was split across args
#   [BUG-4] multireact duplicate check compared un-normalised strings
#   [BUG-5] superreact lowercased the emoji string before storing it
#   [BUG-6] vsniper created a new ClientSession per entry per tick
#   [BUG-7] react patch raised wrong error on non-204 status
#   [BUG-8] autoreact command: when user typed .autoreact <custom_emoji>,
#           the raw message content passes the emoji as <:name:id> but
#           modifyself splits on whitespace so args[1] = "<:name" and
#           args[2] = "id>" — fixed by re-joining and regex-extracting.

import asyncio
import re
import sys
import urllib.parse

import aiohttp
import modifyself_shim as discord
from . import state as S

# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------
_RE_CUSTOM    = re.compile(r"<(a?):([A-Za-z0-9_]+):(\d+)>")
_RE_SHORTCODE = re.compile(r":([A-Za-z0-9_]+):")
_RE_NITRO     = re.compile(r"discord\.gift/([A-Za-z0-9]+)")
_RE_GIVEAWAY  = re.compile(r"🎉|giveaway", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sync(**kw):
    main = sys.modules.get("__main__")
    if main is None:
        return
    for k, v in kw.items():
        try:
            setattr(main, k, v)
        except Exception:
            pass


def _emoji_to_str(emoji) -> str:
    if isinstance(emoji, str):
        return emoji
    name     = getattr(emoji, "name",     None)
    eid      = getattr(emoji, "id",       None)
    animated = getattr(emoji, "animated", False)
    if name and eid:
        return f"<{'a' if animated else ''}:{name}:{eid}>"
    return str(name or emoji)


def _emoji_url_part(emoji_str: str) -> str:
    """
    Encode emoji for Discord's reaction URL.
    Custom: <a:name:id> or <:name:id>  →  name%3Aid
    Unicode: 👍  →  %F0%9F%91%8D
    """
    m = _RE_CUSTOM.match(emoji_str.strip())
    if m:
        return urllib.parse.quote(f"{m.group(2)}:{m.group(3)}", safe="")
    return urllib.parse.quote(emoji_str, safe="")


def _parse_emoji_arg(args: list, start: int = 1) -> str:
    """
    Re-join args[start:] and extract the first emoji token.
    Fixes the split-token bug where <:name:id> becomes ["<:name", "id>"].
    """
    raw = " ".join(args[start:]).strip()
    m = _RE_CUSTOM.search(raw)
    if m:
        return m.group(0)
    m = _RE_SHORTCODE.search(raw)
    if m:
        return m.group(0)
    parts = raw.split()
    return parts[0] if parts else raw


def _resolve_emoji(client, raw: str) -> str:
    """
    :shortcode: or bare name → <:name:id> by searching joined guilds.
    Full <:name:id> and unicode pass through unchanged.
    """
    if _RE_CUSTOM.match(raw.strip()):
        return raw
    target = raw.strip(": ").lower()
    for guild in (getattr(client, "guilds", None) or []):
        for e in getattr(guild, "emojis", []):
            if getattr(e, "name", "").lower() == target:
                prefix = "a" if getattr(e, "animated", False) else ""
                return f"<{prefix}:{e.name}:{e.id}>"
    return raw


# ---------------------------------------------------------------------------
# REST reaction
# ---------------------------------------------------------------------------

async def _react_once(session, channel_id, message_id, emoji) -> tuple:
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


async def _react(channel_id, message_id, emoji, session=None) -> int:
    own = session is None
    if own:
        session = aiohttp.ClientSession()
    try:
        for attempt in range(4):
            status, retry_after = await _react_once(session, channel_id, message_id, emoji)
            if status == 429:
                wait = max(float(retry_after or 1.0), 0.5)
                print(f"[auto] rate-limit, wait {wait:.2f}s (attempt {attempt + 1})")
                await asyncio.sleep(wait)
                continue
            if status == -1:
                await asyncio.sleep(0.5)
                continue
            return status
        return 429
    finally:
        if own:
            await session.close()


# ---------------------------------------------------------------------------
# Shim patch
# ---------------------------------------------------------------------------

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
            ch_id  = getattr(self, "channel_id", None)
            if ch_id is None:
                ch = getattr(self, "channel", None)
                ch_id = getattr(ch, "id", None) if ch else None
            msg_id = getattr(self, "id", None)
            if not ch_id or not msg_id:
                raise err
            status = await _react(ch_id, msg_id, emoji)
            if isinstance(status, int) and status in (200, 204):
                return None
            raise RuntimeError(
                f"[auto] react failed HTTP {status} "
                f"ch={ch_id} msg={msg_id} emoji={_emoji_to_str(emoji)!r}"
            )

    M.add_reaction       = _patched
    M._raw_react_patched = True
    print("[auto] Message.add_reaction patched")
    return True


# ---------------------------------------------------------------------------
# Nitro sniper
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Vanity sniper
# ---------------------------------------------------------------------------

async def _vsniper_loop():
    while True:
        entries = list(S._vsniper_list)
        if entries:
            h = {"Authorization": S.TOKEN, "Content-Type": "application/json",
                 "User-Agent": S.USER_AGENT}
            async with aiohttp.ClientSession() as s:
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
                                        S.log_msg("VSNIPER", f"CLAIMED {code} guild={guild_id}")
                    except Exception as e:
                        print(f"[auto] vsniper error {code}: {e}")
        await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# AutoCog
# ---------------------------------------------------------------------------

class AutoCog:
    COMMANDS = {
        "giveaway", "nitrosniper",
        "autoreact", "autoreactstop",
        "multireact", "multiautoreact",
        "vsniper",
        "superreact", "superreactstop",
        "reactdiag",
    }

    def __init__(self):
        _install_react_patch()

    # ── internal ──────────────────────────────────────────────────────

    @staticmethod
    def _client():
        main = sys.modules.get("__main__")
        return getattr(main, "client", None) if main else None

    def _resolve(self, raw: str) -> str:
        client = self._client()
        return _resolve_emoji(client, raw) if client else raw

    # ── ON MESSAGE — this is what was missing ─────────────────────────

    async def on_message(self, message):
        """
        Called for every message the selfbot sees.
        Handles: autoreact, multireact, superreact, giveaway, nitrosniper.
        Must be wired up in your main dispatcher:
            await auto_cog.on_message(message)
        """
        content = getattr(message, "content", "") or ""
        ch_id   = getattr(message, "channel_id", None)
        if ch_id is None:
            ch    = getattr(message, "channel", None)
            ch_id = getattr(ch, "id", None) if ch else None
        msg_id  = getattr(message, "id", None)

        if not ch_id or not msg_id:
            return

        author_id = str(getattr(getattr(message, "author", None), "id", ""))
        my_id     = str(getattr(self._client(), "user", None) and
                        getattr(self._client().user, "id", ""))

        # ── nitro sniper ──────────────────────────────────────────────
        if S._nitrosniper_enabled:
            for code in _RE_NITRO.findall(content):
                asyncio.create_task(_snipe_nitro(code))

        # ── giveaway auto-enter ───────────────────────────────────────
        if S._giveaway_enabled and _RE_GIVEAWAY.search(content):
            # React with 🎉 to enter — common giveaway bot trigger
            asyncio.create_task(_react(ch_id, msg_id, "🎉"))

        # ── superreact (react to your OWN messages) ───────────────────
        if S._superreact_emoji and my_id and author_id == my_id:
            asyncio.create_task(_react(ch_id, msg_id, S._superreact_emoji))
            return  # don't double-react your own messages with autoreact too

        # ── autoreact (react to ALL messages) ────────────────────────
        if S._autoreact_emoji:
            asyncio.create_task(_react(ch_id, msg_id, S._autoreact_emoji))

        # ── multireact (react with every emoji in pool) ───────────────
        if S._multireact_enabled and S._multireact_pool:
            async def _multi():
                async with aiohttp.ClientSession() as sess:
                    for e in list(S._multireact_pool):
                        status = await _react(ch_id, msg_id, e, session=sess)
                        if status not in (200, 204):
                            print(f"[auto] multireact failed {e!r} → HTTP {status}")
                        await asyncio.sleep(0.3)   # small gap between each emoji
            asyncio.create_task(_multi())

    # ── COMMANDS ──────────────────────────────────────────────────────

    async def handle(self, message, cmd, args):

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
            if len(args) < 2:
                return await message.edit(
                    content=S.ui_err("usage: autoreact <emoji>"))
            # Re-join in case emoji was split: .autoreact <:name:id> → args=["<:name","id>"]
            raw   = _parse_emoji_arg(args, 1)
            emoji = self._resolve(raw)
            S._autoreact_emoji = emoji
            _sync(_autoreact_emoji=emoji)
            await message.edit(content=S.ui_ok(f"autoreact → {emoji}"))

        elif cmd == "autoreactstop":
            S._autoreact_emoji = None
            _sync(_autoreact_emoji=None)
            await message.edit(content=S.ui_ok("autoreact → off"))

        elif cmd == "superreact":
            if len(args) < 2:
                return await message.edit(
                    content=S.ui_err("usage: superreact <emoji> | superreact stop"))
            ctrl = args[1].lower()
            if ctrl in ("stop", "off", "disable"):
                S._superreact_emoji = None
                _sync(_superreact_emoji=None)
                return await message.edit(content=S.ui_ok("superreact → off"))
            raw   = _parse_emoji_arg(args, 1)
            emoji = self._resolve(raw)
            S._superreact_emoji = emoji
            _sync(_superreact_emoji=emoji)
            await message.edit(content=S.ui_ok(f"superreact → {emoji}"))

        elif cmd == "superreactstop":
            S._superreact_emoji = None
            _sync(_superreact_emoji=None)
            await message.edit(content=S.ui_ok("superreact → off"))

        elif cmd == "reactdiag":
            try:
                from modifyself.models.message import Message as _M
                patched = bool(getattr(_M, "_raw_react_patched", False))
            except ImportError:
                patched = "import-failed"
            main       = sys.modules.get("__main__")
            main_ar    = getattr(main, "_autoreact_emoji",    None) if main else None
            main_sr    = getattr(main, "_superreact_emoji",   None) if main else None
            main_multi = getattr(main, "_multireact_enabled", None) if main else None
            lines = [
                f"  patch installed:     {patched}",
                f"  S._autoreact_emoji:  {S._autoreact_emoji!r}",
                f"  main._autoreact:     {main_ar!r}",
                f"  S._superreact_emoji: {S._superreact_emoji!r}",
                f"  main._superreact:    {main_sr!r}",
                f"  S._multireact_en:    {S._multireact_enabled}",
                f"  main._multireact:    {main_multi}",
                f"  pool (S):            {S._multireact_pool}",
                f"  on_message wired:    (check your main dispatcher)",
            ]
            await message.edit(content=S._ansi_block(lines))

        elif cmd in ("multireact", "multiautoreact"):
            sub = args[1].lower() if len(args) > 1 else ""

            if sub == "add" and len(args) >= 3:
                raw   = _parse_emoji_arg(args, 2)
                emoji = self._resolve(raw)
                if emoji in S._multireact_pool:
                    return await message.edit(content=S.ui_info("already in pool"))
                S._multireact_pool.append(emoji)
                await message.edit(content=S.ui_ok(
                    f"added {emoji} ({len(S._multireact_pool)} in pool)"))

            elif sub in ("remove", "rem", "del") and len(args) >= 3:
                raw   = _parse_emoji_arg(args, 2)
                emoji = self._resolve(raw)
                if emoji not in S._multireact_pool:
                    return await message.edit(content=S.ui_err("not in pool"))
                S._multireact_pool.remove(emoji)
                await message.edit(content=S.ui_ok(
                    f"removed ({len(S._multireact_pool)} remaining)"))

            elif sub == "list":
                rows = [f"  {S.GREY}{i:2}.{S.RESET}  {e}"
                        for i, e in enumerate(S._multireact_pool, 1)]
                await message.edit(
                    content=S.ui_box(
                        f"multi pool — {'ON' if S._multireact_enabled else 'OFF'}", rows)
                    if rows else S.ui_info("pool is empty"))

            elif sub in ("on", "enable"):
                if not S._multireact_pool:
                    return await message.edit(content=S.ui_err("pool is empty"))
                S._multireact_enabled = True
                _sync(_multireact_enabled=True)
                await message.edit(content=S.ui_ok("multireact → on"))

            elif sub in ("off", "disable"):
                S._multireact_enabled = False
                _sync(_multireact_enabled=False)
                await message.edit(content=S.ui_ok("multireact → off"))

            elif sub == "clear":
                S._multireact_pool.clear()
                S._multireact_enabled = False
                _sync(_multireact_enabled=False)
                await message.edit(content=S.ui_ok("pool cleared"))

            else:
                await message.edit(content=S.ui_info(
                    "multireact add/remove/list/on/off/clear <emoji>"))

        elif cmd == "vsniper":
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
