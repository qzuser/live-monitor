"""直播房间与B站UP主稿件监控的离线冒烟测试。

运行：python tests/test_monitor.py

0.2.0 起「群友投稿 + 管理员审核」被移除，取而代之的是按 mid 添加的B站UP主新稿件
监控。这里覆盖：数据格式迁移、UP 主指令、稿件基线与推送、批量合并、白名单、轮询。
桩模块见 stubs.py（B站接口全部替掉，不联网）。
"""

import asyncio
import base64
import tempfile
import time
from pathlib import Path

# 必须先导入 stubs（它会替换 sys.modules 里的 aiohttp），再导入 aiohttp
from stubs import (  # noqa: E402
    ADMIN_UMO,
    BILI_6,
    REAL_FETCH_UP_INFO,
    REAL_FETCH_UP_UPLOADS,
    UPLOADS,
    USER_UMO,
    admin,
    build,
    fake_up_info,
    fake_up_uploads,
    main,
    read_data,
    run,
    upload_item,
    user,
    write_data,
)
import aiohttp  # noqa: E402

MID = "946974"


def room_record(**overrides):
    room = {
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
    room.update(overrides)
    return room


def add_up(plugin, mid=MID, umo=ADMIN_UMO):
    """绕开指令直接登记一个已订阅的 UP 主，让用例专注在检查/推送逻辑上。"""
    up = plugin._register_up(mid)
    up["name"] = f"UP主{mid}"
    up["archive_count"] = 12
    up["watchers"][umo] = "订阅者"
    return up


async def check_up(plugin, up):
    async with aiohttp.ClientSession() as session:
        await plugin._check_up(session, up)


async def wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


async def stop_poll(task):
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------- 数据格式迁移 ----------


async def test_legacy_flat_data_upgraded(tmp):
    """0.1.1 的扁平 rooms.json 要能读出来，并写成 v3 结构。"""
    write_data(tmp, {"bilibili:6": room_record(title="老标题")})
    plugin = build(tmp)
    assert plugin.rooms["bilibili:6"]["title"] == "老标题"
    assert plugin.ups == {}
    await run(plugin.cmd_del, admin(), "bilibili:6")
    payload = read_data(tmp)
    assert payload["version"] == 3
    assert set(payload) == {"version", "rooms", "ups"}


async def test_v2_submission_data_is_dropped(tmp):
    """0.1.2 的投稿队列在 0.2.0 移除：旧数据要读得开，投稿段丢掉并留日志。"""
    write_data(
        tmp,
        {
            "version": 2,
            "rooms": {"bilibili:6": room_record()},
            "submissions": {
                "1": {
                    "platform": "bilibili",
                    "room_id": "7",
                    "submitter_umo": USER_UMO,
                }
            },
            "reviewers": {ADMIN_UMO: "管理员"},
        },
    )
    plugin = build(tmp)
    assert plugin.rooms["bilibili:6"]["watchers"] == {USER_UMO: "订阅者"}
    assert plugin.ups == {}
    assert not hasattr(plugin, "submissions")
    assert not hasattr(plugin, "reviewers")
    logs = "".join(m for _, m in plugin.logger.messages)
    assert "投稿队列" in logs, plugin.logger.messages
    assert "监控UP" in logs, plugin.logger.messages
    await run(plugin.cmd_add_up, admin(), MID)  # 触发一次落盘
    assert set(read_data(tmp)) == {"version", "rooms", "ups"}


async def test_corrupted_up_data_is_dropped(tmp):
    """手工编辑过的 ups 不能让轮询炸掉：脏条目跳过、字段类型修正。"""
    write_data(
        tmp,
        {
            "version": 3,
            "rooms": {},
            "ups": {
                "abc": {"mid": "abc", "watchers": {}},
                "5": "不是对象",
                "6": {
                    "mid": 6,
                    "name": None,
                    "archive_count": "很多",
                    "watchers": {"": "空的", 7: 7},
                    "seen": ["BV1", "", 2, "BV2"],
                },
            },
        },
    )
    plugin = build(tmp)
    assert list(plugin.ups) == ["6"]
    up = plugin.ups["6"]
    assert up["name"] == "" and up["archive_count"] == 0
    assert up["watchers"] == {"7": "7"}
    assert up["seen"] == ["BV1", "2", "BV2"]
    assert up["platform"] == "bilibili"  # _notify 靠这个字段挑封面请求头


# ---------- UP 主指令 ----------


async def test_add_up_rejects_bad_input(tmp):
    plugin = build(tmp, conf={"admin_ids": "1001"})
    assert "仅限管理员" in (await run(plugin.cmd_add_up, user(), MID))[0]
    assert "用法" in (await run(plugin.cmd_add_up, admin(), None))[0]
    assert (
        "无法识别" in (await run(plugin.cmd_add_up, admin(), "https://example.com"))[0]
    )
    # 直播链接里的数字是房间号，不能当 mid 用
    assert "无法识别" in (await run(plugin.cmd_add_up, admin(), BILI_6))[0]
    assert "校验失败" in (await run(plugin.cmd_add_up, admin(), "999"))[0]
    assert plugin.ups == {}


async def test_add_up_by_space_link(tmp):
    plugin = build(tmp)
    texts = await run(
        plugin.cmd_add_up, admin(), f"https://space.bilibili.com/{MID}?spm_id_from=333"
    )
    assert f"已添加UP主监控: UP主{MID}" in texts[0], texts
    assert "300 秒" in texts[0]  # 默认稿件检查间隔
    up = plugin.ups[MID]
    assert up["watchers"] == {ADMIN_UMO: f"昵称-{ADMIN_UMO.split(':')[-1]}"}
    assert up["archive_count"] == 12
    assert read_data(tmp)["ups"][MID]["name"] == f"UP主{MID}"

    again = await run(plugin.cmd_add_up, admin(), int(MID))  # 纯数字参数是 int
    assert "已在监控列表中" in again[0], again
    assert "首轮只记录" not in again[0]


async def test_up_commands_with_int_param(tmp):
    plugin = build(tmp)
    await run(plugin.cmd_add_up, admin(), int(MID))
    assert "已订阅该UP主的新稿件通知" in (await run(plugin.cmd_sub_up, user(), MID))[0]
    assert "未找到该UP主" in (await run(plugin.cmd_unsub_up, admin(), 9))[0]
    assert (
        "已取消本会话对该UP主的稿件通知"
        in (await run(plugin.cmd_unsub_up, admin(), int(MID)))[0]
    )
    assert set(plugin.ups[MID]["watchers"]) == {USER_UMO}
    assert "本会话未订阅" in (await run(plugin.cmd_unsub_up, admin(), MID))[0]
    # 不含空格的昵称也能定位
    assert "已订阅" in (await run(plugin.cmd_sub_up, admin(), f"UP主{MID}"))[0]
    assert "用法" in (await run(plugin.cmd_del_up, admin(), None))[0]
    assert "未监控过" in (await run(plugin.cmd_del_up, admin(), "不存在的UP"))[0]
    plugin._last_check[f"up:{MID}"] = time.monotonic()
    assert "已移除UP主监控" in (await run(plugin.cmd_del_up, admin(), int(MID)))[0]
    assert plugin.ups == {}
    assert f"up:{MID}" not in plugin._last_check


# ---------- 稿件基线与推送 ----------


async def test_up_baseline_then_new_upload_notifies(tmp):
    plugin = build(tmp)
    up = add_up(plugin)
    UPLOADS[MID] = ["BV3", "BV2", "BV1"]
    await check_up(plugin, up)
    assert plugin.context.sent == []  # 首轮只记录基线，不补推送历史视频
    assert up["seen"] == ["BV3", "BV2", "BV1"]

    await check_up(plugin, up)
    assert plugin.context.sent == []  # 没有新稿件不推送

    UPLOADS[MID] = ["BV4", "BV3", "BV2", "BV1"]
    await check_up(plugin, up)
    delivered = plugin.context.texts(ADMIN_UMO)
    assert len(delivered) == 1, delivered
    text = delivered[0][1]
    assert "📺 投稿通知" in text and f"UP主{MID}" in text, text
    assert "稿件BV4" in text and "BV4" in text
    assert "🕒 发布：20" in text  # pubtime 换算成本地时间
    assert "🔗 https://www.bilibili.com/video/BV4" in text
    assert up["seen"][0] == "BV4"
    assert read_data(tmp)["ups"][MID]["seen"] == ["BV4", "BV3", "BV2", "BV1"]


async def test_new_uploads_merged_oldest_first(tmp):
    plugin = build(tmp, conf={"upload_max_notify": 5})
    up = add_up(plugin)
    UPLOADS[MID] = ["BV1"]
    await check_up(plugin, up)
    UPLOADS[MID] = ["BV3", "BV2", "BV1"]
    await check_up(plugin, up)
    delivered = plugin.context.texts(ADMIN_UMO)
    assert len(delivered) == 1, delivered  # 两条以上合并成一条，避免刷屏
    text = delivered[0][1]
    assert "新增 2 条稿件" in text, text
    assert text.index("稿件BV2") < text.index("稿件BV3")  # 按UP主投稿先后


async def test_batch_cap_lists_recent_and_counts_rest(tmp):
    plugin = build(tmp, conf={"upload_max_notify": 2})
    up = add_up(plugin)
    UPLOADS[MID] = ["BV0"]
    await check_up(plugin, up)
    UPLOADS[MID] = ["BV5", "BV4", "BV3", "BV2", "BV1", "BV0"]
    await check_up(plugin, up)
    assert len(plugin.context.texts(ADMIN_UMO)) == 1
    text = plugin.context.texts(ADMIN_UMO)[0][1]
    assert "新增 5 条稿件，列出最近 2 条" in text, text
    assert "稿件BV4" in text and "稿件BV5" in text
    assert "稿件BV3" not in text
    assert "另有 3 条未列出" in text


async def test_single_notify_cap_keeps_one_message(tmp):
    """cap=1 且一次冒出多条时，仍然只发一条消息，并说明还有几条没列。"""
    plugin = build(tmp, conf={"upload_max_notify": 1})
    up = add_up(plugin)
    UPLOADS[MID] = ["BV0"]
    await check_up(plugin, up)
    UPLOADS[MID] = ["BV4", "BV3", "BV2", "BV1", "BV0"]
    await check_up(plugin, up)
    assert len(plugin.context.texts(ADMIN_UMO)) == 1
    text = plugin.context.texts(ADMIN_UMO)[0][1]
    assert "📺 投稿通知" in text and "🔖 稿件：BV4" in text, text
    assert "另有 3 条新稿件未列出" in text


async def test_one_new_upload_uses_detail_format(tmp):
    plugin = build(tmp, conf={"upload_max_notify": 1})
    up = add_up(plugin)
    UPLOADS[MID] = ["BV1"]
    await check_up(plugin, up)
    UPLOADS[MID] = ["BV2", "BV1"]
    await check_up(plugin, up)
    text = plugin.context.texts(ADMIN_UMO)[0][1]
    assert "🔖 稿件：BV2" in text and "🎬 标题：稿件BV2" in text, text


async def test_notify_upload_off_still_tracks_baseline(tmp):
    plugin = build(tmp, conf={"notify_upload": False})
    up = add_up(plugin)
    UPLOADS[MID] = ["BV1"]
    await check_up(plugin, up)
    UPLOADS[MID] = ["BV2", "BV1"]
    await check_up(plugin, up)
    assert plugin.context.sent == []
    assert up["seen"] == ["BV2", "BV1"]
    # 重新打开后不会把关闭期间漏掉的稿件补推送
    plugin.conf["notify_upload"] = True
    await check_up(plugin, up)
    assert plugin.context.sent == []


async def test_upload_notify_respects_whitelist(tmp):
    plugin = build(tmp, conf={"notify_whitelist": USER_UMO})
    up = add_up(plugin, umo=ADMIN_UMO)
    UPLOADS[MID] = ["BV1"]
    await check_up(plugin, up)
    UPLOADS[MID] = ["BV2", "BV1"]
    await check_up(plugin, up)
    assert plugin.context.sent == []
    logs = "".join(m for _, m in plugin.logger.messages)
    assert "不在 notify_whitelist 内" in logs


async def test_cover_failure_still_sends_text(tmp):
    """封面下载失败只降级成纯文字，不能把整条稿件通知弄没。"""
    state = {"items": []}

    async def with_cover(session, mid, cookie="", limit=20):
        return list(state["items"])

    plugin = build(tmp)
    up = add_up(plugin)
    old = upload_item("BV1")
    state["items"] = [old]
    main.fetch_up_uploads = with_cover
    try:
        await check_up(plugin, up)
        new = upload_item("BV2")
        new["cover"] = "https://i0.hdslb.com/new.jpg"
        state["items"] = [new, old]
        await check_up(plugin, up)
    finally:
        main.fetch_up_uploads = fake_up_uploads
    text = plugin.context.texts(ADMIN_UMO)[0][1]
    assert "📺 投稿通知" in text and "稿件BV2" in text
    assert "封面下载失败" in "".join(m for _, m in plugin.logger.messages)


async def test_seen_history_is_capped(tmp):
    plugin = build(tmp)
    up = add_up(plugin)
    cap = main.MAX_SEEN_UPLOADS
    UPLOADS[MID] = ["BV_ROOT"]
    await check_up(plugin, up)
    for i in range(cap + 5):
        UPLOADS[MID] = [f"NEW{i}"] + UPLOADS[MID]
        await check_up(plugin, up)
    assert len(up["seen"]) == cap
    assert up["seen"][0] == f"NEW{cap + 4}"
    assert "BV_ROOT" not in up["seen"]


# ---------- 稿件接口解析（真实解析函数 + 假 HTTP） ----------


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, **kwargs):
        return self.payload


