import ast
import asyncio
import base64
import io
import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

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

# UP 主稿件历史只用于去重，保留最近这么多条即可覆盖几次漏检，不能让列表无限增长。
MAX_SEEN_UPLOADS = 60
# 一次检查最多单独推送几条新稿件。UP 主批量补档时会一次冒出一堆，超过就合并成一条。
DEFAULT_UPLOAD_MAX_NOTIFY = 3
DEFAULT_UPLOAD_INTERVAL = 300
MIN_UPLOAD_INTERVAL = 60

DIVIDER = "────────────────"

# 自动更新检查：启动后第一次检查的延迟。延迟是为了避开插件加载阶段
# （此时主动消息通道可能还没就绪），之后按配置项 update_check_interval_hours 定期查。
UPDATE_FIRST_DELAY = 60
DEFAULT_UPDATE_INTERVAL_HOURS = 24

# 热更新（/直播监控 更新）：下载包体积上限。插件本体只有几十 KB，
# 超过这个数说明拿错了东西（比如被代理页替换成了 html），直接放弃。
MAX_UPDATE_BYTES = 2 * 1024 * 1024
# 只从更新包里取这些文件覆盖到自己目录：多余的目录、依赖、脚本一律不落地。
UPDATE_REQUIRED_FILES = ("main.py", "metadata.yaml", "_conf_schema.json")
UPDATE_OPTIONAL_FILES = ("README.md", "LICENSE")
UPDATE_BACKUP_KEEP = 5
# 回复发出去以后到真正调用热重载之间的等待时间。热重载会终止当前实例，
# 必须留够时间把指令回复推送完，否则管理员看不到结果。
RELOAD_DELAY = 5.0
PENDING_UPDATE_FILE = "update_pending.json"

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

BILI_SPACE_URL_RE = re.compile(r"space\.bilibili\.com/(\d+)", re.IGNORECASE)
BILI_LIVE_URL_RE = re.compile(r"live\.bilibili\.com", re.IGNORECASE)

# 各平台房间号的合法字符。链接解析出来的天然满足，但 "平台:房间号" 的写法与手工编辑
# 过的 rooms.json 必须自己校验：房间号会被拼进请求 URL，放过 & ? / # 就等于让外人
# 改写查询串和路径。
ROOM_ID_RE = {
    "bilibili": re.compile(r"\d{1,15}"),
    "douyin": re.compile(r"\d{1,20}"),
    "douyu": re.compile(r"[A-Za-z0-9]{1,32}"),
    "huya": re.compile(r"[A-Za-z0-9_-]{1,48}"),
}

# 登录 Cookie 只发给B站自己的域名：封面地址来自接口，也可能被手工改过，
# 不加限制就等于把登录态转发给任意站点。
BILI_COOKIE_HOSTS = ("bilibili.com", "bilibili.cn", "hdslb.com")


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


def _space_headers(cookie: str = "") -> dict:
    """B站空间（非直播）接口的请求头。Referer 必须是空间页，用直播 Referer 会被风控。"""
    h = dict(HEADERS)
    h["Referer"] = "https://space.bilibili.com/"
    if cookie:
        h["Cookie"] = cookie
    return h


def _as_int(value, fallback: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return fallback


async def fetch_up_info(
    session: aiohttp.ClientSession, mid: str, cookie: str = ""
) -> dict:
    """校验 mid 是否真实存在，并取回 UP 主昵称、头像与稿件总数。"""
    data = await _get_json(
        session,
        f"https://api.bilibili.com/x/web-interface/card?mid={mid}&photo=false",
        _space_headers(cookie),
    )
    if data.get("code") != 0:
        raise RoomCheckError(
            f"bilibili 接口返回 code={data.get('code')}，mid 可能不存在"
        )
    d = data.get("data") or {}
    name = str(((d.get("card") or {}).get("name")) or "").strip()
    if not name:
        raise RoomCheckError("该 mid 查不到 UP 主信息，请确认空间链接里的数字")
    return {
        "name": name,
        "face": str((d.get("card") or {}).get("face") or ""),
        "archive_count": _as_int(d.get("archive_count")),
    }


async def fetch_up_uploads(
    session: aiohttp.ClientSession, mid: str, cookie: str = "", limit: int = 20
) -> list:
    """取 UP 主最新稿件，按发布时间从新到旧。

    用 medialist/resource/list 而不是 x/space/wbi/arc/search：后者要 WBI 签名且
    容易被风控（返回 -352/-403），这个接口免 WBI、免 Cookie 就能拿到列表。
    """
    url = (
        "https://api.bilibili.com/x/v2/medialist/resource/list"
        f"?type=1&biz_id={mid}&otype=2&ps={limit}&m_source=11&platform=web"
    )
    data = await _get_json(session, url, _space_headers(cookie))
    if data.get("code") != 0:
        raise RoomCheckError(
            f"bilibili 稿件接口返回 code={data.get('code')}（{data.get('message')}）"
        )
    items = (data.get("data") or {}).get("media_list") or []
    uploads = []
    for item in items:
        if not isinstance(item, dict):
            continue
        bvid = str(item.get("bv_id") or "").strip()
        if not bvid:
            continue
        # 只认该 UP 主自己的稿件：接口偶尔会混入合集里的他人视频
        if str((item.get("upper") or {}).get("mid") or "") != mid:
            continue
        uploads.append(
            {
                "bvid": bvid,
                "title": str(item.get("title") or ""),
                "cover": str(item.get("cover") or ""),
                "pubtime": _as_int(item.get("pubtime")),
                "url": f"https://www.bilibili.com/video/{bvid}",
            }
        )
    # 接口按从新到旧返回，第 i 条的"上一条"就是更旧的第 i+1 条；
    # 列表里最后一条没有参照，取不到时距离上次更新这一行会被省略
    for idx, current in enumerate(uploads):
        older = uploads[idx + 1] if idx + 1 < len(uploads) else None
        current["prev_pubtime"] = older["pubtime"] if older else 0
    return uploads


def format_pubtime(pubtime: int) -> str:
    if not pubtime:
        return "时间未知"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(pubtime))
    except (OverflowError, OSError, ValueError):
        return "时间未知"


def format_since(prev_ts, cur_ts=None) -> str:
    """把"距上次"的间隔写成 xx天 / xx小时（不满一天按小时）。

    取不到上次时间、缺少本次时间参照、或算出负数（时钟回拨、接口给的 pubtime 比
    上一条还早）都返回空串，由调用方直接省略这一行，而不是显示 0天/未知。
    """
    prev = _as_int(prev_ts)
    cur = _as_int(cur_ts) if cur_ts is not None else int(time.time())
    if prev <= 0 or cur <= 0:
        return ""
    delta = cur - prev
    if delta < 0:
        return ""
    hours = delta // 3600
    if hours < 1:
        return "不到1小时"
    if hours < 24:
        return f"{hours}小时"
    return f"{hours // 24}天"


