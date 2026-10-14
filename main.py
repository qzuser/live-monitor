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

# 投稿防刷：投稿指令对所有用户开放，靠房间接口校验之外再加两道数量限制。
MAX_PENDING_PER_SESSION = 3
MAX_PENDING_TOTAL = 50

DIVIDER = "────────────────"

# 自动更新检查：启动后第一次检查的延迟。延迟是为了避开插件加载阶段
# （此时主动消息通道可能还没就绪），之后按配置项 update_check_interval_hours 定期查。
UPDATE_FIRST_DELAY = 60
DEFAULT_UPDATE_INTERVAL_HOURS = 24

# 与 AstrBot 自带更新器（core/zip_updator.py check_update）保持一致的预发布标签口径，
# 否则插件提醒的版本和 WebUI 里能更新的版本可能对不上。
PRERELEASE_RE = re.compile(r"[\-_.]?(alpha|beta|rc|dev)[\-_.]?\d*$", re.IGNORECASE)

GITHUB_REPO_RE = re.compile(r"github\.com/([\w.()-]+)/([\w.()-]+)", re.IGNORECASE)

GITHUB_HEADERS = dict(HEADERS)
GITHUB_HEADERS["Accept"] = "application/vnd.github+json"

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
    """解析用户输入，成功返回 (platform, room_id, is_bili_uid)，失败返回 None。

    必须 str() 兜底：AstrBot 会把纯数字参数转成 int（见 command.py
    validate_and_convert_params），直接 .strip() 会抛 AttributeError。
    """
    text = str(text).strip()
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


def read_plugin_meta() -> tuple[str, str]:
    """从插件自身目录的 metadata.yaml 读取 (version, repo)。

    版本号只在 metadata 里维护一份：这里直接解析，避免 main.py 再抄一遍常量后
    和 metadata.yaml 漂移；顺带把 repo 也取出来当作更新检查的地址来源。
    """
    try:
        text = (Path(__file__).parent / "metadata.yaml").read_text(encoding="utf-8")
    except Exception:
        return "", ""
    version = ""
    repo = ""
    m = re.search(r"^version:\s*(\S+)\s*$", text, re.MULTILINE)
    if m:
        version = m.group(1).strip("\"'")
    m = re.search(r"^repo:\s*(.+?)\s*$", text, re.MULTILINE)
    if m:
        repo = m.group(1).strip("\"'")
    return version, repo


def parse_version(text) -> tuple:
    """取出版本号里的数字段，v 前缀、缺位、非数字后缀都忽略。"""
    m = re.search(r"v?(\d+(?:\.\d+)*)", str(text or ""))
    if not m:
        return ()
    return tuple(int(part) for part in m.group(1).split("."))


def version_is_newer(candidate, current) -> bool:
    """比较两个版本号，位数不足的一侧按 0 补齐（0.1.10 > 0.1.9）。"""
    left = parse_version(candidate)
    right = parse_version(current)
    if not left or not right:
        return False
    size = max(len(left), len(right))
    left += (0,) * (size - len(left))
    right += (0,) * (size - len(right))
    return left > right


