# cogs/auto.py | giveaway, nitrosniper, autoreact, multireact, vsniper, superreact
#
# FIXES vs original:
#  [BUG-1] _react_once returned ("err:...", None) — a str — but callers did
#          `status, retry_after = ...` and then compared `status == 429` (int).
#          An error string never matched, so the loop silently swallowed every
#          network/connection error and returned the last bad status.  Fixed by
#          always returning a 2-tuple and treating non-int status as a hard fail.
#
#  [BUG-2] Custom-emoji encoding was wrong for the REST reaction endpoint.
#          Discord expects   name:id   (no angle brackets, no "a:" prefix in the
#          URL path) for custom emoji, and the plain codepoint for unicode emoji.
#          The original just urllib-encoded the full "<a:name:id>" string which
#          produced %3Ca%3Aname%3Aid%3E — Discord rejected it with 400.
#          Fixed in _emoji_url_part().
#
#  [BUG-3] autoreact / superreact / multireact stored args[1] raw.  When a user
#          typed a custom emoji like <:zaraki:123456> the shell/Discord client
#          sometimes passes it as a single token, sometimes split across args.
#          Added _parse_emoji_arg() which re-joins args and extracts the emoji
#          correctly whether it is a unicode char, :name: shortcode, or the full
#          <[a]:name:id> form.
#
#  [BUG-4] multireact "add" compared args[2] (raw string) against pool entries
#          that might have been normalised differently — duplicate-check could
#          fail.  Now we normalise before comparing.
#
#  [BUG-5] superreact stored args[1].lower() when checking "stop/off/disable",
#          but then stored the lowercased string as the emoji.  If the sub was
#          NOT a control word the branch fell through and stored the lowercased
#          emoji name, breaking case-sensitive custom emoji names.  Fixed.
#
#  [BUG-6] vsniper loop created a new aiohttp.ClientSession() per entry per
#          tick — up to hundreds of sessions/second for large watch lists.
#          Fixed: one session per loop tick, shared across entries.
#
#  [BUG-7] _install_react_patch() patched Message.add_reaction but the fallback
#          only checked status in (200, 204).  Discord returns 204 No Content on
#          success; 200 is never returned for PUT reactions.  The 200 check is
#          harmless but the real bug was that any non-204 non-200 int (e.g. 403)
#          silently re-raised the original shim error instead of a useful one.
#          Now raises a descriptive RuntimeError on unexpected status codes.
#
#  [NEW]   _resolve_custom_emoji(client, name) — looks up a custom emoji by name
#          across all guilds the selfbot is in, returning the correct URL-encoded
#          path component so commands like  autoreact :zaraki:  work without
#          needing the full <:zaraki:123456789> syntax.

import asyncio
import re
import sys
import urllib.parse

import aiohttp
import modifyself_shim as discord
from . import state as S

# ---------------------------------------------------------------------------
# Regex patterns for emoji forms
# ---------------------------------------------------------------------------
_RE_CUSTOM   = re.compile(r"<(a?):([A-Za-z0-9_]+):(\d+)>")   # <a:name:id> or <:name:id>
_RE_SHORTCODE = re.compile(r":([A-Za-z0-9_]+):")               # :name:  (no id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sync(**kw):
    """Mirror state values onto __main__ so other modules see them."""
    main = sys.modules.get("__main__")
    if main is None:
        return
    for k, v in kw.items():
        try:
            setattr(main, k, v)
        except Exception:
            pass


def _emoji_to_str(emoji) -> str:
    """Convert an emoji object or string to its canonical string form."""
    if isinstance(emoji, str):
        return emoji
    name     = getattr(emoji, "name",     None)
    eid      = getattr(emoji, "id",       None)
    animated = getattr(emoji, "animated", False)
    if name and eid:
        prefix = "a" if animated else ""
        return f"<{prefix}:{name}:{eid}>"
    if name:
        return str(name)
    return str(emoji)


def _emoji_url_part(emoji_str: str) -> str:
    """
    Convert an emoji string to the correctly encoded URL path segment for
    Discord's reaction endpoint.

    Discord REST API rules:
      • Unicode emoji  →  percent-encode the raw codepoints
                          e.g.  👍  →  %F0%9F%91%8D
      • Custom emoji   →  name:id   (NO angle brackets, NO "a:" prefix)
                          e.g.  <a:zaraki:123456>  →  zaraki:123456
                          then percent-encode just the colon:  zaraki%3A123456

    BUG-2 fix: the original code encoded the full "<a:name:id>" string which
    Discord rejected with HTTP 400.
    """
    m = _RE_CUSTOM.match(emoji_str.strip())
    if m:
        # Custom emoji: drop < > and animated prefix; keep name:id
        name, eid = m.group(2), m.group(3)
        return urllib.parse.quote(f"{name}:{eid}", safe="")
    # Unicode / plain text emoji
    return urllib.parse.quote(emoji_str, safe="")


