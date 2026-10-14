"""投稿投送流程的离线冒烟测试。

运行：python tests/test_submission.py

不发网络请求、不依赖 AstrBot：先往 sys.modules 注入 astrbot 桩模块，
再导入插件 main.py，用假的 event / context 直接驱动指令处理函数。
"""

import asyncio
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


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


def _install_stubs():
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


_install_stubs()

if "aiohttp" in sys.modules:
    aiohttp = sys.modules["aiohttp"]
else:
    aiohttp = types.ModuleType("aiohttp")

    class ClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class ClientTimeout:
        def __init__(self, *args, **kwargs):
            pass

    aiohttp.ClientSession = ClientSession
    aiohttp.ClientTimeout = ClientTimeout
    sys.modules["aiohttp"] = aiohttp

import main  # noqa: E402  必须在桩模块之后导入


async def fake_bilibili(session, room_id, cookie=""):
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


async def _noop_poll(self):
    await asyncio.sleep(3600)


main.LiveMonitorPlugin._poll_loop = _noop_poll


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


async def run(handler, *args):
    return [res.text async for res in handler(*args)]


def build(tmp, conf=None, fail=()):
    StarTools.data_dir = Path(tmp)
    return main.LiveMonitorPlugin(Context(fail=fail), conf or {})