def format_duration(seconds) -> str:
    """把直播时长写成 x天x小时 / x小时y分钟 / y分钟。

    拿不到开播时刻（0）或算出负数（时钟回拨、手工把 live_since 改坏）都返回空串，
    由调用方省略整行；不满一分钟写"不到1分钟"，免得短播读成空白。
    """
    total = _as_int(seconds)
    if total <= 0:
        return ""
    minutes = total // 60
    if minutes < 1:
        return "不到1分钟"
    if minutes < 60:
        return f"{minutes}分钟"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小时{mins}分钟" if mins else f"{hours}小时"
    days, rem = divmod(hours, 24)
    return f"{days}天{rem}小时" if rem else f"{days}天"


def format_upload(up: dict, item: dict, since: str = "") -> str:
    """渲染单条新稿件通知。"""
    lines = [
        "📺 投稿通知",
        DIVIDER,
        f"👤 UP：{up['name'] or up['mid']}",
        f"🎬 标题：{item['title'] or '(无标题)'}",
        f"🔖 稿件：{item['bvid']}",
        f"🕒 发布：{format_pubtime(item['pubtime'])}",
    ]
    if since:
        lines.append(f"⏳ 距上次更新：{since}")
    if item["url"]:
        lines.append(f"🔗 {item['url']}")
    return "\n".join(lines)


def format_uploads_batch(up: dict, fresh: list, shown: list, since: str = "") -> str:
    """一次出现多条新稿件（UP 主批量补档）时合并成一条，避免刷屏。"""
    lines = [
        "📺 投稿通知",
        DIVIDER,
        f"👤 UP：{up['name'] or up['mid']}",
    ]
    if since:
        lines.append(f"⏳ 最新一条距上次更新：{since}")
    lines.append(f"🆕 新增 {len(fresh)} 条稿件，列出最近 {len(shown)} 条：")
    for item in shown:
        lines.append(f"· {item['title'] or '(无标题)'}（{item['bvid']}）")
        lines.append(f"  {item['url']}")
    if len(fresh) > len(shown):
        lines.append(f"…另有 {len(fresh) - len(shown)} 条未列出")
    return "\n".join(lines)


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
        platform = PREFIX_MAP[m.group(1).lower()]
        room_id = m.group(2).strip()
        # 前缀写法的房间号是用户原样给的，必须校验；链接解析出来的已经被
        # URL_PATTERNS 限过字符集了
        if not valid_room_id(platform, room_id):
            return None
        return platform, room_id, False
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


def valid_room_id(platform: str, room_id: str) -> bool:
    """房间号是否只含该平台允许的字符；不合法就不进监控表，也不拼进请求 URL。"""
    pattern = ROOM_ID_RE.get(platform)
    return bool(pattern) and bool(pattern.fullmatch(str(room_id)))


def parse_up_input(text) -> str | None:
    """从空间链接 / 平台前缀:数字 / 纯数字里取出 UP 主 mid，取不到返回 None。

    直播间链接必须排除：live.bilibili.com 后面的数字是房间号，和 mid 不是一回事，
    拿去当 mid 会监控到另一个人。同样要 str() 兜住 AstrBot 传来的 int。
    """
    text = str(text).strip()
    if BILI_LIVE_URL_RE.search(text):
        return None
    m = BILI_SPACE_URL_RE.search(text)
    if m:
        return m.group(1)
    parsed = parse_room_input(text)
    if parsed and parsed[0] == "bilibili":
        return str(parsed[1])
    if re.fullmatch(r"\d{2,}", text):
        return text
    return None


def parse_meta_text(text: str) -> dict:
    """从 metadata.yaml 文本里取出 name / version / repo 三个标量字段。"""
    fields = {}
    for key in ("name", "version", "repo"):
        m = re.search(rf"^{key}:\s*(.+?)\s*$", text, re.MULTILINE)
        fields[key] = m.group(1).strip("\"'") if m else ""
    return fields


def read_plugin_meta() -> tuple[str, str, str]:
    """从插件自身目录的 metadata.yaml 读取 (version, repo, name)。

    版本号只在 metadata 里维护一份：这里直接解析，避免 main.py 再抄一遍常量后
    和 metadata.yaml 漂移；顺带把 repo 取出来当作更新检查的地址来源、name 取出来
    当作热重载定位插件的名字来源。
    """
    try:
        text = (Path(__file__).parent / "metadata.yaml").read_text(encoding="utf-8")
    except Exception:
        return "", "", ""
    fields = parse_meta_text(text)
    return fields["version"], fields["repo"], fields["name"]


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


@filter.command_group("直播监控", alias={"开播监控"})
def monitor_group():
    """直播监控指令组"""


def pick_update_asset(item: dict) -> dict:
    """从 Release 里挑要下载的更新包。

    优先本插件自己上传的 live_monitor_v*.zip（只含 4 个插件文件），没有就退回
    GitHub 自动生成的源码包 zipball_url。带 size 且明显超限的资产直接跳过，
    省得把一个不相干的大文件拉下来。
    """
    assets = item.get("assets")
    candidates = (
        [a for a in assets if isinstance(a, dict)] if isinstance(assets, list) else []
    )
    for key in (r"^live_monitor_v?[\d.]+\.zip$", r"^live[\w.-]*\.zip$"):
        pattern = re.compile(key, re.IGNORECASE)
        for asset in candidates:
            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            if not pattern.match(name) or not url.startswith("https://"):
                continue
            if _as_int(asset.get("size")) > MAX_UPDATE_BYTES:
                continue
            return {"url": url, "name": name}
    zipball = str(item.get("zipball_url") or "")
    if zipball.startswith("https://"):
        return {"url": zipball, "name": "源码包"}
    return {}


def _safe_entry_path(name: str) -> str:
    """把 zip 里的条目名规范成正斜杠相对路径，越界的一律判非法。"""
    path = str(name).replace("\\", "/")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return ""
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return ""
    return "/".join(parts)


def find_update_root(entries: list):
    """定位插件文件所在目录：本插件的 zip 是平铺的，GitHub 源码包多一层同名目录。

    以 metadata.yaml 所在的那一层为根，只接受根目录或一层目录，避免把子目录里的
    同名文件当成插件本体。找不到返回 None，由调用方报错。
    """
    best = None
    prefix = ""
    for entry in entries:
        parts = entry.split("/")
        if parts[-1] != "metadata.yaml":
            continue
        depth = len(parts) - 1
        if depth > 1:
            continue
        if best is None or depth < best:
            best = depth
            prefix = "" if depth == 0 else parts[0]
    if best is None:
        return None
    return prefix


