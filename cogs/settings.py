# cogs/settings.py | SettingsCog — prefix, cooldowns, aliases, disabled cmds
# Replaces the library GuildSettings file that was wrongly placed here.
import modifyself_shim as discord
from . import state as S


class SettingsCog:
    COMMANDS = {"setprefix", "setcooldown", "alias", "disable", "enable",
                "settings", "serverprefix", "clearprefix"}

    async def handle(self, message, cmd, args):

        if cmd == "setprefix":
            if len(args) < 2:
                return await message.edit(content=S.ui_err("usage: setprefix <prefix>"))
            S.PREFIX = args[1]
            cfg = S.load_config() or {}
            cfg["prefix"] = args[1]; S.save_config(cfg)
            await message.edit(content=S.ui_ok(f"prefix → `{args[1]}`"))

        elif cmd == "serverprefix":
            gid = str(getattr(message, "guild_id", None) or "")
            if not gid or len(args) < 2:
                return await message.edit(content=S.ui_err("usage: serverprefix <prefix>"))
            S._server_prefixes[gid] = args[1]
            await message.edit(content=S.ui_ok(f"server prefix → `{args[1]}`"))

        elif cmd == "clearprefix":
            gid = str(getattr(message, "guild_id", None) or "")
            if gid:
                S._server_prefixes.pop(gid, None)
            await message.edit(content=S.ui_ok("server prefix cleared"))

        elif cmd == "setcooldown":
            if len(args) < 2 or not args[1].replace(".", "").isdigit():
                return await message.edit(content=S.ui_err("usage: setcooldown <seconds>"))
            secs = float(args[1])
            if len(args) >= 3:
                S._cooldowns[args[2]] = secs
                await message.edit(content=S.ui_ok(f"cooldown `{args[2]}` → {secs}s"))
            else:
                S._global_cooldown = secs
                await message.edit(content=S.ui_ok(f"global cooldown → {secs}s"))

        elif cmd == "alias":
            sub = args[1].lower() if len(args) > 1 else ""
            if sub == "add" and len(args) >= 4:
                S._aliases[args[2]] = args[3]
                await message.edit(content=S.ui_ok(f"alias `{args[2]}` → `{args[3]}`"))
            elif sub == "remove" and len(args) >= 3:
                S._aliases.pop(args[2], None)
                await message.edit(content=S.ui_ok(f"alias `{args[2]}` removed"))
            elif sub == "list":
                rows = [f"  {S.GREY}•{S.RESET} `{k}` → `{v}`"
                        for k, v in S._aliases.items()]
                await message.edit(content=S._paginate("aliases", "", rows)
                                   if rows else S.ui_info("no aliases"))
            elif sub == "clear":
                S._aliases.clear()
                await message.edit(content=S.ui_ok("aliases cleared"))
            else:
                await message.edit(content=S.ui_info("alias add/remove/list/clear"))

        elif cmd == "disable":
            if len(args) < 2:
                return await message.edit(content=S.ui_err("usage: disable <cmd>"))
            S._cmd_disabled.add(args[1])
            await message.edit(content=S.ui_ok(f"`{args[1]}` disabled"))

        elif cmd == "enable":
            if len(args) < 2:
                return await message.edit(content=S.ui_err("usage: enable <cmd>"))
            S._cmd_disabled.discard(args[1])
            await message.edit(content=S.ui_ok(f"`{args[1]}` enabled"))

        elif cmd == "settings":
            sub = args[1].lower() if len(args) > 1 else "status"
            if sub == "status":
                rows = [
                    f"  {S.DIM}prefix{S.RESET}        {S.PREFIX}",
                    f"  {S.DIM}global cooldown{S.RESET} {getattr(S,'_global_cooldown',0)}s",
                    f"  {S.DIM}aliases{S.RESET}       {len(S._aliases)}",
                    f"  {S.DIM}disabled cmds{S.RESET} {len(S._cmd_disabled)}",
                    f"  {S.DIM}server prefixes{S.RESET} {len(getattr(S,'_server_prefixes',{}))}",
                    f"  {S.DIM}cooldown rules{S.RESET} {len(S._cooldowns)}",
                ]
                await message.edit(content=S.ui_box("settings", rows))
            elif sub == "reset":
                S._aliases.clear(); S._cmd_disabled.clear(); S._cooldowns.clear()
                await message.edit(content=S.ui_ok("settings reset"))
            else:
                await message.edit(content=S.ui_info("settings status/reset"))