def write_data(tmp, payload):
    Path(tmp, "rooms.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def read_data(tmp):
    return json.loads(Path(tmp, "rooms.json").read_text(encoding="utf-8"))


ADMIN_UMO = "aiocqhttp:GroupMessage:1001"
USER_UMO = "aiocqhttp:GroupMessage:2002"
BILI_6 = "https://live.bilibili.com/6"


def admin():
    return FakeEvent(ADMIN_UMO)


def user():
    return FakeEvent(USER_UMO)


async def test_legacy_data_upgraded(tmp):
    """0.1.1 的扁平 rooms.json 要能读出来，并自动升级成 v2 结构。"""
    write_data(
        tmp,
        {
            "bilibili:6": {
                "platform": "bilibili",
                "room_id": "6",
                "title": "老标题",
                "anchor": "老主播",
                "cover": "",
                "url": BILI_6,
                "live": False,
                "fails": 3,
                "watchers": {USER_UMO: "老订阅者"},
            }
        },
    )
    plugin = build(tmp)
    assert plugin.rooms["bilibili:6"]["title"] == "老标题"
    assert plugin.rooms["bilibili:6"]["watchers"] == {USER_UMO: "老订阅者"}
    assert plugin.submissions == {}
    assert plugin.reviewers == {}
    assert plugin._next_sub_id == 1
    await run(plugin.cmd_review_on, admin())
    payload = read_data(tmp)
    assert payload["version"] == 2
    assert set(payload) == {"version", "rooms", "submissions", "reviewers"}
    assert "bilibili:6" in payload["rooms"]


async def test_submit_is_delivered_to_reviewers(tmp):
    plugin = build(tmp)
    await run(plugin.cmd_review_on, admin())
    texts = await run(plugin.cmd_submit, user(), BILI_6)
    assert "已投稿（#1）" in texts[0], texts
    assert "投送给 1 个审核会话" in texts[0]
    delivered = plugin.context.texts(ADMIN_UMO)
    assert len(delivered) == 1
    text = delivered[0][1]
    assert "📮 新投稿 #1 待审核" in text
    assert "主播6" in text and "直播标题6" in text
    assert "/直播监控 投稿通过 1" in text
    assert f"昵称-{USER_UMO.split(':')[-1]}" in text
    assert plugin.submissions["1"]["live"] is True
    assert plugin.rooms == {}


async def test_submit_without_reviewers(tmp):
    """没有审核会话时不能假装已推送。"""
    plugin = build(tmp)
    texts = await run(plugin.cmd_submit, user(), BILI_6)
    assert "还没有审核会话" in texts[0], texts
    assert len(plugin.submissions) == 1


async def test_submit_delivery_failure_is_reported(tmp):
    """审核会话都推送失败时，要说失败而不是谎报已投送。"""
    plugin = build(tmp, fail={ADMIN_UMO})
    await run(plugin.cmd_review_on, admin())
    texts = await run(plugin.cmd_submit, user(), BILI_6)
    assert "推送给 1 个审核会话均失败" in texts[0], texts
    assert "投稿列表" in texts[0]
    assert len(plugin.submissions) == 1
    assert "推送通知" in plugin.logger.messages[-1][1]


async def test_submit_rejects_bad_input(tmp):
    plugin = build(tmp)
    assert (
        "无法识别" in (await run(plugin.cmd_submit, user(), "https://example.com"))[0]
    )
    assert "用法" in (await run(plugin.cmd_submit, user(), None))[0]
    assert "校验失败" in (await run(plugin.cmd_submit, user(), "bili:999"))[0]
    assert plugin.submissions == {}


async def test_submit_dedupe_and_limits(tmp):
    plugin = build(tmp, conf={"admin_ids": "1001"})
    await run(plugin.cmd_review_on, admin())
    await run(plugin.cmd_submit, user(), BILI_6)
    again = await run(plugin.cmd_submit, user(), BILI_6)
    assert "已有待审核投稿（#1）" in again[0], again
    assert "仅限管理员" in (await run(plugin.cmd_submissions, user()))[0]
    for rid in ("7", "8"):
        await run(plugin.cmd_submit, user(), f"bili:{rid}")
    limit = await run(plugin.cmd_submit, user(), "bili:9")
    assert "已达上限 3 条" in limit[0], limit
    other = await run(
        plugin.cmd_submit, FakeEvent("aiocqhttp:GroupMessage:3003"), "bili:9"
    )
    assert "已投稿（#4）" in other[0], other
    assert len(plugin.submissions) == 4


async def test_total_queue_is_capped(tmp):
    """待审核总数封顶，防止刷屏式投稿把队列撑爆。"""
    plugin = build(tmp)
    plugin.submissions = {
        str(i): {
            "platform": "bilibili",
            "room_id": str(100 + i),
            "title": "",
            "anchor": "",
            "url": "",
            "live": False,
            "submitter_umo": f"aiocqhttp:GroupMessage:{i}",
            "submitter_name": f"投稿人{i}",
            "created_at": 0,
        }
        for i in range(1, main.MAX_PENDING_TOTAL + 1)
    }
    plugin._next_sub_id = main.MAX_PENDING_TOTAL + 1
    texts = await run(plugin.cmd_submit, user(), BILI_6)
    assert "排满" in texts[0], texts
    assert len(plugin.submissions) == main.MAX_PENDING_TOTAL


async def test_approve_registers_room_and_notifies_submitter(tmp):
    plugin = build(tmp)
    await run(plugin.cmd_review_on, admin())
    await run(plugin.cmd_submit, user(), BILI_6)
    texts = await run(plugin.cmd_approve, admin(), "1")
    assert "已通过投稿 #1" in texts[0] and "订阅数 2" in texts[0], texts
    room = plugin.rooms["bilibili:6"]
    assert room["watchers"] == {
        USER_UMO: f"昵称-{USER_UMO.split(':')[-1]}",
        ADMIN_UMO: f"昵称-{ADMIN_UMO.split(':')[-1]}",
    }
    assert room["live"] is None  # 新房间首次检测只记录基线
    assert plugin.submissions == {}
    back = plugin.context.texts(USER_UMO)
    assert any("✅ 投稿 #1 已通过审核" in t for _, t in back), back


async def test_approve_same_session_and_unknown_id(tmp):
    plugin = build(tmp)
    await run(plugin.cmd_submit, admin(), "bili:7")
    texts = await run(plugin.cmd_approve, admin(), "#1")
    assert "已通过投稿 #1" in texts[0]
    assert plugin.rooms["bilibili:7"]["watchers"] == {
        ADMIN_UMO: f"昵称-{ADMIN_UMO.split(':')[-1]}"
    }
    assert plugin.context.texts(ADMIN_UMO) == []  # 同会话不重复推送
    bad = await run(plugin.cmd_approve, admin(), "9")
    assert "未找到该待审核投稿" in bad[0]


async def test_reject_notifies_submitter_with_reason(tmp):
    plugin = build(tmp)
    await run(plugin.cmd_review_on, admin())
    await run(plugin.cmd_submit, user(), BILI_6)
    texts = await run(plugin.cmd_reject, admin(), "1", "重复投稿")
    assert "已拒绝投稿 #1，结果已回投投稿会话" in texts[0], texts
    assert plugin.submissions == {}
    back = plugin.context.texts(USER_UMO)[-1][1]
    assert "❌ 投稿 #1 未通过审核" in back and "原因：重复投稿" in back, back


async def test_room_number_as_int_param(tmp):
    """AstrBot 会把纯数字参数转成 int，插件必须兜底。"""
    plugin = build(tmp)
    texts = await run(plugin.cmd_submit, user(), 123456)
    assert "已投稿（#1）" in texts[0], texts
    assert plugin.submissions["1"]["room_id"] == "123456"
    await run(plugin.cmd_approve, admin(), "1")
    assert plugin.rooms["bilibili:123456"]["room_id"] == "123456"
    assert plugin._resolve_key(123456) == "bilibili:123456"


async def test_reload_keeps_submissions(tmp):
    plugin = build(tmp)
    await run(plugin.cmd_review_on, admin())
    await run(plugin.cmd_submit, user(), BILI_6)
    await run(plugin.cmd_submit, user(), "bili:8")
    reloaded = build(tmp)
    assert reloaded.reviewers == {ADMIN_UMO: f"昵称-{ADMIN_UMO.split(':')[-1]}"}
    assert sorted(reloaded.submissions) == ["1", "2"]
    assert reloaded._next_sub_id == 3
    texts = await run(reloaded.cmd_submissions, admin())
    assert "待审核投稿 2 条" in texts[0]
    assert "主播6" in texts[0] and "主播8" in texts[0]
    assert "投稿通过 1" not in texts[0]  # 多条时用 <编号> 占位
    await run(reloaded.cmd_review_off, admin())
    assert build(tmp).reviewers == {}
    assert (
        "本会话不在投稿投送列表中" in (await run(reloaded.cmd_review_off, admin()))[0]
    )


async def test_corrupted_submission_data_is_dropped(tmp):
    write_data(
        tmp,
        {
            "version": 2,
            "rooms": [],
            "submissions": {
                "1": {"platform": "nope", "room_id": "6", "submitter_umo": USER_UMO},
                "x": {
                    "platform": "bilibili",
                    "room_id": "6",
                    "submitter_umo": USER_UMO,
                },
                "2": {"platform": "bilibili", "room_id": "", "submitter_umo": USER_UMO},
                "3": {"platform": "bilibili", "room_id": "6"},
                "4": {
                    "platform": "bilibili",
                    "room_id": 6,
                    "submitter_umo": USER_UMO,
                    "submitter_name": "小明",
                    "created_at": "abc",
                },
            },
            "reviewers": {"": "空", 5: 5, "k": None},
        },
    )
    plugin = build(tmp)
    assert plugin.rooms == {}
    assert list(plugin.submissions) == ["4"]
    assert plugin.submissions["4"]["room_id"] == "6"
    assert plugin.submissions["4"]["created_at"] == 0.0
    assert plugin._next_sub_id == 5
    assert plugin.reviewers == {"5": "5", "k": "None"}
    assert "时间未知" in (await run(plugin.cmd_submissions, admin()))[0]


async def test_poll_loop_still_works(tmp):
    """投稿不应干扰原有轮询/通知路径。"""
    plugin = build(tmp)
    plugin.rooms["bilibili:6"] = {
        "platform": "bilibili",
        "room_id": "6",
        "title": "",
        "anchor": "",
        "cover": "",
        "url": "",
        "live": False,
        "watchers": {USER_UMO: "订阅者"},
        "fails": 0,
    }
    import aiohttp

    async with aiohttp.ClientSession() as session:
        await plugin._check_room(session, plugin.rooms["bilibili:6"])
    assert plugin.rooms["bilibili:6"]["live"] is True
    assert "🔴 开播通知" in plugin.context.texts(USER_UMO)[-1][1]


async def main_test():
    import tempfile

    tests = [
        ("旧版数据升级", test_legacy_data_upgraded),
        ("投稿投送给审核会话", test_submit_is_delivered_to_reviewers),
        ("无审核会话时的提示", test_submit_without_reviewers),
        ("投送失败不谎报", test_submit_delivery_failure_is_reported),
        ("非法输入与校验失败", test_submit_rejects_bad_input),
        ("查重/限流/权限", test_submit_dedupe_and_limits),
        ("待审核总数封顶", test_total_queue_is_capped),
        ("通过后加入监控并回投", test_approve_registers_room_and_notifies_submitter),
        ("同会话审核与编号不存在", test_approve_same_session_and_unknown_id),
        ("拒绝带原因回投", test_reject_notifies_submitter_with_reason),
        ("纯数字房间号参数", test_room_number_as_int_param),
        ("重载后投稿与审核会话保留", test_reload_keeps_submissions),
        ("脏投稿数据被清理", test_corrupted_submission_data_is_dropped),
        ("原轮询通知不受影响", test_poll_loop_still_works),
    ]
    failed = 0
    for name, func in tests:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                await func(tmp)
            except Exception as e:
                failed += 1
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
            else:
                print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_test()))
