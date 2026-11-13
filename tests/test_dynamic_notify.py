"""UP主动态监控 + 聊天指令切换通知开关的离线冒烟测试。

运行：python tests/test_dynamic_notify.py

动态那一路覆盖：WBI 签名口径、buvid3 组装、各类动态条目解析、接口的重试与抛错、
基线/新动态/合并推送、与稿件的互相去重、seen_dyn 持久化与脏值清理、开关关闭时
完全不发请求。通知开关那一路覆盖：运行时覆盖优先于配置文件、持久化活过重载、
恢复默认、简写与 int 参数、非法输入不动状态、非管理员拒绝。桩模块见 stubs.py。
"""

import asyncio
import time
import tempfile

# 必须先导入 stubs（它会替换 sys.modules 里的 aiohttp），再导入 aiohttp
from stubs import (  # noqa: E402
    DYNAMICS,
    DYN_CALLS,
    REAL_FETCH_UP_DYNAMICS,
    REAL_POLL,
    UPLOADS,
    USER_UMO,
    admin,
    build,
    dynamic_item,
    main,
    read_data,
    run,
    user,
    write_data,
)
import aiohttp  # noqa: E402

MID = "946974"
OTHER = "22082958"
# 长度与B站真实 key 一致：不足 32 位会被当成"取密钥失败"
IMG_KEY = "7cd084941338484aae1ad9425b841299"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
DAY = 86400
# stubs 把 main.fetch_up_dynamics 换成了假实现，"接口调用"那组用例要跑真代码
real_fetch_dynamics = REAL_FETCH_UP_DYNAMICS


def prime_api_cache():
    """预先塞好签名密钥与 buvid3（视为未过期），让用例只跑动态接口这一段。"""
    main._wbi_cache.update(mixin="a" * 32, ts=time.time() + 3600)
    main._buvid_cache.update(value="BUVIDXYZ", ts=time.time() + DAY)


def reset_api_cache():
    main._wbi_cache.update(mixin="", ts=0.0)
    main._buvid_cache.update(value="", ts=0.0)


def add_up(plugin, mid=MID, dyn=False, notify_upload=True):
    """绕开指令直接登记一个已订阅的UP主，让用例专注在检查/推送逻辑上。"""
    up = plugin._register_up(mid)
    up["name"] = f"UP主{mid}"
    up["watchers"] = {USER_UMO: "订阅者"}
    conf = dict(plugin.conf)
    conf.setdefault("notify_dynamic", dyn)
    conf.setdefault("notify_upload", notify_upload)
    plugin.conf = conf
    return up


async def check_up(plugin, up):
    async with aiohttp.ClientSession() as session:
        await plugin._check_up(session, up)


def last_text(plugin, umo=USER_UMO):
    texts = plugin.context.texts(umo)
    return texts[-1][1] if texts else ""


async def _expect_raises(coro, exc):
    try:
        await coro
    except exc:
        return
    raise AssertionError(f"应当抛出 {exc.__name__}")


# ---------- WBI 签名与 Cookie ----------


def test_wbi_signature_shape():
    """签名必须带 w_rid/wts，参数按 key 排序，w_rid 由排序串 + mixin 算出。"""
    from urllib.parse import parse_qsl, urlencode
    import hashlib

    mixin = "a" * 32
    query = main._wbi_signed_query({"host_mid": "42", "offset": ""}, mixin)
    pairs = dict(parse_qsl(query, keep_blank_values=True))
    assert "w_rid" in pairs and "wts" in pairs, query
    # 参与签名的串不含 w_rid：把其余参数按原序拼回来应当能复现同一个 md5
    body = urlencode(
        [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "w_rid"]
    )
    assert pairs["w_rid"] == hashlib.md5((body + mixin).encode()).hexdigest()
    keys = [k for k, _ in parse_qsl(query, keep_blank_values=True)]
    assert keys == sorted(keys), "参数必须排序，否则服务端算出的签名对不上"


def test_wbi_strips_unsafe_chars():
    """签名值要先剔除 !'()*（与B站前端口径一致），发出去的值仍保持原样。"""
    import hashlib
    from urllib.parse import parse_qsl, urlencode

    mixin = "a" * 32
    raw = "item!Opus(Style)*"
    query = main._wbi_signed_query({"features": raw}, mixin)
    sent = dict(parse_qsl(query, keep_blank_values=True))
    assert sent["features"] == raw, "剔除只影响签名串，不能改参数值"
    signed = urlencode({"features": "itemOpusStyle", "wts": sent["wts"]})
    assert sent["w_rid"] == hashlib.md5((signed + mixin).encode()).hexdigest()
    unstripped = urlencode({"features": raw, "wts": sent["wts"]})
    assert sent["w_rid"] != hashlib.md5((unstripped + mixin).encode()).hexdigest(), (
        "没剔除就算不出服务端那个签名"
    )


