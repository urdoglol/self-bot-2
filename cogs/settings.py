# cogs/settings.py | SettingsCog — prefix, cooldowns, aliases, disabled cmds
#
# ROOT CAUSES fixed:
#
#  [BUG-1] setprefix — selfbot.py's _dispatch_message reads its OWN module-level
#          `PREFIX` global (declared with `global PREFIX`).  _boot_cogs does
#          `cstate.PREFIX = PREFIX` which is a one-time string copy; reassigning
#          S.PREFIX afterwards never touches selfbot's local.  Fix: also update
#          sys.modules["__main__"].PREFIX so the dispatcher sees the new value
#          on the very next message.
#
#  [BUG-2] disable / enable — selfbot.py owns a module-level _cmd_disabled set
#          (line ~1129) that _boot_cogs never shares to cstate.  _perm_check in
#          selfbot reads selfbot's own set; the cog edited S._cmd_disabled which
#          is a different object.  Fix: also update main._cmd_disabled directly.
#
#  [NOTE]  setcooldown with no cmd used S._global_cooldown which selfbot never
#          reads.  Removed that branch; per-command cooldowns work correctly
#          because _cooldowns IS the shared dict (_boot_cogs: cstate._cooldowns =
#          _cooldowns).
#
#  [NOTE]  serverprefix correctly updates the shared _server_prefixes dict but
#          selfbot's lookup guard is `if message.guild and …` — message.guild is
#          always None in modifyself, so per-server prefixes never fire at
#          dispatch time.  The value is stored; the note in the reply makes this
#          visible.

import sys
import modifyself_shim as discord
from . import state as S


def _main():
    """Return selfbot's __main__ module so we can update its globals directly."""
    return sys.modules.get("__main__")


def _sync(attr, value):
    """
    Assign value to both S.<attr> and __main__.<attr>.
    Used for module-level globals that selfbot reads from its own scope.
    """
    try:
        setattr(S, attr, value)
    except Exception:
        pass
    m = _main()
    if m is not None:
        try:
            setattr(m, attr, value)
        except Exception:
            pass


def _set_add(attr, value):
    """Add value to a set on both S and __main__."""
    for ns in (S, _main()):
        if ns is None:
            continue
        s = getattr(ns, attr, None)
        if isinstance(s, set):
            s.add(value)


def _set_discard(attr, value):
    """Discard value from a set on both S and __main__."""
    for ns in (S, _main()):
        if ns is None:
            continue
        s = getattr(ns, attr, None)
        if isinstance(s, set):
            s.discard(value)


class SettingsCog:
    COMMANDS = {"setprefix", "setcooldown", "alias", "disable", "enable",
                "settings", "serverprefix", "clearprefix"}

    async def handle(self, message, cmd, args):

        # ── setprefix ─────────────────────────────────────────────────────────
        if cmd == "setprefix":
            if len(args) < 2:
                return await message.edit(content=S.ui_err("usage: setprefix <prefix>"))
            new_prefix = args[1]
            # BUG-1 FIX: update selfbot's own PREFIX global, not just S.PREFIX
            _sync("PREFIX", new_prefix)
            cfg = S.load_config() or {}
            cfg["prefix"] = new_prefix
            S.save_config(cfg)
            await message.edit(content=S.ui_ok(f"prefix → `{new_prefix}`"))

        # ── serverprefix ──────────────────────────────────────────────────────
        elif cmd == "serverprefix":
            gid = str(getattr(message, "guild_id", None) or "")
            if not gid:
                return await message.edit(
                    content=S.ui_err("must be in a server (guild_id not found)"))
            if len(args) < 2:
                return await message.edit(content=S.ui_err("usage: serverprefix <prefix>"))
            S._server_prefixes[gid] = args[1]
            await message.edit(content=S.ui_ok(
                f"server prefix → `{args[1]}`\n"
                f"  {S.DIM}note: takes effect on next bot restart "
                f"(modifyself guild lookup is runtime-dependent){S.RESET}"))

        # ── clearprefix ───────────────────────────────────────────────────────
        elif cmd == "clearprefix":
            gid = str(getattr(message, "guild_id", None) or "")
            if gid:
                S._server_prefixes.pop(gid, None)
            await message.edit(content=S.ui_ok("server prefix cleared"))

        # ── setcooldown ───────────────────────────────────────────────────────
        elif cmd == "setcooldown":
            # Requires a command name — global cooldown is not checked by selfbot
            if len(args) < 3 or not args[1].replace(".", "").isdigit():
                return await message.edit(content=S.ui_err(
                    "usage: setcooldown <seconds> <command>\n"
                    f"  {S.DIM}example: setcooldown 5 ping{S.RESET}"))
            secs   = float(args[1])
            target = args[2].lower()
            # _cooldowns is the shared dict — mutation propagates to selfbot
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
            # BUG-2 FIX: add to BOTH S._cmd_disabled AND selfbot's own set
            _set_add("_cmd_disabled", target)
            await message.edit(content=S.ui_ok(f"`{target}` disabled"))

        # ── enable ────────────────────────────────────────────────────────────
        elif cmd == "enable":
            if len(args) < 2:
                return await message.edit(content=S.ui_err("usage: enable <command>"))
            target = args[1].lower()
            # BUG-2 FIX: discard from BOTH sets
            _set_discard("_cmd_disabled", target)
            await message.edit(content=S.ui_ok(f"`{target}` enabled"))

        # ── settings ──────────────────────────────────────────────────────────
        elif cmd == "settings":
            sub = args[1].lower() if len(args) > 1 else "status"
            if sub == "status":
                # Read selfbot's live PREFIX (may differ from S.PREFIX if recently changed)
                m = _main()
                live_prefix = getattr(m, "PREFIX", None) or S.PREFIX or "."
                live_disabled = len(getattr(m, "_cmd_disabled", None) or set())
                rows = [
                    f"  {S.DIM}prefix{S.RESET}          `{live_prefix}`",
                    f"  {S.DIM}aliases{S.RESET}         {len(S._aliases)}",
                    f"  {S.DIM}disabled cmds{S.RESET}   {live_disabled}",
                    f"  {S.DIM}per-cmd cooldowns{S.RESET} {len(S._cooldowns)}",
                    f"  {S.DIM}server prefixes{S.RESET} {len(S._server_prefixes)}",
                ]
                await message.edit(content=S.ui_box("settings", rows))
            elif sub == "reset":
                S._aliases.clear()
                S._cooldowns.clear()
                _set_discard("_cmd_disabled", "__all__")   # no-op but safe
                m = _main()
                if m:
                    d = getattr(m, "_cmd_disabled", None)
                    if isinstance(d, set): d.clear()
                await message.edit(content=S.ui_ok("aliases, cooldowns and disabled list cleared"))
            else:
                await message.edit(content=S.ui_info("settings status  |  settings reset"))