def inspect_update_zip(data: bytes, cur_name: str, cur_version: str) -> dict:
    """在内存里校验更新包，返回 {version, files}；不通过就抛 ValueError 说明原因。

    覆盖插件目录等于让下载来的代码在机器人进程里跑，所以先把住几道关：包结构
    合法、路径不越界、插件名没被换成别的项目、版本号确实更新、main.py 能编译且
    找得到 Star 插件主类、schema 是合法 JSON。只读白名单文件，包里的其他条目
    一个字节都不落盘。
    """
    if len(data) > MAX_UPDATE_BYTES:
        raise ValueError(f"更新包超过 {MAX_UPDATE_BYTES // 1024 // 1024}MB 上限")
    if data[:2] != b"PK":
        raise ValueError("更新包不是 zip 格式")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        raise ValueError(f"更新包打不开: {e}")
    with archive as zf:
        entries = {}
        for info in zf.infolist():
            path = _safe_entry_path(info.filename)
            if not path:
                raise ValueError(f"更新包里有非法路径: {info.filename}")
            if not path.startswith("__MACOSX/"):
                entries[path] = info
        root = find_update_root(list(entries))
        if root is None:
            if not any(p.split("/")[-1] == "metadata.yaml" for p in entries):
                raise ValueError("更新包缺少 metadata.yaml，不是插件包")
            raise ValueError("更新包里的 metadata.yaml 层级太深，无法确定插件目录")
        files = {}
        for fname in UPDATE_REQUIRED_FILES:
            path = f"{root}/{fname}" if root else fname
            info = entries.get(path)
            if info is None:
                raise ValueError(f"更新包缺少 {fname}")
            if info.file_size > MAX_UPDATE_BYTES:
                raise ValueError(f"更新包里的 {fname} 体积异常")
            files[fname] = zf.read(info)
        for fname in UPDATE_OPTIONAL_FILES:
            path = f"{root}/{fname}" if root else fname
            info = entries.get(path)
            if info is not None and info.file_size <= MAX_UPDATE_BYTES:
                files[fname] = zf.read(info)
    try:
        meta_text = files["metadata.yaml"].decode("utf-8")
    except Exception:
        raise ValueError("更新包里的 metadata.yaml 不是 UTF-8 文本")
    meta = parse_meta_text(meta_text)
    if not meta["name"]:
        raise ValueError("更新包的 metadata.yaml 缺少 name")
    if cur_name and meta["name"] != cur_name:
        raise ValueError(f"更新包的插件名是 {meta['name']}，不是本插件 {cur_name}")
    if not meta["version"]:
        raise ValueError("更新包的 metadata.yaml 缺少 version")
    if not version_is_newer(meta["version"], cur_version):
        raise ValueError(f"更新包版本 {meta['version']} 不比当前版本 {cur_version} 新")
    try:
        json.loads(files["_conf_schema.json"].decode("utf-8"))
    except Exception as e:
        raise ValueError(f"更新包里的 _conf_schema.json 不是合法 JSON: {e}")
    try:
        tree = ast.parse(files["main.py"].decode("utf-8"))
    except Exception as e:
        raise ValueError(f"更新包里的 main.py 语法不通过: {e}")
    if not _has_plugin_class(tree):
        raise ValueError(
            "更新包里的 main.py 找不到以 Plugin 结尾的主类，装上也不会被加载"
        )
    return {"version": meta["version"], "files": files}