class _Http:
    """只记录 URL/请求头并返回固定 JSON，用来单测真实的解析函数。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Resp(self.payload)


async def test_fetch_up_uploads_parses_payload(tmp):
    payload = {
        "code": 0,
        "message": "OK",
        "data": {
            "media_list": [
                {
                    "bv_id": "BV1",
                    "title": "甲",
                    "cover": "http://i0.hdslb.com/a.jpg",
                    "pubtime": 1760000000,
                    "upper": {"mid": 946974},
                },
                {"bv_id": "", "upper": {"mid": 946974}},  # 没有 BV 号，丢弃
                {"bv_id": "BV2", "upper": {"mid": 1}},  # 合集里别人的稿件，丢弃
                "脏数据",
                {
                    "bv_id": "BV0",
                    "title": "乙",
                    "cover": "",
                    "pubtime": 1759000000,
                    "upper": {"mid": "946974"},
                },
            ]
        },
    }
    http = _Http(payload)
    main.fetch_up_uploads = REAL_FETCH_UP_UPLOADS
    try:
        items = await main.fetch_up_uploads(http, "946974", cookie="SESSDATA=x")
        assert [item["bvid"] for item in items] == ["BV1", "BV0"]
        assert items[0]["url"] == "https://www.bilibili.com/video/BV1"
        assert items[0]["title"] == "甲"
        # 上一条投稿的时间用来算"距上次更新"；列表里最后一条没有参照，标 0
        assert items[0]["prev_pubtime"] == 1759000000
        assert items[1]["prev_pubtime"] == 0
        assert (
            main.format_since(items[0]["prev_pubtime"], items[0]["pubtime"]) == "11天"
        )
        url, kwargs = http.calls[0]
        assert "x/v2/medialist/resource/list" in url and "biz_id=946974" in url
        assert "otype=2" in url and "ps=20" in url
        headers = kwargs["headers"]
        assert headers["Referer"] == "https://space.bilibili.com/"
        assert headers["Cookie"] == "SESSDATA=x"

        try:
            await main.fetch_up_uploads(_Http({"code": -352, "message": "风控"}), "1")
        except main.RoomCheckError as e:
            assert "code=-352" in str(e) and "风控" in str(e)
        else:
            raise AssertionError("接口返回非 0 应抛 RoomCheckError")
        assert await main.fetch_up_uploads(_Http({"code": 0, "data": None}), "1") == []
    finally:
        main.fetch_up_uploads = fake_up_uploads


async def test_fetch_up_info_parses_payload(tmp):
    main.fetch_up_info = REAL_FETCH_UP_INFO
    try:
        http = _Http(
            {
                "code": 0,
                "data": {
                    "card": {"name": " 影视飓风 ", "face": "f"},
                    "archive_count": "951",
                },
            }
        )
        assert await main.fetch_up_info(http, "946974") == {
            "name": "影视飓风",
            "face": "f",
            "archive_count": 951,
        }
        url = http.calls[0][0]
        assert "web-interface/card" in url and "mid=946974" in url
        for payload, why in (
            ({"code": -352, "message": "风控"}, "code=-352"),
            ({"code": 0, "data": {"card": {"name": "  "}}}, "查不到"),
            ({"code": 0}, "查不到"),
        ):
            try:
                await main.fetch_up_info(_Http(payload), "1")
            except main.RoomCheckError as e:
                assert why in str(e), (why, e)
            else:
                raise AssertionError(f"{why} 时应抛 RoomCheckError")
    finally:
        main.fetch_up_info = fake_up_info


# ---------- 列表 / 投递目标 / 轮询 ----------


async def test_list_shows_rooms_and_ups(tmp):
    plugin = build(tmp)
    plugin.rooms["bilibili:6"] = room_record(watchers={ADMIN_UMO: "管理员"})
    add_up(plugin, MID, ADMIN_UMO)
    add_up(plugin, "555", USER_UMO)
    text = (await run(plugin.cmd_list, admin()))[0]
    assert "当前监控的直播间:" in text
    assert "√ [B站直播] bilibili:6 [未开播]" in text
    assert "当前监控的UP主（B站新稿件）:" in text
    assert f"√ [B站UP] up:{MID} UP主{MID}" in text
    assert "- [B站UP] up:555 UP主555" in text
    assert "待首次检测" in text
    assert f"↳ {ADMIN_UMO}" in text


async def test_list_when_nothing_monitored(tmp):
    plugin = build(tmp)
    text = (await run(plugin.cmd_list, admin()))[0]
    assert "没有监控任何直播间或UP主" in text
    assert "监控UP" in text


async def test_deliver_targets_all_watchers(tmp):
    """投稿队列没了，系统消息（新版本提醒）改发给所有订阅会话。"""
    plugin = build(tmp)
    plugin.rooms["bilibili:6"] = room_record(watchers={ADMIN_UMO: "管理员"})
    add_up(plugin, MID, ADMIN_UMO)  # 与房间重复，去重后只发一次
    add_up(plugin, "555", USER_UMO)
    assert plugin._notify_targets() == sorted([ADMIN_UMO, USER_UMO])
    assert await plugin._deliver("系统消息") == 2
    assert [umo for umo, _ in plugin.context.texts()] == sorted([ADMIN_UMO, USER_UMO])

    plugin.conf["notify_whitelist"] = USER_UMO
    assert plugin._notify_targets() == [USER_UMO]


async def test_poll_checks_ups_on_interval(tmp):
    UPLOADS[MID] = ["BV1"]
    plugin = build(tmp, poll=True)
    try:
        add_up(plugin, MID, ADMIN_UMO)
        assert await wait_until(lambda: plugin.ups[MID]["seen"] == ["BV1"]), (
            plugin.logger.messages
        )
        assert f"up:{MID}" in plugin._last_check
        # 没有订阅者的 UP 主不检查
        add_up(plugin, "555")
        plugin.ups["555"]["watchers"] = {}
        UPLOADS["555"] = ["BV9"]
        await asyncio.sleep(0.2)
        assert plugin.ups["555"]["seen"] == []
        assert "up:555" not in plugin._last_check
    finally:
        await stop_poll(main._poll_task)


async def test_poll_survives_up_failure(tmp):
    """UP 主接口被风控只记失败次数，轮询与房间通知不能受影响。"""

    async def broken(session, mid, cookie="", limit=20):
        raise main.RoomCheckError("bilibili 稿件接口返回 code=-352（风控）")

    plugin = build(tmp, poll=True)
    plugin.rooms["bilibili:6"] = room_record(live=False, watchers={USER_UMO: "订阅者"})
    add_up(plugin, MID, ADMIN_UMO)
    main.fetch_up_uploads = broken
    try:
        assert await wait_until(
            lambda: any("检查UP主" in m for _, m in plugin.logger.messages)
        ), plugin.logger.messages
        assert plugin.ups[MID]["fails"] == 1
        assert not main._poll_task.done()
        assert await wait_until(
            lambda: any("开播通知" in t for _, t in plugin.context.texts(USER_UMO))
        ), plugin.context.texts(USER_UMO)
    finally:
        main.fetch_up_uploads = fake_up_uploads
        await stop_poll(main._poll_task)


async def test_upload_interval_clamped(tmp):
    plugin = build(tmp)
    assert plugin._upload_interval() == 300
    plugin.conf["upload_check_interval"] = 5
    assert plugin._upload_interval() == 60  # 最小 60 秒，防止打爆B站
    plugin.conf["upload_check_interval"] = 900
    assert plugin._upload_interval() == 900


DAY = 86400


def live_switch(live: bool):
    """临时替换B站直播接口，返回原实现供用例结束后恢复。"""
    origin = main.CHECKERS["bilibili"]

    async def checker(session, room_id, cookie=""):
        return {
            "live": live,
            "title": f"直播标题{room_id}",
            "anchor": f"主播{room_id}",
            "anchor_uid": "",
            "cover": "",
            "url": f"https://live.bilibili.com/{room_id}",
        }

    main.CHECKERS["bilibili"] = checker
    return origin


def use_uploads(items):
    """按 (bvid, pubtime) 顶替稿件接口，像真实接口一样把上一条时间标回去。"""
    origin = main.fetch_up_uploads
    uploads = [upload_item(bvid, pubtime) for bvid, pubtime in items]
    for idx, current in enumerate(uploads):
        older = uploads[idx + 1] if idx + 1 < len(uploads) else None
        current["prev_pubtime"] = older["pubtime"] if older else 0

    async def fetch(session, mid, cookie="", limit=20):
        return [dict(item) for item in uploads[:limit]]

    main.fetch_up_uploads = fetch
    return origin


async def test_format_since_units(tmp):
    """天/小时的分界，以及取不到参照时必须返回空串（由调用方省略整行）。"""
    now = int(time.time())
    assert main.format_since(now - 3 * DAY, now) == "3天"
    assert main.format_since(now - DAY, now) == "1天"
    assert main.format_since(now - 23 * 3600, now) == "23小时"
    assert main.format_since(now - 5 * 3600, now) == "5小时"
    assert main.format_since(now - 1800, now) == "不到1小时"
    assert main.format_since(0, now) == ""  # 没有上次记录
    assert main.format_since(None, now) == ""
    assert main.format_since("abc", now) == ""
    assert main.format_since(now + DAY, now) == ""  # 时间倒挂不显示负数
    assert main.format_since(now - DAY, 0) == ""  # 缺本次参照
    assert main.format_since(now - 2 * 86400 * 365, now) == "730天"


async def test_upload_notify_shows_since_update(tmp):
    """稿件通知里的"距上次更新"取新稿件与上一条投稿的时间差。"""
    now = int(time.time())
    plugin = build(tmp)
    up = add_up(plugin)
    origin = use_uploads([("BV1", now - 3 * DAY)])
    try:
        await check_up(plugin, up)
        assert plugin.context.sent == []  # 基线不推送

        use_uploads([("BV2", now), ("BV1", now - 3 * DAY)])
        await check_up(plugin, up)
        text = plugin.context.texts(ADMIN_UMO)[-1][1]
        assert "🔖 稿件：BV2" in text, text
        assert "⏳ 距上次更新：3天" in text, text

        # 只有一条稿件、拿不到上一条时间：整行省略，不能出现空字段
        up["seen"] = ["BV2"]
        use_uploads([("BV3", now + DAY)])
        await check_up(plugin, up)
        text = plugin.context.texts(ADMIN_UMO)[-1][1]
        assert "🔖 稿件：BV3" in text and "⏳" not in text, text

        use_uploads([("BV4", now + DAY), ("BV3", now + DAY - 5 * 3600)])
        await check_up(plugin, up)
        text = plugin.context.texts(ADMIN_UMO)[-1][1]
        assert "⏳ 距上次更新：5小时" in text, text
    finally:
        main.fetch_up_uploads = origin


async def test_batch_notify_since_uses_newest(tmp):
    """合并推送按最新一条稿件算距上次更新，并且不能因为多一行而变成两条消息。"""
    now = int(time.time())
    plugin = build(tmp)
    up = add_up(plugin)
    origin = use_uploads([("BV1", now - 6 * 3600)])
    try:
        await check_up(plugin, up)
        use_uploads(
            [
                ("BV3", now),
                ("BV2", now - 2 * 3600),
                ("BV1", now - 6 * 3600),
            ]
        )
        await check_up(plugin, up)
        delivered = plugin.context.texts(ADMIN_UMO)
        assert len(delivered) == 1, delivered
        text = delivered[0][1]
        assert "新增 2 条稿件，列出最近 2 条" in text, text
        # BV3 的上一条是同样是新的 BV2，间隔按实际投稿时间差算，不是距基线
        assert "⏳ 最新一条距上次更新：2小时" in text, text
    finally:
        main.fetch_up_uploads = origin


async def test_elapsed_switch_off_hides_both_fields(tmp):
    """show_elapsed 关掉后，稿件通知与开播通知都不再显示"距离上次"。"""
    now = int(time.time())
    plugin = build(tmp, conf={"show_elapsed": False})
    up = add_up(plugin)
    origin = use_uploads([("BV1", now - 3 * DAY)])
    try:
        await check_up(plugin, up)
        use_uploads([("BV2", now), ("BV1", now - 3 * DAY)])
        await check_up(plugin, up)
        text = plugin.context.texts(ADMIN_UMO)[-1][1]
        assert "🔖 稿件：BV2" in text and "⏳" not in text, text

        plugin.rooms["bilibili:6"] = room_record(live=False, last_live_at=now - 2 * DAY)
        swap = live_switch(True)
        try:
            async with aiohttp.ClientSession() as session:
                await plugin._check_room(session, plugin.rooms["bilibili:6"])
        finally:
            main.CHECKERS["bilibili"] = swap
        text = plugin.context.texts(USER_UMO)[-1][1]
        assert "🔴 开播通知" in text and "⏳" not in text, text

        # 重新打开就恢复显示
        plugin.conf["show_elapsed"] = True
        room = plugin.rooms["bilibili:6"]
        room["live"] = False
        room["last_live_at"] = now - 2 * DAY
        async with aiohttp.ClientSession() as session:
            await plugin._check_room(session, room)
        assert "⏳ 距离上次直播：2天" in plugin.context.texts(USER_UMO)[-1][1]
    finally:
        main.fetch_up_uploads = origin


async def test_live_notify_shows_since_last_live(tmp):
    """开播通知显示距离上次直播，并持久化最后一次在播的时刻。"""
    now = int(time.time())
    plugin = build(tmp)
    plugin.rooms["bilibili:6"] = room_record(live=False)
    swap = live_switch(True)
    try:
        async with aiohttp.ClientSession() as session:
            await plugin._check_room(session, plugin.rooms["bilibili:6"])
            text = plugin.context.texts(USER_UMO)[-1][1]
            assert "🔴 开播通知" in text, text
            # 老数据里没有 last_live_at，取不到上次开播信息就不能显示这一行
            assert "⏳" not in text, text
            room = plugin.rooms["bilibili:6"]
            assert room["last_live_at"] >= now

            # 关播不显示该字段，但记录的时刻保留，供下次开播时计算
            live_switch(False)
            await plugin._check_room(session, room)
            assert "⚫ 关播通知" in plugin.context.texts(USER_UMO)[-1][1]
            assert room["last_live_at"] >= now
            assert read_data(tmp)["rooms"]["bilibili:6"]["last_live_at"] >= now

            room["last_live_at"] = now - 3 * DAY - 60
            live_switch(True)
            await plugin._check_room(session, room)
            text = plugin.context.texts(USER_UMO)[-1][1]
            assert "🔴 开播通知" in text, text
            assert "⏳ 距离上次直播：3天" in text, text
            assert text.index("📺 标题") < text.index("⏳ 距离上次直播")
            assert text.index("⏳ 距离上次直播") < text.index("────────────────\n🔗")
    finally:
        main.CHECKERS["bilibili"] = swap


async def test_last_live_at_survives_reload_and_dirty_data(tmp):
    """重启后 last_live_at 要能读回来；脏值归零而不是让轮询崩掉。"""
    now = int(time.time())
    plugin = build(tmp)
    plugin.rooms["bilibili:6"] = room_record(live=False, last_live_at=now - DAY)
    plugin._save()
    assert build(tmp).rooms["bilibili:6"]["last_live_at"] == now - DAY

    write_data(
        tmp,
        {
            "version": 3,
            "rooms": {
                "bilibili:7": room_record(room_id="7", last_live_at="abc"),
                "bilibili:8": room_record(room_id="8", last_live_at=None),
                "bilibili:9": room_record(room_id="9"),
            },
            "ups": {},
        },
    )
    restored = build(tmp)
    assert restored.rooms["bilibili:7"]["last_live_at"] == 0
    assert restored.rooms["bilibili:8"]["last_live_at"] == 0
    assert restored.rooms["bilibili:9"]["last_live_at"] == 0


# ---------- 安全与健壮性（代码审查后补的用例） ----------


async def test_room_id_charset_rejects_url_injection(tmp):
    """房间号会拼进请求 URL，"平台:房间号" 的写法与手工数据都得挡住 & ? / #。"""
    assert main.parse_room_input("bilibili:6") == ("bilibili", "6", False)
    assert main.parse_room_input("huya:abc-1_2") == ("huya", "abc-1_2", False)
    for bad in (
        "bilibili:6&admin=1",
        "bilibili:../../x",
        "bilibili:6?x=1",
        "斗鱼:abc#1",
    ):
        assert main.parse_room_input(bad) is None, bad
    assert main.valid_room_id("douyin", "1" * 30) is False

    plugin = build(tmp)
    kept = plugin._sanitize(
        {
            "bilibili:6": room_record(room_id="6"),
            "bilibili:x": room_record(room_id="6 or 1=1"),
            "bilibili:y": room_record(room_id="6?callback=1"),
        }
    )
    assert list(kept) == ["bilibili:6"]
    assert "房间号含非法字符" in "".join(m for _, m in plugin.logger.messages)
    assert "无法识别" in (await run(plugin.cmd_add, admin(), "bilibili:6&admin=1"))[0]


