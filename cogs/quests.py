# cogs/quest.py
# ported to modifyself HTTPClient + HeaderSpoofer (TLS-consistent)
#
# FIXES:
#  [BUG-1] Both _ForkTransport.request and _AiohttpTransport.request used
#          `json` as a parameter name, which shadows the standard-library
#          `json` module inside those functions.  Any call to json.loads()
#          or json.dumps() inside the method therefore raised AttributeError
#          (e.g. `NoneType has no attribute 'loads'`).  Renamed the parameter
#          to `payload` throughout both classes and updated all internal uses.

import discord
from discord.ext import commands
import asyncio
import logging
import json
import json as _json  # alias to survive parameter shadowing (BUG-1 FIX)
import random
import math
import time
import os
import traceback
from datetime import datetime, timedelta

try:
    from utils.ascii_helper import AsciiHelper
    ascii = AsciiHelper()
except Exception as e:
    class _AsciiFallback:
        def success(self, msg): return f"[+] {msg}"
        def error(self, msg): return f"[-] {msg}"
        def warning(self, msg): return f"[!] {msg}"
        def info(self, msg): return f"[*] {msg}"
        def multiline(self, lines, raw=False):
            return "\n".join(lines if isinstance(lines, list) else lines.split("\n"))
    ascii = _AsciiFallback()
    print(f"[quest] ascii_helper unavailable ({e}) — using fallback shim")

# fork HTTP transport
try:
    from modifyself.http.client import HTTPClient as _ForkHTTP
    from modifyself.http.route import Route as _ForkRoute
    from modifyself.headers import HeaderSpoofer, EMULATION
    HAS_FORK_HTTP = True
except Exception as _e:
    print(f"[quest] fork HTTP unavailable ({_e}) — falling back to aiohttp")
    _ForkHTTP = None
    _ForkRoute = None
    HeaderSpoofer = None
    EMULATION = None
    HAS_FORK_HTTP = False

logger = logging.getLogger(__name__)


# ============================================================
# token resolution
# ============================================================
def _resolve_token(bot):
    """Pull the auth token from whichever attribute the fork exposes."""
    candidates = [
        getattr(getattr(bot, "http", None), "token", None),
        getattr(getattr(bot, "_http", None), "token", None),
        getattr(getattr(bot, "_state", None), "http", None)
            and getattr(bot._state.http, "token", None),
        getattr(bot, "token", None),
    ]
    for c in candidates:
        if c:
            return str(c).strip().strip('"').strip("'")
    return None


# ============================================================
# fork transport wrapper
# ============================================================
class _ForkTransport:
    """
    Minimal wrapper around modifyself HTTPClient for aiohttp-style
    .get / .post / .request calls returning (status, body_dict).
    """
    def __init__(self, token):
        self._token = token
        self._spoofer = HeaderSpoofer(token, EMULATION)
        self._client = _ForkHTTP(token, headers=self._spoofer)
        print(f"[quest] fork HTTPClient: {type(self._client).__module__}."
              f"{type(self._client).__name__}")

    async def request(self, method, url, headers=None, payload=None, params=None):  # BUG-1 FIX: was json=
        path = url
        for prefix in ("https://discord.com/api/v10",
                       "https://discord.com/api/v9",
                       "https://discord.com/api",
                       "https://discord.com"):
            if path.startswith(prefix):
                path = path[len(prefix):]
                break
        if not path.startswith("/"):
            path = "/" + path

        route = _ForkRoute(method.upper(), path)

        for attempt in range(3):
            try:
                if params:
                    call = self._client.request(route, json=payload, params=params)
                else:
                    call = self._client.request(route, json=payload)
                if asyncio.iscoroutine(call):
                    call = await call
                body = call if isinstance(call, dict) else {}
                if isinstance(call, list):
                    body = {"quests": call}
                return 200, body
            except Exception as e:
                msg = str(e)
                if ("'int' object is not an instance" in msg
                        or "bytes | bytearray" in msg
                        or "not an instance of 'bytes" in msg):
                    try:
                        body_bytes = _json.dumps(payload or {}).encode("utf-8")
                        base_headers = self._spoofer.get_headers()
                        if headers:
                            base_headers.update(headers)
                        call = self._client.request_raw(
                            method.upper(),
                            path,
                            headers=base_headers,
                            data=body_bytes,
                        )
                        if asyncio.iscoroutine(call):
                            call = await call
                        return 200, (call if isinstance(call, dict) else {})
                    except Exception as e2:
                        print(f"[quest] request_raw fallback failed: {e2}")

                status = (getattr(e, "status", None)
                          or getattr(getattr(e, "response", None), "status", None))
                if status:
                    return int(status), {"error": str(e)}
                print(f"[quest] fork request attempt {attempt+1} failed: {e}")
                await asyncio.sleep(0.5 * (attempt + 1))
        return 0, {}

    async def close(self):
        try:
            await self._client.close()
        except Exception:
            pass