def _parse_emoji_arg(args: list, start: int = 1) -> str:
    """
    Re-join args from `start` onwards and extract the first emoji-like token.

    Handles:
      • Single unicode emoji:       👍
      • Custom emoji full form:     <:zaraki:123456>  or  <a:zaraki:123456>
      • Shortcode (name only):      :zaraki:
      • Plain name (fallback):      zaraki

    BUG-3 fix: args can be split mid-emoji by the command parser.
    """
    raw = " ".join(args[start:]).strip()

    # Full custom emoji (may have been split across tokens — rejoin first)
    m = _RE_CUSTOM.search(raw)
    if m:
        return m.group(0)

    # Shortcode :name:
    m = _RE_SHORTCODE.search(raw)
    if m:
        return m.group(0)   # keep as :name: for _resolve_custom_emoji later

    # Return first whitespace-free token (unicode emoji or plain word)
    return raw.split()[0] if raw.split() else raw


def _resolve_custom_emoji(client, name_or_shortcode: str):
    """
    Given  :zaraki:  or  zaraki  try to find the matching custom emoji across
    all guilds and return the full  <a:name:id>  string.

    Returns the original string unchanged if nothing is found.
    This lets users type  autoreact :zaraki:  without knowing the emoji ID.
    """
    target = name_or_shortcode.strip(": ").lower()
    guilds = getattr(client, "guilds", None) or []
    for guild in guilds:
        for emoji in getattr(guild, "emojis", []):
            if getattr(emoji, "name", "").lower() == target:
                animated = getattr(emoji, "animated", False)
                eid      = getattr(emoji, "id",       None)
                ename    = getattr(emoji, "name",     target)
                prefix   = "a" if animated else ""
                return f"<{prefix}:{ename}:{eid}>"
    return name_or_shortcode   # unchanged — might be unicode or unknown


# ---------------------------------------------------------------------------
# Core reaction REST call
# ---------------------------------------------------------------------------

async def _react_once(session: aiohttp.ClientSession,
                      channel_id, message_id, emoji) -> tuple:
    """
    PUT one reaction.  Always returns a 2-tuple (status, retry_after).

    BUG-1 fix: previously returned ("err:...", None) on exception which broke
    the int comparison in _react().  Now returns (-1, None) on error so the
    caller can handle it uniformly.
    """
    emoji_str = _emoji_to_str(emoji)
    emoji_enc = _emoji_url_part(emoji_str)          # BUG-2 fix
    url = (
        f"https://discord.com/api/v9/channels/{channel_id}"
        f"/messages/{message_id}/reactions/{emoji_enc}/@me"
    )
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
        print(f"[auto] _react_once network error: {e}")
        return -1, None     # BUG-1 fix: always return int status