def format_submission(sub: dict, header: str, actions: list[str] | None = None) -> str:
    """渲染投稿记录：投送给审核会话时带处理指令，回投投稿人时不带。"""
    lines = [
        header,
        DIVIDER,
        f"👤 主播：{sub['anchor'] or sub['room_id']}",
        f"📡 平台：{PLATFORM_NAMES[sub['platform']]}",
        f"🚪 房间：{sub['room_id']}",
        f"📺 标题：{sub['title'] or '(无标题)'}",
        f"📊 状态：{'直播中' if sub['live'] else '未开播'}",
        f"🙋 投稿人：{sub['submitter_name']}",
    ]
    if sub["url"]:
        lines.append(f"🔗 {sub['url']}")
    if actions:
        lines.append(DIVIDER)
        lines.extend(actions)
    return "\n".join(lines)


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
        self.submissions: dict = {}
        self.reviewers: dict = {}
        self._next_sub_id = 1
        self._last_check: dict[str, float] = {}
        self.version, repo_url = read_plugin_meta()
        m = GITHUB_REPO_RE.search(repo_url)
        self._repo_slug = f"{m.group(1)}/{m.group(2)}" if m else ""
        self._repo_page = (
            f"https://github.com/{self._repo_slug}" if self._repo_slug else ""
        )
        self._next_update_check = time.monotonic() + UPDATE_FIRST_DELAY
        self._update_hint_version = ""
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
                if isinstance(raw, dict) and any(
                    key in raw
                    for key in ("version", "rooms", "submissions", "reviewers")
                ):
                    self.rooms = self._sanitize(raw.get("rooms"))
                    self.submissions = self._sanitize_submissions(
                        raw.get("submissions")
                    )
                    self.reviewers = self._sanitize_sessions(raw.get("reviewers"))
                else:
                    # 0.1.1 及以前：顶层对象就是房间表，直接按旧格式载入
                    self.rooms = self._sanitize(raw)
                    self.submissions = {}
                    self.reviewers = {}
                ids = [int(sid) for sid in self.submissions]
                self._next_sub_id = (max(ids) + 1) if ids else 1
        except Exception as e:
            self.logger.warning(f"读取监控数据失败: {e}")
            self.rooms = {}
            self.submissions = {}
            self.reviewers = {}

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
            watchers = self._sanitize_sessions(room.get("watchers"))
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

    def _sanitize_sessions(self, raw) -> dict:
        """校验 {会话UMO: 名称} 结构（订阅者 watchers、审核者 reviewers 同构）。"""
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if str(k)}

    def _sanitize_submissions(self, raw) -> dict:
        subs = {}
        if not isinstance(raw, dict):
            return subs
        for sid, sub in raw.items():
            if not isinstance(sub, dict) or not str(sid).isdigit():
                continue
            platform = sub.get("platform")
            room_id = str(sub.get("room_id") or "").strip()
            if platform not in CHECKERS or not room_id:
                continue
            submitter_umo = str(sub.get("submitter_umo") or "")
            if not submitter_umo:
                # 没有投稿人就没人能收到审核结果，这条留着只会挡住同名投稿
                continue
            try:
                created_at = float(sub.get("created_at") or 0)
            except (TypeError, ValueError):
                created_at = 0.0
            subs[str(sid)] = {
                "platform": platform,
                "room_id": room_id,
                "title": str(sub.get("title") or ""),
                "anchor": str(sub.get("anchor") or ""),
                "url": str(sub.get("url") or ""),
                "live": sub.get("live") is True,
                "submitter_umo": submitter_umo,
                "submitter_name": str(sub.get("submitter_name") or submitter_umo),
                "created_at": created_at,
            }
        return subs

    def _save(self):
        payload = {
            "version": 2,
            "rooms": self.rooms,
            "submissions": self.submissions,
            "reviewers": self.reviewers,
        }
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            self.data_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
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
        existed = key in self.rooms
        room = self._register_room(platform, room_id)
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
        if self.submissions and self._is_admin(event):
            lines.append(
                f"另有 {len(self.submissions)} 条待审核投稿，"
                "用 /直播监控 投稿列表 查看。"
            )
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

    @monitor_group.command("投稿")
    async def cmd_submit(self, event, room_input: str | None = None):
        """投稿直播间，投送给管理员审核。用法: /直播监控 投稿 <直播间链接>"""
        if not room_input:
            yield event.plain_result(
                "用法: /直播监控 投稿 <直播间链接>\n"
                "示例: /直播监控 投稿 https://live.bilibili.com/123456\n"
                "投稿不需要管理员权限：插件会先校验房间是否存在，再投送给管理员审核，"
                "通过后本会话收到开播/关播通知。"
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
        try:
            async with aiohttp.ClientSession() as session:
                if is_bili_uid:
                    room_id = await resolve_bilibili_uid(session, room_id)
                info = await self._fetch_room_info(session, platform, room_id)
        except Exception as e:
            yield event.plain_result(f"直播间校验失败: {e}")
            return
        key = room_key(platform, room_id)
        pname = PLATFORM_NAMES[platform]
        if key in self.rooms:
            yield event.plain_result(
                f"{pname} 房间 {room_id} 已在监控列表中，无需投稿。\n"
                f"想让本会话接收通知，请管理员执行 /直播监控 订阅 {key}。"
            )
            return
        for sid, sub in self.submissions.items():
            if room_key(sub["platform"], sub["room_id"]) == key:
                yield event.plain_result(
                    f"该房间已有待审核投稿（#{sid}），请等管理员处理后再投。"
                )
                return
        umo = event.unified_msg_origin
        mine = sorted(
            (sid for sid, s in self.submissions.items() if s["submitter_umo"] == umo),
            key=int,
        )
        if len(mine) >= MAX_PENDING_PER_SESSION:
            yield event.plain_result(
                f"你的待审核投稿已达上限 {MAX_PENDING_PER_SESSION} 条"
                f"（#{', #'.join(mine)}），请等管理员处理后再投。"
            )
            return
        if len(self.submissions) >= MAX_PENDING_TOTAL:
            yield event.plain_result("待审核投稿已经排满了，请联系管理员先处理一批。")
            return

        sid = str(self._next_sub_id)
        self._next_sub_id += 1
        sub = {
            "platform": platform,
            "room_id": room_id,
            "title": info.get("title", ""),
            "anchor": info.get("anchor", ""),
            "url": info.get("url", ""),
            "live": info.get("live") is True,
            "submitter_umo": umo,
            "submitter_name": event.get_sender_name() or event.get_sender_id(),
            "created_at": time.time(),
        }
        self.submissions[sid] = sub
        self._save()
        sent = await self._deliver(
            format_submission(
                sub,
                f"📮 新投稿 #{sid} 待审核",
                [
                    f"/直播监控 投稿通过 {sid}",
                    f"/直播监控 投稿拒绝 {sid} [原因]",
                ],
            )
        )
        if sent:
            tip = (
                f"已投稿（#{sid}），并投送给 {sent} 个审核会话。"
                "管理员通过后本会话会收到开播/关播通知。"
            )
        elif self.reviewers:
            tip = (
                f"已投稿（#{sid}），但推送给 {len(self.reviewers)} 个审核会话均失败"
                "（详见插件日志），管理员请用 /直播监控 投稿列表 查看待审。"
            )
        else:
            tip = (
                f"已投稿（#{sid}）。\n"
                "目前还没有审核会话接收投稿，管理员请用 "
                "/直播监控 投稿列表 查看待审，或在想接收投稿的群里执行 "
                "/直播监控 审核订阅。"
            )
        yield event.plain_result(tip)

    @monitor_group.command("投稿列表")
    async def cmd_submissions(self, event):
        """查看待审核投稿。用法: /直播监控 投稿列表"""
        if deny := self._admin_only(event):
            yield deny
            return
        lines = [f"待审核投稿 {len(self.submissions)} 条:"]
        for sid in sorted(self.submissions, key=int):
            sub = self.submissions[sid]
            title = f" | {sub['title']}" if sub["title"] else ""
            lines.append(
                f"#{sid} [{PLATFORM_NAMES[sub['platform']]}] "
                f"{sub['platform']}:{sub['room_id']} "
                f"{sub['anchor'] or '(未知主播)'}{title} "
                f"来自 {sub['submitter_name']}，{self._ago(sub['created_at'])}"
            )
        if len(self.submissions) == 1:
            sid = next(iter(self.submissions))
            lines.append(f"处理: /直播监控 投稿通过 {sid} 或 投稿拒绝 {sid} [原因]")
        else:
            lines.append("处理: /直播监控 投稿通过 <编号> 或 投稿拒绝 <编号> [原因]")
        if self.reviewers:
            lines.append(
                "投送目标: "
                + "、".join(f"{umo}({name})" for umo, name in self.reviewers.items())
            )
        else:
            lines.append(
                "⚠ 尚未配置投送目标，新投稿不会主动推送；"
                "在需要接收的会话中执行 /直播监控 审核订阅。"
            )
        yield event.plain_result("\n".join(lines))

    @monitor_group.command("投稿通过")
    async def cmd_approve(self, event, sub_id: str | None = None):
        """通过投稿并加入监控。用法: /直播监控 投稿通过 <编号>"""
        if deny := self._admin_only(event):
            yield deny
            return
        sid, sub = self._find_submission(sub_id)
        if not sub:
            yield event.plain_result(
                "未找到该待审核投稿，可先用 /直播监控 投稿列表 查看编号。"
            )
            return
        key = room_key(sub["platform"], sub["room_id"])
        is_new = key not in self.rooms
        room = self._register_room(sub["platform"], sub["room_id"])
        room["watchers"][sub["submitter_umo"]] = sub["submitter_name"]
        room["watchers"][event.unified_msg_origin] = (
            event.get_sender_name() or event.get_sender_id()
        )
        self.submissions.pop(sid)
        self._save()
        pname = PLATFORM_NAMES[sub["platform"]]
        if sub["submitter_umo"] != event.unified_msg_origin:
            await self._send(
                sub["submitter_umo"],
                [
                    Plain(
                        f"✅ 投稿 #{sid} 已通过审核\n"
                        f"{pname} 房间 {sub['room_id']} 已加入监控，"
                        "本会话将收到开播/关播通知。"
                    )
                ],
            )
        tip = f"已通过投稿 #{sid}: {pname} 房间 {sub['room_id']}，订阅数 {len(room['watchers'])}。"
        if not is_new:
            tip += (
                "\n该房间此前已在监控列表中（可能有人直接添加过），本次只追加了订阅。"
            )
        yield event.plain_result(tip)

    @monitor_group.command("投稿拒绝")
    async def cmd_reject(
        self, event, sub_id: str | None = None, note: str | None = None
    ):
        """拒绝投稿。用法: /直播监控 投稿拒绝 <编号> [原因]"""
        if deny := self._admin_only(event):
            yield deny
            return
        sid, sub = self._find_submission(sub_id)
        if not sub:
            yield event.plain_result(
                "未找到该待审核投稿，可先用 /直播监控 投稿列表 查看编号。"
            )
            return
        reason = str(note).strip() if note else ""
        self.submissions.pop(sid)
        self._save()
        text = (
            f"❌ 投稿 #{sid} 未通过审核\n"
            f"📝 原因：{reason or '管理员未说明'}\n"
            "可以修改后重新投稿（/直播监控 投稿 <直播间链接>）。"
        )
        if sub["submitter_umo"] != event.unified_msg_origin:
            await self._send(sub["submitter_umo"], [Plain(text)])
            tip = f"已拒绝投稿 #{sid}，结果已回投投稿会话。"
        else:
            tip = f"已拒绝投稿 #{sid}。"
        yield event.plain_result(tip)

    @monitor_group.command("审核订阅")
    async def cmd_review_on(self, event):
        """把本会话加入投稿投送列表。用法: /直播监控 审核订阅"""
        if deny := self._admin_only(event):
            yield deny
            return
        self.reviewers[event.unified_msg_origin] = (
            event.get_sender_name() or event.get_sender_id()
        )
        self._save()
        yield event.plain_result(
            f"本会话已加入投稿投送列表（当前 {len(self.reviewers)} 个会话），"
            "新投稿会自动推送到这里。"
        )

    @monitor_group.command("审核取消")
    async def cmd_review_off(self, event):
        """把本会话移出投稿投送列表。用法: /直播监控 审核取消"""
        if deny := self._admin_only(event):
            yield deny
            return
        if event.unified_msg_origin not in self.reviewers:
            yield event.plain_result("本会话不在投稿投送列表中。")
            return
        self.reviewers.pop(event.unified_msg_origin)
        self._save()
        yield event.plain_result(
            f"本会话已移出投稿投送列表（剩余 {len(self.reviewers)} 个会话）。"
        )

    def _find_submission(self, sub_id):
        """按编号取待审核投稿，编号可带 # 前缀；找不到返回 (None, None)。"""
        if sub_id is None:
            return None, None
        sid = str(sub_id).lstrip("#").strip()
        sub = self.submissions.get(sid)
        return (sid, sub) if sub else (None, None)

    def _register_room(self, platform: str, room_id: str) -> dict:
        """把房间加入监控表（不落盘），已存在则返回原记录。"""
        key = room_key(platform, room_id)
        room = self.rooms.get(key)
        if room is None:
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
        return room

    async def _fetch_room_info(
        self, session: aiohttp.ClientSession, platform: str, room_id: str
    ) -> dict:
        """投稿时拉一次房间信息：既验证房间真实存在，也给审核消息补上主播与标题。"""
        cookie = str(self.conf.get("bilibili_cookie") or "")
        if platform == "bilibili":
            result = await CHECKERS[platform](session, room_id, cookie)
            # B站接口只给 uid，昵称另外查一次
            if not result.get("anchor") and result.get("anchor_uid"):
                result["anchor"] = await resolve_bilibili_anchor(
                    session, result["anchor_uid"], cookie
                )
            return result
        return await CHECKERS[platform](session, room_id)

    async def _deliver(self, text: str) -> int:
        """把投稿消息投送给所有审核会话，返回成功送达的会话数。"""
        targets = list(self.reviewers.keys())
        sent = 0
        for idx, umo in enumerate(targets):
            if await self._send(umo, [Plain(text)]):
                sent += 1
            if idx + 1 < len(targets):
                await asyncio.sleep(1)
        return sent

    @staticmethod
    def _ago(created_at: float) -> str:
        if not created_at:
            return "时间未知"
        minutes = int((time.time() - created_at) // 60)
        if minutes < 1:
            return "刚刚"
        if minutes < 60:
            return f"{minutes} 分钟前"
        if minutes < 24 * 60:
            return f"{minutes // 60} 小时前"
        return f"{minutes // (24 * 60)} 天前"

    # ---------- 插件自动更新检查 ----------

    async def _fetch_releases(self, session: aiohttp.ClientSession) -> list:
        url = f"https://api.github.com/repos/{self._repo_slug}/releases"
        async with session.get(
            url, headers=GITHUB_HEADERS, timeout=aiohttp.ClientTimeout(10)
        ) as resp:
            if resp.status != 200:
                raise RoomCheckError(f"GitHub API HTTP {resp.status}")
            data = await resp.json(**_get_json_kwargs())
        if not isinstance(data, list):
            raise RoomCheckError("GitHub Release 返回格式不对（顶层不是数组）")
        return data

    def _pick_release(self, releases: list) -> dict | None:
        """取最新的正式版：跳过 draft、prerelease 与 alpha/beta/rc/dev 标签。"""
        for item in releases:
            if not isinstance(item, dict):
                continue
            if item.get("draft") or item.get("prerelease"):
                continue
            tag = str(item.get("tag_name") or "")
            if not tag or PRERELEASE_RE.search(tag):
                continue
            return item
        return None

    @staticmethod
    def _release_info(item: dict) -> dict:
        return {
            "version": str(item.get("tag_name") or ""),
            "published_at": str(item.get("published_at") or "")[:10],
            "body": str(item.get("body") or ""),
            "url": str(item.get("html_url") or ""),
        }

    def _update_text(self, release: dict, header: str) -> str:
        lines = [
            header,
            DIVIDER,
            f"📦 当前版本：v{self.version or '未知'}",
            f"✨ 最新版本：{release['version']}",
        ]
        if release["published_at"]:
            lines.append(f"🗓 发布于：{release['published_at']}")
        if self._repo_page:
            lines.append(f"🔗 {self._repo_page}/releases")
        body = [ln.strip() for ln in release["body"].splitlines() if ln.strip()]
        if body:
            lines.append(DIVIDER)
            lines.extend(body[:6])
        lines.append(DIVIDER)
        lines.append(
            "更新方式：AstrBot WebUI → 插件管理 → 本插件 → 更新"
            "（由 AstrBot 覆盖插件目录，监控数据在 data/plugin_data 下不受影响）"
        )
        return "\n".join(lines)

    async def _check_plugin_update(self, session: aiohttp.ClientSession, now: float):
        """后台定期查版本；只在有新版本且没提醒过该版本时投送一次，避免每次轮询刷屏。"""
        self._next_update_check = (
            now
            + max(
                1,
                self._conf_int(
                    "update_check_interval_hours", DEFAULT_UPDATE_INTERVAL_HOURS
                ),
            )
            * 3600
        )
        if not self._repo_slug or not self.version:
            self.logger.warning("metadata.yaml 缺少 version 或 repo，跳过更新检查")
            return
        try:
            item = self._pick_release(await self._fetch_releases(session))
        except Exception as e:
            self.logger.warning(f"检查插件更新失败: {e}")
            return
        if not item:
            self.logger.warning("GitHub 仓库上没有可用的正式版 Release")
            return
        release = self._release_info(item)
        if not version_is_newer(release["version"], self.version):
            return
        if self._update_hint_version == release["version"]:
            return
        self._update_hint_version = release["version"]
        sent = await self._deliver(self._update_text(release, "🆕 直播监控插件可更新"))
        if sent:
            self.logger.info(
                f"发现新版本 {release['version']}（当前 v{self.version}），已提醒 {sent} 个审核会话"
            )
        else:
            self.logger.info(
                f"发现新版本 {release['version']}（当前 v{self.version}），"
                "但没有审核会话可提醒，管理员请用 /直播监控 检查更新 查看"
            )

    @monitor_group.command("检查更新")
    async def cmd_check_update(self, event):
        """检查插件是否有新版本。用法: /直播监控 检查更新"""
        if deny := self._admin_only(event):
            yield deny
            return
        if not self._repo_slug or not self.version:
            yield event.plain_result(
                "metadata.yaml 里没有可用的 version / repo，无法检查更新。"
            )
            return
        try:
            async with aiohttp.ClientSession() as session:
                item = self._pick_release(await self._fetch_releases(session))
        except Exception as e:
            yield event.plain_result(f"检查更新失败: {e}")
            return
        if not item:
            yield event.plain_result(
                f"{self._repo_page} 上还没有正式版 Release，无法比较版本。"
            )
            return
        release = self._release_info(item)
        if not version_is_newer(release["version"], self.version):
            yield event.plain_result(
                f"当前 v{self.version} 已是最新版本（仓库最新正式版：{release['version']}）。"
            )
            return
        # 手动查过就视为已提醒，避免后台再推一遍同样的内容
        self._update_hint_version = release["version"]
        yield event.plain_result(self._update_text(release, "🆕 发现新版本"))

    @monitor_group.command("帮助")
    async def cmd_help(self, event):
        """查看直播监控帮助"""
        yield event.plain_result(
            "=== 直播开播监控 ===\n"
            "指令前缀: /直播监控 或 /开播监控\n"
            "添加 <直播间链接或 平台:房间号>  添加监控并订阅通知（管理员）\n"
            "删除 <平台:房间号>  删除监控\n"
            "列表  查看监控列表及状态\n"
            "订阅 <平台:房间号>  订阅本会话通知\n"
            "取消 <平台:房间号>  取消本会话通知\n"
            "投稿 <直播间链接>  投稿房间，投送给管理员审核（所有人可用）\n"
            "投稿列表  查看待审核投稿（管理员）\n"
            "投稿通过 <编号> / 投稿拒绝 <编号> [原因]  处理投稿（管理员，原因不能含空格）\n"
            "审核订阅 / 审核取消  让本会话接收（或停止接收）投稿推送（管理员）\n"
            "检查更新  立即查一次 GitHub 上有没有新版插件（管理员）\n"
            "支持平台: B站直播(bili)、斗鱼(douyu)、虎牙(huya/hy)、抖音(douyin)\n"
            "开播/关播会自动推送主播名、房间号、标题、链接与封面。\n"
            "除 投稿/列表/帮助 外的管理指令仅限管理员（插件配置 admin_ids，留空则不限）。\n"
            "通知白名单：插件配置 notify_whitelist，填写允许接收通知的会话UMO，留空不限制。"
        )

    def _resolve_key(self, text: str):
        text = str(text).strip()
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
                if self.conf.get("update_check", True) and (
                    now >= self._next_update_check
                ):
                    await self._check_plugin_update(session, now)
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
                f"{DIVIDER}\n"
                f"👤 主播：{anchor}\n"
                f"📡 平台：{pname}\n"
                f"🚪 房间：{room['room_id']}\n"
                f"📺 标题：{result['title'] or '(无标题)'}\n"
                f"{DIVIDER}\n"
                f"🔗 {result['url']}"
            )
            await self._notify(session, room, text, room.get("cover", ""))
        elif prev is True and now is False and self.conf.get("notify_end", True):
            text = (
                "⚫ 关播通知\n"
                f"{DIVIDER}\n"
                f"👤 主播：{anchor}\n"
                f"📡 平台：{pname}\n"
                f"🚪 房间：{room['room_id']}\n"
                f"{DIVIDER}\n"
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