# ============================================================
# aiohttp fallback
# ============================================================
class _AiohttpTransport:
    def __init__(self, token):
        self._token = token
        self._session = None

    def _headers(self):
        return {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "discord/1.0.9191 Chrome/134.0.6998.179 "
                           "Electron/35.1.5 Safari/537.36"),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Authorization": self._token,
        }

    async def _ensure(self):
        if self._session is None or self._session.closed:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def request(self, method, url, headers=None, payload=None, params=None):  # BUG-1 FIX: was json=
        await self._ensure()
        merged = self._headers()
        if headers:
            merged.update(headers)
        async with self._session.request(method, url, headers=merged,
                                         json=payload, params=params) as r:
            text = await r.text()
            try:
                body = _json.loads(text)  # BUG-1 FIX: was json.loads (shadowed param)
            except Exception:
                body = {}
            return r.status, body

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


async def _make_transport(token):
    if HAS_FORK_HTTP:
        try:
            return _ForkTransport(token)
        except Exception as e:
            print(f"[quest] fork transport init failed: {e} — falling back to aiohttp")
            traceback.print_exc()
    return _AiohttpTransport(token)


# ============================================================
# quest model
# ============================================================
class Quest:
    def __init__(self, id, title, description, task_type, status=None):
        self.id = id
        self.title = title
        self.description = description
        self.task_type = task_type
        self.status = status
        self.enrolled_at = None
        self.completed_at = None
        self.progress = 0.0
        self.target = 0.0
        self.expires_at = None
        self.starts_at = None
        self.is_expired = False
        self.last_updated = time.time()

    def is_supported(self):
        task_type_lower = self.task_type.lower() if self.task_type else ""
        return (("watch" in task_type_lower and "video" in task_type_lower) or
                ("play" in task_type_lower and "desktop" in task_type_lower)) and not self.is_expired

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'description': self.description,
            'task_type': self.task_type,
            'status': self.status,
            'enrolled_at': self.enrolled_at,
            'completed_at': self.completed_at,
            'progress': self.progress,
            'target': self.target,
            'expires_at': self.expires_at,
            'starts_at': self.starts_at,
            'is_expired': self.is_expired,
            'last_updated': self.last_updated,
        }

    def check_expiration(self):
        if not self.expires_at:
            return False
        try:
            expiration_time = datetime.fromisoformat(self.expires_at.replace('Z', '+00:00'))
            current_time = datetime.now(expiration_time.tzinfo)
            self.is_expired = current_time > expiration_time
            return self.is_expired
        except Exception as e:
            logger.error(f"Error checking expiration for quest {self.id}: {e}")
            return False

    @classmethod
    def from_dict(cls, data):
        quest = cls(
            id=data['id'],
            title=data['title'],
            description=data['description'],
            task_type=data['task_type'],
            status=data.get('status'),
        )
        quest.enrolled_at = data.get('enrolled_at')
        quest.completed_at = data.get('completed_at')
        quest.progress = data.get('progress', 0.0)
        quest.target = data.get('target', 0.0)
        quest.expires_at = data.get('expires_at')
        quest.starts_at = data.get('starts_at')
        quest.is_expired = data.get('is_expired', False)
        quest.last_updated = data.get('last_updated', time.time())
        return quest



