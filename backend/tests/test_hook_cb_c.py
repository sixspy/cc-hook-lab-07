"""hook-07 争用失败测丙（服务：端点熔断同一把锁 + 投递临界区）。

事故推理：读熔断态与写尝试必须同成败。熔断落下的瞬间，投递若缺同一把锁，
两个并发请求会同时穿过检查、各写一条成功壳（双成）。

本测在改主干前必须红：
  1) app.services.熔断 尚未接线（无 端点锁）；
  2) 熔断后并发两投在基线双双 状态='成功'（len(成)==2）。
救绿归属服务：app/services/熔断.py 的按端点同锁 + app/services/投递.py 临界区。
收尾另有 scripts/并发双请求.py 对活服务打同一把端点锁。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_DIR = tempfile.mkdtemp(prefix="hook_cb_c_")
os.environ["数据目录"] = _DIR
os.environ["夹具目录"] = str(_BACKEND.parent / "fixtures")

import pytest  # noqa: E402

from app import seed  # noqa: E402
import app.db as db  # noqa: E402
from app.services import 端点, 入队, 投递, 签名目录  # noqa: E402

try:  # 基线此模块不存在
    from app.services import 熔断  # type: ignore
except Exception:  # pragma: no cover - 红测阶段
    熔断 = None  # type: ignore


@pytest.fixture(autouse=True)
def _独立库(monkeypatch):
    monkeypatch.setattr(db, "数据目录", Path(_DIR))
    monkeypatch.setattr(db, "库路径", Path(_DIR) / "cb_c.db")
    seed.初始化库()
    yield


def _熔断端点(阈值: int = 3) -> str:
    键 = "ep-c-" + uuid.uuid4().hex[:8]
    端点.登记({
        "端点键": 键,
        "名称": "丙测端点",
        "回调URL": f"https://c.local/{键}",
        "密钥版本": "v1",
    })
    签名目录.改签名规({"熔断阈值": str(阈值)})
    for i in range(阈值):
        ev = 入队.建事件({
            "事件类型": f"c{i}.fail",
            "端点键": 键,
            "载荷摘要": f"c-fail-{i}",
        })
        r = 投递.投递一次(ev["事件号"], 强制失败=True)
        assert r["状态"] == "失败"
    return 键


def _成功数(键: str) -> int:
    return sum(
        1 for r in 签名目录.投递列表()
        if r["端点键"] == 键 and r["状态"] == "成功"
    )


def test_丙_同一端点必须是同一把锁且互斥():
    assert 熔断 is not None and hasattr(熔断, "端点锁"), "熔断服务/端点锁尚未接线"
    la = 熔断.端点锁("ep-c-same-lock")
    lb = 熔断.端点锁("ep-c-same-lock")
    assert la is lb, "同一端点键必须返回同一把锁（收尾双请求要打同锁）"
    lc = 熔断.端点锁("ep-c-other-lock")
    assert lc is not la, "不同端点应使用不同锁，避免无谓互斥"

    # 互斥性：被持有时，另一线程/请求不得立即拿到同一把锁
    got = threading.Event()
    release = threading.Event()

    def _hold():
        with 熔断.端点锁("ep-c-mutex"):
            got.set()
            release.wait(timeout=2)

    t = threading.Thread(target=_hold)
    t.start()
    assert got.wait(timeout=2), "持锁线程应已进入临界区"
    main_lock = 熔断.端点锁("ep-c-mutex")
    assert main_lock.acquire(timeout=0.3) is False, "同一把锁必须互斥"
    release.set()
    t.join(timeout=2)
    assert not t.is_alive()
    assert main_lock.acquire(timeout=2) is True
    main_lock.release()


def test_丙_熔断后双请求并发不得双成():
    键 = _熔断端点(3)
    e1 = 入队.建事件({"事件类型": "invoice.a", "端点键": 键, "载荷摘要": "pa"})
    e2 = 入队.建事件({"事件类型": "invoice.b", "端点键": 键, "载荷摘要": "pb"})

    before = _成功数(键)
    barrier = threading.Barrier(2)
    outs: list[dict] = []
    errs: list[BaseException] = []

    def _work(eid: str):
        barrier.wait(timeout=5)  # 尽量同时撞临界区
        try:
            outs.append(投递.投递一次(eid))
        except BaseException as e:  # noqa: BLE001
            errs.append(e)

    t1 = threading.Thread(target=_work, args=(e1["事件号"],))
    t2 = threading.Thread(target=_work, args=(e2["事件号"],))
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive(), "并发投递线程超时未结束"

    成功返回 = [r for r in outs if r.get("状态") == "成功"]
    after = _成功数(键)

    assert len(成功返回) < 2, f"熔断瞬间双请求双成：outs={outs} errs={errs!r}"
    assert after - before == 0, f"熔断后成功行增量必须为0，实际 +{after - before}"
    assert len(outs) + len(errs) == 2, "两个请求都必须有明确下落（旁路结果或拒绝）"
