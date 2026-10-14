"""插件自动更新检查的离线冒烟测试。

运行：python tests/test_update_check.py

更新检查要访问 GitHub API，这里一律替掉 _fetch_releases 返回假 Release 列表；
只有一条用例故意不替（验证真实网络异常不会打断轮询）。桩模块见 stubs.py。
"""

import asyncio
import tempfile
import time

import aiohttp
from stubs import (
    ADMIN_UMO,
    USER_UMO,
    admin,
    build,
    main,
    run,
    user,
)


def release(tag, **overrides):
    item = {
        "tag_name": tag,
        "published_at": "2026-10-10T08:00:00Z",
        "body": "- 新增投稿审核\n- 修复纯数字房间号崩溃",
        "html_url": f"https://github.com/qzuser/live-monitor/releases/tag/{tag}",
        "draft": False,
        "prerelease": False,
    }
    item.update(overrides)
    return item


def patch_releases(plugin, payload):
    """替掉 GitHub 请求；payload 是异常对象时模拟请求失败。"""
    plugin.fetch_calls = 0

    async def fake(session):
        plugin.fetch_calls += 1
        if isinstance(payload, BaseException):
            raise payload
        return payload

    plugin._fetch_releases = fake


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


async def test_version_compare(tmp):
    assert main.version_is_newer("v0.1.10", "0.1.9") is True  # 不能按字符串比
    assert main.version_is_newer("0.2", "0.1.9") is True
    assert main.version_is_newer("0.1.2", "0.1.2") is False
    assert main.version_is_newer("0.1.1", "0.1.2") is False
    assert main.version_is_newer("1.0", "1.0.0") is False
    assert main.version_is_newer("", "0.1.2") is False
    assert main.version_is_newer("abc", "0.1.2") is False
    assert main.version_is_newer("0.1.3", "") is False
    assert main.parse_version("v1.2.3-rc1") == (1, 2, 3)
    assert main.parse_version(None) == ()


async def test_meta_comes_from_metadata(tmp):
    """版本号只在 metadata.yaml 维护一份，插件要读对并推出仓库地址。"""
    version, repo = main.read_plugin_meta()
    assert version, version
    plugin = build(tmp)
    assert plugin.version == version
    assert "live-monitor" in repo and "live-monitor" in plugin._repo_slug
    assert plugin._repo_page == f"https://github.com/{plugin._repo_slug}"
    assert plugin._next_update_check > time.monotonic()  # 启动后延迟首查
    assert plugin._update_hint_version == ""


async def test_pick_release_skips_non_stable(tmp):
    plugin = build(tmp)
    picked = plugin._pick_release(
        [
            release("v1.0.0", draft=True),
            release("v0.9.9", prerelease=True),
            release("v0.9.0-beta.2"),
            release("v0.8.0-rc1"),
            {"tag_name": ""},
            "脏数据",
            release("v0.6.0"),
        ]
    )
    assert picked["tag_name"] == "v0.6.0"
    assert plugin._pick_release([]) is None
    assert plugin._release_info({"tag_name": "v0.5.0"}) == {
        "version": "v0.5.0",
        "published_at": "",
        "body": "",
        "url": "",
    }
    assert (
        plugin._release_info(release("v0.5.0", published_at="2026-01-02T03:04:05Z"))[
            "published_at"
        ]
        == "2026-01-02"
    )


async def test_background_hint_once_per_version(tmp):
    plugin = build(tmp)
    plugin.version = "0.1.2"
    await run(plugin.cmd_review_on, admin())
    patch_releases(plugin, [release("v0.2.0")])
    async with aiohttp.ClientSession() as session:
        await plugin._check_plugin_update(session, time.monotonic())
    delivered = plugin.context.texts(ADMIN_UMO)
    assert len(delivered) == 1, delivered
    text = delivered[0][1]
    assert "🆕 直播监控插件可更新" in text
    assert "当前版本：v0.1.2" in text and "最新版本：v0.2.0" in text
    assert "发布于：2026-10-10" in text
    assert f"{plugin._repo_page}/releases" in text
    assert "新增投稿审核" in text and "AstrBot WebUI" in text
    assert plugin._update_hint_version == "v0.2.0"
    assert plugin.fetch_calls == 1

    # 同一版本不重复刷屏（仍会查一次，但不再推送）
    async with aiohttp.ClientSession() as session:
        await plugin._check_plugin_update(session, time.monotonic())
    assert len(plugin.context.texts(ADMIN_UMO)) == 1
    assert plugin.fetch_calls == 2

    patch_releases(plugin, [release("v0.1.2")])
    async with aiohttp.ClientSession() as session:
        await plugin._check_plugin_update(session, time.monotonic())
    assert len(plugin.context.texts(ADMIN_UMO)) == 1
    assert plugin._update_hint_version == "v0.2.0"