async def _react(channel_id, message_id, emoji, session=None) -> int:
    """
    Retry-aware reaction sender.  Retries up to 4 times on 429.
    Returns the final HTTP status code (int).
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        for attempt in range(4):
            status, retry_after = await _react_once(
                session, channel_id, message_id, emoji)

            if status == 429:
                wait = max(float(retry_after or 1.0), 0.5)
                print(f"[auto] rate-limited, sleeping {wait:.2f}s (attempt {attempt+1})")
                await asyncio.sleep(wait)
                continue

            if status == -1:
                # network error — short backoff then retry
                await asyncio.sleep(0.5)
                continue

            return status   # success or hard Discord error (400, 403, …)

        return 429  # exhausted retries
    finally:
        if own_session:
            await session.close()


# ---------------------------------------------------------------------------
# Message.add_reaction shim patch
# ---------------------------------------------------------------------------

def _install_react_patch():
    try:
        from modifyself.models.message import Message as _MSMessage
    except ImportError as e:
        print(f"[auto] cannot import Message for react patch: {e}")
        return False

    if getattr(_MSMessage, "_raw_react_patched", False):
        return True

    original = _MSMessage.add_reaction

    async def patched_add_reaction(self, emoji, *args, **kwargs):
        try:
            return await original(self, emoji, *args, **kwargs)
        except Exception as shim_err:
            ch_id  = getattr(self, "channel_id", None)
            if ch_id is None:
                ch    = getattr(self, "channel", None)
                ch_id = getattr(ch, "id", None) if ch is not None else None
            msg_id = getattr(self, "id", None)
            if ch_id is None or msg_id is None:
                raise shim_err

            status = await _react(ch_id, msg_id, emoji)

            # BUG-7 fix: Discord only ever returns 204 for PUT reactions.
            # Treat 204 (and 200 defensively) as success; anything else is
            # a real failure — raise a descriptive error instead of the
            # original shim error which had no useful info.
            if isinstance(status, int) and status in (200, 204):
                return None
            raise RuntimeError(
                f"[auto] raw react failed: HTTP {status} "
                f"(ch={ch_id} msg={msg_id} emoji={_emoji_to_str(emoji)!r})"
            )

    _MSMessage.add_reaction       = patched_add_reaction
    _MSMessage._raw_react_patched = True
    print("[auto] Message.add_reaction patched (shim → raw REST fallback)")
    return True


# ---------------------------------------------------------------------------
# Vanity-URL sniper loop
# ---------------------------------------------------------------------------

async def _vsniper_loop():
    """
    BUG-6 fix: original created one aiohttp.ClientSession per entry per tick,
    potentially hundreds per second for large watch lists.  One session per
    tick is correct.
    """
    while True:
        entries = list(S._vsniper_list)
        if entries:
            h = {
                "Authorization":  S.TOKEN,
                "Content-Type":   "application/json",
                "User-Agent":     S.USER_AGENT,
            }
            async with aiohttp.ClientSession() as s:       # BUG-6 fix
                for entry in entries:
                    code     = entry["code"]
                    guild_id = entry["guild_id"]
                    try:
                        async with s.get(
                            f"https://discord.com/api/v9/invites/{code}",
                            headers=h
                        ) as r:
                            if r.status == 404:
                                async with s.patch(
                                    f"https://discord.com/api/v9/guilds/{guild_id}/vanity-url",
                                    headers=h,
                                    json={"code": code},
                                ) as r2:
                                    if r2.status in (200, 204) and S.log_msg:
                                        S.log_msg("VSNIPER",
                                                  f"CLAIMED {code} for guild {guild_id}")
                    except Exception as e:
                        print(f"[auto] vsniper error for {code}: {e}")
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

    # ------------------------------------------------------------------
    # Utility: get the client from state if available (for emoji lookup)
    # ------------------------------------------------------------------
    @staticmethod
    def _client():
        main = sys.modules.get("__main__")
        return getattr(main, "client", None) if main else None

    def _resolve(self, raw: str) -> str:
        """
        Resolve :shortcode: → full <:name:id> using joined guilds.
        Passes through unicode emoji and already-full custom emoji unchanged.
        """
        if _RE_CUSTOM.match(raw.strip()):
            return raw  # already full form
        if _RE_SHORTCODE.match(raw.strip()) or not raw.startswith("<"):
            client = self._client()
            if client:
                return _resolve_custom_emoji(client, raw)
        return raw

    # ------------------------------------------------------------------
    # Command handler
    # ------------------------------------------------------------------

    async def handle(self, message, cmd, args):

        # ── giveaway ───────────────────────────────────────────────────
        if cmd == "giveaway":
            S._giveaway_enabled = len(args) < 2 or args[1].lower() in ("on", "enable")
            _sync(_giveaway_enabled=S._giveaway_enabled)
            await message.edit(content=S.ui_ok(
                f"giveaway → {'on' if S._giveaway_enabled else 'off'}"))

        # ── nitrosniper ────────────────────────────────────────────────
        elif cmd == "nitrosniper":
            S._nitrosniper_enabled = len(args) < 2 or args[1].lower() in ("on", "enable")
            _sync(_nitrosniper_enabled=S._nitrosniper_enabled)
            await message.edit(content=S.ui_ok(
                f"nitrosniper → {'on' if S._nitrosniper_enabled else 'off'}"))

        # ── autoreact ──────────────────────────────────────────────────
        elif cmd == "autoreact":
            if len(args) < 2:
                return await message.edit(
                    content=S.ui_err("usage: autoreact <emoji>  (unicode, :name:, or <:name:id>)"))
            # BUG-3 fix: parse + resolve custom emoji
            raw = _parse_emoji_arg(args, 1)
            emoji = self._resolve(raw)
            S._autoreact_emoji = emoji
            _sync(_autoreact_emoji=emoji)
            await message.edit(content=S.ui_ok(f"reacting with {emoji}"))

        # ── autoreactstop ──────────────────────────────────────────────
        elif cmd == "autoreactstop":
            S._autoreact_emoji = None
            _sync(_autoreact_emoji=None)
            await message.edit(content=S.ui_ok("autoreact → stopped"))

        # ── superreact ─────────────────────────────────────────────────
        elif cmd == "superreact":
            if len(args) < 2:
                return await message.edit(
                    content=S.ui_err(
                        "usage: superreact <emoji>  |  superreact stop\n"
                        "       emoji can be unicode, :name:, or <:name:id>"))

            # BUG-5 fix: check control word BEFORE lowercasing the emoji
            ctrl = args[1].lower()
            if ctrl in ("stop", "off", "disable"):
                S._superreact_emoji = None
                _sync(_superreact_emoji=None)
                return await message.edit(content=S.ui_ok("superreact → off"))

            raw   = _parse_emoji_arg(args, 1)
            emoji = self._resolve(raw)
            S._superreact_emoji = emoji
            _sync(_superreact_emoji=emoji)
            await message.edit(content=S.ui_ok(
                f"superreact → {emoji}  (reacting to your msgs)"))

        # ── superreactstop ─────────────────────────────────────────────
        elif cmd == "superreactstop":
            S._superreact_emoji = None
            _sync(_superreact_emoji=None)
            await message.edit(content=S.ui_ok("superreact → off"))

        # ── reactdiag ──────────────────────────────────────────────────
        elif cmd == "reactdiag":
            try:
                from modifyself.models.message import Message as _MSMessage
                patched = bool(getattr(_MSMessage, "_raw_react_patched", False))
            except ImportError:
                patched = "import-failed"
            main      = sys.modules.get("__main__")
            main_ar   = getattr(main, "_autoreact_emoji",   None) if main else None
            main_sr   = getattr(main, "_superreact_emoji",  None) if main else None
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
                f"  __main__ present:    {main is not None}",
            ]
            await message.edit(content=S._ansi_block(lines))

        # ── multireact / multiautoreact ────────────────────────────────
        elif cmd in ("multireact", "multiautoreact"):
            sub = args[1].lower() if len(args) > 1 else ""

            if sub == "add" and len(args) >= 3:
                # BUG-3 + BUG-4 fix: parse & resolve before duplicate check
                raw   = _parse_emoji_arg(args, 2)
                emoji = self._resolve(raw)
                # BUG-4: compare normalised form
                if emoji in S._multireact_pool:
                    return await message.edit(content=S.ui_info("already in pool"))
                S._multireact_pool.append(emoji)
                await message.edit(
                    content=S.ui_ok(f"added {emoji}  ({len(S._multireact_pool)} in pool)"))

            elif sub in ("remove", "rem", "del") and len(args) >= 3:
                raw   = _parse_emoji_arg(args, 2)
                emoji = self._resolve(raw)
                if emoji not in S._multireact_pool:
                    return await message.edit(content=S.ui_err("not in pool"))
                S._multireact_pool.remove(emoji)
                await message.edit(
                    content=S.ui_ok(f"removed  ({len(S._multireact_pool)} remaining)"))

            elif sub == "list":
                rows = [f"  {S.GREY}{i:2}.{S.RESET}  {e}"
                        for i, e in enumerate(S._multireact_pool, 1)]
                await message.edit(
                    content=S.ui_box(
                        f"multi pool — {'ON' if S._multireact_enabled else 'OFF'}", rows)
                    if rows else S.ui_info("pool is empty"))

            elif sub in ("on", "enable"):
                if not S._multireact_pool:
                    return await message.edit(content=S.ui_err("pool is empty — add emojis first"))
                S._multireact_enabled = True
                _sync(_multireact_enabled=True)
                await message.edit(content=S.ui_ok("multireact → enabled"))

            elif sub in ("off", "disable"):
                S._multireact_enabled = False
                _sync(_multireact_enabled=False)
                await message.edit(content=S.ui_ok("multireact → disabled"))

            elif sub == "clear":
                S._multireact_pool.clear()
                S._multireact_enabled = False
                _sync(_multireact_enabled=False)
                await message.edit(content=S.ui_ok("pool cleared"))

            else:
                await message.edit(content=S.ui_info(
                    "usage: multireact add/remove/list/on/off/clear <emoji>\n"
                    "       emoji can be unicode, :name:, or <:name:id>"))

        # ── vsniper ────────────────────────────────────────────────────
        elif cmd == "vsniper":
            sub = args[1].lower() if len(args) > 1 else ""

            if sub == "add" and len(args) >= 4:
                S._vsniper_list.append({"code": args[2], "guild_id": args[3]})
                await message.edit(content=S.ui_ok(f"watching vanity: {args[2]}"))

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
                rows = [
                    f"  {S.GREY}•{S.RESET} {e['code']}  "
                    f"{S.DIM}guild {e['guild_id']}{S.RESET}"
                    for e in S._vsniper_list
                ]
                await message.edit(
                    content=S._paginate("vsniper", "watch list", rows)
                    if rows else S.ui_info("watch list is empty"))

            else:
                await message.edit(
                    content=S.ui_info("usage: vsniper add <code> <guild_id> | start | stop | list"))