def _has_plugin_class(tree: ast.AST) -> bool:
    """main.py 里必须有一个名字以 Plugin 结尾的类，这是 AstrBot 扫描插件的硬条件。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.endswith("Plugin"):
            return True
    return False


def prune_update_backups(backup_root: Path, keep: int = UPDATE_BACKUP_KEEP):
    """备份只留最近 keep 份，其余删掉。备份是本插件自己的目录副本，不需要长期留。"""
    try:
        dirs = sorted(
            [p for p in backup_root.iterdir() if p.is_dir()],
            key=lambda p: p.name,
            reverse=True,
        )
    except Exception:
        return
    for stale in dirs[keep:]:
        try:
            shutil.rmtree(stale)
        except Exception:
            pass


_poll_task: asyncio.Task | None = None
"""模块级后台任务引用。热重载时模块会被重新导入，旧引用随之失效，
因此必须在创建新任务前取消它，防止新旧两个轮询任务并发导致重复推送。"""

_reload_task: asyncio.Task | None = None
"""热更新排队中的重载任务引用，防止连续两次 更新 指令排出两个重载。"""


class LiveMonitorPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.conf = config or {}
        self.data_dir: Path = StarTools.get_data_dir()
        self.data_file: Path = self.data_dir / "rooms.json"
        self.plugin_dir = Path(__file__).parent
        self.rooms: dict = {}
        self.ups: dict = {}
        self._last_check: dict[str, float] = {}
        self.version, repo_url, self.meta_name = read_plugin_meta()
        m = GITHUB_REPO_RE.search(repo_url)
        self._repo_slug = f"{m.group(1)}/{m.group(2)}" if m else ""
        self._repo_page = (
            f"https://github.com/{self._repo_slug}" if self._repo_slug else ""
        )
        self._next_update_check = time.monotonic() + UPDATE_FIRST_DELAY
        self._update_hint_version = ""
        self._pending_update = self._read_pending_update()
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
                    key in raw for key in ("version", "rooms", "ups")
                ):
                    self.rooms = self._sanitize(raw.get("rooms"))
                    self.ups = self._sanitize_ups(raw.get("ups"))
                    if raw.get("submissions") or raw.get("reviewers"):
                        # 0.1.2 的「群友投稿 + 管理员审核」在 0.2.0 移除：
                        # 旧数据读不读没意义，直接丢掉，下次 _save 就写成 v3 结构
                        self.logger.info(
                            "已忽略旧数据里的投稿队列（submissions/reviewers），"
                            "该功能在 0.2.0 移除，改用 /直播监控 监控UP 监控UP主新稿件"
                        )
                else:
                    # 0.1.1 及以前：顶层对象就是房间表，直接按旧格式载入
                    self.rooms = self._sanitize(raw)
        except Exception as e:
            self.logger.warning(f"读取监控数据失败: {e}")
            self.rooms = {}
            self.ups = {}
            self._quarantine_data_file(e)

    def _quarantine_data_file(self, error):
        """把读不了的 rooms.json 改名留底。原地留着的话，下一次 _save 会直接覆盖，
        手工编辑写错一个逗号就等于把全部监控和订阅清空。"""
        try:
            if not self.data_file.exists():
                return
            bad = self.data_file.with_name(
                f"rooms.json.bad-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            os.replace(self.data_file, bad)
            self.logger.warning(
                f"损坏的监控数据已改名保留为 {bad.name}（原因: {error}），"
                "修好该文件后重载插件即可恢复"
            )
        except Exception as e:
            self.logger.warning(f"保留损坏的监控数据失败: {e}")

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
            if not valid_room_id(platform, room_id):
                self.logger.warning(f"房间 {key} 房间号含非法字符: {room_id}，已跳过")
                continue
            watchers = self._sanitize_sessions(room.get("watchers"))
            live = room.get("live")
            live = live if live in (True, False) else None
            # 没有订阅者的房间不会被轮询，留着上次在播时刻只会在重新订阅后
            # 显示一个误导性的"距离上次直播"
            last_live_at = _as_int(room.get("last_live_at")) if watchers else 0
            # 开播时刻只在确实处于在播时才有意义：状态是关播/首次检测却带着
            # live_since，下一轮开播会算出一个跨场的假时长
            live_since = (
                _as_int(room.get("live_since")) if (watchers and live is True) else 0
            )
            rooms[str(key)] = {
                "platform": platform,
                "room_id": room_id,
                "title": str(room.get("title") or ""),
                "anchor": str(room.get("anchor") or ""),
                "cover": str(room.get("cover") or ""),
                "url": str(room.get("url") or ""),
                "live": live,
                "fails": 0,
                "watchers": watchers,
                "last_live_at": last_live_at,
                "live_since": live_since,
            }
        return rooms

    def _sanitize_sessions(self, raw) -> dict:
        """校验 {会话UMO: 名称} 结构（房间与 UP 主的订阅者同构）。"""
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if str(k)}

    def _sanitize_ups(self, raw) -> dict:
        """校验 ups 段，理由同 _sanitize：手工编辑过的数据不能让轮询炸掉。"""
        ups = {}
        if not isinstance(raw, dict):
            return ups
        for mid, up in raw.items():
            mid = str(mid).strip()
            if not mid.isdigit() or not isinstance(up, dict):
                self.logger.warning(f"UP 主 {mid} 数据不对，已跳过")
                continue
            seen = [str(b).strip() for b in (up.get("seen") or []) if str(b).strip()]
            ups[mid] = {
                "mid": mid,
                "platform": "bilibili",
                "name": str(up.get("name") or ""),
                "face": str(up.get("face") or ""),
                "archive_count": _as_int(up.get("archive_count")),
                "watchers": self._sanitize_sessions(up.get("watchers")),
                # 列表按从新到旧保存，超出上限的旧稿件直接丢弃
                "seen": seen[:MAX_SEEN_UPLOADS],
                "fails": 0,
            }
        return ups

    def _save(self):
        payload = {"version": 4, "rooms": self.rooms, "ups": self.ups}
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再改名覆盖：直接 write_text 若在写一半时崩溃，
            # 留下的半个 JSON 会让下一次载入把数据当成空的
            tmp = self.data_file.with_name(self.data_file.name + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.data_file)
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
        """查看当前监控的直播间与UP主列表"""
        if not self.rooms and not self.ups:
            yield event.plain_result(
                "当前没有监控任何直播间或UP主。\n"
                "直播间: /直播监控 添加 <链接>\n"
                "UP主新稿件: /直播监控 监控UP <mid 或 空间链接>"
            )
            return
        is_admin = self._is_admin(event)
        lines = []
        if self.rooms:
            lines.append("当前监控的直播间:")
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
                if is_admin:
                    for umo in room["watchers"]:
                        lines.append(f"    ↳ {umo}")
        if self.ups:
            lines.append("当前监控的UP主（B站新稿件）:")
            for up in self.ups.values():
                sub = "√" if event.unified_msg_origin in up["watchers"] else "-"
                known = (
                    f"，已记录 {len(up['seen'])} 条稿件"
                    if up["seen"]
                    else "，待首次检测"
                )
                lines.append(
                    f"{sub} [B站UP] up:{up['mid']} {up['name'] or '(未解析)'}"
                    f"（{up['archive_count']} 稿件，订阅数 {len(up['watchers'])}{known}）"
                )
                if is_admin:
                    for umo in up["watchers"]:
                        lines.append(f"    ↳ {umo}")
        lines.append("（√ 表示本会话已订阅通知；房间用 平台:房间号，UP主用 mid）")
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
        room = self.rooms[key]
        room["watchers"].pop(event.unified_msg_origin, None)
        if not room["watchers"]:
            # 没人订阅就不会再被轮询，上次在播的时刻一并清零，
            # 免得以后重新订阅时显示一个假的"距离上次直播"，本轮开播时刻同理
            room["last_live_at"] = 0
            room["live_since"] = 0
        self._save()
        yield event.plain_result("已取消本会话对该房间的通知。")

    @monitor_group.command("监控UP")
    async def cmd_add_up(self, event, up_input: str | None = None):
        """添加B站UP主并监控其新稿件。用法: /直播监控 监控UP <mid 或 空间链接>"""
        if deny := self._admin_only(event):
            yield deny
            return
        if not up_input:
            yield event.plain_result(
                "用法: /直播监控 监控UP <UP主mid 或 空间链接>\n"
                "示例:\n"
                "/直播监控 监控UP https://space.bilibili.com/946974\n"
                "/直播监控 监控UP 946974\n"
                "mid 就是UP主空间地址 space.bilibili.com/ 后面的数字，只支持B站UP主。"
            )
            return
        mid = parse_up_input(up_input)
        if not mid:
            yield event.plain_result(
                "无法识别的UP主ID。请发送空间链接（space.bilibili.com/数字），"
                "或直接给 mid 数字。\n"
                "注意: live.bilibili.com 后面的数字是直播间房间号，不能当UP主ID用。"
            )
            return
        cookie = str(self.conf.get("bilibili_cookie") or "")
        try:
            async with aiohttp.ClientSession() as session:
                info = await fetch_up_info(session, mid, cookie)
        except Exception as e:
            yield event.plain_result(f"UP主校验失败: {e}")
            return
        is_new = mid not in self.ups
        up = self._register_up(mid)
        up["name"] = info["name"]
        up["face"] = info["face"]
        up["archive_count"] = info["archive_count"]
        up["watchers"][event.unified_msg_origin] = (
            event.get_sender_name() or event.get_sender_id()
        )
        self._save()
        if not is_new:
            yield event.plain_result(
                f"该UP主已在监控列表中（{info['name']}，mid {mid}），"
                f"当前订阅数 {len(up['watchers'])}。"
            )
            return
        yield event.plain_result(
            f"已添加UP主监控: {info['name']}（mid {mid}，"
            f"共 {info['archive_count']} 条稿件），本会话将收到新稿件通知。\n"
            f"稿件检查间隔 {self._upload_interval()} 秒，"
            "首轮只记录已有稿件作为基线，不会补推送历史视频。"
        )

    @monitor_group.command("移除UP")
    async def cmd_del_up(self, event, up_input: str | None = None):
        """移除UP主稿件监控。用法: /直播监控 移除UP <mid 或 UP主昵称>"""
        if deny := self._admin_only(event):
            yield deny
            return
        if not up_input:
            yield event.plain_result(
                "用法: /直播监控 移除UP <mid 或 UP主昵称>，可先用 /直播监控 列表 查看。"
            )
            return
        mid = self._resolve_up(up_input)
        if not mid:
            yield event.plain_result(
                f"未监控过 {up_input}，可先用 /直播监控 列表 查看已添加的UP主。"
            )
            return
        up = self.ups.pop(mid)
        self._last_check.pop(f"up:{mid}", None)
        self._save()
        yield event.plain_result(f"已移除UP主监控: {up['name'] or mid}（mid {mid}）")

    @monitor_group.command("订阅UP")
    async def cmd_sub_up(self, event, up_input: str | None = None):
        """订阅某个已监控UP主的新稿件通知。用法: /直播监控 订阅UP <mid 或 UP主昵称>"""
        if deny := self._admin_only(event):
            yield deny
            return
        mid = self._resolve_up(up_input)
        if not mid:
            yield event.plain_result(
                "未找到该UP主，可先用 /直播监控 列表 查看，"
                "或用 /直播监控 监控UP <mid> 添加。"
            )
            return
        self.ups[mid]["watchers"][event.unified_msg_origin] = (
            event.get_sender_name() or event.get_sender_id()
        )
        self._save()
        yield event.plain_result("已订阅该UP主的新稿件通知。")

    @monitor_group.command("退订UP")
    async def cmd_unsub_up(self, event, up_input: str | None = None):
        """取消本会话的UP主稿件通知（不移除监控）。用法: /直播监控 退订UP <mid>"""
        if deny := self._admin_only(event):
            yield deny
            return
        mid = self._resolve_up(up_input)
        if not mid:
            yield event.plain_result("未找到该UP主，可先用 /直播监控 列表 查看。")
            return
        if event.unified_msg_origin not in self.ups[mid]["watchers"]:
            yield event.plain_result("本会话未订阅该UP主的稿件通知。")
            return
        self.ups[mid]["watchers"].pop(event.unified_msg_origin)
        self._save()
        yield event.plain_result("已取消本会话对该UP主的稿件通知。")

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
                # 最近一次看到在播的时刻，用于开播通知里的"距离上次直播"；0 表示没有记录
                "last_live_at": 0,
                # 本轮开播的时刻，用于关播通知里的"本次时长"；0 表示没抓到开播时刻
                "live_since": 0,
            }
            self.rooms[key] = room
        return room

    def _register_up(self, mid: str) -> dict:
        """把UP主加入监控表（不落盘），已存在则返回原记录。"""
        up = self.ups.get(mid)
        if up is None:
            up = {
                "mid": mid,
                # 与房间记录同构，_notify 复用同一套白名单与封面下载逻辑
                "platform": "bilibili",
                "name": "",
                "face": "",
                "archive_count": 0,
                "watchers": {},
                "seen": [],
                "fails": 0,
            }
            self.ups[mid] = up
        return up

    def _resolve_up(self, up_input) -> str | None:
        """把 mid / 空间链接 / UP主昵称 解析成监控中的 mid，找不到返回 None。"""
        if up_input is None:
            return None
        text = str(up_input).strip()
        if not text:
            return None
        parsed = parse_up_input(text)
        if parsed and parsed in self.ups:
            return parsed
        if text in self.ups:
            return text
        for mid, up in self.ups.items():
            if text == up["name"]:
                return mid
        return None

    def _notify_targets(self) -> list:
        """所有订阅过通知的会话（房间 + UP主，去重），并按 notify_whitelist 过滤。"""
        watchers = set()
        for room in self.rooms.values():
            watchers.update(room["watchers"])
        for up in self.ups.values():
            watchers.update(up["watchers"])
        whitelist = self._notify_whitelist()
        return sorted(umo for umo in watchers if not whitelist or umo in whitelist)

    async def _deliver(self, text: str) -> int:
        """把系统消息（如新版本提醒）群发给所有订阅会话，返回成功送达的会话数。"""
        targets = self._notify_targets()
        sent = 0
        for idx, umo in enumerate(targets):
            if await self._send(umo, [Plain(text)]):
                sent += 1
            if idx + 1 < len(targets):
                await asyncio.sleep(1)
        return sent

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
            f"更新方式：管理员在群里发 /直播监控 更新 即可自动下载并热更新到 v"
            f"{release['version'].lstrip('v') or '?'}；"
            "也可用 AstrBot WebUI → 插件管理 → 本插件 → 更新"
            "（监控数据在 data/plugin_data 下，两种方式都不受影响）"
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
                f"发现新版本 {release['version']}（当前 v{self.version}），"
                f"已提醒 {sent} 个订阅会话"
            )
        else:
            self.logger.info(
                f"发现新版本 {release['version']}（当前 v{self.version}），"
                "但没有订阅会话可提醒，管理员请用 /直播监控 检查更新 查看"
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

    # ---------- 插件热更新（下载 -> 校验 -> 覆盖 -> 重载） ----------

    def _plugin_name(self) -> str:
        """AstrBot 注册用的插件名，热重载靠它定位自己。

        优先问 Context 要本实例对应的 StarMetadata：loader 注入的名字才是 reload
        认的那个（插件目录名和 metadata.name 不一致时以注册名为准）；拿不到就退回
        metadata.yaml 里的 name。
        """
        try:
            for star in self.context.get_all_stars():
                if getattr(star, "star_cls", None) is self:
                    return str(getattr(star, "name", "") or "") or self.meta_name
        except Exception:
            pass
        return self.meta_name

    def _pending_file(self) -> Path:
        return self.data_dir / PENDING_UPDATE_FILE

    def _read_pending_update(self):
        """读上次热更新留下的标记；标记本身坏了就当没有，不能连累监控启动。"""
        try:
            path = self._pending_file()
            if not path.exists():
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not raw.get("to"):
                return None
            return raw
        except Exception as e:
            self.logger.warning(f"读取更新标记失败: {e}")
            return None

    def _write_pending_update(self, to: str, umo: str, backup: str):
        marker = {
            "from": self.version,
            "to": to,
            "umo": umo,
            "backup": backup,
            "at": int(time.time()),
        }
        try:
            self._pending_file().write_text(
                json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            self.logger.warning(f"写入更新标记失败，重载后不会有确认消息: {e}")

    def _clear_pending_update(self):
        try:
            path = self._pending_file()
            if path.exists():
                os.remove(path)
        except Exception as e:
            self.logger.warning(f"清理更新标记失败: {e}")

    async def _confirm_update(self):
        """热重载后的第一次轮询里把结果回执给发起更新的会话，随后删掉标记。"""
        marker = self._pending_update
        self._pending_update = None
        self._clear_pending_update()
        to = str(marker.get("to") or "")
        backup = str(marker.get("backup") or "")
        umo = str(marker.get("umo") or "")
        done = parse_version(self.version) == parse_version(to)
        lines = []
        if done:
            lines += [
                f"✅ 已更新到 v{to.lstrip('vV')} 并完成热重载",
                DIVIDER,
                f"📦 原版本：v{marker.get('from') or '未知'}",
            ]
            if backup:
                lines.append(f"🗂 旧版备份：{backup}")
            lines.append("监控与订阅数据未受影响。")
            self.logger.info(f"热更新完成：v{marker.get('from')} -> v{to}")
        else:
            lines += [
                "⚠️ 插件文件已覆盖，但没能载入新版本",
                DIVIDER,
                f"🎯 目标版本：v{to.lstrip('vV')}",
                f"📦 当前载入：v{self.version or '未知'}",
            ]
            if backup:
                lines.append(f"🗂 旧版备份：{backup}")
            lines += [
                "请到 AstrBot WebUI → 插件管理 重载本插件看报错；",
                "仍然失败就用备份目录覆盖插件目录回滚。",
            ]
            self.logger.error(f"热更新未生效：目标 v{to}，实际 v{self.version}")
        if not umo:
            return
        whitelist = self._notify_whitelist()
        if whitelist and umo not in whitelist:
            self.logger.info(f"以下会话不在 notify_whitelist 内，跳过推送: [{umo}]")
            return
        await self._send(umo, [Plain("\n".join(lines))])

    def _download_url(self, url: str) -> str:
        """拼上可选的加速前缀。国内直连 GitHub 下载经常断，给个镜像口子，
        但只许 https，免得把更新包重定向到明文源。"""
        proxy = str(self.conf.get("update_asset_proxy") or "").strip()
        if not proxy:
            return url
        if not proxy.startswith("https://"):
            raise ValueError("update_asset_proxy 必须以 https:// 开头")
        return f"{proxy.rstrip('/')}/{url}"

    async def _download_update_archive(self, session, url: str) -> bytes:
        target = self._download_url(url)
        async with session.get(
            target, headers=GITHUB_HEADERS, timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"下载更新包 HTTP {resp.status}")
            if (resp.content_length or 0) > MAX_UPDATE_BYTES:
                raise ValueError(f"更新包过大: {resp.content_length} 字节")
            data = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                data += chunk
                # 响应头不报长度的（分块传输）只能边读边卡，防止无限吃内存
                if len(data) > MAX_UPDATE_BYTES:
                    raise ValueError(
                        f"更新包超过 {MAX_UPDATE_BYTES // 1024 // 1024}MB 上限，已放弃"
                    )
        return bytes(data)

    def _install_update(self, files: dict) -> str:
        """先备份再逐个原子覆盖，返回备份目录路径（备份没做成返回空串）。

        只覆盖更新包里带回来的那几个文件，绝不删插件目录：AstrBot 自带的更新器
        是 remove_dir + 解压，一旦新版加载不起来手工回滚就很麻烦，这里刻意留
        「用备份目录盖回来」这条退路。
        """
        backup_dir = None
        root = self.data_dir / "backup"
        try:
            root.mkdir(parents=True, exist_ok=True)
            backup_dir = (
                root / f"v{self.version or 'unknown'}-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            backup_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.logger.warning(f"创建更新备份目录失败，本次更新不留旧版副本: {e}")
        if backup_dir is not None:
            for name in files:
                src = self.plugin_dir / name
                if not src.exists():
                    continue
                try:
                    shutil.copy2(src, backup_dir / name)
                except Exception as e:
                    self.logger.warning(f"备份 {name} 失败: {e}")
            prune_update_backups(root)
        for name, data in files.items():
            target = self.plugin_dir / name
            tmp = target.with_name(target.name + ".update-tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return str(backup_dir) if backup_dir else ""

    def _reload_blocker(self) -> str:
        """能不能自动热重载；返回不能的原因，空串表示可以。

        reload(name) 匹配不到名字时会退化成重载**所有**插件，所以必须先用公开接口
        确认这个名字确实注册过，绝不能拿个错名字去调。
        """
        name = self._plugin_name()
        if not name:
            return "拿不到 AstrBot 注册的插件名"
        manager = getattr(self.context, "_star_manager", None)
        if manager is None or not hasattr(manager, "reload"):
            return "当前 AstrBot 版本没有向插件暴露热重载接口"
        try:
            registered = self.context.get_registered_star(name)
        except Exception:
            registered = None
        if registered is None:
            return f"插件名 {name} 没有在 AstrBot 注册"
        return ""

    def _schedule_reload(self) -> str:
        """把热重载排进独立任务，返回不能自动重载的原因（空串表示已排队）。

        不能在指令协程里直接 await reload：那一步会终止当前实例，本条指令的回复
        就发不出去了。
        """
        blocker = self._reload_blocker()
        if blocker:
            return blocker
        global _reload_task
        if _reload_task is not None and not _reload_task.done():
            return ""  # 已经有一次重载在排队
        manager = self.context._star_manager
        name = self._plugin_name()
        _reload_task = asyncio.create_task(self._reload_later(manager, name))
        return ""

    async def _reload_later(self, manager, name: str):
        try:
            await asyncio.sleep(RELOAD_DELAY)
            success, error = await manager.reload(name)
            if success:
                self.logger.info(f"插件 {name} 已热重载，等待新版回报")
            else:
                self.logger.error(f"插件 {name} 热重载失败: {error}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error(f"热重载插件异常: {e!r}")

    def _pick_target(self, releases: list, want: str) -> dict | None:
        """want 为空取最新正式版；指定版本号时按 tag 精确匹配（可指名 prerelease）。"""
        if not want:
            return self._pick_release(releases)
        want_num = want.lstrip("vV")
        for item in releases:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            tag = str(item.get("tag_name") or "")
            if tag and tag.lstrip("vV") == want_num:
                return item
        return None

    async def _perform_update(self, want: str, umo: str) -> tuple[bool, str]:
        """下载、校验、覆盖、排队重载；返回 (是否成功, 回给管理员的文本)。"""
        backup = ""
        try:
            async with aiohttp.ClientSession() as session:
                releases = await self._fetch_releases(session)
                item = self._pick_target(releases, want)
                if not item:
                    where = f"{want} 这个版本" if want else "仓库上"
                    return False, f"{where}没有可用的 Release，无法更新。"
                release = self._release_info(item)
                to = release["version"]
                if parse_version(to) == parse_version(self.version):
                    return False, f"当前已经是 v{self.version}，无需更新。"
                if not version_is_newer(to, self.version):
                    return False, (
                        f"目标版本 {to} 比当前 v{self.version} 旧：热更新只往前装，"
                        "回滚请用备份目录覆盖插件目录，或在 WebUI 里手工装旧版。"
                    )
                asset = pick_update_asset(item)
                if not asset:
                    return False, (
                        f"{to} 这个 Release 上没有可下载的更新包"
                        "（既没有 live_monitor_v*.zip 也没有源码包地址）。"
                    )
                data = await self._download_update_archive(session, asset["url"])
            try:
                info = inspect_update_zip(data, self.meta_name, self.version)
            except ValueError as e:
                return False, f"更新包校验未通过，已放弃覆盖：{e}"
            try:
                backup = self._install_update(info["files"])
            except Exception as e:
                return False, (
                    f"覆盖插件文件时出错（可能只写了一部分）: {e}\n"
                    f"🗂 旧版备份：{backup or '未生成'}\n"
                    "请先在 WebUI 里用备份目录覆盖插件目录，再重载插件。"
                )
        except Exception as e:
            return False, f"更新失败: {e}"
        self._update_hint_version = to
        self._write_pending_update(info["version"], umo, backup)
        blocker = self._schedule_reload()
        lines = [
            f"✅ 已下载 v{info['version'].lstrip('vV')} 并覆盖 {len(info['files'])} 个文件",
            DIVIDER,
            f"📦 原版本：v{self.version}",
            f"📥 更新包：{asset['name']}",
        ]
        if backup:
            lines.append(f"🗂 旧版备份：{backup}")
        lines.append(DIVIDER)
        if blocker:
            lines += [
                f"⚠️ 自动热重载不可用（{blocker}），文件已经换成新版了，",
                "请立刻到 AstrBot WebUI → 插件管理 手动重载插件。",
            ]
        else:
            lines.append(
                f"♻️ {RELOAD_DELAY:.0f} 秒后自动热重载，重载成功后本会话会再收到一条确认；"
                "没收到就去 WebUI → 插件管理 看原因，必要时用备份回滚。"
            )
        return True, "\n".join(lines)

    @monitor_group.command("更新")
    async def cmd_update(self, event, target_version: str | None = None):
        """下载最新版并自动热更新插件。用法: /直播监控 更新 [版本号]"""
        if deny := self._admin_only(event):
            yield deny
            return
        if not self.conf.get("self_update", True):
            yield event.plain_result(
                "插件配置 self_update 已关闭，不允许自动下载覆盖插件文件。\n"
                "要更新请在 WebUI 插件设置里打开 self_update，"
                "或用 /直播监控 检查更新 查版本号后手工更新。"
            )
            return
        if not self._repo_slug or not self.version:
            yield event.plain_result(
                "metadata.yaml 里没有可用的 version / repo，无法更新。"
            )
            return
        want = str(target_version or "").strip()
        yield event.plain_result(
            f"⏬ 正在从 {self._repo_page} 下载{' ' + want if want else '最新版'}更新包…"
        )
        ok, text = await self._perform_update(want, event.unified_msg_origin)
        if ok:
            self.logger.info(f"管理员 {event.get_sender_id()} 触发热更新成功：{text}")
        yield event.plain_result(text)

    @monitor_group.command("帮助")
    async def cmd_help(self, event):
        """查看直播监控帮助"""
        yield event.plain_result(
            "=== 直播开播 / UP主稿件监控 ===\n"
            "指令前缀: /直播监控 或 /开播监控\n"
            "—— 直播间 ——\n"
            "添加 <直播间链接或 平台:房间号>  添加监控并订阅通知（管理员）\n"
            "删除 <平台:房间号>  删除监控\n"
            "订阅 <平台:房间号>  订阅本会话通知\n"
            "取消 <平台:房间号>  取消本会话通知\n"
            "—— B站UP主新稿件 ——\n"
            "监控UP <mid 或 space.bilibili.com/数字>  添加UP主并订阅新稿件通知（管理员）\n"
            "移除UP <mid 或 UP主昵称>  移除该UP主的稿件监控（管理员）\n"
            "订阅UP <mid 或 UP主昵称>  订阅本会话稿件通知\n"
            "退订UP <mid 或 UP主昵称>  取消本会话稿件通知\n"
            "—— 通用 ——\n"
            "列表  查看监控的直播间与UP主\n"
            "检查更新  立即查一次 GitHub 上有没有新版插件（管理员）\n"
            "更新 [版本号]  从 GitHub 下载更新包、覆盖插件文件并自动热重载（管理员）；"
            "带版本号可指名要装的版本但必须比当前新，回滚请用备份目录；"
            "插件配置 self_update 可关闭该能力\n"
            "支持平台: B站直播(bili)、斗鱼(douyu)、虎牙(huya/hy)、抖音(douyin)；"
            "UP主稿件仅支持B站。\n"
            "开播/关播会推送主播名、房间号、标题、链接与封面；"
            "UP主发新稿件会推送标题、BV号、发布时间与封面。\n"
            "插件配置 show_elapsed（默认开启）后，开播通知会带上距离上次直播的时间，"
            "稿件通知会带上距上次更新的时间；不满一天按小时显示，"
            "取不到上一次时间时这一行自动省略。\n"
            "关播通知会带上本次直播时长（x天x小时 / x小时y分钟 / y分钟），"
            "没抓到这轮开播时刻时省略该行。\n"
            "除 列表/帮助 外的管理指令仅限管理员（插件配置 admin_ids，留空则不限）。\n"
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
                try:
                    await self._poll_once(session)
                except Exception as e:
                    # 单轮的意外异常必须咽下去：后台任务一退出，所有房间和UP主的
                    # 监控就静默停了，而且不会再有任何通知
                    self.logger.error(f"本轮监控检查异常，5 秒后继续: {e!r}")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        finally:
            await session.close()

    async def _poll_once(self, session: aiohttp.ClientSession):
        now = time.monotonic()
        if self._pending_update is not None:
            # 热重载后的第一次轮询：先回报更新结果。放在这里而不是 __init__ 里，
            # 是因为插件刚加载时主动消息通道可能还没就绪
            await self._confirm_update()
        if self.conf.get("update_check", True) and (now >= self._next_update_check):
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
        up_interval = self._upload_interval()
        due_ups = []
        for mid, up in self.ups.items():
            if not up["watchers"]:
                continue
            if now - self._last_check.get(f"up:{mid}", 0) >= up_interval:
                due_ups.append(mid)
        for mid in due_ups:
            up = self.ups.get(mid)
            if not up:
                continue
            try:
                await self._check_up(session, up)
                up["fails"] = 0
            except Exception as e:
                up["fails"] = up.get("fails", 0) + 1
                if up["fails"] in (1, 10) or up["fails"] % 30 == 0:
                    self.logger.warning(f"检查UP主 {mid} 失败(第{up['fails']}次): {e}")
            finally:
                self._last_check[f"up:{mid}"] = time.monotonic()
            await asyncio.sleep(1)

    def _show_elapsed(self) -> bool:
        """通知里是否显示"距离上次…"字段：开播通知的距离上次直播、稿件通知的距离上次更新。"""
        return bool(self.conf.get("show_elapsed", True))

    def _upload_interval(self) -> int:
        return max(
            MIN_UPLOAD_INTERVAL,
            self._conf_int("upload_check_interval", DEFAULT_UPLOAD_INTERVAL),
        )

    async def _check_up(self, session: aiohttp.ClientSession, up: dict):
        """检查UP主有没有新稿件。seen 为空视为基线：只记录、不推送。"""
        cookie = str(self.conf.get("bilibili_cookie") or "")
        uploads = await fetch_up_uploads(session, up["mid"], cookie)
        latest = [item["bvid"] for item in uploads]
        if not up["seen"]:
            up["seen"] = latest[:MAX_SEEN_UPLOADS]
            self._save()
            return
        known = set(up["seen"])
        # 接口按从新到旧返回，倒过来通知，顺序与UP主投稿时间一致
        fresh = [item for item in uploads if item["bvid"] not in known][::-1]
        merged = latest + [bvid for bvid in up["seen"] if bvid not in set(latest)]
        up["seen"] = merged[:MAX_SEEN_UPLOADS]
        if not fresh:
            self._save()
            return
        if self.conf.get("notify_upload", True):
            cap = max(
                1,
                self._conf_int("upload_max_notify", DEFAULT_UPLOAD_MAX_NOTIFY),
            )
            shown = fresh[-cap:]
            newest = fresh[-1]
            since = (
                format_since(newest.get("prev_pubtime"), newest.get("pubtime"))
                if self._show_elapsed()
                else ""
            )
            if len(shown) == 1:
                text = format_upload(up, shown[0], since)
                if len(fresh) > len(shown):
                    text += f"\n另有 {len(fresh) - len(shown)} 条新稿件未列出"
                await self._notify(session, up, text, shown[0]["cover"])
            else:
                await self._notify(
                    session, up, format_uploads_batch(up, fresh, shown, since), ""
                )
            self.logger.info(
                f"UP主 {up['mid']}（{up['name']}）新增 {len(fresh)} 条稿件，已推送"
            )
        self._save()

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
        now_ts = int(time.time())
        # 先取本次之前的最后一次在播时刻，再刷新它，否则算出来的间隔永远是本轮询周期
        last_live_at = _as_int(room.get("last_live_at"))
        live_since = _as_int(room.get("live_since"))
        room["title"] = result["title"] or room.get("title", "")
        room["anchor"] = result.get("anchor") or room.get("anchor", "")
        room["cover"] = result.get("cover") or room.get("cover", "")
        room["url"] = result["url"]
        room["live"] = now
        if now is True:
            room["last_live_at"] = now_ts
            if prev is False:
                # 只认真正抓到的开播瞬间；首次检测（prev 是 None）发现房间本来就在播时
                # 不补记起点，宁可关播时不显示时长，也不报一个从"我发现它"算起的假时长
                room["live_since"] = live_since = now_ts
            else:
                room["live_since"] = live_since
        else:
            room["live_since"] = 0

        if prev is None:
            # 首次检测只记录基线，不推送
            self._save()
            return
        anchor = room["anchor"] or room["room_id"]
        pname = PLATFORM_NAMES[platform]
        if prev is False and now is True and self.conf.get("notify_start", True):
            since = format_since(last_live_at, now_ts) if self._show_elapsed() else ""
            lines = [
                "🔴 开播通知",
                DIVIDER,
                f"👤 主播：{anchor}",
                f"📡 平台：{pname}",
                f"🚪 房间：{room['room_id']}",
                f"📺 标题：{result['title'] or '(无标题)'}",
            ]
            if since:
                lines.append(f"⏳ 距离上次直播：{since}")
            lines += [DIVIDER, f"🔗 {result['url']}"]
            await self._notify(session, room, "\n".join(lines), room.get("cover", ""))
        elif prev is True and now is False and self.conf.get("notify_end", True):
            # 用"最后一次看到在播的时刻"减"本轮开播的时刻"：真实的关播时刻落在两者之间，
            # 读数是最接近的下界，误差最多一个关播检测间隔。没抓到开播时刻就不显示，
            # 否则拿时间戳去减 0 会算出几万天
            duration = (
                format_duration(last_live_at - live_since)
                if live_since and last_live_at > live_since
                else ""
            )
            lines = [
                "⚫ 关播通知",
                DIVIDER,
                f"👤 主播：{anchor}",
                f"📡 平台：{pname}",
                f"🚪 房间：{room['room_id']}",
            ]
            if duration:
                lines.append(f"⏱ 本次时长：约{duration}")
            lines += [DIVIDER, "直播已结束，下次开播再会～"]
            await self._notify(session, room, "\n".join(lines), "")
        self._save()

    def _notify_whitelist(self) -> set:
        raw = self.conf.get("notify_whitelist") or ""
        if isinstance(raw, list):
            parts = [str(x) for x in raw]
        else:
            parts = re.split(r"[,，;；\s]+", str(raw))
        return {p.strip() for p in parts if p.strip()}

    def _cookie_for(self, url: str) -> str:
        """只有B站自己的域名才带登录 Cookie。

        封面地址来自接口，也可能被手工编辑过，不加限制就等于把登录态发给任意站点。
        """
        cookie = str(self.conf.get("bilibili_cookie") or "")
        if not cookie:
            return ""
        host = (urlparse(url).hostname or "").lower()
        for domain in BILI_COOKIE_HOSTS:
            if host == domain or host.endswith("." + domain):
                return cookie
        return ""

    async def _fetch_cover_base64(
        self, session: aiohttp.ClientSession, room: dict, cover: str
    ) -> str:
        if not cover.startswith(("http://", "https://")):
            raise RoomCheckError("封面地址不是 http(s) 链接")
        headers = (
            _bili_headers(self._cookie_for(cover))
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