def test_wbi_mixin_key_len():
    """mixin key 由 img_key+sub_key 按下标重排取前 32 位，且算法稳定。"""
    key = main._wbi_mixin_key(IMG_KEY, SUB_KEY)
    assert len(key) == 32, key
    assert key == main._wbi_mixin_key(IMG_KEY, SUB_KEY)


async def test_fetch_wbi_mixin_caches():
    """密钥一天只换几次：nav 只查一次，缓存没过期就不再请求。"""
    calls = []

    async def fake_get_json(session, url, headers):
        calls.append(url)
        return {
            "code": 0,
            "data": {
                "wbi_img": {
                    "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.jpg",
                    "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.jpg",
                }
            },
        }

    real = main._get_json
    main._get_json = fake_get_json
    reset_api_cache()
    try:
        first = await main.fetch_wbi_mixin(None, "")
        second = await main.fetch_wbi_mixin(None, "")
        assert first == second == main._wbi_mixin_key(IMG_KEY, SUB_KEY)
        assert len(calls) == 1, "没过期就不该再请求 nav"
        main._wbi_cache["ts"] = 0.0  # 假装过期
        await main.fetch_wbi_mixin(None, "")
        assert len(calls) == 2, "过期后必须重新取密钥"
    finally:
        main._get_json = real
        reset_api_cache()


async def test_fetch_wbi_mixin_missing_key():
    """接口没给 wbi_img 时报错而不是拿空密钥去签名（空签名必然被风控）。"""

    async def fake_get_json(session, url, headers):
        return {"code": 0, "data": {}}

    real = main._get_json
    main._get_json = fake_get_json
    reset_api_cache()
    try:
        await _expect_raises(main.fetch_wbi_mixin(None, ""), main.RoomCheckError)
    finally:
        main._get_json = real
        reset_api_cache()


async def test_buvid_appended_to_cookie():
    """用户 Cookie 优先，缺 buvid3 才补，且不能补两次。"""
    calls = []

    async def fake_get_json(session, url, headers):
        calls.append(url)
        return {"code": 0, "data": {"b_3": "BUVIDXYZ"}}

    real = main._get_json
    main._get_json = fake_get_json
    reset_api_cache()
    try:
        assert await main._dynamic_cookie(None, "") == "buvid3=BUVIDXYZ"
        assert (
            await main._dynamic_cookie(None, "SESSDATA=abc")
            == "SESSDATA=abc; buvid3=BUVIDXYZ"
        )
        assert await main._dynamic_cookie(None, "buvid3=MINE") == "buvid3=MINE"
        assert len(calls) == 1, "取到一次就该缓存复用"
    finally:
        main._get_json = real
        reset_api_cache()


async def test_buvid_failure_not_fatal():
    """取不到 buvid3 也不抛：带着用户 Cookie 照样发请求，让接口自己决定。"""

    async def boom(session, url, headers):
        raise RuntimeError("网络断了")

    real = main._get_json
    main._get_json = boom
    reset_api_cache()
    try:
        assert await main._dynamic_cookie(None, "SESSDATA=abc") == "SESSDATA=abc"
    finally:
        main._get_json = real
        reset_api_cache()


class _RawResp:
    """假响应：可以模拟风控直接回 HTML 的情况。"""

    def __init__(self, status=200, body="<html>412</html>", broken=False):
        self.status = status
        self.body = body
        self.broken = broken

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, **kwargs):
        if self.broken:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        import json as _json

        return _json.loads(self.body)


class _RawHttp:
    def __init__(self, resp):
        self.resp = resp

    def get(self, url, **kwargs):
        return self.resp


async def test_get_json_reports_risk_control_readably():
    """风控回 412/HTML 时要抛可读的 RoomCheckError，而不是 JSON 解析报错。"""
    await _expect_raises(
        main._get_json(
            _RawHttp(_RawResp(status=412)), "https://api.bilibili.com/x", {}
        ),
        main.RoomCheckError,
    )
    try:
        await main._get_json(_RawHttp(_RawResp(broken=True)), "https://x", {})
    except main.RoomCheckError as e:
        assert "不是 JSON" in str(e), e
    else:
        raise AssertionError("非 JSON 响应应抛 RoomCheckError")
    got = await main._get_json(_RawHttp(_RawResp(body='{"code":0}')), "https://x", {})
    assert got == {"code": 0}, "正常 JSON 照常返回"


# ---------- 条目解析 ----------