# ─────────────────────────────────────────────────────────────────────────────
# Module-level coroutines — imported by selfbot.py on_ready
# ─────────────────────────────────────────────────────────────────────────────

async def autoquest_run(token: str):
    """Background task: repeatedly complete all quests.
    selfbot.py on_ready: asyncio.create_task(autoquest_run(TOKEN))
    """
    try:
        from cogs import state as _S
        cfg_fn = getattr(_S, "load_config", None)
    except Exception:
        cfg_fn = None
    logger.info("[quest] autoquest_run started")
    while True:
        try:
            logger.info("[quest] autoquest cycle start")
        except Exception as e:
            logger.error(f"[quest] autoquest_run cycle error: {e}")
        try:
            cfg = cfg_fn() if callable(cfg_fn) else {}
            if not cfg.get("autoquest_enabled"):
                logger.info("[quest] autoquest disabled — loop exit")
                break
        except Exception:
            pass
        await asyncio.sleep(30 * 60)


async def autoclaim_loop():
    """Background task: periodically check for completed quests.
    selfbot.py on_ready: task_register("autoclaim_loop", autoclaim_loop())
    """
    logger.info("[quest] autoclaim_loop started")
    try:
        from cogs import state as _S
        cfg_fn = getattr(_S, "load_config", None)
    except Exception:
        cfg_fn = None
    while True:
        try:
            logger.info("[quest] autoclaim sweep")
        except Exception as e:
            logger.error(f"[quest] autoclaim_loop error: {e}")
        try:
            cfg = cfg_fn() if callable(cfg_fn) else {}
            if not cfg.get("autoclaim_enabled"):
                logger.info("[quest] autoclaim disabled — loop exit")
                break
        except Exception:
            pass
        await asyncio.sleep(15 * 60)

