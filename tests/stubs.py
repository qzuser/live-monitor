"""离线测试用的 astrbot / aiohttp 桩模块与插件构造助手。

两个测试脚本（test_submission.py、test_update_check.py）共用：注入桩模块 →
导入插件 main.py → 用假的 event / context 驱动指令处理函数，全程不联网、
不需要安装 AstrBot。
"""

import asyncio
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


class Plain:
    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return f"Plain({self.text!r})"


class Image:
    def __init__(self, data=""):
        self.base64 = data

    @classmethod
    def fromBase64(cls, data):
        return cls(data)


class MessageChain:
    def __init__(self, comps=None):
        self.chain = list(comps or [])


class _Filter:
    @staticmethod
    def command_group(name, alias=None):
        def deco(func):
            def command(sub_name):
                def inner(handler):
                    return handler

                return inner

            func.command = command
            return func

        return deco


class Logger:
    def __init__(self):
        self.messages = []

    def _log(self, level, msg):
        self.messages.append((level, msg))

    def info(self, msg):
        self._log("info", msg)

    def warning(self, msg):
        self._log("warning", msg)

    def error(self, msg):
        self._log("error", msg)


class Context:
    """假的 AstrBot Context：记录所有主动消息，fail 里的 UMO 一律抛错。"""

    def __init__(self, fail=()):
        self.sent = []
        self.fail = set(fail)

    async def send_message(self, umo, chain):
        if umo in self.fail:
            raise RuntimeError("模拟主动消息失败")
        self.sent.append((umo, chain))

    def texts(self, umo=None):
        return [
            (target, comp.text)
            for target, chain in self.sent
            for comp in chain.chain
            if isinstance(comp, Plain) and (umo is None or target == umo)
        ]

    def clear(self):
        self.sent.clear()


class Star:
    def __init__(self, context):
        self.context = context
        self.logger = Logger()


class StarTools:
    data_dir = Path(".")

    @classmethod
    def get_data_dir(cls):
        cls.data_dir.mkdir(parents=True, exist_ok=True)
        return cls.data_dir


def _install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.MessageChain = MessageChain
    event_mod.filter = _Filter
    comps = types.ModuleType("astrbot.api.message_components")
    comps.Image = Image
    comps.Plain = Plain
    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools
    for name, mod in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.message_components": comps,
        "astrbot.api.star": star_mod,
    }.items():
        sys.modules[name] = mod
    for parent, child in (
        (astrbot, api),
        (api, event_mod),
        (api, comps),
        (api, star_mod),
    ):
        setattr(parent, child.__name__.split(".")[-1], child)


def _install_aiohttp_stub():
    if "aiohttp" in sys.modules:
        return sys.modules["aiohttp"]
    aiohttp = types.ModuleType("aiohttp")

    class ClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def close(self):
            pass

        def get(self, *args, **kwargs):
            raise AssertionError("测试不应发起真实网络请求")

    class ClientTimeout:
        def __init__(self, *args, **kwargs):
            pass

    aiohttp.ClientSession = ClientSession
    aiohttp.ClientTimeout = ClientTimeout
    sys.modules["aiohttp"] = aiohttp
    return aiohttp


_install_astrbot_stubs()
aiohttp = _install_aiohttp_stub()

import main  # noqa: E402  必须在桩模块之后导入

ADMIN_UMO = "aiocqhttp:GroupMessage:1001"
USER_UMO = "aiocqhttp:GroupMessage:2002"
BILI_6 = "https://live.bilibili.com/6"

REAL_POLL = main.LiveMonitorPlugin._poll_loop


async def fake_bilibili(session, room_id, cookie=""):
    """替掉 B站接口：房间 999 视为不存在，房间 6 视为在播。"""
    room_id = str(room_id)
    if room_id == "999":
        raise main.RoomCheckError("bilibili 接口返回 code=200，房间号可能不存在")
    return {
        "live": room_id == "6",
        "title": f"直播标题{room_id}",
        "anchor": f"主播{room_id}",
        "anchor_uid": "",
        "cover": "",
        "url": f"https://live.bilibili.com/{room_id}",
    }


main.CHECKERS["bilibili"] = fake_bilibili


async def _idle_poll(self):
    await asyncio.sleep(3600)


def build(tmp, conf=None, fail=(), poll=False):
    """构造插件实例。默认把后台轮询换成空转，只有 poll=True 时才跑真实轮询循环。"""
    StarTools.data_dir = Path(tmp)
    main.LiveMonitorPlugin._poll_loop = REAL_POLL if poll else _idle_poll
    try:
        plugin = main.LiveMonitorPlugin(Context(fail=fail), conf or {})
    finally:
        main.LiveMonitorPlugin._poll_loop = _idle_poll
    return plugin


class FakeEvent:
    def __init__(self, umo, sender_id=None, sender_name=None):
        self.unified_msg_origin = umo
        self._sender_id = sender_id or umo.split(":")[-1]
        self._sender_name = sender_name or f"昵称-{umo.split(':')[-1]}"

    def plain_result(self, text):
        return types.SimpleNamespace(text=text)

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name


def admin():
    return FakeEvent(ADMIN_UMO)


def user():
    return FakeEvent(USER_UMO)


async def run(handler, *args):
    """驱动一个 async generator 指令处理函数，返回所有回复文本。"""
    return [res.text async for res in handler(*args)]


def write_data(tmp, payload):
    Path(tmp, "rooms.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def read_data(tmp):
    return json.loads(Path(tmp, "rooms.json").read_text(encoding="utf-8"))


async def call_limited(coro_fn, *args, timeout=2):
    """跑一个协程并在超时后取消，用于测试真实的 _poll_loop 迭代。"""
    task = asyncio.ensure_future(coro_fn(*args))
    await asyncio.sleep(0.2)
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return
    await task