def test_parse_dynamic_archive():
    """视频动态：标题/封面/链接取 archive，并带出 bvid 供稿件去重。"""
    item = {
        "id_str": "123",
        "type": "DYNAMIC_TYPE_AV",
        "modules": {
            "module_author": {"pub_ts": "1760000000", "pub_action": "发布了视频"},
            "module_dynamic": {
                "desc": {"text": "带你看展"},
                "major": {
                    "type": "MAJOR_TYPE_ARCHIVE",
                    "archive": {
                        "bvid": "BV1xx",
                        "title": "视频标题",
                        "cover": "//i0.hdslb.com/a.jpg",
                        "jump_url": "//www.bilibili.com/video/BV1xx",
                    },
                },
            },
        },
    }
    parsed = main.parse_dynamic(item)
    assert parsed["kind"] == "视频"
    assert parsed["bvid"] == "BV1xx"
    assert parsed["title"] == "视频标题"
    assert parsed["text"] == "带你看展"
    assert parsed["url"] == "https://www.bilibili.com/video/BV1xx"
    assert parsed["cover"] == "https://i0.hdslb.com/a.jpg", "协议相对链接要补 https:"
    assert parsed["pubtime"] == 1760000000
    assert parsed["action"] == "发布了视频"


def test_parse_dynamic_opus_and_forward():
    """图文取 opus 的标题/首图；转发没有 major，退回动态永久链接。"""
    opus = {
        "id_str": "456",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"pub_ts": "1760000100"},
            "module_dynamic": {
                "major": {
                    "type": "MAJOR_TYPE_OPUS",
                    "opus": {
                        "summary": {"text": "最近看到不少朋友在催更"},
                        "pics": [{"url": "//i0.hdslb.com/p1.jpg"}],
                        "jump_url": "//www.bilibili.com/opus/456",
                    },
                }
            },
        },
    }
    parsed = main.parse_dynamic(opus)
    assert parsed["kind"] == "图文"
    assert parsed["title"] == "最近看到不少朋友在催更"
    assert parsed["cover"] == "https://i0.hdslb.com/p1.jpg"
    assert parsed["url"] == "https://www.bilibili.com/opus/456"

    forward = {
        "id_str": "789",
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {
            "module_author": {"pub_ts": "1760000200"},
            "module_dynamic": {"desc": {"text": "大家中午好"}},
        },
    }
    parsed = main.parse_dynamic(forward)
    assert parsed["kind"] == "转发"
    assert parsed["url"] == "https://t.bilibili.com/789", "拿不到跳转就该回永久链接"
    assert parsed["cover"] == ""
    assert parsed["bvid"] == ""


def test_format_dynamic_no_duplicate_body():
    """图文的正文接口只把文字放在"标题"位置，不能再原样打第二遍；过长要截断。"""
    up = {"mid": MID, "name": "影视飓风"}
    long_text = "图" * 130
    opus = dynamic_item("1", kind="图文", title="", text="")
    opus["title"] = long_text
    text = main.format_dynamic(up, opus)
    assert f"📌 标题：{'图' * 120}…" in text, text
    assert "📝 内容" not in text, "同一段文字不该出现两次"

    video = dynamic_item(
        "2", kind="视频", title="动态视频｜逛展", text="带你沉浸式逛进博会～"
    )
    text = main.format_dynamic(up, video)
    assert (
        "📌 标题：动态视频｜逛展" in text and "📝 内容：带你沉浸式逛进博会～" in text
    ), text

    plain = dynamic_item("3", kind="纯文字", title="", text="大家中午好")
    text = main.format_dynamic(up, plain)
    assert "📌 标题" not in text and "📝 内容：大家中午好" in text, text


def test_parse_dynamic_dirty():
    """脏数据不能让轮询炸掉：认不出 id 的条目返回 None 由上层丢弃。"""
    assert main.parse_dynamic(None) is None
    assert main.parse_dynamic("x") is None
    assert main.parse_dynamic({"type": "DYNAMIC_TYPE_AV"}) is None
    # 认不出的新类型按默认标签处理，仍然可以推送
    parsed = main.parse_dynamic(
        {"id_str": "1", "type": "DYNAMIC_TYPE_FUTURE_THING", "modules": None}
    )
    assert parsed["kind"] == main.DEFAULT_DYNAMIC_KIND
    assert parsed["pubtime"] == 0


# ---------- 接口调用 ----------


def _queue_http(responses):
    """造一个按顺序吐响应的假动态接口，最后一次响应会一直重复。"""
    calls = []

    async def fake(session, url, headers):
        calls.append((url, headers))
        return responses[min(len(calls) - 1, len(responses) - 1)]

    return fake, calls


async def _no_sleep(_seconds):
    return None