class _ImgResp:
    def __init__(self, data=b"fake-image"):
        self.status = 200
        self.content_type = "image/jpeg"
        self.content_length = len(data)
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self):
        return self._data


class _ImgHttp:
    """记录封面请求的 URL 与请求头，返回固定图片字节。"""

    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _ImgResp()


async def test_login_cookie_only_goes_to_bilibili(tmp):
    """封面地址可能被手工编辑过，登录 Cookie 不能跟着发给任意站点。"""
    plugin = build(tmp, {"bilibili_cookie": "SESSDATA=x"})
    room = room_record()
    http = _ImgHttp()

    data = await plugin._fetch_cover_base64(http, room, "https://i0.hdslb.com/a.jpg")
    assert data == base64.b64encode(b"fake-image").decode()
    assert http.calls[0][1]["headers"]["Cookie"] == "SESSDATA=x"

    for url in ("https://example.com/a.jpg", "https://hdslb.com.evil.net/a.jpg"):
        await plugin._fetch_cover_base64(http, room, url)
        assert "Cookie" not in http.calls[-1][1]["headers"], url

    plugin.conf["bilibili_cookie"] = ""
    assert plugin._cookie_for("https://i0.hdslb.com/a.jpg") == ""


async def test_corrupt_data_is_quarantined(tmp):
    """手工编辑写坏一个逗号不能静默清空：原文件改名留底，修好后重载即可恢复。"""
    Path(tmp, "rooms.json").write_text('{"version": 3, "rooms": {,,}', encoding="utf-8")
    plugin = build(tmp)
    assert plugin.rooms == {} and plugin.ups == {}

    bad = list(Path(tmp).glob("rooms.json.bad-*"))
    assert len(bad) == 1, plugin.logger.messages
    assert "{,," in bad[0].read_text(encoding="utf-8")
    assert not Path(tmp, "rooms.json").exists()
    logs = "".join(m for _, m in plugin.logger.messages)
    assert "读取监控数据失败" in logs and "损坏的监控数据已改名保留" in logs


