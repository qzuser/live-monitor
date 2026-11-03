"""插件热更新（下载 -> 校验 -> 覆盖 -> 重载）的离线冒烟测试。

运行：python tests/test_self_update.py

全程不联网、不碰仓库里的 main.py：GitHub 请求与下载都替掉，插件目录指向临时目录
（stubs.build 已把 plugin_dir 挪进 tmp），假 Context 提供假的 StarMetadata 注册表
和假的 reload 管理器。桩模块见 stubs.py。
"""

import asyncio
import io
import json
import tempfile
import time
import zipfile
from pathlib import Path

from stubs import (  # noqa: E402
    ADMIN_UMO,
    USER_UMO,
    StarMeta,
    admin,
    build,
    main,
    run,
    user,
)

GOOD_MAIN = (
    "from astrbot.api.star import Star\n\n\nclass LiveMonitorPlugin(Star):\n    pass\n"
)
OLD_SCHEMA = '{"admin_ids": {"description": "管理员", "type": "string", "default": ""}}'
ASSET_URL = (
    "https://github.com/qzuser/live-monitor/releases/download/"
    "v0.3.0/live_monitor_v0.3.0.zip"
)


def plugin_files(version="0.3.0", name="live_monitor", main_src=GOOD_MAIN, schema=None):
    """一份合法的更新包内容。"""
    return {
        "main.py": main_src,
        "metadata.yaml": (
            f"name: {name}\n"
            "display_name: 直播开播/稿件监控\n"
            f"version: {version}\n"
            'repo: "https://github.com/qzuser/live-monitor"\n'
        ),
        "_conf_schema.json": schema if schema is not None else OLD_SCHEMA,
        "README.md": "# 文档\n",
    }


