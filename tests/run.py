"""一次跑完全部离线冒烟测试。

运行：python tests/run.py
"""

import asyncio
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SUITES = ("test_submission", "test_update_check")


async def run_all():
    status = 0
    for name in SUITES:
        suite = importlib.import_module(name)
        status |= await suite.main_test()
    return status


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_all()))
