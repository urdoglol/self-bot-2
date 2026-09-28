# cogs/settings.py | SettingsCog — prefix, cooldowns, aliases, disabled cmds
#
# ROOT CAUSES fixed:
#
#  [BUG-1] setprefix — selfbot._dispatch_message uses `global PREFIX` so it
#          reads selfbot's own module-level dict, NOT S.PREFIX.  The previous
#          fix used sys.modules["__main__"] which is unreliable.
#
#          The guaranteed path: _boot_cogs stores _dispatch_message on
#          S.HOSTED_DISPATCH.  Every function carries __globals__ — a direct
#          reference to its owning module's global dict.  Writing to
#          S.HOSTED_DISPATCH.__globals__["PREFIX"] updates the exact variable
#          that `global PREFIX` resolves to.  Works regardless of __main__.
#
#  [BUG-2] disable / enable — selfbot owns _cmd_disabled (a set).  _boot_cogs
#          never shares it to cstate, so S._cmd_disabled is a different object.
#          Same fix: get the live set via __globals__["_cmd_disabled"] and
#          mutate it in-place (.add / .discard) — no reassignment needed since
#          sets are mutable and selfbot's _perm_check holds the same reference.

import modifyself_shim as discord
from . import state as S


# ── selfbot globals accessor ──────────────────────────────────────────────────

def _sb_globals() -> dict | None:
    """
    Return selfbot's own global namespace via the dispatch function's __globals__.
    This is the same dict that `global PREFIX`, `global _cmd_disabled`, etc.
    resolve against inside _dispatch_message.
    """
    fn = getattr(S, "HOSTED_DISPATCH", None)
    if fn and hasattr(fn, "__globals__"):
        return fn.__globals__
    return None


def _sb_get(key, fallback=None):
    g = _sb_globals()
    return g.get(key, fallback) if g else fallback


def _sb_set(key, value):
    """Assign a new value to a selfbot global (use for immutables like PREFIX)."""
    g = _sb_globals()
    if g is not None:
        g[key] = value


def _sb_set_add(key, value):
    """Add to a set in selfbot's globals (in-place, no reassignment)."""
    g = _sb_globals()
    if g is None:
        return
    s = g.get(key)
    if isinstance(s, set):
        s.add(value)


def _sb_set_discard(key, value):
    """Discard from a set in selfbot's globals (in-place)."""
    g = _sb_globals()
    if g is None:
        return
    s = g.get(key)
    if isinstance(s, set):
        s.discard(value)


# ── cog ───────────────────────────────────────────────────────────────────────