async def test_fetch_dynamics_code_error():
    """接口返回非 0 要上抛，让上层跳过这一轮而不是当成"没有动态"。"""
    real = main._get_json
    fake, _ = _queue_http([{"code": -352, "message": "风险", "data": {}}])
    main._get_json = fake
    prime_api_cache()
    try:
        await _expect_raises(real_fetch_dynamics(None, MID), main.RoomCheckError)
    finally:
        main._get_json = real
        reset_api_cache()


async def test_fetch_dynamics_retries_empty():
    """code=0 但列表为空是风控假空：重试到位再交回，调用方才不会当基线。"""
    empty = {"code": 0, "message": "0", "data": {"items": []}}
    full = {
        "code": 0,
        "data": {
            "items": [
                {
                    "id_str": "1",
                    "type": "DYNAMIC_TYPE_DRAW",
                    "modules": {"module_author": {"pub_ts": "1760000000"}},
                }
            ]
        },
    }
    real, real_sleep = main._get_json, asyncio.sleep
    fake, calls = _queue_http([empty, empty, full])
    main._get_json = fake
    asyncio.sleep = _no_sleep
    prime_api_cache()
    try:
        out = await real_fetch_dynamics(None, MID)
        assert len(calls) == main.DYNAMIC_EMPTY_RETRY, "前两次空列表应当重试"
        assert [d["id"] for d in out] == ["1"]
        assert all("w_rid=" in url for url, _ in calls), "每次重试都要重新签名"
    finally:
        main._get_json = real
        asyncio.sleep = real_sleep
        reset_api_cache()


async def test_fetch_dynamics_gives_up_empty():
    """一直空就交回空列表，由 _check_dynamics 决定不覆盖基线。"""
    empty = {"code": 0, "data": {"items": []}}
    real, real_sleep = main._get_json, asyncio.sleep
    fake, calls = _queue_http([empty])
    main._get_json = fake
    asyncio.sleep = _no_sleep
    prime_api_cache()
    try:
        assert await real_fetch_dynamics(None, MID) == []
        assert len(calls) == main.DYNAMIC_EMPTY_RETRY
    finally:
        main._get_json = real
        asyncio.sleep = real_sleep
        reset_api_cache()


async def test_fetch_dynamics_uses_dynamic_referer():
    """Referer 必须指到该UP主的动态页：实测只写空间首页会回 HTML 风控页。"""
    real = main._get_json
    fake, calls = _queue_http([{"code": 0, "data": {"items": []}}])
    main._get_json = fake
    real_sleep, asyncio.sleep = asyncio.sleep, _no_sleep
    prime_api_cache()
    try:
        await real_fetch_dynamics(None, MID)
        feed_url, headers = calls[-1]
        assert "/feed/space" in feed_url, feed_url
        assert headers["Referer"] == f"https://space.bilibili.com/{MID}/dynamic"
        assert headers["Cookie"] == "buvid3=BUVIDXYZ", "没带 buvid3 会被直接 -352"
        assert "w_rid=" in feed_url and "wts=" in feed_url, "缺签名同样是 -352"
    finally:
        main._get_json = real
        asyncio.sleep = real_sleep
        reset_api_cache()


async def test_fetch_dynamics_orders_and_caps():
    """列表保持从新到旧，并给每条挂上"上一条"的时间用于算间隔。"""
    items = [
        {
            "id_str": str(4 - i),
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {"module_author": {"pub_ts": str(1760000000 + i * 100)}},
        }
        for i in range(4)
    ]
    real = main._get_json
    fake, _ = _queue_http([{"code": 0, "data": {"items": items}}])
    main._get_json = fake
    prime_api_cache()
    try:
        out = await real_fetch_dynamics(None, MID, limit=3)
        assert [d["id"] for d in out] == ["4", "3", "2"], "limit 截断不能打乱顺序"
        assert out[0]["prev_pubtime"] == out[1]["pubtime"]
        assert out[-1]["prev_pubtime"] == 0, "最后一条没有参照，间隔行会被省略"
    finally:
        main._get_json = real
        reset_api_cache()


# ---------- 动态检查与推送 ----------


async def test_dynamic_first_round_is_baseline(tmp):
    """首轮只记基线：不然刚加上的UP主会把自己半年前的动态全推一遍。"""
    DYNAMICS[MID] = [dynamic_item("10"), dynamic_item("9")]
    plugin = build(tmp)
    up = add_up(plugin, dyn=True)
    await check_up(plugin, up)
    assert plugin.context.texts() == [], "基线轮不该有任何推送"
    assert up["seen_dyn"] == ["10", "9"]
    assert read_data(tmp)["ups"][MID]["seen_dyn"] == ["10", "9"], "基线必须落盘"


