# cogs/voice.py | VoiceCog — VC join/leave, mute/deafen, vckeep, selfmute/deaf
# Replaces the library VoiceClient file that was wrongly placed here.
# Uses REST API for server mute/deafen; sends gateway op 4 for self-states.
import asyncio
import time
import aiohttp
import modifyself_shim as discord
from . import state as S


def _headers():
    return {"Authorization": S.TOKEN, "User-Agent": S.USER_AGENT,
            "Content-Type": "application/json"}


async def _rest(method, url, **kwargs):
    fn = getattr(S, "_get_session", None)
    h  = _headers()
    sess = fn() if callable(fn) else None
    try:
        if sess:
            async with sess.request(method, url, headers=h, **kwargs) as r:
                return r.status
        async with aiohttp.ClientSession() as s:
            async with s.request(method, url, headers=h, **kwargs) as r:
                return r.status
    except Exception as e:
        print(f"[voice] REST {method} {url}: {e}")
        return 0


async def _voice_state(guild_id, channel_id,
                       self_mute=False, self_deaf=False,
                       self_video=False, self_stream=False):
    """Send a Voice State Update (op 4) via the gateway."""
    import json as _json
    payload = {"op": 4, "d": {
        "guild_id":   str(guild_id),
        "channel_id": str(channel_id) if channel_id else None,
        "self_mute":  self_mute, "self_deaf": self_deaf,
        "self_video": self_video, "self_stream": self_stream,
    }}
    cl = S.CLIENT
    if not cl:
        return False
    for attr in ("_gateway", "gateway", "ws", "_ws"):
        gw = getattr(cl, attr, None)
        if gw and hasattr(gw, "send_json"):
            try:
                await gw.send_json(payload)
                return True
            except Exception as e:
                print(f"[voice] send_json via {attr}: {e}")
    return False


def _get_guild_id(message):
    gid = getattr(message, "guild_id", None)
    if gid: return int(gid)
    try:
        ch = getattr(message, "channel", None)
        if ch:
            gid = getattr(ch, "guild_id", None)
            if gid: return int(gid)
    except Exception: pass
    return None