def zip_bytes(files, root="", extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, text in files.items():
            zf.writestr(f"{root}/{path}" if root else path, text)
        for path, text in (extra or {}).items():
            zf.writestr(path, text)
    return buf.getvalue()


def good_zip(**kwargs):
    return zip_bytes(plugin_files(**kwargs))


def release(tag, assets=None, **overrides):
    item = {
        "tag_name": tag,
        "published_at": "2026-11-03T08:00:00Z",
        "body": "- 新增热更新",
        "html_url": f"https://github.com/qzuser/live-monitor/releases/tag/{tag}",
        "draft": False,
        "prerelease": False,
    }
    if assets is not None:
        item["assets"] = assets
    item.update(overrides)
    return item


def asset(name, url, size=40_000):
    return {"name": name, "browser_download_url": url, "size": size}


def patch_update(plugin, data, releases=None):
    """替掉 GitHub Release 列表与更新包下载，返回记录下载调用地址的列表。"""
    if releases is None:
        releases = [
            release(
                "v0.3.0",
                assets=[asset("live_monitor_v0.3.0.zip", ASSET_URL)],
            )
        ]
    urls = []

    async def fake_releases(session):
        return releases

    async def fake_download(session, url):
        urls.append(url)
        return data

    plugin._fetch_releases = fake_releases
    plugin._download_update_archive = fake_download
    return urls


class _Body:
    def __init__(self, data):
        self._data = data

    def iter_chunked(self, size):
        async def gen():
            for i in range(0, len(self._data), size):
                yield self._data[i : i + size]

        return gen()


_UNSET = object()


class _Resp:
    def __init__(self, data, status=200, length=_UNSET):
        self.status = status
        # length 不传按正文长度报；显式传 None 模拟分块传输（响应头里没有长度）
        self.content_length = len(data) if length is _UNSET else length
        self.content = _Body(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _DlHttp:
    """假 aiohttp.ClientSession：记录请求地址并按预设内容返回。"""

    def __init__(self, data=b"", status=200, length=_UNSET):
        self.calls = []
        self.data = data
        self.status = status
        self.length = length

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Resp(self.data, self.status, self.length)


async def stop_task(task):
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_pick_update_asset_prefers_own_zip(tmp):
    zip_url = "https://codeload.github.com/qzuser/live-monitor/zip/refs/tags/v0.3.0"
    ours = asset("live_monitor_v0.3.0.zip", "https://github.com/x/zip/a.zip")
    picked = main.pick_update_asset(
        release("v0.3.0", assets=[asset("other.zip", zip_url), ours])
    )
    assert picked["name"] == "live_monitor_v0.3.0.zip", picked

    # 没有自研资产时退回 GitHub 自动源码包
    picked = main.pick_update_asset(release("v0.3.0", zipball_url=zip_url))
    assert picked["url"] == zip_url, picked

    # 超过体积上限的资产不能下
    assert (
        main.pick_update_asset(
            release(
                "v0.3.0",
                assets=[asset("live_monitor_v0.3.0.zip", "https://a/b.zip", size=9e8)],
            )
        )
        == {}
    )
    # 明文地址一律不用
    assert (
        main.pick_update_asset(
            release(
                "v0.3.0", assets=[asset("live_monitor_v0.3.0.zip", "http://a/b.zip")]
            )
        )
        == {}
    )
    assert main.pick_update_asset(release("v0.3.0")) == {}
    assert main.pick_update_asset(release("v0.3.0", assets="脏数据")) == {}


async def test_inspect_update_zip_accepts_both_layouts(tmp):
    info = main.inspect_update_zip(good_zip(), "live_monitor", "0.2.1")
    assert info["version"] == "0.3.0"
    assert set(info["files"]) == {
        "main.py",
        "metadata.yaml",
        "_conf_schema.json",
        "README.md",
    }
    # GitHub 源码包多一层 <repo>-<sha> 目录，同样要认
    nested = main.inspect_update_zip(
        zip_bytes(plugin_files(), root="live-monitor-abcdef0123"),
        "live_monitor",
        "0.2.1",
    )
    assert nested["version"] == "0.3.0"
    assert nested["files"]["main.py"].decode() == GOOD_MAIN
    # 没有 README 也照样能装（只要求三件必需文件）
    lean = zip_bytes({k: v for k, v in plugin_files().items() if k != "README.md"})
    assert (
        "README.md"
        not in main.inspect_update_zip(lean, "live_monitor", "0.2.1")["files"]
    )


async def test_inspect_update_zip_rejects_bad_packages(tmp):
    def reject(data, expect_name="live_monitor", cur="0.2.1"):
        try:
            main.inspect_update_zip(data, expect_name, cur)
        except ValueError as e:
            return str(e)
        raise AssertionError("这个包不该通过校验")

    assert "zip" in reject(b"not a zip at all")
    assert "缺少" in reject(zip_bytes({"main.py": GOOD_MAIN}))
    assert "非法路径" in reject(
        zip_bytes(plugin_files(), extra={"../../evil.txt": "x"})
    )
    assert "插件名" in reject(good_zip(name="another_plugin"))
    assert "不比当前版本" in reject(good_zip(version="0.2.1"))
    assert "不比当前版本" in reject(good_zip(version="0.1.0"))
    assert "缺少 main.py" in reject(
        zip_bytes({k: v for k, v in plugin_files().items() if k != "main.py"})
    )
    assert "语法" in reject(good_zip(main_src="def broken(:\n"))
    assert "主类" in reject(good_zip(main_src="x = 1\n"))
    assert "JSON" in reject(good_zip(schema="{不是json"))
    assert "UTF-8" in reject(
        zip_bytes({**plugin_files(), "metadata.yaml": b"\xff\xfe"})
    )
    assert "层级太深" in reject(zip_bytes(plugin_files(), root="a/b"))
    # 包里塞进来的多余文件不能被当成插件本体
    assert "缺少 main.py" in reject(
        zip_bytes({"junk.bin": "x"}, extra={"x/metadata.yaml": "y"})
    )


async def test_download_archive_caps_and_proxy(tmp):
    plugin = build(tmp)
    data = good_zip()

    http = _DlHttp(data)
    got = await plugin._download_update_archive(http, "https://github.com/a/b.zip")
    assert got == data
    assert http.calls[0][0] == "https://github.com/a/b.zip"
    assert http.calls[0][1]["headers"] is main.GITHUB_HEADERS

    # 加速前缀只接受 https
    plugin.conf["update_asset_proxy"] = "https://mirror.example.com"
    http = _DlHttp(data)
    await plugin._download_update_archive(http, "https://github.com/a/b.zip")
    assert http.calls[0][0] == "https://mirror.example.com/https://github.com/a/b.zip"
    plugin.conf["update_asset_proxy"] = "http://mirror.example.com"
    try:
        await plugin._download_update_archive(
            _DlHttp(data), "https://github.com/a/b.zip"
        )
    except ValueError as e:
        assert "https://" in str(e)
    else:
        raise AssertionError("明文前缀必须拒绝")
    plugin.conf["update_asset_proxy"] = ""

    # 响应头报超长 / 不报长度但实际超长，都要在下载阶段就断掉
    try:
        await plugin._download_update_archive(
            _DlHttp(data, length=main.MAX_UPDATE_BYTES + 1),
            "https://github.com/a/b.zip",
        )
    except ValueError as e:
        assert "过大" in str(e)
    else:
        raise AssertionError("超体积上限必须拒绝")
    big = b"PK" + b"x" * (main.MAX_UPDATE_BYTES + 10)
    try:
        await plugin._download_update_archive(
            _DlHttp(big, length=None), "https://github.com/a/b.zip"
        )
    except ValueError as e:
        assert "上限" in str(e)
    else:
        raise AssertionError("流式超限必须拒绝")
    try:
        await plugin._download_update_archive(_DlHttp(data, status=404), "https://x/y")
    except ValueError as e:
        assert "HTTP 404" in str(e)
    else:
        raise AssertionError("非 200 必须拒绝")


async def test_install_update_backups_then_writes(tmp):
    plugin = build(tmp)
    target = plugin.plugin_dir
    (target / "main.py").write_text("# 旧版\n", encoding="utf-8")
    (target / "metadata.yaml").write_text("name: live_monitor\nversion: 0.2.1\n")
    (target / "logo.png").write_bytes(b"keep-me")

    backup = plugin._install_update(
        {
            "main.py": GOOD_MAIN.encode(),
            "metadata.yaml": b"name: live_monitor\nversion: 0.3.0\n",
            "_conf_schema.json": b"{}",
        }
    )
    assert backup and Path(backup).is_dir()
    assert (target / "main.py").read_text(encoding="utf-8") == GOOD_MAIN
    assert "0.3.0" in (target / "metadata.yaml").read_text(encoding="utf-8")
    # 没在更新包里的文件（logo）不能被碰
    assert (target / "logo.png").read_bytes() == b"keep-me"
    # 备份里留着旧版，随时能盖回去
    assert (Path(backup) / "main.py").read_text(encoding="utf-8") == "# 旧版\n"
    assert not list(target.glob("*.update-tmp"))

    # 反复更新只保留最近 UPDATE_BACKUP_KEEP 份
    root = Path(tmp) / "backup"
    for idx in range(8):
        name = f"v0.2.{idx}-2026{11 - idx:02d}01-000000"
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "main.py").write_text("x")
    plugin._install_update({"main.py": b"new"})
    kept = sorted(p.name for p in root.iterdir() if p.is_dir())
    assert len(kept) == main.UPDATE_BACKUP_KEEP, kept


async def test_update_command_happy_path(tmp):
    plugin = build(tmp, conf={"admin_ids": "1001"})
    plugin.version = "0.2.1"
    plugin.meta_name = "live_monitor"
    for name in main.UPDATE_REQUIRED_FILES:
        (plugin.plugin_dir / name).write_text("# old\n", encoding="utf-8")
    manager = FakeManager()
    plugin.context._star_manager = manager
    urls = patch_update(plugin, good_zip())
    delay = main.RELOAD_DELAY
    main.RELOAD_DELAY = 0.05
    try:
        texts = await run(plugin.cmd_update, user())
        assert "仅限管理员" in texts[0], texts

        texts = await run(plugin.cmd_update, admin())
        assert len(texts) == 2, texts
        assert "⏬ 正在从" in texts[0]
        assert "✅ 已下载 v0.3.0 并覆盖" in texts[1], texts[1]
        assert "4 个文件" in texts[1], texts[1]
        assert "🗂 旧版备份：" in texts[1] and "秒后自动热重载" in texts[1]
        assert urls == [ASSET_URL], urls
        assert plugin._update_hint_version == "v0.3.0"
        assert (plugin.plugin_dir / "main.py").read_text(encoding="utf-8") == GOOD_MAIN

        marker = json.loads((Path(tmp) / main.PENDING_UPDATE_FILE).read_text("utf-8"))
        assert marker["from"] == "0.2.1" and marker["to"] == "0.3.0"
        assert marker["umo"] == ADMIN_UMO and marker["backup"]

        await asyncio.wait_for(main._reload_task, 5)
        assert manager.calls == ["live_monitor"], manager.calls
    finally:
        main.RELOAD_DELAY = delay
        await stop_task(main._reload_task)

    # 重载后的新实例（同一份假数据目录）在第一次轮询里回报结果
    reloaded = build(tmp)
    reloaded.version = "0.3.0"
    assert reloaded._pending_update and reloaded._pending_update["to"] == "0.3.0"
    await reloaded._confirm_update()
    sent = reloaded.context.texts(ADMIN_UMO)
    assert len(sent) == 1, sent
    assert "✅ 已更新到 v0.3.0 并完成热重载" in sent[0][1]
    assert "旧版备份" in sent[0][1]
    assert not (Path(tmp) / main.PENDING_UPDATE_FILE).exists()
    assert reloaded._pending_update is None


class FakeManager:
    """假的 star manager：只记 reload 被谁调用，返回 (成功, 错误)。"""

    def __init__(self, result=(True, None)):
        self.calls = []
        self.result = result

    async def reload(self, specified_plugin_name=None):
        self.calls.append(specified_plugin_name)
        return self.result


async def test_update_rejects_when_no_new_version(tmp):
    plugin = build(tmp)
    plugin.version = "0.3.0"
    patch_update(plugin, good_zip(version="0.3.0"))
    texts = await run(plugin.cmd_update, admin())
    assert "当前已经是 v0.3.0" in texts[1], texts
    assert not (plugin.plugin_dir / "main.py").exists()
    assert not (Path(tmp) / main.PENDING_UPDATE_FILE).exists()

    # 指名旧版本：热更新只往前装，回滚走备份目录
    patch_update(
        plugin,
        good_zip(version="0.1.0"),
        releases=[release("v0.3.0"), release("v0.1.0")],
    )
    texts = await run(plugin.cmd_update, admin(), "0.1.0")
    assert "比当前 v0.3.0 旧" in texts[1] and "备份目录" in texts[1], texts
    assert not (plugin.plugin_dir / "main.py").exists()


async def test_bad_zip_leaves_plugin_untouched(tmp):
    plugin = build(tmp)
    plugin.version = "0.2.1"
    for name in main.UPDATE_REQUIRED_FILES:
        (plugin.plugin_dir / name).write_text("# old\n", encoding="utf-8")
    patch_update(plugin, good_zip(name="other_plugin"))
    plugin.context._star_manager = FakeManager()
    texts = await run(plugin.cmd_update, admin())
    assert "校验未通过" in texts[1] and "插件名" in texts[1], texts
    assert (plugin.plugin_dir / "main.py").read_text(encoding="utf-8") == "# old\n"
    assert not (Path(tmp) / main.PENDING_UPDATE_FILE).exists()
    assert main._reload_task is None or main._reload_task.done()


async def test_self_update_gate_and_missing_meta(tmp):
    plugin = build(tmp, conf={"self_update": False})
    texts = await run(plugin.cmd_update, admin())
    assert "self_update 已关闭" in texts[0], texts

    plugin2 = build(tmp, conf={"self_update": True})
    plugin2._repo_slug = ""
    assert "metadata.yaml" in (await run(plugin2.cmd_update, admin()))[0]

    # 没有更新包 / 下载失败 都要如实回报，不能假装成功
    plugin3 = build(tmp)
    plugin3.version = "0.2.1"
    patch_update(plugin3, b"", releases=[release("v0.3.0")])
    assert "没有可下载的更新包" in (await run(plugin3.cmd_update, admin()))[1]

    plugin4 = build(tmp)
    plugin4.version = "0.2.1"

    async def boom(session, url):
        raise ValueError("下载更新包 HTTP 500")

    plugin4._fetch_releases = lambda session: _returns(
        [
            release(
                "v0.3.0", assets=[asset("live_monitor_v0.3.0.zip", "https://a/b.zip")]
            )
        ]
    )
    plugin4._download_update_archive = boom
    assert (
        "更新失败: 下载更新包 HTTP 500" in (await run(plugin4.cmd_update, admin()))[1]
    )


async def _returns(value):
    return value


async def test_reload_unavailable_still_reports_backup(tmp):
    """文件已经换成新版但没法自动重载时，必须把"手动去 WebUI 重载"讲清楚。"""
    plugin = build(tmp)
    plugin.version = "0.2.1"
    patch_update(plugin, good_zip())
    # 1) Context 没暴露热重载接口
    assert "没有向插件暴露热重载接口" in plugin._reload_blocker()
    plugin.context._star_manager = FakeManager()
    # 2) 插件名没在 AstrBot 注册 -> 不许调 reload（会退化成重载所有插件）
    plugin.context.stars = []
    assert "没有在 AstrBot 注册" in plugin._reload_blocker()
    plugin.context.stars = [StarMeta("live_monitor", plugin)]
    assert plugin._reload_blocker() == ""

    texts = await run(plugin.cmd_update, admin())
    assert "✅ 已下载" in texts[1]
    delay = main.RELOAD_DELAY
    main.RELOAD_DELAY = 0.05
    try:
        await asyncio.wait_for(main._reload_task, 5)
    finally:
        main.RELOAD_DELAY = delay
        await stop_task(main._reload_task)

    # 拿不到注册名时走 metadata.name 兜底
    plugin.context.stars = []
    assert plugin._plugin_name() == "live_monitor"


async def test_reload_failure_is_logged(tmp):
    plugin = build(tmp)
    plugin.version = "0.2.1"
    patch_update(plugin, good_zip())
    manager = FakeManager(result=(False, "插件导入失败"))
    plugin.context._star_manager = manager
    delay = main.RELOAD_DELAY
    main.RELOAD_DELAY = 0.05
    try:
        await run(plugin.cmd_update, admin())
        await asyncio.wait_for(main._reload_task, 5)
    finally:
        main.RELOAD_DELAY = delay
    assert "热重载失败: 插件导入失败" in plugin.logger.messages[-1][1]


async def test_confirm_update_reports_failed_reload(tmp):
    plugin = build(tmp)
    plugin.version = "0.2.1"  # 覆盖成功但载入的还是旧版
    (Path(tmp) / main.PENDING_UPDATE_FILE).write_text(
        json.dumps(
            {
                "from": "0.2.1",
                "to": "0.3.0",
                "umo": ADMIN_UMO,
                "backup": "C:/x/backup/v0.2.1-1",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    again = build(tmp)
    # 文件已经覆盖成新版，但载入的还是旧版（导入失败后 AstrBot 留在旧进程里）
    again.version = "0.2.1"
    assert again._pending_update["to"] == "0.3.0"
    await again._confirm_update()
    text = again.context.texts(ADMIN_UMO)[-1][1]
    assert "⚠️ 插件文件已覆盖，但没能载入新版本" in text, text
    assert "目标版本：v0.3.0" in text and "当前载入：v0.2.1" in text
    assert "旧版备份：C:/x/backup/v0.2.1-1" in text
    assert not (Path(tmp) / main.PENDING_UPDATE_FILE).exists()
    assert any("热更新未生效" in m for _, m in again.logger.messages)


async def test_pending_marker_robust_and_whitelisted(tmp):
    """标记文件坏了/缺字段不能拖垮启动；白名单外的会话不回确认。"""
    (Path(tmp) / main.PENDING_UPDATE_FILE).write_text("{不是 json", encoding="utf-8")
    plugin = build(tmp)
    assert plugin._pending_update is None
    assert "读取更新标记失败" in plugin.logger.messages[-1][1]

    (Path(tmp) / main.PENDING_UPDATE_FILE).write_text('{"umo": "x"}', encoding="utf-8")
    assert build(tmp)._pending_update is None  # 缺 to 的标记没有意义

    plugin = build(tmp, conf={"notify_whitelist": USER_UMO})
    plugin._pending_update = {"from": "0.2.1", "to": "0.3.0", "umo": ADMIN_UMO}
    await plugin._confirm_update()
    assert plugin.context.sent == []
    assert "跳过推送" in plugin.logger.messages[-1][1]

    # 标记只消费一次：确认过后状态清空，不会再回第二条
    assert plugin._pending_update is None
    assert not (Path(tmp) / main.PENDING_UPDATE_FILE).exists()


async def test_target_version_selection(tmp):
    plugin = build(tmp)
    releases = [
        release("v0.4.0-rc1"),
        release("v0.3.1"),
        release("v0.3.0", prerelease=True),
        release("v0.1.0", draft=True),
        "脏数据",
    ]
    assert plugin._pick_target(releases, "")["tag_name"] == "v0.3.1"
    assert plugin._pick_target(releases, "0.1.0") is None  # 草稿不给装
    assert plugin._pick_target(releases, "v0.3.0")["tag_name"] == "v0.3.0"
    assert plugin._pick_target(releases, "0.4.0-rc1")["tag_name"] == "v0.4.0-rc1"
    assert plugin._pick_target(releases, "9.9.9") is None


async def test_help_mentions_update(tmp):
    plugin = build(tmp)
    text = (await run(plugin.cmd_help, admin()))[0]
    assert "更新 [版本号]" in text
    assert "self_update" in text

    # 后台提醒里也要给出这条指令
    plugin.version = "0.2.1"
    plugin.rooms["bilibili:6"] = {
        "platform": "bilibili",
        "room_id": "6",
        "title": "",
        "anchor": "",
        "cover": "",
        "url": "",
        "live": False,
        "watchers": {ADMIN_UMO: "订阅者"},
        "fails": 0,
    }
    text = plugin._update_text(
        plugin._release_info(release("v0.3.0")), "🆕 直播监控插件可更新"
    )
    assert "/直播监控 更新" in text and "AstrBot WebUI" in text


async def test_pending_confirmed_from_poll_loop(tmp):
    """真跑一轮轮询：热重载后的确认消息要在第一次 _poll_once 里发出去。"""
    plugin = build(tmp, conf={"update_check": False}, poll=True)
    plugin._pending_update = {"from": "0.2.1", "to": "0.3.0", "umo": ADMIN_UMO}
    plugin.version = "0.3.0"
    task = main._poll_task
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not plugin.context.texts(ADMIN_UMO):
            await asyncio.sleep(0.05)
    finally:
        await stop_task(task)
    text = plugin.context.texts(ADMIN_UMO)[-1][1]
    assert "✅ 已更新到 v0.3.0" in text, text


async def main_test():
    tests = [
        ("更新包地址挑选", test_pick_update_asset_prefers_own_zip),
        ("校验接受平铺与源码包", test_inspect_update_zip_accepts_both_layouts),
        ("脏包一律拒绝", test_inspect_update_zip_rejects_bad_packages),
        ("下载体积与前缀把关", test_download_archive_caps_and_proxy),
        ("备份后原子覆盖", test_install_update_backups_then_writes),
        ("更新指令全流程", test_update_command_happy_path),
        ("无新版不覆盖", test_update_rejects_when_no_new_version),
        ("校验失败不动文件", test_bad_zip_leaves_plugin_untouched),
        ("开关与缺元信息", test_self_update_gate_and_missing_meta),
        ("无法自动重载要讲清", test_reload_unavailable_still_reports_backup),
        ("重载失败留日志", test_reload_failure_is_logged),
        ("重载未生效回执", test_confirm_update_reports_failed_reload),
        ("标记容错与白名单", test_pending_marker_robust_and_whitelisted),
        ("指定版本挑选", test_target_version_selection),
        ("帮助与提醒文案", test_help_mentions_update),
        ("轮询里回报结果", test_pending_confirmed_from_poll_loop),
    ]
    failed = 0
    for name, func in tests:
        with tempfile.TemporaryDirectory() as tmp:
            main._reload_task = None
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
