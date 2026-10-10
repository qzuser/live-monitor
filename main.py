import asyncio
import base64
import json
import re
import time
from pathlib import Path

import aiohttp
from astrbot.api.event import MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# 封面下载上限。QQ 主动消息对图片体积敏感，过大直接放弃、退化成纯文字。
MAX_COVER_BYTES = 2 * 1024 * 1024

PLATFORM_NAMES = {
    "bilibili": "B站直播",
    "douyu": "斗鱼",
    "huya": "虎牙",
    "douyin": "抖音直播",
}

URL_PATTERNS = [
    ("bilibili", re.compile(r"(live|space)\.bilibili\.com/(\d+)", re.IGNORECASE)),
    ("douyu", re.compile(r"(?:www|m)\.douyu\.com/([a-zA-Z0-9]+)", re.IGNORECASE)),
    ("huya", re.compile(r"(?:www\.)?huya\.com/([a-zA-Z0-9]+)", re.IGNORECASE)),
    ("douyin", re.compile(r"live\.douyin\.com/(\d+)", re.IGNORECASE)),
]

EXPLICIT_PREFIX = re.compile(
    r"^(bilibili|bili|b站|抖音|douyin|斗鱼|douyu|dy|虎牙|huya|hy|b)"
    r"\s*[:：]\s*(.+)$",
    re.IGNORECASE,
)

PREFIX_MAP = {
    "bilibili": "bilibili",
    "bili": "bilibili",
    "b站": "bilibili",
    "b": "bilibili",
    "斗鱼": "douyu",
    "douyu": "douyu",
    "dy": "douyu",
    "虎牙": "huya",
    "huya": "huya",
    "hy": "huya",
    "抖音": "douyin",
    "douyin": "douyin",
}


class RoomCheckError(Exception):
    pass


def _get_json_kwargs():
    return {"content_type": None}


async def _get_json(session: aiohttp.ClientSession, url: str, headers: dict):
    async with session.get(
        url, headers=headers, timeout=aiohttp.ClientTimeout(10)
    ) as resp:
        return await resp.json(**_get_json_kwargs())


async def check_bilibili(
    session: aiohttp.ClientSession, room_id: str, cookie: str = ""
):
    h = _bili_headers(cookie)
    data = await _get_json(
        session,
        f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}",
        h,
    )
    if data.get("code") != 0:
        raise RoomCheckError(
            f"bilibili 接口返回 code={data.get('code')}，房间号可能不存在"
        )
    d = data.get("data") or {}
    live = d.get("live_status") == 1
    real_room = str(d.get("room_id") or room_id)
    return {
        "live": live,
        "title": d.get("title", ""),
        "anchor_uid": str(d.get("uid", "")),
        "cover": d.get("cover", ""),
        "url": f"https://live.bilibili.com/{real_room}",
    }


async def resolve_bilibili_anchor(
    session: aiohttp.ClientSession, uid: str, cookie: str = ""
) -> str:
    """通过 uid 获取B站主播昵称，失败返回空字符串。"""
    try:
        data = await _get_json(
            session,
            f"https://api.live.bilibili.com/live_user/v1/Master/info?uid={uid}",
            _bili_headers(cookie),
        )
        if data.get("code") == 0:
            return ((data.get("data") or {}).get("info") or {}).get("uname", "")
    except Exception:
        pass
    return ""


def _bili_headers(cookie: str = "") -> dict:
    h = dict(HEADERS)
    h["Referer"] = "https://live.bilibili.com/"
    if cookie:
        h["Cookie"] = cookie
    return h


async def resolve_bilibili_uid(session: aiohttp.ClientSession, uid: str) -> str:
    data = await _get_json(
        session,
        f"https://api.live.bilibili.com/live_user/v1/Master/info?uid={uid}",
        HEADERS,
    )
    if data.get("code") != 0:
        raise RoomCheckError("无法解析该B站主播的直播间，请直接发送直播间链接")
    room_id = (data.get("data") or {}).get("room_id")
    if not room_id:
        raise RoomCheckError("该B站用户没有直播间")
    return str(room_id)