async def test_dynamic_new_item_pushed(tmp):
    DYNAMICS[MID] = [dynamic_item("20"), dynamic_item("10")]
    plugin = build(tmp)
    up = add_up(plugin, dyn=True)
    await check_up(plugin, up)
    DYNAMICS[MID] = [
        dynamic_item("30", kind="转发", text="恭喜中奖"),
        dynamic_item("20"),
    ]
    await check_up(plugin, up)
    text = last_text(plugin)
    assert "📣 动态通知" in text, text
    assert "🏷 类型：转发" in text and "恭喜中奖" in text, text
    assert "https://t.bilibili.com/30" in text, text
    assert "UP主946974" in text, text
    assert up["seen_dyn"][:2] == ["30", "20"], "新动态排最前，旧历史保留"


async def test_dynamic_video_deduped_with_uploads(tmp):
    """同一支视频既进稿件列表也会生成视频动态，只能提醒一次。

    稿件那一路先跑，会把新 bvid 记进 seen；动态里同 bvid 的新条目据此跳过。
    """
    UPLOADS[MID] = ["BVold"]
    DYNAMICS[MID] = [dynamic_item("5", kind="图文", text="旧的图文")]
    plugin = build(tmp)
    up = add_up(plugin, dyn=True)
    await check_up(plugin, up)
    assert plugin.context.texts() == []

    UPLOADS[MID] = ["BVnew", "BVold"]
    DYNAMICS[MID] = [
        dynamic_item("6", kind="视频", bvid="BVnew", title="同一支视频"),
        dynamic_item("5", kind="图文", text="旧的图文"),
    ]
    plugin.context.clear()
    await check_up(plugin, up)
    texts = [t for _, t in plugin.context.texts(USER_UMO)]
    assert len(texts) == 1, f"只该推一次稿件，实际 {len(texts)} 条：{texts}"
    assert "BVnew" in texts[0] or "稿件BVnew" in texts[0], texts[0]
    assert "同一支视频" not in texts[0], "视频动态被稿件抢了先，不该重复"


async def test_dynamic_video_pushed_when_upload_notify_off(tmp):
    """关掉稿件通知时，动态里的视频仍要推出去（"动态视频"本来就不在稿件列表）。"""
    DYNAMICS[MID] = [dynamic_item("7", kind="视频", bvid="BVdyn", title="旧的动态视频")]
    plugin = build(tmp)
    up = add_up(plugin, dyn=True, notify_upload=False)
    await check_up(plugin, up)
    assert plugin.context.texts() == []
    DYNAMICS[MID] = [
        dynamic_item("8", kind="视频", bvid="BVdyn2", title="动态视频｜新一场"),
        dynamic_item("7", kind="视频", bvid="BVdyn", title="旧的动态视频"),
    ]
    plugin.context.clear()
    await check_up(plugin, up)
    text = last_text(plugin)
    assert "📌 标题：动态视频｜新一场" in text, text
    assert "https://t.bilibili.com/8" in text, text
    # 推过的视频 bvid 记进稿件历史，稿件接口以后不会再补一刀
    assert "BVdyn2" in up["seen"]


async def test_dynamic_merged_and_capped(tmp):
    """一次冒出多条时合并成一条，并受 upload_max_notify 约束。"""
    DYNAMICS[MID] = [dynamic_item("1")]
    plugin = build(tmp, {"upload_max_notify": 2})
    up = add_up(plugin, dyn=True)
    await check_up(plugin, up)
    DYNAMICS[MID] = [dynamic_item(str(i), text=f"第{i}条") for i in range(9, 3, -1)]
    plugin.context.clear()
    await check_up(plugin, up)
    texts = plugin.context.texts(USER_UMO)
    assert len(texts) == 1, "多条动态要合成一条消息"
    text = texts[0][1]
    assert "新增 6 条动态，列出最近 2 条" in text, text
    assert "另有 4 条动态未列出" in text, text
    assert "[图文] 第9条" in text and "[图文] 第8条" in text, "列出的是最近两条"


async def test_dynamic_history_capped(tmp):
    """seen_dyn 只保留最近 MAX_SEEN_DYNAMICS 条，数据文件不会无限膨胀。"""
    plugin = build(tmp)
    up = add_up(plugin, dyn=True)
    for round_no in range(4):
        DYNAMICS[MID] = [dynamic_item(str(round_no * 20 + i)) for i in range(20, 0, -1)]
        plugin.context.clear()
        await check_up(plugin, up)
    assert len(up["seen_dyn"]) == main.MAX_SEEN_DYNAMICS
    assert len(up["seen_dyn"]) == len(set(up["seen_dyn"])), "去重历史不能有重复项"


async def test_dynamic_empty_response_keeps_baseline(tmp):
    """接口返回空列表时绝不清空基线，否则下一轮会整屏重推。"""
    DYNAMICS[MID] = [dynamic_item("10"), dynamic_item("9")]
    plugin = build(tmp)
    up = add_up(plugin, dyn=True)
    await check_up(plugin, up)
    DYNAMICS[MID] = []
    plugin.context.clear()
    await check_up(plugin, up)
    assert up["seen_dyn"] == ["10", "9"]
    assert plugin.context.texts() == []