async def test_check_schedule_and_missing_meta(tmp):
    plugin = build(tmp, conf={"update_check_interval_hours": 6})
    plugin.version = "0.1.2"
    now = 1_000.0
    patch_releases(plugin, [release("v0.1.2")])
    async with aiohttp.ClientSession() as session:
        await plugin._check_plugin_update(session, now)
    assert plugin._next_update_check == now + 6 * 3600

    plugin.conf["update_check_interval_hours"] = 0  # 非法值回落默认 24 小时
    async with aiohttp.ClientSession() as session:
        await plugin._check_plugin_update(session, now)
    assert plugin._next_update_check == now + 24 * 3600

    plugin.version = ""
    async with aiohttp.ClientSession() as session:
        await plugin._check_plugin_update(session, now)
    assert "缺少 version 或 repo" in plugin.logger.messages[-1][1]
    assert plugin.fetch_calls == 2  # 缺元信息时不再发请求


async def test_no_reviewers_and_failures_are_logged(tmp):
    """没审核会话/请求失败都不该抛异常，但要在日志里留痕。"""
    plugin = build(tmp)
    plugin.version = "0.1.2"
    patch_releases(plugin, [release("v0.3.0")])
    async with aiohttp.ClientSession() as session:
        await plugin._check_plugin_update(session, time.monotonic())
    assert plugin.context.sent == []
    assert "没有审核会话可提醒" in plugin.logger.messages[-1][1]
    assert "/直播监控 检查更新" in plugin.logger.messages[-1][1]

    patch_releases(plugin, main.RoomCheckError("GitHub API HTTP 403"))
    async with aiohttp.ClientSession() as session:
        await plugin._check_plugin_update(session, time.monotonic())
    assert "检查插件更新失败" in plugin.logger.messages[-1][1]

    patch_releases(plugin, [])
    async with aiohttp.ClientSession() as session:
        await plugin._check_plugin_update(session, time.monotonic())
    assert "没有可用的正式版 Release" in plugin.logger.messages[-1][1]


async def test_check_update_command(tmp):
    plugin = build(tmp, conf={"admin_ids": "1001"})
    plugin.version = "0.1.2"
    assert "仅限管理员" in (await run(plugin.cmd_check_update, user()))[0]

    patch_releases(plugin, [release("v0.2.0")])
    texts = await run(plugin.cmd_check_update, admin())
    assert "🆕 发现新版本" in texts[0], texts
    assert "当前版本：v0.1.2" in texts[0]
    assert plugin._update_hint_version == "v0.2.0"
    assert plugin.context.texts(ADMIN_UMO) == []  # 只回本会话，不额外推送

    patch_releases(plugin, [release("v0.1.2")])
    assert "已是最新版本" in (await run(plugin.cmd_check_update, admin()))[0]

    patch_releases(plugin, [])
    assert "还没有正式版 Release" in (await run(plugin.cmd_check_update, admin()))[0]

    patch_releases(plugin, RuntimeError("网络不可达"))
    assert "检查更新失败" in (await run(plugin.cmd_check_update, admin()))[0]

    plugin._repo_slug = ""
    assert "metadata.yaml" in (await run(plugin.cmd_check_update, admin()))[0]

    assert "检查更新" in (await run(plugin.cmd_help, admin()))[0]


async def test_update_check_config_gate(tmp):
    """update_check 关掉后轮询循环不该再触发检查，开着时只触发一次。"""
    for enabled, expected in ((True, 1), (False, 0)):
        plugin = build(tmp, conf={"update_check": enabled}, poll=True)
        calls = []

        async def spy(session, now):
            calls.append(now)

        plugin._check_plugin_update = spy
        plugin._next_update_check = 0.0
        task = main._poll_task
        try:
            await asyncio.wait_for(asyncio.shield(task), 1)
        except (TimeoutError, asyncio.CancelledError):
            pass
        await stop_poll(task)
        assert len(calls) == expected, (enabled, calls)


async def test_failed_check_does_not_stop_polling(tmp):
    """检查更新真的连不上 GitHub 时不能带崩轮询，开播通知要照常发。"""
    plugin = build(tmp, conf={"update_check": True}, poll=True)
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
    plugin._next_update_check = 0.0
    task = main._poll_task
    try:
        assert await wait_until(
            lambda: (
                any("检查插件更新失败" in m for _, m in plugin.logger.messages)
                and any("开播通知" in t for _, t in plugin.context.texts(USER_UMO))
            )
        ), (plugin.logger.messages, plugin.context.texts(USER_UMO))
        assert not task.done()
    finally:
        await stop_poll(task)


async def main_test():
    tests = [
        ("版本号比较", test_version_compare),
        ("版本取自 metadata.yaml", test_meta_comes_from_metadata),
        ("跳过草稿与预发布", test_pick_release_skips_non_stable),
        ("后台提醒每版一次", test_background_hint_once_per_version),
        ("检查间隔与缺元信息", test_check_schedule_and_missing_meta),
        ("无审核会话/失败留痕", test_no_reviewers_and_failures_are_logged),
        ("检查更新指令", test_check_update_command),
        ("update_check 开关", test_update_check_config_gate),
        ("检查失败不影响轮询", test_failed_check_does_not_stop_polling),
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
    raise SystemExit(asyncio.run(main_test()))
