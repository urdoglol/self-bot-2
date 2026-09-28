# cogs/settings.py | SettingsCog — prefix, cooldowns, aliases, disabled cmds
#
# PREFIX FIX (final):
#   selfbot.py's _dispatch_message now reads (getattr(_cstate_ref,"PREFIX") or PREFIX)
#   on every message.  _cstate_ref is the shared state module set in _boot_cogs.
#   So setting S.PREFIX here propagates instantly — no __globals__ tricks needed.
#
# DISABLE/ENABLE FIX:
#   selfbot._perm_check reads its own _cmd_disabled set.  We get that set via
#   S.HOSTED_DISPATCH.__globals__["_cmd_disabled"] and mutate it in-place.

import modifyself_shim as discord
from . import state as S


def _sb_disabled_set():
    """Return selfbot's live _cmd_disabled set via the dispatch function's globals."""
    fn = getattr(S, "HOSTED_DISPATCH", None)
    if fn and hasattr(fn, "__globals__"):
        d = fn.__globals__.get("_cmd_disabled")
        if isinstance(d, set):
            return d
    return None


class SettingsCog:
    COMMANDS = {"setprefix", "setcooldown", "alias", "disable", "enable",
                "settings", "serverprefix", "clearprefix"}

    async def handle(self, message, cmd, args):

        # ── setprefix ─────────────────────────────────────────────────────────
        if cmd == "setprefix":
            if len(args) < 2:
                return await message.edit(
                    content=S.ui_err("usage: setprefix <prefix>"))
            new_prefix = args[1]
            # selfbot._dispatch_message now reads S.PREFIX on every message
            # so this single assignment takes effect immediately
            S.PREFIX = new_prefix
            cfg = S.load_config() or {}
            cfg["prefix"] = new_prefix
            S.save_config(cfg)
            await message.edit(content=S.ui_ok(f"prefix → `{new_prefix}`"))

        # ── serverprefix ──────────────────────────────────────────────────────
        elif cmd == "serverprefix":
            gid = str(getattr(message, "guild_id", None) or "")
            if not gid:
                return await message.edit(
                    content=S.ui_err("must be in a server (no guild_id on message)"))
            if len(args) < 2:
                return await message.edit(
                    content=S.ui_err("usage: serverprefix <prefix>"))
            S._server_prefixes[gid] = args[1]
            await message.edit(
                content=S.ui_ok(f"server prefix → `{args[1]}`"))

        # ── clearprefix ───────────────────────────────────────────────────────
        elif cmd == "clearprefix":
            gid = str(getattr(message, "guild_id", None) or "")
            if gid:
                S._server_prefixes.pop(gid, None)
            await message.edit(content=S.ui_ok("server prefix cleared"))

        # ── setcooldown ───────────────────────────────────────────────────────
        elif cmd == "setcooldown":
            if len(args) < 3:
                return await message.edit(content=S.ui_err(
                    "usage: setcooldown <seconds> <command>  "
                    f"{S.DIM}e.g. setcooldown 5 ping{S.RESET}"))
            if not args[1].replace(".", "").isdigit():
                return await message.edit(
                    content=S.ui_err("seconds must be a number"))
            secs   = float(args[1])
            target = args[2].lower()
            S._cooldowns[target] = secs   # shared dict — propagates immediately
            await message.edit(content=S.ui_ok(f"cooldown `{target}` → {secs}s"))

        # ── alias ─────────────────────────────────────────────────────────────
        elif cmd == "alias":
            sub = args[1].lower() if len(args) > 1 else ""
            if sub == "add":
                if len(args) < 4:
                    return await message.edit(content=S.ui_err(
                        "usage: alias add <alias> <command>"))
                S._aliases[args[2]] = args[3]
                await message.edit(
                    content=S.ui_ok(f"alias `{args[2]}` → `{args[3]}`"))
            elif sub == "remove":
                if len(args) < 3:
                    return await message.edit(content=S.ui_err(
                        "usage: alias remove <alias>"))
                S._aliases.pop(args[2], None)
                await message.edit(content=S.ui_ok(f"alias `{args[2]}` removed"))
            elif sub == "list":
                rows = [f"  {S.GREY}•{S.RESET} `{k}` → `{v}`"
                        for k, v in S._aliases.items()]
                await message.edit(
                    content=S._paginate("aliases", "", rows)
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
            d = _sb_disabled_set()
            if d is not None:
                d.add(target)
            await message.edit(content=S.ui_ok(f"`{target}` disabled"))

        # ── enable ────────────────────────────────────────────────────────────
        elif cmd == "enable":
            if len(args) < 2:
                return await message.edit(content=S.ui_err("usage: enable <command>"))
            target = args[1].lower()
            d = _sb_disabled_set()
            if d is not None:
                d.discard(target)
            await message.edit(content=S.ui_ok(f"`{target}` enabled"))

        # ── settings status ───────────────────────────────────────────────────
        elif cmd == "settings":
            sub = args[1].lower() if len(args) > 1 else "status"
            if sub == "status":
                d = _sb_disabled_set()
                rows = [
                    f"  {S.DIM}prefix{S.RESET}           `{S.PREFIX}`",
                    f"  {S.DIM}aliases{S.RESET}          {len(S._aliases)}",
                    f"  {S.DIM}disabled cmds{S.RESET}    {len(d) if d is not None else '?'}",
                    f"  {S.DIM}per-cmd cooldowns{S.RESET} {len(S._cooldowns)}",
                    f"  {S.DIM}server prefixes{S.RESET}  {len(S._server_prefixes)}",
                ]
                await message.edit(content=S.ui_box("settings", rows))
            elif sub == "reset":
                S._aliases.clear()
                S._cooldowns.clear()
                d = _sb_disabled_set()
                if d is not None:
                    d.clear()
                await message.edit(content=S.ui_ok(
                    "aliases, cooldowns and disabled list cleared"))
            else:
                await message.edit(content=S.ui_info(
                    "settings status  |  settings reset"))