async def test_save_is_atomic(tmp):
    """保存先写临时文件再改名：序列化炸了也不能留下半个 rooms.json。"""
    plugin = build(tmp)
    plugin.rooms["bilibili:6"] = room_record()
    plugin._save()
    good = Path(tmp, "rooms.json").read_text(encoding="utf-8")
    assert not Path(tmp, "rooms.json.tmp").exists()
    assert read_data(tmp)["rooms"]["bilibili:6"]["watchers"] == {USER_UMO: "订阅者"}

    plugin.rooms["bilibili:6"]["watchers"] = {object(): "订阅者"}  # JSON 序列化不了
    plugin._save()
    assert Path(tmp, "rooms.json").read_text(encoding="utf-8") == good
    assert not Path(tmp, "rooms.json.tmp").exists()
    assert "保存监控数据失败" in "".join(m for _, m in plugin.logger.messages)


async def test_watcherless_room_loses_live_baseline(tmp):
    """没人订阅就不会再被轮询，上次在播时刻要一并清掉，否则重新订阅后显示假间隔。"""
    plugin = build(tmp)
    plugin.rooms["bilibili:6"] = room_record(last_live_at=1760000000)
    plugin.rooms["bilibili:6"]["watchers"][ADMIN_UMO] = "管理员"

    assert "已取消" in (await run(plugin.cmd_unsub, user(), "6"))[0]
    assert plugin.rooms["bilibili:6"]["last_live_at"] == 1760000000

    assert "已取消" in (await run(plugin.cmd_unsub, admin(), "6"))[0]
    assert plugin.rooms["bilibili:6"]["watchers"] == {}
    assert plugin.rooms["bilibili:6"]["last_live_at"] == 0

    cleaned = plugin._sanitize(
        {"bilibili:6": room_record(last_live_at=1760000000, watchers={})}
    )
    assert cleaned["bilibili:6"]["last_live_at"] == 0


