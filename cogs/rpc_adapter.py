# cogs/rpc_adapter.py | bridges RPCCog into the selfbot dispatcher
#
# FIXES:
#  [BUG-1] When rpc.py had a syntax/import error, _inner was set to None at
#          boot and stayed None forever — every rpc command failed until the
#          bot was restarted, even after rpc.py was fixed.  Added lazy
#          re-import: every handle() call attempts to load RPCCog if _inner
#          is still None, so fixing rpc.py takes effect on the next command
#          with no restart required.
#
#  [BUG-2] .rpc with no subcommand called _usage() unconditionally, which
#          bypassed the _inner=None guard.  Now shows "not loaded" when inner
#          is missing and the usage panel only when inner is healthy.
#
#  [NEW]   .rpc reload — force a fresh import/reinit of RPCCog mid-session.

import traceback
import sys
from . import state as S


class RpcAdapterCog:
    COMMANDS = {
        "rpc", "rpc1", "rpc2", "rpc3", "rpc4", "rpc5", "rpc6",
        "playing", "listening", "listen", "watching", "watch",
        "competing", "stopactivity", "aoff", "clear_multi_rpc",
        "rpc_status", "spotify", "youtube", "xbox", "ps", "ps4",
        "crunchy", "vrchat", "meta", "rstatus", "remoji",
        "stopstatus", "stopemoji", "setpresencestatus", "roblox",
        "rpcwatchdog",
    }

    _ALIASES = {"listen": "listening", "watch": "watching"}

    def __init__(self):
        self._inner = None
        self._load_error: str = ""

    # ── load / lazy-reload ────────────────────────────────────────────────────

    def _try_load(self, client=None) -> bool:
        """Attempt to import RPCCog and initialise it.  Returns True on success."""
        # Evict cached failed module so Python re-reads the file
        for key in list(sys.modules.keys()):
            if "cogs.rpc" in key and "rpc_adapter" not in key:
                del sys.modules[key]

        try:
            from cogs.rpc import RPCCog
        except Exception as e:
            self._load_error = str(e)
            print(f"[rpc-adapter] cannot import cogs.rpc: {e}")
            traceback.print_exc()
            self._inner = None
            return False

        try:
            cl = client or getattr(S, "CLIENT", None)
            self._inner = RPCCog(cl)
        except Exception as e:
            self._load_error = str(e)
            print(f"[rpc-adapter] RPCCog init failed: {e}")
            self._inner = None
            return False

        try:
            cl = client or getattr(S, "CLIENT", None)
            if cl and hasattr(self._inner, "register"):
                self._inner.register(cl)
        except Exception as e:
            print(f"[rpc-adapter] inner register failed: {e}")

        self._load_error = ""
        print("[rpc-adapter] RPCCog loaded successfully")
        return True

    def register(self, client):
        self._try_load(client)

    # ── dispatch ──────────────────────────────────────────────────────────────

    async def handle(self, message, cmd, args):
        # BUG-1 FIX: lazy re-import on every call while _inner is None
        if self._inner is None:
            self._try_load()

        # .rpc reload — explicit force-reload
        if cmd == "rpc" and len(args) > 1 and args[1].lower() == "reload":
            ok = self._try_load()
            try:
                await message.edit(content=(
                    S.ui_ok("rpc cog reloaded") if ok
                    else S.ui_err(f"reload failed — {self._load_error}")))
            except Exception:
                pass
            return

        # BUG-2 FIX: check _inner before showing usage for bare .rpc
        if cmd == "rpc" and len(args) < 2:
            if self._inner is None:
                try:
                    await message.edit(content=S.ui_err(
                        f"rpc cog not loaded — {self._load_error or 'see console'}\n"
                        f"  fix rpc.py then run  .rpc reload"))
                except Exception:
                    pass
            else:
                await self._usage(message)
            return

        if self._inner is None:
            try:
                await message.edit(content=S.ui_err(
                    f"rpc cog not loaded — {self._load_error or 'see console'}\n"
                    f"  fix rpc.py then run  .rpc reload"))
            except Exception:
                pass
            return

        cmd = self._ALIASES.get(cmd, cmd)

        try:
            await self._inner.handle(message, cmd, args)
        except Exception as e:
            print(f"[rpc-adapter:{cmd}] {e}")
            traceback.print_exc()
            try:
                await message.edit(content=S.ui_err(
                    f"rpc `{cmd}` errored — check console"))
            except Exception:
                pass

    # ── usage panel ───────────────────────────────────────────────────────────

    async def _usage(self, message):
        lines = [
            "  rpc <1-6> <field> <value>    set a slot",
            "  rpc status                    show all slots",
            "  rpc clearall                  wipe every slot",
            "  rpc reload                    re-import rpc.py without restart",
            "",
            "  rpc1 name <text>              activity name",
            "  rpc1 details <text>           details line",
            "  rpc1 state <text>             state line",
            "  rpc1 type <type>              playing/streaming/listening/watching/competing",
            "  rpc1 platform <preset>        xbox/ps/ps4/crunchyroll/youtube/twitch/vrchat/meta/roblox",
            "  rpc1 large_image/small_image <url>",
            "  rpc1 timestamp <val>          3600 | 1:00:00 | clear",
            "  rpc1 btn1/btn2 <label> <url>",
            "  rpc1 clear                    wipe slot 1",
            "",
            "  spotify <song - artist> [slot]",
            "  youtube/roblox/xbox/ps/ps4/crunchy/vrchat/meta [args] [slot]",
            "  playing/listening/watching/competing <text>",
            "  aoff / stopactivity           clear current activity",
            "  rstatus / remoji <a, b, c>    rotate status / emoji",
            "  stopstatus / stopemoji        stop rotation",
            "  rpcwatchdog                   watchdog status/stop/start",
        ]
        try:
            await message.edit(content=S._ansi_block(lines))
        except Exception:
            pass