class VoiceCog:
    COMMANDS = {
        "vcjoin", "vcleave", "vcmute", "vcunmute", "vcdeafen", "vcundeafen",
        "vckick", "vcmove", "vcmoveall", "vcreconnect",
        "vckeep", "selfmute", "selfdeaf", "selfstream", "selfcamera", "vcdiag",
    }

    def __init__(self):
        self._self_state: dict = {}   # guild_id → {mute, deaf, stream, video}
        self._keep_tasks: dict = {}   # guild_id → asyncio.Task
        self._reconnect: dict = {}    # guild_id → ch_id

    def _st(self, gid: int) -> dict:
        return self._self_state.setdefault(gid, {"mute":False,"deaf":False,"stream":False,"video":False})

    async def handle(self, message, cmd, args):
        gid = _get_guild_id(message)

        # ── vcjoin ────────────────────────────────────────────────────────────
        if cmd == "vcjoin":
            if not gid or len(args) < 2 or not args[1].isdigit():
                return await message.edit(content=S.ui_err("usage: vcjoin <channel_id>"))
            ok = await _voice_state(gid, int(args[1]))
            await message.edit(content=S.ui_ok(f"joining #{args[1]}") if ok
                               else S.ui_err("gateway send failed"))

        # ── vcleave ───────────────────────────────────────────────────────────
        elif cmd == "vcleave":
            if not gid:
                return await message.edit(content=S.ui_err("must be in a server"))
            ok = await _voice_state(gid, None)
            await message.edit(content=S.ui_ok("left VC") if ok
                               else S.ui_err("gateway send failed"))

        # ── vcmute / vcunmute ─────────────────────────────────────────────────
        elif cmd in ("vcmute", "vcunmute"):
            if not gid or len(args) < 2 or not args[1].isdigit():
                return await message.edit(content=S.ui_err(f"usage: {cmd} <user_id>"))
            muted = cmd == "vcmute"
            s = await _rest("PATCH",
                f"https://discord.com/api/v9/guilds/{gid}/members/{args[1]}",
                json={"mute": muted})
            await message.edit(content=S.ui_ok(f"{'muted' if muted else 'unmuted'} <@{args[1]}>")
                               if s in (200,204) else S.ui_err(f"failed (HTTP {s})"))

        # ── vcdeafen / vcundeafen ─────────────────────────────────────────────
        elif cmd in ("vcdeafen", "vcundeafen"):
            if not gid or len(args) < 2 or not args[1].isdigit():
                return await message.edit(content=S.ui_err(f"usage: {cmd} <user_id>"))
            deafened = cmd == "vcdeafen"
            s = await _rest("PATCH",
                f"https://discord.com/api/v9/guilds/{gid}/members/{args[1]}",
                json={"deaf": deafened})
            await message.edit(content=S.ui_ok(f"{'deafened' if deafened else 'undeafened'} <@{args[1]}>")
                               if s in (200,204) else S.ui_err(f"failed (HTTP {s})"))

        # ── vckick ────────────────────────────────────────────────────────────
        elif cmd == "vckick":
            if not gid or len(args) < 2 or not args[1].isdigit():
                return await message.edit(content=S.ui_err("usage: vckick <user_id>"))
            # Move to no channel
            s = await _rest("PATCH",
                f"https://discord.com/api/v9/guilds/{gid}/members/{args[1]}",
                json={"channel_id": None})
            await message.edit(content=S.ui_ok(f"kicked <@{args[1]}> from VC")
                               if s in (200,204) else S.ui_err(f"failed (HTTP {s})"))

        # ── vcmove ────────────────────────────────────────────────────────────
        elif cmd == "vcmove":
            if not gid or len(args) < 3:
                return await message.edit(content=S.ui_err("usage: vcmove <user_id> <channel_id>"))
            s = await _rest("PATCH",
                f"https://discord.com/api/v9/guilds/{gid}/members/{args[1]}",
                json={"channel_id": args[2]})
            await message.edit(content=S.ui_ok(f"moved <@{args[1]}> → #{args[2]}")
                               if s in (200,204) else S.ui_err(f"failed (HTTP {s})"))

        # ── vcmoveall ─────────────────────────────────────────────────────────
        elif cmd == "vcmoveall":
            if not gid or len(args) < 3:
                return await message.edit(content=S.ui_err("usage: vcmoveall <from_ch_id> <to_ch_id>"))
            await message.edit(content=S.ui_info("moving all members…"))
            # Fetch members of source channel
            _, data = (0, {}); moved = 0
            try:
                fn = getattr(S, "_get_session", None)
                sess = fn() if callable(fn) else None
                h = _headers()
                url = f"https://discord.com/api/v9/channels/{args[1]}"
                if sess:
                    async with sess.get(url, headers=h) as r:
                        data = await r.json(content_type=None) if r.status==200 else {}
                for uid in [v["user"]["id"] for v in data.get("voice_states",[])
                            if data.get("voice_states")]:
                    s = await _rest("PATCH",
                        f"https://discord.com/api/v9/guilds/{gid}/members/{uid}",
                        json={"channel_id": args[2]})
                    if s in (200,204): moved += 1
                    await asyncio.sleep(0.2)
            except Exception as e:
                print(f"[voice] vcmoveall: {e}")
            await message.edit(content=S.ui_ok(f"moved {moved} member(s)"))

        # ── vcreconnect ───────────────────────────────────────────────────────
        elif cmd == "vcreconnect":
            sub = args[1].lower() if len(args) > 1 else "status"
            if sub == "on" and gid:
                ch_id = int(args[2]) if len(args) > 2 and args[2].isdigit() else None
                if ch_id: self._reconnect[gid] = ch_id
                await message.edit(content=S.ui_ok("vcreconnect → on"))
            elif sub == "off":
                self._reconnect.pop(gid, None)
                await message.edit(content=S.ui_ok("vcreconnect → off"))
            else:
                await message.edit(content=S.ui_box("vcreconnect", [
                    f"  {S.DIM}channels tracked{S.RESET}  {len(self._reconnect)}"]))

        # ── vckeep ────────────────────────────────────────────────────────────
        elif cmd == "vckeep":
            sub = args[1].lower() if len(args) > 1 else "status"
            if sub == "on" and gid:
                ch_id = int(args[2]) if len(args) > 2 and args[2].isdigit() else None
                if not ch_id:
                    return await message.edit(content=S.ui_err("usage: vckeep on <channel_id>"))
                existing = self._keep_tasks.get(gid)
                if existing and not existing.done(): existing.cancel()
                self._keep_tasks[gid] = asyncio.create_task(self._keep_loop(gid, ch_id))
                await message.edit(content=S.ui_ok(f"vckeep on → #{ch_id} (reconnects every 15s)"))
            elif sub == "off":
                t = self._keep_tasks.pop(gid, None)
                if t and not t.done(): t.cancel()
                await message.edit(content=S.ui_ok("vckeep off"))
            else:
                running = [g for g, t in self._keep_tasks.items() if not t.done()]
                await message.edit(content=S.ui_box("vckeep", [
                    f"  {S.DIM}active loops{S.RESET}  {len(running)}"]))

        # ── selfmute ──────────────────────────────────────────────────────────
        elif cmd == "selfmute":
            if not gid:
                return await message.edit(content=S.ui_err("must be in a server"))
            st = self._st(gid); st["mute"] = not st["mute"]
            ok = await _voice_state(gid, None, self_mute=st["mute"], self_deaf=st["deaf"])
            await message.edit(content=S.ui_ok(f"self-mute → {'on' if st['mute'] else 'off'}"))

        # ── selfdeaf ──────────────────────────────────────────────────────────
        elif cmd == "selfdeaf":
            if not gid:
                return await message.edit(content=S.ui_err("must be in a server"))
            st = self._st(gid); st["deaf"] = not st["deaf"]
            ok = await _voice_state(gid, None, self_mute=st["mute"], self_deaf=st["deaf"])
            await message.edit(content=S.ui_ok(f"self-deaf → {'on' if st['deaf'] else 'off'}"))

        # ── selfstream ────────────────────────────────────────────────────────
        elif cmd == "selfstream":
            if not gid:
                return await message.edit(content=S.ui_err("must be in a server"))
            st = self._st(gid); st["stream"] = not st["stream"]
            ok = await _voice_state(gid, None, self_mute=st["mute"], self_deaf=st["deaf"],
                                    self_stream=st["stream"])
            await message.edit(content=S.ui_ok(f"stream → {'on' if st['stream'] else 'off'}"))

        # ── selfcamera ────────────────────────────────────────────────────────
        elif cmd == "selfcamera":
            if not gid:
                return await message.edit(content=S.ui_err("must be in a server"))
            st = self._st(gid); st["video"] = not st["video"]
            ok = await _voice_state(gid, None, self_mute=st["mute"], self_deaf=st["deaf"],
                                    self_video=st["video"])
            await message.edit(content=S.ui_ok(f"camera → {'on' if st['video'] else 'off'}"))

        # ── vcdiag ────────────────────────────────────────────────────────────
        elif cmd == "vcdiag":
            cl = S.CLIENT
            gw_found = any(getattr(cl, a, None) and hasattr(getattr(cl,a,None),"send_json")
                           for a in ("_gateway","gateway","ws","_ws")) if cl else False
            rows = [
                f"  {S.DIM}gateway found{S.RESET}  {gw_found}",
                f"  {S.DIM}vckeep loops{S.RESET}   {len([t for t in self._keep_tasks.values() if not t.done()])}",
                f"  {S.DIM}reconnect map{S.RESET}  {len(self._reconnect)}",
            ]
            for g_id, st in self._self_state.items():
                rows.append(f"  {S.DIM}guild {g_id}{S.RESET}  mute={st['mute']} deaf={st['deaf']}")
            await message.edit(content=S.ui_box("voice diagnostics", rows))

    async def _keep_loop(self, guild_id: int, channel_id: int):
        while True:
            await asyncio.sleep(15)
            try:
                ok = await _voice_state(guild_id, channel_id)
                if ok:
                    print(f"[voice] vckeep ping — guild {guild_id} ch {channel_id}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[voice] vckeep error: {e}")