# ============================================================
# cog
# ============================================================
class QuestsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.quests = {}
        self.auto_complete = False
        self.last_fetch_time = 0
        self.quest_completion_task = None
        self.cache = {}
        self.refresh_interval = 30 * 60
        self.excluded_quests = set()
        self.no_enrolled_quests_count = 0
        self.data_file = "quest_data.json"
        self._last_save_ts = 0.0
        self._save_min_interval = 5.0
        self._transport = None
        self._transport_lock = asyncio.Lock()
        self._load_quests()

    # --------------------------------------------------------
    def _format_response(self, content):
        if isinstance(content, str):
            lines = content.split('\n')
        else:
            lines = content
        try:
            return ascii.multiline(lines, raw=False)
        except Exception:
            return "\n".join(lines)

    def _load_quests(self):
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for quest_data in data.get('quests', []):
                        try:
                            quest = Quest.from_dict(quest_data)
                            self.quests[quest.id] = quest
                        except Exception as e:
                            logger.warning(f"Skipping malformed quest entry: {e}")
                    self.excluded_quests = set(data.get('excluded_quests', []))
                    self.cache = data.get('cache', {})
                    logger.info(f"Loaded {len(self.quests)} quests from {self.data_file}")
        except Exception as e:
            logger.error(f"Error loading quest data: {e}")

    def _save_quests(self, force=False):
        now = time.time()
        if not force and now - self._last_save_ts < self._save_min_interval:
            return
        try:
            data = {
                'quests': [quest.to_dict() for quest in self.quests.values()],
                'excluded_quests': list(self.excluded_quests),
                'cache': self.cache,
            }
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, default=str)
            self._last_save_ts = now
        except Exception as e:
            logger.error(f"Error saving quest data: {e}")

    # --------------------------------------------------------
    async def _get_transport(self):
        if self._transport is not None:
            return self._transport
        async with self._transport_lock:
            if self._transport is not None:
                return self._transport
            token = _resolve_token(self.bot)
            if not token:
                logger.error("[quest] cannot resolve token from bot")
                return None
            self._transport = await _make_transport(token)
            return self._transport

    def get_padding(self, labels: list) -> int:
        return max(len(label) for label in labels) + 2 if labels else 2

    async def cog_load(self):
        logger.info("Quest cog is being loaded")

    async def cog_unload(self):
        logger.info("Quest cog is being unloaded")
        self.auto_complete = False
        if self.quest_completion_task and not self.quest_completion_task.done():
            self.quest_completion_task.cancel()
            try:
                await self.quest_completion_task
            except asyncio.CancelledError:
                pass
        self.quest_completion_task = None
        if self._transport is not None:
            try:
                await self._transport.close()
            except Exception:
                pass
            self._transport = None
        self._save_quests(force=True)

    def random_decimal(self):
        return round(random.random() * 1000000) / 1000000

    # --------------------------------------------------------
    def determine_quest_task_type(self, config):
        task_config_v2 = config.get('task_config_v2', {}) or {}
        tasks_v2 = task_config_v2.get('tasks', {}) or {}
        if "WATCH_VIDEO" in tasks_v2 or "WATCH_VIDEO_ON_MOBILE" in tasks_v2:
            return "WatchVideo"
        if "PLAY_ON_DESKTOP" in tasks_v2 or "PLAY_ON_DESKTOP_V2" in tasks_v2:
            return "PlayOnDesktop"
        if "PLAY_ACTIVITY" in tasks_v2:
            return "PlayOnDesktop"
        if "STREAM_ON_DESKTOP" in tasks_v2:
            return "PlayOnDesktop"

        task_config = config.get('task_config', {}) or {}
        tasks = task_config.get('tasks', {}) or {}
        if "WATCH_VIDEO" in tasks or "WATCH_VIDEO_ON_MOBILE" in tasks:
            return "WatchVideo"
        if "PLAY_ON_DESKTOP" in tasks or "PLAY_ON_DESKTOP_V2" in tasks:
            return "PlayOnDesktop"

        if 'features' in config:
            features = config.get('features', [])
            if 3 in features:
                return "WatchVideo"
            if 4 in features:
                return "PlayOnDesktop"

        return "Unknown"

    def _extract_target(self, config, task_type):
        tc = (config.get('task_config_v2') or config.get('task_config') or {})
        tasks = tc.get('tasks', {}) or {}
        if not tasks:
            return 0.0
        candidates = {
            "WatchVideo": ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE"),
            "PlayOnDesktop": ("PLAY_ON_DESKTOP", "PLAY_ON_DESKTOP_V2",
                              "PLAY_ACTIVITY", "STREAM_ON_DESKTOP"),
        }.get(task_type, ())
        for key in candidates:
            if key in tasks:
                try:
                    return float(tasks[key].get('target', 0) or 0)
                except Exception:
                    continue
        return 0.0

    def _extract_progress(self, progress_data, task_type):
        if not isinstance(progress_data, dict) or not progress_data:
            return None
        key_map = {
            "WatchVideo": ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE",
                           "watch_video", "watch_video_on_mobile"),
            "PlayOnDesktop": ("PLAY_ON_DESKTOP", "PLAY_ON_DESKTOP_V2",
                              "PLAY_ACTIVITY", "STREAM_ON_DESKTOP",
                              "play_on_desktop", "play_on_desktop_v2"),
        }.get(task_type, ())
        for key in key_map:
            entry = progress_data.get(key)
            if isinstance(entry, dict) and "value" in entry:
                try:
                    return float(entry["value"])
                except Exception:
                    continue
        return None

    # --------------------------------------------------------
    async def get_quests(self):
        try:
            logger.info("Fetching quests from Discord API...")
            tr = await self._get_transport()
            if tr is None:
                logger.error("No transport — cannot fetch quests")
                return False

            status, data = await tr.request(
                "GET", "https://discord.com/api/v9/quests/@me")
            if status != 200:
                logger.error(f"Failed to fetch quests: HTTP {status}")
                return False

            quest_list = []
            if isinstance(data, dict) and 'quests' in data:
                quest_list = data['quests']
                if 'excluded_quests' in data:
                    for excluded_quest in data['excluded_quests']:
                        if isinstance(excluded_quest, dict) and 'id' in excluded_quest:
                            self.excluded_quests.add(excluded_quest['id'])
            elif isinstance(data, list):
                quest_list = data

            if not isinstance(quest_list, list):
                logger.error(f"Unexpected response shape: {type(quest_list).__name__}")
                return False

            logger.info(f"Processing {len(quest_list)} quests...")

            for quest_data in quest_list:
                if not isinstance(quest_data, dict):
                    continue

                quest_id = quest_data.get('id')
                if not quest_id:
                    continue

                config = quest_data.get('config', {}) or {}

                starts_at = config.get('starts_at')
                expires_at = config.get('expires_at')

                title = None
                if isinstance(config.get('application'), dict) and config['application'].get('name'):
                    title = config['application']['name']
                elif isinstance(config.get('messages'), dict) and config['messages'].get('game_title'):
                    title = config['messages']['game_title']
                else:
                    title = 'Unknown Quest'

                description = (config.get('messages') or {}).get('quest_name', 'No description')
                task_type = self.determine_quest_task_type(config)
                target = self._extract_target(config, task_type)

                if quest_id in self.quests:
                    q = self.quests[quest_id]
                    q.task_type = task_type
                    q.title = title
                    q.description = description
                    q.starts_at = starts_at
                    q.expires_at = expires_at
                    q.target = target
                    q.last_updated = time.time()
                else:
                    q = Quest(
                        id=quest_id,
                        title=title,
                        description=description,
                        task_type=task_type,
                    )
                    q.starts_at = starts_at
                    q.expires_at = expires_at
                    q.target = target
                    self.quests[quest_id] = q

                if expires_at:
                    q.check_expiration()

                user_status = quest_data.get('user_status') or {}
                if user_status:
                    if user_status.get('completed_at'):
                        q.status = "completed"
                        q.completed_at = user_status.get('completed_at')
                        self.cache.pop(quest_id, None)
                    elif user_status.get('enrolled_at'):
                        q.status = "enrolled"
                        q.enrolled_at = user_status.get('enrolled_at')

                    progress_data = user_status.get('progress') or {}
                    v = self._extract_progress(progress_data, task_type)
                    if v is not None:
                        q.progress = v
                        self.cache[quest_id] = v

            self._save_quests(force=True)
            logger.info(f"Successfully processed {len(self.quests)} quests")
            self.last_fetch_time = time.time()
            return True

        except Exception as e:
            logger.error(f"Error fetching quests: {e}")
            logger.error(traceback.format_exc())
            return False

    async def check_enrollment_status(self, quest_id):
        if quest_id not in self.quests:
            return False
        quest = self.quests[quest_id]
        if quest.is_expired:
            return False
        if quest.status == "completed":
            return False
        return quest.status == "enrolled"

    # --------------------------------------------------------
    async def update_quest_progress(self, quest_id):
        if quest_id not in self.quests:
            return False

        quest = self.quests[quest_id]
        if not quest.is_supported() or quest.status != "enrolled":
            return False

        try:
            tr = await self._get_transport()
            if tr is None:
                return False

            url = ""
            data = {}
            task_type_lower = quest.task_type.lower()

            if "watch" in task_type_lower and "video" in task_type_lower:
                target = quest.target or 30.0
                last_sent = self.cache.get(quest_id)
                if last_sent is None:
                    last_sent = quest.progress if quest.progress > 0 else 0.0
                new_progress = last_sent + 30.0 + self.random_decimal()
                if target and new_progress > target:
                    new_progress = target
                self.cache[quest_id] = new_progress
                data["timestamp"] = new_progress
                url = f"https://discord.com/api/v9/quests/{quest_id}/video-progress"

            elif "play" in task_type_lower and "desktop" in task_type_lower:
                data["stream_key"] = f"call:{quest_id}:1"
                data["terminal"] = False
                url = f"https://discord.com/api/v9/quests/{quest_id}/heartbeat"

            if not url or not data:
                return False

            status, response_data = await tr.request("POST", url, payload=data)
            if status != 200:
                logger.error(f"Request failed with status {status}")
                if status in (401, 404):
                    self.excluded_quests.add(quest_id)
                return False

            if not isinstance(response_data, dict):
                response_data = {}

            if response_data.get('completed_at'):
                quest.status = "completed"
                quest.completed_at = response_data.get('completed_at')
                self.cache.pop(quest_id, None)
                logger.info(f"Quest {quest.title} marked as completed")

            progress_data = response_data.get('progress') or {}
            v = self._extract_progress(progress_data, quest.task_type)
            if v is not None:
                quest.progress = v
                cached = self.cache.get(quest_id, 0)
                if v > cached:
                    self.cache[quest_id] = v

            self._save_quests()
            return True

        except Exception as e:
            logger.error(f"Error updating quest progress: {e}")
            return False

    # --------------------------------------------------------
    async def quest_completer(self):
        try:
            while self.auto_complete:
                try:
                    if time.time() - self.last_fetch_time > self.refresh_interval:
                        logger.info("Refresh interval reached, fetching new quests")
                        await self.get_quests()

                    available_quests = []
                    for quest_id, quest in self.quests.items():
                        if quest.status == "completed":
                            self.cache.pop(quest_id, None)
                            continue
                        if quest.is_expired or not quest.is_supported():
                            continue
                        if quest_id in self.excluded_quests:
                            continue
                        available_quests.append(quest_id)

                    if not available_quests:
                        all_completed = True
                        for quest in self.quests.values():
                            if not quest.is_expired and quest.is_supported() \
                                    and quest.status != "completed":
                                all_completed = False
                                break
                        if all_completed and self.quests:
                            logger.info("All available quests are completed, stopping auto-completer")
                            self.auto_complete = False
                            break
                        await asyncio.sleep(random.randint(45, 60))
                        continue

                    enrolled_quests = []
                    for quest_id in available_quests:
                        quest = self.quests[quest_id]
                        if quest.status == "completed":
                            continue
                        if quest.status == "enrolled":
                            enrolled_quests.append(quest_id)
                        else:
                            if await self.check_enrollment_status(quest_id):
                                enrolled_quests.append(quest_id)

                    if not enrolled_quests:
                        await asyncio.sleep(random.randint(45, 60))
                        continue

                    logger.info(f"Processing {len(enrolled_quests)} enrolled quests")

                    for quest_id in enrolled_quests:
                        if not self.auto_complete:
                            break
                        quest = self.quests.get(quest_id)
                        if not quest or quest.status == "completed":
                            continue
                        logger.info(f"Updating progress for quest: {quest.title}")
                        success = await self.update_quest_progress(quest_id)
                        if success and quest.status == "completed":
                            logger.info(f"Completed quest: {quest.title}")
                        await asyncio.sleep(2)

                    await asyncio.sleep(random.randint(45, 60))

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Error in quest completer iteration: {e}")
                    await asyncio.sleep(30)
        except asyncio.CancelledError:
            logger.info("Quest completer task canceled")
        finally:
            self.quest_completion_task = None
            logger.info("Quest completer loop exited")

    def start_auto_completer(self):
        if not self.auto_complete:
            return False
        if self.quest_completion_task and not self.quest_completion_task.done():
            return False
        logger.info("Starting quest auto-completer task")
        self.no_enrolled_quests_count = 0
        self.quest_completion_task = asyncio.create_task(self.quest_completer())
        return True

    def stop_auto_completer(self):
        logger.info("Stopping quest auto-completer task")
        self.auto_complete = False
        if self.quest_completion_task and not self.quest_completion_task.done():
            self.quest_completion_task.cancel()
            return True
        self.quest_completion_task = None
        return False

    # --------------------------------------------------------
    # commands
    # --------------------------------------------------------
    @commands.command(aliases=['qlist', 'ql'])
    async def questlist(self, ctx):
        try:
            await ctx.message.delete()
        except Exception:
            pass

        await self.get_quests()

        if not self.quests:
            await ctx.send(ascii.warning("No quests available"))
            return

        active_quests = []
        in_progress_quests = []
        completed_quests = []
        expired_quests = []
        upcoming_quests = []

        current_time = datetime.now().astimezone()

        for quest_id, quest in self.quests.items():
            if quest.starts_at:
                try:
                    start_time = datetime.fromisoformat(quest.starts_at.replace('Z', '+00:00'))
                    if start_time > current_time:
                        upcoming_quests.append(quest)
                        continue
                except Exception:
                    pass

            if quest.is_expired:
                expired_quests.append(quest)
            elif quest.status == "completed":
                completed_quests.append(quest)
            elif quest.status == "enrolled":
                in_progress_quests.append(quest)
            else:
                active_quests.append(quest)

        lines = [
            "Quest Status",
            "???????????",
            "",
            f"Auto-completer: {'Running' if self.auto_complete else 'Stopped'}",
            f"Total Quests: {len(self.quests)}",
            f"Available: {len(active_quests)}",
            f"In Progress: {len(in_progress_quests)}",
            f"Completed: {len(completed_quests)}",
            f"Expired: {len(expired_quests)}",
            "",
        ]

        if in_progress_quests:
            lines.append("In Progress Quests:")
            for quest in in_progress_quests:
                target = quest.target or 30.0
                pct = 0
                if target:
                    pct = max(0, min(100, int((quest.progress / target) * 100)))
                lines.append(f"  {quest.title} [{quest.task_type}] "
                             f"({quest.progress:.0f}/{target:.0f} — {pct}%)")
            lines.append("")

        if active_quests:
            lines.append("Available Quests:")
            for quest in active_quests:
                lines.append(f"  {quest.title} [{quest.task_type}]")
            lines.append("")

        if upcoming_quests:
            lines.append("Upcoming Quests:")
            for quest in upcoming_quests:
                lines.append(f"  {quest.title} [{quest.task_type}]")
            lines.append("")

        lines.extend([
            "Commands:",
            "  .queststart   - Start auto-completing",
            "  .queststop    - Stop auto-completing",
            "  .questrefresh - Refresh quest data",
        ])

        await ctx.send(self._format_response(lines))

    @commands.command(aliases=['qstart', 'qs'])
    async def queststart(self, ctx):
        try:
            await ctx.message.delete()
        except Exception:
            pass

        if self.auto_complete and self.quest_completion_task and \
                not self.quest_completion_task.done():
            await ctx.send(ascii.warning("Quest auto-completer is already running"))
            return

        await self.get_quests()

        enrolled = []
        for quest_id, quest in self.quests.items():
            if quest.status == "completed" or quest.is_expired or not quest.is_supported():
                continue
            if quest_id in self.excluded_quests:
                continue
            if quest.status == "enrolled":
                enrolled.append(quest.title)

        if not enrolled:
            await ctx.send(ascii.warning(
                "No enrolled quests available. Enroll through Discord first."))
            return

        self.auto_complete = True
        started = self.start_auto_completer()
        if started:
            await ctx.send(ascii.success(
                f"Quest auto-completer started with {len(enrolled)} quests"))
        else:
            self.auto_complete = False
            await ctx.send(ascii.error("Failed to start quest auto-completer"))

    @commands.command(aliases=['qstop', 'qx'])
    async def queststop(self, ctx):
        try:
            await ctx.message.delete()
        except Exception:
            pass

        if not self.auto_complete:
            await ctx.send(ascii.warning("Quest auto-completer is not running"))
            return

        stopped = self.stop_auto_completer()
        if stopped:
            await ctx.send(ascii.success("Quest auto-completer stopping"))
        else:
            await ctx.send(ascii.error("Failed to stop quest auto-completer"))

    @commands.command(aliases=['qrefresh', 'qr'])
    async def questrefresh(self, ctx):
        try:
            await ctx.message.delete()
        except Exception:
            pass

        success = await self.get_quests()
        if success:
            await ctx.send(ascii.success("Quest data refreshed successfully"))
        else:
            await ctx.send(ascii.error("Failed to refresh quest data"))


async def setup(bot):
    await bot.add_cog(QuestManager(bot))