async def test_dynamic_switch_off_sends_no_request(tmp):
    """默认关闭时一个动态请求都不发：不该为没开的功能多吃一次风控。"""
    DYNAMICS[MID] = [dynamic_item("1")]
    plugin = build(tmp)
    up = add_up(plugin, dyn=False)
    await check_up(plugin, up)
    assert DYN_CALLS == [], f"开关关着却请求了动态接口: {DYN_CALLS}"


async def test_dynamic_elapsed_respects_show_elapsed(tmp):
    """「距上次动态」跟随 show_elapsed，取不到上一条时整行省略。"""
    DYNAMICS[MID] = [
        dynamic_item("2", pubtime=1760000000),
        dynamic_item("1", pubtime=1759992800),
    ]
    plugin = build(tmp, {"show_elapsed": True})
    up = add_up(plugin, dyn=True)
    await check_up(plugin, up)
    DYNAMICS[MID] = [
        dynamic_item("3", pubtime=1760030000),
        dynamic_item("2", pubtime=1760000000),
        dynamic_item("1", pubtime=1759992800),
    ]
    plugin.context.clear()
    await check_up(plugin, up)
    assert "⏳ 距上次动态：8小时" in last_text(plugin), last_text(plugin)

    plugin.conf["show_elapsed"] = False
    DYNAMICS[MID] = [dynamic_item("4", pubtime=1760040000)] + DYNAMICS[MID]
    plugin.context.clear()
    await check_up(plugin, up)
    assert "距上次动态" not in last_text(plugin), "关掉后不该再显示间隔"


async def test_dynamic_seen_dyn_survives_reload_and_dirty(tmp):
    """重启读回；手工编辑过的脏 seen_dyn 要清干净而不是带着跑。"""
    write_data(
        tmp,
        {
            "version": 5,
            "rooms": {},
            "ups": {
                MID: {
                    "mid": MID,
                    "name": "老数据",
                    "watchers": {USER_UMO: "订阅者"},
                    "seen": ["BV1"],
                    "seen_dyn": ["a", 3, "", "  "],
                },
                "脏key": {"mid": "脏key"},
            },
        },
    )
    plugin = build(tmp)
    assert plugin.ups[MID]["seen_dyn"] == ["a", "3"], "只留能当去重键的非空项"
    assert "脏key" not in plugin.ups


async def test_v4_data_gets_empty_dynamic_history(tmp):
    """0.3.x 的 v4 数据没有 seen_dyn，载入后是空列表并在首轮建基线。"""
    write_data(
        tmp,
        {
            "version": 4,
            "rooms": {},
            "ups": {
                MID: {
                    "mid": MID,
                    "name": "旧版UP",
                    "watchers": {USER_UMO: "订阅者"},
                    "seen": ["BV1"],
                }
            },
        },
    )
    plugin = build(tmp)
    assert plugin.ups[MID]["seen_dyn"] == []
    DYNAMICS[MID] = [dynamic_item("1")]
    plugin.conf["notify_dynamic"] = True
    await check_up(plugin, plugin.ups[MID])
    assert plugin.ups[MID]["seen_dyn"] == ["1"]
    assert plugin.context.texts() == [], "老数据首次开动态也不能补推历史"


# ---------- 通知开关 ----------


async def test_notify_status_lists_every_flag(tmp):
    plugin = build(tmp)
    text = (await run(plugin.cmd_notify, admin()))[0]
    for name in main.NOTIFY_FLAGS:
        assert name in text, f"状态里少了 {name}"
    assert "配置文件" in text, text
    assert "⚪️ 动态" in text, "动态默认关，状态里要看得见面前是关的"


async def test_notify_query_only_does_not_change(tmp):
    """只写项目不写开关＝查询，不能顺手改状态。"""
    plugin = build(tmp)
    text = (await run(plugin.cmd_notify, admin(), "稿件"))[0]
    assert "🔔 通知开关" in text, text
    assert plugin._notify_over == {}


async def test_notify_switch_blocks_push(tmp):
    """关掉稿件后接口照查、基线照记，只是不推送。"""
    UPLOADS[MID] = ["BV1"]
    plugin = build(tmp)
    up = add_up(plugin)
    await check_up(plugin, up)
    await run(plugin.cmd_notify, admin(), "稿件", "关")
    UPLOADS[MID] = ["BV2", "BV1"]
    plugin.context.clear()
    await check_up(plugin, up)
    assert plugin.context.texts() == [], "关了还推就是没生效"
    assert up["seen"][0] == "BV2", "基线要跟着走，重新打开才不会补推历史"
    await run(plugin.cmd_notify, admin(), "稿件", "开")
    UPLOADS[MID] = ["BV3", "BV2", "BV1"]
    await check_up(plugin, up)
    assert "BV3" in last_text(plugin), "重新打开后应当恢复推送"