class SettingsCog:
    COMMANDS = {"setprefix", "setcooldown", "alias", "disable", "enable",
                "settings", "serverprefix", "clearprefix"}

    async def handle(self, message, cmd, args):

        # ── setprefix ─────────────────────────────────────────────────────────
        if cmd == "setprefix":
            if len(args) < 2:
                return await message.edit(content=S.ui_err("usage: setprefix <prefix>"))
            new_prefix = args[1]

            # BUG-1 FIX: write directly into selfbot's module globals
            _sb_set("PREFIX", new_prefix)
            # Also keep S.PREFIX in sync (used by help and status display)
            S.PREFIX = new_prefix

            cfg = S.load_config() or {}
            cfg["prefix"] = new_prefix
            S.save_config(cfg)

            # Confirm the write succeeded
            live = _sb_get("PREFIX", "?")
            if live == new_prefix:
                await message.edit(content=S.ui_ok(f"prefix → `{new_prefix}`"))
            else:
                await message.edit(content=S.ui_err(
                    f"prefix stored in config but could not update live dispatcher "
                    f"(HOSTED_DISPATCH not available — restart to apply)"))

        # ── serverprefix ──────────────────────────────────────────────────────
        elif cmd == "serverprefix":
            gid = str(getattr(message, "guild_id", None) or "")
            if not gid:
                return await message.edit(
                    content=S.ui_err("must be in a server (no guild_id on message)"))
            if len(args) < 2:
                return await message.edit(content=S.ui_err("usage: serverprefix <prefix>"))
            # _server_prefixes IS the shared dict (passed by reference in _boot_cogs)
            S._server_prefixes[gid] = args[1]
            await message.edit(content=S.ui_ok(f"server prefix → `{args[1]}`"))

        # ── clearprefix ───────────────────────────────────────────────────────
        elif cmd == "clearprefix":
            gid = str(getattr(message, "guild_id", None) or "")
            if gid:
                S._server_prefixes.pop(gid, None)
            await message.edit(content=S.ui_ok("server prefix cleared"))

        # ── setcooldown ───────────────────────────────────────────────────────
        elif cmd == "setcooldown":
            # selfbot only checks _cooldowns[cmd], not a global cooldown value
            if len(args) < 3:
                return await message.edit(content=S.ui_err(
                    "usage: setcooldown <seconds> <command>  "
                    f"{S.DIM}e.g. setcooldown 5 ping{S.RESET}"))
            if not args[1].replace(".", "").isdigit():
                return await message.edit(content=S.ui_err("seconds must be a number"))
            secs   = float(args[1])
            target = args[2].lower()
            # _cooldowns is the shared dict — mutation propagates immediately
            S._cooldowns[target] = secs
            await message.edit(content=S.ui_ok(f"cooldown `{target}` → {secs}s"))

        # ── alias ─────────────────────────────────────────────────────────────
        elif cmd == "alias":
            sub = args[1].lower() if len(args) > 1 else ""
            if sub == "add":
                if len(args) < 4:
                    return await message.edit(content=S.ui_err(
                        "usage: alias add <alias> <command>"))
                S._aliases[args[2]] = args[3]
                await message.edit(content=S.ui_ok(f"alias `{args[2]}` → `{args[3]}`"))
            elif sub == "remove":
                if len(args) < 3:
                    return await message.edit(content=S.ui_err(
                        "usage: alias remove <alias>"))
                S._aliases.pop(args[2], None)
                await message.edit(content=S.ui_ok(f"alias `{args[2]}` removed"))
            elif sub == "list":
                rows = [f"  {S.GREY}•{S.RESET} `{k}` → `{v}`"
                        for k, v in S._aliases.items()]
                await message.edit(content=S._paginate("aliases", "", rows)
                                   if rows else S.ui_info("no aliases set"))
            elif sub == "clear":
                S._aliases.clear()
                await message.edit(content=S.ui_ok("aliases cleared"))
            else:
                await message.edit(content=S.ui_info(
                    "alias add <alias> <cmd>  |  remove <alias>  |  list  |  clear"))

        # ── disable ───────────────────────────────────────────────────────────
        elif cmd == "disable":
            if len(args) < 2:
                return await message.edit(content=S.ui_err("usage: disable <command>"))
            target = args[1].lower()
            # BUG-2 FIX: mutate selfbot's own _cmd_disabled set in-place
            _sb_set_add("_cmd_disabled", target)
            await message.edit(content=S.ui_ok(f"`{target}` disabled"))

        # ── enable ────────────────────────────────────────────────────────────
        elif cmd == "enable":
            if len(args) < 2:
                return await message.edit(content=S.ui_err("usage: enable <command>"))
            target = args[1].lower()
            # BUG-2 FIX: discard from selfbot's own set in-place
            _sb_set_discard("_cmd_disabled", target)
            await message.edit(content=S.ui_ok(f"`{target}` enabled"))

        # ── settings status ───────────────────────────────────────────────────
        elif cmd == "settings":
            sub = args[1].lower() if len(args) > 1 else "status"
            if sub == "status":
                live_prefix  = _sb_get("PREFIX", S.PREFIX or ".")
                live_disabled = _sb_get("_cmd_disabled", set())
                rows = [
                    f"  {S.DIM}prefix{S.RESET}           `{live_prefix}`",
                    f"  {S.DIM}aliases{S.RESET}          {len(S._aliases)}",
                    f"  {S.DIM}disabled cmds{S.RESET}    {len(live_disabled)}",
                    f"  {S.DIM}per-cmd cooldowns{S.RESET} {len(S._cooldowns)}",
                    f"  {S.DIM}server prefixes{S.RESET}  {len(S._server_prefixes)}",
                    f"  {S.DIM}dispatcher linked{S.RESET} "
                    f"{'yes' if _sb_globals() is not None else 'NO — restart required'}",
                ]
                await message.edit(content=S.ui_box("settings", rows))
            elif sub == "reset":
                S._aliases.clear()
                S._cooldowns.clear()
                d = _sb_get("_cmd_disabled")
                if isinstance(d, set):
                    d.clear()
                await message.edit(content=S.ui_ok(
                    "aliases, cooldowns and disabled list cleared"))
            else:
                await message.edit(content=S.ui_info(
                    "settings status  |  settings reset"))
