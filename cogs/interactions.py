# cogs/interactions.py | InteractionsCog — button/modal toggle + interaction log
# Replaces the library Interaction class file that was wrongly placed here.
from . import state as S


class InteractionsCog:
    COMMANDS = {"buttons", "modals", "interact"}

    async def handle(self, message, cmd, args):
        sub = args[1].lower() if len(args) > 1 else ""

        if cmd == "buttons":
            S._buttons_enabled = sub in ("on", "enable") if sub else not S._buttons_enabled
            await message.edit(content=S.ui_ok(f"buttons → {'on' if S._buttons_enabled else 'off'}"))

        elif cmd == "modals":
            S._modals_enabled = sub in ("on", "enable") if sub else not S._modals_enabled
            await message.edit(content=S.ui_ok(f"modals → {'on' if S._modals_enabled else 'off'}"))

        elif cmd == "interact":
            if sub == "clear":
                S._pending_interactions.clear()
                await message.edit(content=S.ui_ok("interaction log cleared"))
            elif sub == "list":
                rows = [f"  {S.GREY}•{S.RESET} {i}" for i in S._pending_interactions[-40:]]
                await message.edit(content=S._paginate("interactions", "pending", rows)
                                   if rows else S.ui_info("none"))
            else:
                await message.edit(content=S.ui_box("interactions", [
                    f"  {S.DIM}buttons{S.RESET}  {S._buttons_enabled}",
                    f"  {S.DIM}modals{S.RESET}   {S._modals_enabled}",
                    f"  {S.DIM}pending{S.RESET}  {len(S._pending_interactions)}",
                ]))