async def test_notify_override_survives_reload(tmp):
    """覆盖写进数据文件，重载插件后仍然是关的（配置文件那边没动）。"""
    plugin = build(tmp)
    await run(plugin.cmd_notify, admin(), "开播", "关")
    assert read_data(tmp)["notify_over"] == {"notify_start": False}
    again = build(tmp)
    assert again._flag("notify_start") is False
    text = (await run(again.cmd_notify, admin()))[0]
    assert "指令覆盖：关" in text, text


async def test_notify_default_follows_config(tmp):
    """默认＝删掉覆盖，回到配置文件；配置文件关着就是关。"""
    plugin = build(tmp, {"notify_end": False})
    await run(plugin.cmd_notify, admin(), "关播", "开")
    assert plugin._flag("notify_end") is True
    out = (await run(plugin.cmd_notify, admin(), "关播", "默认"))[0]
    assert "已恢复跟随配置文件（当前关）" in out, out
    assert plugin._flag("notify_end") is False
    assert read_data(tmp)["notify_over"] == {}


async def test_notify_all_and_aliases(tmp):
    plugin = build(tmp)
    await run(plugin.cmd_notify, admin(), "全部", "关")
    for key in main.NOTIFY_FLAGS.values():
        assert plugin._flag(key) is False, f"{key} 没跟着一起关"
    # 简写别名与 int 参数（AstrBot 会把纯数字参数转成 int）
    await run(plugin.cmd_notify, admin(), "投稿", 1)
    assert plugin._flag("notify_upload") is True
    await run(plugin.cmd_notify, admin(), "距离上次", "0")
    assert plugin._flag("show_elapsed") is False
    await run(plugin.cmd_notify, admin(), "更新提醒", "off")
    assert plugin._flag("update_check") is False


async def test_notify_rejects_bad_input(tmp):
    plugin = build(tmp)
    before = dict(plugin._notify_over)
    text = (await run(plugin.cmd_notify, admin(), "不存在的项", "开"))[0]
    assert "不认识的通知项目" in text, text
    assert "开播" in text, "报错时把可选项列出来"
    assert plugin._notify_over == before, "认不出的项目不该改任何状态"
    text = (await run(plugin.cmd_notify, admin(), "稿件", "也许"))[0]
    assert "没看懂开关" in text, text
    assert plugin._notify_over == before


async def test_notify_requires_admin(tmp):
    plugin = build(tmp, {"admin_ids": "1001"})
    text = (await run(plugin.cmd_notify, user(), "稿件", "关"))[0]
    assert "管理员" in text, text
    assert plugin._notify_over == {}


async def test_notify_dynamic_via_command_turns_on_fetch(tmp):
    """开关本身也能用指令打开，开了才会去请求动态接口。"""
    DYNAMICS[MID] = [dynamic_item("1")]
    plugin = build(tmp)
    up = add_up(plugin)
    await check_up(plugin, up)
    assert DYN_CALLS == []
    await run(plugin.cmd_notify, admin(), "动态", "开")
    await check_up(plugin, up)
    assert DYN_CALLS == [MID], "打开后应当开始查动态"
    assert up["seen_dyn"] == ["1"], "第一次只建基线"


async def test_notify_over_dirty_data_ignored(tmp):
    """手工写坏的开关项忽略并留日志，不能让未知键变成隐形开关。"""
    write_data(
        tmp,
        {
            "version": 5,
            "rooms": {},
            "ups": {},
            "notify_over": {"notify_start": False, "不存在": True, "notify_end": "yes"},
        },
    )
    plugin = build(tmp)
    assert plugin._notify_over == {"notify_start": False}
    assert "未知项" in "".join(msg for _, msg in plugin.logger.messages)
    assert plugin._flag("notify_end") is True, "值不是布尔就当没写过这条覆盖"


async def test_help_mentions_dynamic_and_notify(tmp):
    plugin = build(tmp)
    text = (await run(plugin.cmd_help, admin()))[0]
    assert "通知" in text and "开播" in text, "帮助里要能查到通知开关的用法"
    assert "动态" in text, "动态监控要在帮助里说明"