async def test_poll_loop_survives_round_error(tmp):
    """单轮的意外异常必须咽下去：后台任务一退出，所有监控就静默停了。"""
    plugin = build(tmp, poll=True)

    async def boom(session):
        raise RuntimeError("模拟本轮检查炸掉")

    plugin._poll_once = boom
    try:
        assert await wait_until(
            lambda: any("本轮监控检查异常" in m for _, m in plugin.logger.messages)
        ), plugin.logger.messages
        assert any(level == "error" for level, _ in plugin.logger.messages)
        assert not main._poll_task.done()
    finally:
        await stop_poll(main._poll_task)


async def test_room_flow_untouched(tmp):
    """移除投稿不能影响原有直播间监控/通知路径。"""
    plugin = build(tmp)
    plugin.rooms["bilibili:6"] = room_record(live=False)
    async with aiohttp.ClientSession() as session:
        await plugin._check_room(session, plugin.rooms["bilibili:6"])
    assert plugin.rooms["bilibili:6"]["live"] is True
    assert "🔴 开播通知" in plugin.context.texts(USER_UMO)[-1][1]

    assert "用法" in (await run(plugin.cmd_add, admin(), None))[0]
    assert "无法识别" in (await run(plugin.cmd_add, admin(), "https://example.com"))[0]
    assert "已添加监控" in (await run(plugin.cmd_add, admin(), 33214))[0]
    assert "已订阅" in (await run(plugin.cmd_sub, admin(), 33214))[0]
    assert "已取消" in (await run(plugin.cmd_unsub, admin(), 33214))[0]
    assert "已删除监控" in (await run(plugin.cmd_del, admin(), 33214))[0]
    help_text = (await run(plugin.cmd_help, admin()))[0]
    assert "投稿列表" not in help_text and "审核订阅" not in help_text
    assert "监控UP" in help_text