async def check_douyu(session: aiohttp.ClientSession, room_id: str):
    data = await _get_json(session, f"https://www.douyu.com/betard/{room_id}", HEADERS)
    room = data.get("room") or {}
    if not room:
        raise RoomCheckError("斗鱼返回数据为空，房间号可能不存在")
    cover = room.get("room_src", "")
    if cover and not cover.startswith("http"):
        cover = "https://rpic.douyucdn.cn/" + cover
    return {
        "live": room.get("show") == 1,
        "title": room.get("room_name", ""),
        "anchor": room.get("owner_name", ""),
        "cover": cover,
        "url": f"https://www.douyu.com/{room_id}",
    }


async def check_huya(session: aiohttp.ClientSession, room_id: str):
    data = await _get_json(
        session,
        f"https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid={room_id}",
        HEADERS,
    )
    if data.get("status") != 0:
        raise RoomCheckError("虎牙接口返回异常，房间号可能不存在")
    d = data.get("data") or {}
    room = d.get("liveRoom") or {}
    anchor = ((d.get("owner") or {}).get("nick")) or room.get("nick", "")
    cover = room.get("thumb1200") or room.get("thumb") or d.get("screenshot") or ""
    return {
        "live": str(d.get("liveStatus")) == "2",
        "title": room.get("name", ""),
        "anchor": anchor,
        "cover": cover,
        "url": f"https://www.huya.com/{room_id}",
    }


_douyin_primed = False


async def check_douyin(session: aiohttp.ClientSession, room_id: str):
    global _douyin_primed
    if not _douyin_primed:
        # 先访问一次页面获取 ttwid 等基础 Cookie，否则 enter 接口会被拒
        try:
            async with session.get(
                "https://live.douyin.com/",
                timeout=aiohttp.ClientTimeout(10),
            ):
                pass
        except Exception:
            pass
        _douyin_primed = True
    data = await _get_json(
        session,
        "https://live.douyin.com/webcast/room/web/enter/"
        f"?aid=6383&app_name=douyin_web&live_id=1&web_rid={room_id}",
        HEADERS,
    )
    try:
        room = data["data"]["data"][0]
    except (KeyError, IndexError, TypeError):
        raise RoomCheckError(
            "抖音接口返回异常（房间号可能不存在或需要登录态），"
            "若长期失败请改用其他平台监控"
        )
    return {
        "live": room.get("status") == 2,
        "title": room.get("title", ""),
        "anchor": (room.get("owner") or {}).get("nickname", ""),
        "cover": ((room.get("cover") or {}).get("url_list") or [""])[0],
        "url": f"https://live.douyin.com/{room_id}",
    }


CHECKERS = {
    "bilibili": check_bilibili,
    "douyu": check_douyu,
    "huya": check_huya,
    "douyin": check_douyin,
}


def parse_room_input(text: str):
    """解析用户输入，成功返回 (platform, room_id, is_bili_uid)，失败返回 None。"""
    text = text.strip()
    m = EXPLICIT_PREFIX.match(text)
    if m:
        return PREFIX_MAP[m.group(1).lower()], m.group(2).strip(), False
    for platform, pattern in URL_PATTERNS:
        m = pattern.search(text)
        if m:
            if platform == "bilibili":
                return platform, m.group(2), m.group(1).lower() == "space"
            return platform, m.group(1), False
    if re.fullmatch(r"\d{4,}", text):
        return "bilibili", text, False
    return None


def room_key(platform: str, room_id: str) -> str:
    return f"{platform}:{room_id}"


@filter.command_group("直播监控", alias={"开播监控"})
def monitor_group():
    """直播监控指令组"""


_poll_task: asyncio.Task | None = None
"""模块级后台任务引用。热重载时模块会被重新导入，旧引用随之失效，
因此必须在创建新任务前取消它，防止新旧两个轮询任务并发导致重复推送。"""


class LiveMonitorPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.conf = config or {}
        self.data_file: Path = StarTools.get_data_dir() / "rooms.json"
        self.rooms: dict = {}
        self._last_check: dict[str, float] = {}
        self._load()
        global _poll_task
        if _poll_task is not None and not _poll_task.done():
            _poll_task.cancel()
        _poll_task = asyncio.create_task(self._poll_loop())

    # ---------- 持久化 ----------

    def _load(self):
        try:
            if self.data_file.exists():
                raw = json.loads(self.data_file.read_text(encoding="utf-8"))
                self.rooms = self._sanitize(raw)
        except Exception as e:
            self.logger.warning(f"读取监控数据失败: {e}")
            self.rooms = {}

    def _sanitize(self, raw) -> dict:
        """校验 rooms.json。手工编辑过的数据可能缺字段、类型不对，甚至 platform
        写错，轮询时会 KeyError/TypeError 整个房间组挂掉，所以入口处一次修好。"""
        rooms = {}
        if not isinstance(raw, dict):
            self.logger.warning("监控数据格式不对（顶层不是对象），已忽略")
            return rooms
        for key, room in raw.items():
            if not isinstance(room, dict):
                self.logger.warning(f"房间 {key} 数据不是对象，已跳过")
                continue
            platform = room.get("platform")
            if platform not in CHECKERS:
                self.logger.warning(f"房间 {key} 平台未知: {platform}，已跳过")
                continue
            room_id = str(room.get("room_id") or "").strip()
            if not room_id:
                self.logger.warning(f"房间 {key} 缺少 room_id，已跳过")
                continue
            watchers = room.get("watchers")
            watchers = (
                {str(k): str(v) for k, v in watchers.items() if str(k)}
                if isinstance(watchers, dict)
                else {}
            )
            live = room.get("live")
            rooms[str(key)] = {
                "platform": platform,
                "room_id": room_id,
                "title": str(room.get("title") or ""),
                "anchor": str(room.get("anchor") or ""),
                "cover": str(room.get("cover") or ""),
                "url": str(room.get("url") or ""),
                "live": live if live in (True, False) else None,
                "fails": 0,
                "watchers": watchers,
            }
        return rooms

    def _save(self):
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            self.data_file.write_text(
                json.dumps(self.rooms, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            self.logger.warning(f"保存监控数据失败: {e}")

    # ---------- 管理员 ----------

    def _admin_ids(self) -> set:
        raw = self.conf.get("admin_ids") or ""
        if isinstance(raw, list):
            parts = [str(x) for x in raw]
        else:
            parts = re.split(r"[,，;；\s]+", str(raw))
        return {p.strip() for p in parts if p.strip()}

    def _is_admin(self, event) -> bool:
        ids = self._admin_ids()
        if not ids:
            return True  # 未配置管理员时所有人可用
        return event.get_sender_id() in ids

    def _admin_only(self, event):
        if self._is_admin(event):
            return None
        return event.plain_result(
            "该操作仅限管理员，请联系管理员在插件配置 admin_ids 中添加你的 ID。"
        )

    # ---------- 指令 ----------

    @monitor_group.command("添加")
    async def cmd_add(self, event, room_input: str | None = None):
        """添加监控直播间。用法: /直播监控 添加 <直播间链接 或 平台:房间号>"""
        if deny := self._admin_only(event):
            yield deny
            return
        if not room_input:
            yield event.plain_result(
                "用法: /直播监控 添加 <直播间链接>\n"
                "示例:\n"
                "/直播监控 添加 https://live.bilibili.com/123456\n"
                "/直播监控 添加 https://space.bilibili.com/123456（主播主页）\n"
                "/直播监控 添加 斗鱼:123456\n"
                "/直播监控 添加 https://www.huya.com/xxx\n"
                "支持: B站直播、斗鱼、虎牙、抖音直播（纯数字默认按 B站房间号处理）"
            )
            return
        parsed = parse_room_input(room_input)
        if not parsed:
            yield event.plain_result(
                "无法识别的直播间链接或房间号。\n"
                "请直接发送直播间链接，或使用 平台:房间号 格式，如 斗鱼:123456"
            )
            return
        platform, room_id, is_bili_uid = parsed
        if is_bili_uid:
            try:
                async with aiohttp.ClientSession() as s:
                    room_id = await resolve_bilibili_uid(s, room_id)
            except Exception as e:
                yield event.plain_result(f"B站主播房间号解析失败: {e}")
                return
        key = room_key(platform, room_id)
        if key in self.rooms:
            room = self.rooms[key]
            existed = True
        else:
            room = {
                "platform": platform,
                "room_id": room_id,
                "title": "",
                "anchor": "",
                "cover": "",
                "url": "",
                "live": None,
                "watchers": {},
                "fails": 0,
            }
            self.rooms[key] = room
            existed = False
        room["watchers"][event.unified_msg_origin] = (
            event.get_sender_name() or event.get_sender_id()
        )
        self._save()
        pname = PLATFORM_NAMES[platform]
        if existed:
            tip = f"该房间已在监控列表中（{pname} 房间 {room_id}），当前订阅数: {len(room['watchers'])}"
        else:
            tip = f"已添加监控: {pname} 房间 {room_id}，本会话将收到开播/关播通知。"
        yield event.plain_result(tip + "\n首次检测只记录当前状态，不会立即推送通知。")

    @monitor_group.command("删除")
    async def cmd_del(self, event, room_input: str | None = None):
        """删除监控直播间。用法: /直播监控 删除 <平台:房间号>"""
        if deny := self._admin_only(event):
            yield deny
            return
        if not room_input:
            yield event.plain_result(
                "用法: /直播监控 删除 <平台:房间号>，可先用 列表 查看"
            )
            return
        key = self._resolve_key(room_input)
        if not key:
            yield event.plain_result("未找到该监控房间，可先用 /直播监控 列表 查看。")
            return
        room = self.rooms.pop(key)
        self._last_check.pop(key, None)
        self._save()
        yield event.plain_result(
            f"已删除监控: {PLATFORM_NAMES[room['platform']]} 房间 {room['room_id']}"
        )

    @monitor_group.command("列表")
    async def cmd_list(self, event):
        """查看当前监控的直播间列表"""
        if not self.rooms:
            yield event.plain_result(
                "当前没有监控任何直播间，使用 /直播监控 添加 <链接> 添加。"
            )
            return
        lines = ["当前监控列表:"]
        for room in self.rooms.values():
            if room["live"] is True:
                status = "[直播中]"
            elif room["live"] is False:
                status = "[未开播]"
            else:
                status = "[待检测]"
            title = f" | {room['title']}" if room["title"] else ""
            sub = "√" if event.unified_msg_origin in room["watchers"] else "-"
            lines.append(
                f"{sub} [{PLATFORM_NAMES[room['platform']]}] "
                f"{room['platform']}:{room['room_id']} {status}"
                f"{title}（订阅数 {len(room['watchers'])}）"
            )
            if self._is_admin(event):
                for umo in room["watchers"]:
                    lines.append(f"    ↳ {umo}")
        lines.append("（√ 表示本会话已订阅通知；删除/订阅 时使用 平台:房间号）")
        yield event.plain_result("\n".join(lines))

    @monitor_group.command("订阅")
    async def cmd_sub(self, event, room_input: str | None = None):
        """订阅某个已监控房间的开播通知。用法: /直播监控 订阅 <平台:房间号>"""
        if deny := self._admin_only(event):
            yield deny
            return
        if not room_input:
            yield event.plain_result("用法: /直播监控 订阅 <平台:房间号>")
            return
        key = self._resolve_key(room_input)
        if not key:
            yield event.plain_result("未找到该监控房间，可先用 /直播监控 列表 查看。")
            return
        self.rooms[key]["watchers"][event.unified_msg_origin] = (
            event.get_sender_name() or event.get_sender_id()
        )
        self._save()
        yield event.plain_result("已订阅该房间的开播/关播通知。")

    @monitor_group.command("取消")
    async def cmd_unsub(self, event, room_input: str | None = None):
        """取消本会话对某个房间的通知（不删除监控）。用法: /直播监控 取消 <平台:房间号>"""
        if deny := self._admin_only(event):
            yield deny
            return
        if not room_input:
            yield event.plain_result("用法: /直播监控 取消 <平台:房间号>")
            return
        key = self._resolve_key(room_input)
        if not key:
            yield event.plain_result("未找到该监控房间，可先用 /直播监控 列表 查看。")
            return
        self.rooms[key]["watchers"].pop(event.unified_msg_origin, None)
        self._save()
        yield event.plain_result("已取消本会话对该房间的通知。")

    @monitor_group.command("帮助")
    async def cmd_help(self, event):
        """查看直播监控帮助"""
        yield event.plain_result(
            "=== 直播开播监控 ===\n"
            "指令前缀: /直播监控 或 /开播监控\n"
            "添加 <直播间链接或 平台:房间号>  添加监控并订阅通知\n"
            "删除 <平台:房间号>  删除监控\n"
            "列表  查看监控列表及状态\n"
            "订阅 <平台:房间号>  订阅本会话通知\n"
            "取消 <平台:房间号>  取消本会话通知\n"
            "支持平台: B站直播(bili)、斗鱼(douyu)、虎牙(huya/hy)、抖音(douyin)\n"
            "开播/关播会自动推送主播名、房间号、标题、链接与封面。\n"
            "添加/删除/订阅/取消 仅限管理员（插件配置 admin_ids，留空则不限）。\n"
            "通知白名单：插件配置 notify_whitelist，填写允许接收通知的会话UMO，留空不限制。"
        )

    def _resolve_key(self, text: str):
        text = text.strip()
        if text in self.rooms:
            return text
        parsed = parse_room_input(text)
        if parsed:
            key = room_key(parsed[0], parsed[1])
            if key in self.rooms:
                return key
        for key, room in self.rooms.items():
            if text == room["room_id"]:
                return key
        return None

    # ---------- 轮询 ----------

    def _conf_int(self, key: str, fallback: int) -> int:
        try:
            return int(self.conf.get(key) or 0) or fallback
        except (TypeError, ValueError):
            return fallback

    def _room_interval(self, room: dict) -> int:
        """未开播房间按开播检测间隔轮询，直播中房间按关播检测间隔轮询。"""
        legacy = self._conf_int("poll_interval", 0)
        if room.get("live") is True:
            base = self._conf_int("end_check_interval", legacy or 60)
        else:
            base = self._conf_int("start_check_interval", legacy or 30)
        return max(15, base)

    async def _poll_loop(self):
        session = aiohttp.ClientSession()
        try:
            while True:
                now = time.monotonic()
                due = []
                for key, room in self.rooms.items():
                    if not room["watchers"]:
                        continue
                    if now - self._last_check.get(key, 0) >= self._room_interval(room):
                        due.append(key)
                for key in due:
                    room = self.rooms.get(key)
                    if not room:
                        continue
                    try:
                        await self._check_room(session, room)
                        room["fails"] = 0
                    except Exception as e:
                        room["fails"] = room.get("fails", 0) + 1
                        if room["fails"] in (1, 10) or room["fails"] % 30 == 0:
                            self.logger.warning(
                                f"检查房间 {key} 失败(第{room['fails']}次): {e}"
                            )
                    finally:
                        self._last_check[key] = time.monotonic()
                    await asyncio.sleep(1)
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        finally:
            await session.close()

    async def _check_room(self, session: aiohttp.ClientSession, room: dict):
        platform = room["platform"]
        checker = CHECKERS[platform]
        cookie = str(self.conf.get("bilibili_cookie") or "")
        if platform == "bilibili":
            result = await checker(session, room["room_id"], cookie)
            # B站接口只给 uid，昵称解析一次后缓存
            if not room.get("anchor"):
                uid = result.get("anchor_uid", "")
                if uid:
                    name = await resolve_bilibili_anchor(session, uid, cookie)
                    if name:
                        room["anchor"] = name
        else:
            result = await checker(session, room["room_id"])

        prev = room.get("live")
        now = result["live"]
        room["title"] = result["title"] or room.get("title", "")
        room["anchor"] = result.get("anchor") or room.get("anchor", "")
        room["cover"] = result.get("cover") or room.get("cover", "")
        room["url"] = result["url"]
        room["live"] = now

        if prev is None:
            # 首次检测只记录基线，不推送
            self._save()
            return
        anchor = room["anchor"] or room["room_id"]
        pname = PLATFORM_NAMES[platform]
        if prev is False and now is True and self.conf.get("notify_start", True):
            text = (
                "🔴 开播通知\n"
                "────────────────\n"
                f"👤 主播：{anchor}\n"
                f"📡 平台：{pname}\n"
                f"🚪 房间：{room['room_id']}\n"
                f"📺 标题：{result['title'] or '(无标题)'}\n"
                "────────────────\n"
                f"🔗 {result['url']}"
            )
            await self._notify(session, room, text, room.get("cover", ""))
        elif prev is True and now is False and self.conf.get("notify_end", True):
            text = (
                "⚫ 关播通知\n"
                "────────────────\n"
                f"👤 主播：{anchor}\n"
                f"📡 平台：{pname}\n"
                f"🚪 房间：{room['room_id']}\n"
                "────────────────\n"
                "直播已结束，下次开播再会～"
            )
            await self._notify(session, room, text, "")
        self._save()

    def _notify_whitelist(self) -> set:
        raw = self.conf.get("notify_whitelist") or ""
        if isinstance(raw, list):
            parts = [str(x) for x in raw]
        else:
            parts = re.split(r"[,，;；\s]+", str(raw))
        return {p.strip() for p in parts if p.strip()}

    async def _fetch_cover_base64(
        self, session: aiohttp.ClientSession, room: dict, cover: str
    ) -> str:
        if not cover.startswith(("http://", "https://")):
            raise RoomCheckError("封面地址不是 http(s) 链接")
        headers = (
            _bili_headers(str(self.conf.get("bilibili_cookie") or ""))
            if room["platform"] == "bilibili"
            else HEADERS
        )
        async with session.get(
            cover, headers=headers, timeout=aiohttp.ClientTimeout(10)
        ) as resp:
            if resp.status != 200:
                raise RoomCheckError(f"HTTP {resp.status}")
            ctype = (resp.content_type or "").lower()
            if ctype and not ctype.startswith("image/"):
                # 平台防盗链/限流时会返回 html，若照样转成 base64 会让发送报错，
                # 进而导致整条通知（连文字一起）发不出去
                raise RoomCheckError(f"返回内容不是图片: {ctype}")
            if (resp.content_length or 0) > MAX_COVER_BYTES:
                raise RoomCheckError(f"封面过大: {resp.content_length} 字节")
            data = await resp.read()
        if not data:
            raise RoomCheckError("封面内容为空")
        if len(data) > MAX_COVER_BYTES:
            raise RoomCheckError(f"封面过大: {len(data)} 字节")
        return base64.b64encode(data).decode()

    async def _notify(
        self, session: aiohttp.ClientSession, room: dict, text: str, cover: str = ""
    ):
        whitelist = self._notify_whitelist()
        watchers = list(room["watchers"].keys())
        targets = [umo for umo in watchers if not whitelist or umo in whitelist]
        skipped = [umo for umo in watchers if whitelist and umo not in whitelist]
        if skipped:
            self.logger.info(f"以下会话不在 notify_whitelist 内，跳过推送: {skipped}")
        if not targets:
            return
        # 封面只下载一次。AstrBot 发送 http 图片时每次都会重新拉图（不带防盗链
        # Referer、也不校验状态码），多群复用同一张 URL 图片时，第 2 个群之后可能
        # 拿到错误页并抛异常，导致整条通知（连文字一起）发不出去。
        cover_b64 = ""
        if cover:
            try:
                cover_b64 = await self._fetch_cover_base64(session, room, cover)
            except Exception as e:
                self.logger.warning(f"封面下载失败，本次仅发送文字: {e}")
        for idx, umo in enumerate(targets):
            # 每个会话都用独立的消息链，避免组件被某个平台改写后影响后面的会话
            sent = False
            if cover_b64:
                sent = await self._send(umo, [Plain(text), Image.fromBase64(cover_b64)])
            if not sent:
                sent = await self._send(umo, [Plain(text)])
            if not sent:
                self.logger.error(
                    f"通知最终未能送达 {umo}，请确认该平台的 ID 正确且支持主动消息"
                    "（qq_official 不支持 context.send_message）"
                )
            if idx + 1 < len(targets):
                await asyncio.sleep(1)

    async def _send(self, umo: str, comps: list) -> bool:
        try:
            await self.context.send_message(umo, MessageChain(comps))
            return True
        except Exception as e:
            self.logger.warning(f"推送通知到 {umo} 失败: {e!r}")
            return False

    async def terminate(self):
        """插件被卸载/停用/热重载时调用：保存数据并停止后台任务。"""
        global _poll_task
        self._save()
        if _poll_task is not None and not _poll_task.done():
            _poll_task.cancel()
            try:
                await _poll_task
            except asyncio.CancelledError:
                pass
        _poll_task = None