async def test_poll_round_checks_dynamics(tmp):
    """真跑一轮轮询：动态监控挂在UP主检查节奏上，一个UP主出错也不影响另一个。"""
    from stubs import _idle_poll

    DYNAMICS[MID] = [dynamic_item("1")]
    DYNAMICS[OTHER] = [dynamic_item("9")]
    plugin = build(tmp, {"notify_dynamic": True}, poll=True)
    for mid in (MID, OTHER):
        up = plugin._register_up(mid)
        up["name"] = f"UP主{mid}"
        up["watchers"] = {USER_UMO: "订阅者"}
    main.LiveMonitorPlugin._poll_loop = REAL_POLL
    task = asyncio.ensure_future(plugin._poll_loop())
    try:
        await asyncio.sleep(1.6)  # 一轮里两个UP主之间还要隔 1 秒
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await plugin.terminate()  # 顺手停掉 build(poll=True) 起的后台轮询
        main.LiveMonitorPlugin._poll_loop = _idle_poll
    assert set(DYN_CALLS) >= {MID, OTHER}, f"两个UP主都该被查到: {DYN_CALLS}"
    assert plugin.ups[MID]["seen_dyn"] == ["1"]


async def main_test():
    tests = [
        ("WBI签名口径", _sync(test_wbi_signature_shape)),
        ("WBI字符剔除", _sync(test_wbi_strips_unsafe_chars)),
        ("WBI密钥重排", _sync(test_wbi_mixin_key_len)),
        ("签名密钥缓存", _no_tmp(test_fetch_wbi_mixin_caches)),
        ("缺签名密钥报错", _no_tmp(test_fetch_wbi_mixin_missing_key)),
        ("buvid3补全与复用", _no_tmp(test_buvid_appended_to_cookie)),
        ("buvid3取不到不炸", _no_tmp(test_buvid_failure_not_fatal)),
        ("风控响应可读报错", _no_tmp(test_get_json_reports_risk_control_readably)),
        ("视频动态解析", _sync(test_parse_dynamic_archive)),
        ("图文与转发解析", _sync(test_parse_dynamic_opus_and_forward)),
        ("脏动态条目丢弃", _sync(test_parse_dynamic_dirty)),
        ("动态渲染不重复正文", _sync(test_format_dynamic_no_duplicate_body)),
        ("动态接口报错上抛", _no_tmp(test_fetch_dynamics_code_error)),
        ("假空列表重试", _no_tmp(test_fetch_dynamics_retries_empty)),
        ("一直空则交回空列表", _no_tmp(test_fetch_dynamics_gives_up_empty)),
        ("动态接口请求头口径", _no_tmp(test_fetch_dynamics_uses_dynamic_referer)),
        ("动态顺序与上限", _no_tmp(test_fetch_dynamics_orders_and_caps)),
        ("动态首轮建基线", test_dynamic_first_round_is_baseline),
        ("新动态推送", test_dynamic_new_item_pushed),
        ("视频只提醒一次", test_dynamic_video_deduped_with_uploads),
        ("稿件关掉时视频走动态", test_dynamic_video_pushed_when_upload_notify_off),
        ("多条动态合并", test_dynamic_merged_and_capped),
        ("动态历史封顶", test_dynamic_history_capped),
        ("空响应不清基线", test_dynamic_empty_response_keeps_baseline),
        ("开关关着不发请求", test_dynamic_switch_off_sends_no_request),
        ("距上次动态受开关控制", test_dynamic_elapsed_respects_show_elapsed),
        ("动态历史持久化与脏值", test_dynamic_seen_dyn_survives_reload_and_dirty),
        ("v4数据补动态字段", test_v4_data_gets_empty_dynamic_history),
        ("通知状态一览", test_notify_status_lists_every_flag),
        ("只写项目等于查询", test_notify_query_only_does_not_change),
        ("关稿件不再推送", test_notify_switch_blocks_push),
        ("覆盖活过重载", test_notify_override_survives_reload),
        ("恢复默认跟随配置", test_notify_default_follows_config),
        ("全部开关与简写", test_notify_all_and_aliases),
        ("非法输入不动状态", test_notify_rejects_bad_input),
        ("通知指令仅限管理员", test_notify_requires_admin),
        ("指令打开动态监控", test_notify_dynamic_via_command_turns_on_fetch),
        ("脏开关项忽略留痕", test_notify_over_dirty_data_ignored),
        ("帮助含动态与通知", test_help_mentions_dynamic_and_notify),
        ("轮询里查动态", test_poll_round_checks_dynamics),
    ]
    failed = 0
    for name, func in tests:
        DYNAMICS.clear()
        DYN_CALLS.clear()
        UPLOADS.clear()
        reset_api_cache()
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


def _sync(func):
    """把纯同步用例包成和异步用例一样的调用形状。"""

    async def wrapper(*args):
        return func()

    return wrapper


def _no_tmp(func):
    """不碰数据文件的用例：忽略 runner 传进来的临时目录。"""

    async def wrapper(_tmp):
        return await func()

    return wrapper


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_test()))