async def main_test():
    tests = [
        ("旧版扁平数据升级 v3", test_legacy_flat_data_upgraded),
        ("v2 投稿数据丢弃留痕", test_v2_submission_data_is_dropped),
        ("脏 UP 数据清理", test_corrupted_up_data_is_dropped),
        ("监控UP 输入校验", test_add_up_rejects_bad_input),
        ("空间链接添加UP主", test_add_up_by_space_link),
        ("int 参数覆盖UP指令", test_up_commands_with_int_param),
        ("稿件基线与新稿件推送", test_up_baseline_then_new_upload_notifies),
        ("多条稿件合并成一条", test_new_uploads_merged_oldest_first),
        ("批量上限列最近几条", test_batch_cap_lists_recent_and_counts_rest),
        ("cap=1 不连发", test_single_notify_cap_keeps_one_message),
        ("单条稿件明细格式", test_one_new_upload_uses_detail_format),
        ("距上次间隔格式", test_format_since_units),
        ("稿件通知距上次更新", test_upload_notify_shows_since_update),
        ("合并通知按最新条算间隔", test_batch_notify_since_uses_newest),
        ("间隔开关同时管两处", test_elapsed_switch_off_hides_both_fields),
        ("开播通知距离上次直播", test_live_notify_shows_since_last_live),
        ("上次在播时刻持久化", test_last_live_at_survives_reload_and_dirty_data),
        ("房间号字符集校验", test_room_id_charset_rejects_url_injection),
        ("登录Cookie只发B站域名", test_login_cookie_only_goes_to_bilibili),
        ("损坏数据改名留底", test_corrupt_data_is_quarantined),
        ("原子保存不留半个文件", test_save_is_atomic),
        ("无订阅者清掉在播基线", test_watcherless_room_loses_live_baseline),
        ("轮询单轮异常不退出", test_poll_loop_survives_round_error),
        ("关稿件通知仍记基线", test_notify_upload_off_still_tracks_baseline),
        ("稿件通知走白名单", test_upload_notify_respects_whitelist),
        ("封面失败降级纯文字", test_cover_failure_still_sends_text),
        ("稿件历史封顶", test_seen_history_is_capped),
        ("稿件接口解析", test_fetch_up_uploads_parses_payload),
        ("UP主校验接口", test_fetch_up_info_parses_payload),
        ("列表含房间与UP主", test_list_shows_rooms_and_ups),
        ("空列表提示", test_list_when_nothing_monitored),
        ("系统消息发给订阅会话", test_deliver_targets_all_watchers),
        ("轮询按间隔查UP主", test_poll_checks_ups_on_interval),
        ("稿件接口失败不崩轮询", test_poll_survives_up_failure),
        ("稿件间隔下限", test_upload_interval_clamped),
        ("原直播监控不受影响", test_room_flow_untouched),
    ]
    failed = 0
    for name, func in tests:
        UPLOADS.clear()
        main.fetch_up_uploads = fake_up_uploads
        main.fetch_up_info = fake_up_info
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
    raise SystemExit(asyncio.run(main_test()))
