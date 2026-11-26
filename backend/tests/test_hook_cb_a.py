"""hook-07 失败测甲（服务：投递服务）。

事故：端点连续强制失败已打满熔断阈，基线「端点熔断」空桩未接线，
熔断后再投正常票仍返回成功并新增一条 状态='成功' 的投递尝试（成功壳）。

本测在改主干前必须红：after == before + 1（熔断后仍增成功行）。
救绿归属服务：app/services/投递.py（读熔断态与写尝试同一把端点锁、同一事务）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_DIR = tempfile.mkdtemp(prefix="hook_cb_a_")
os.environ["数据目录"] = _DIR
os.environ["夹具目录"] = str(_BACKEND.parent / "fixtures")

import pytest  # noqa: E402

from app import seed  # noqa: E402
import app.db as db  # noqa: E402
from app.services import 端点, 入队, 投递, 签名目录  # noqa: E402


@pytest.fixture(autouse=True)
def _独立库(monkeypatch):
    monkeypatch.setattr(db, "数据目录", Path(_DIR))
    monkeypatch.setattr(db, "库路径", Path(_DIR) / "cb_a.db")
    seed.初始化库()
    yield


def _新端点(阈值: int = 3) -> str:
    键 = "ep-a-" + uuid.uuid4().hex[:8]
    端点.登记({
        "端点键": 键,
        "名称": "甲测端点",
        "回调URL": f"https://a.local/{键}",
        "密钥版本": "v1",
    })
    签名目录.改签名规({"熔断阈值": str(阈值)})
    return 键


def _成功数(键: str) -> int:
    return sum(
        1 for r in 签名目录.投递列表()
        if r["端点键"] == 键 and r["状态"] == "成功"
    )


def _打满熔断(键: str, 阈值: int = 3) -> list[str]:
    ids = []
    for i in range(阈值):
        ev = 入队.建事件({
            "事件类型": f"cb{i}.fail",
            "端点键": 键,
            "载荷摘要": f"force-fail-{i}",
        })
        rep = 投递.投递一次(ev["事件号"], 强制失败=True)
        assert rep["状态"] == "失败"
        ids.append(ev["事件号"])
    return ids


def test_甲_夹具事故_熔断后成功尝试不得再增():
    """连续强制失败打满阈后再投：成功行数必须被闸住。"""
    # 夹具必须先在，且确实描述了“打满后再投”的事故剧本
    夹具 = __import__("app.services.夹具", fromlist=["读取"]).读取("熔断事故样例")
    assert 夹具["熔断阈值"] >= 1
    assert len(夹具["事件"]) >= 夹具["熔断阈值"] + 1

    键 = _新端点(阈值=3)
    _打满熔断(键, 3)

    after_ev = 入队.建事件({
        "事件类型": "invoice.paid",
        "端点键": 键,
        "载荷摘要": "after-trip-success-must-not-land",
    })
    before = _成功数(键)
    rep = 投递.投递一次(after_ev["事件号"])  # 不强制失败：基线会成功
    after = _成功数(键)

    assert after == before, (
        f"端点已熔断，熔断后仍增成功行：before={before} after={after} rep={rep}"
    )
    assert rep.get("状态") != "成功", f"熔断后不得返回成功壳：{rep}"
    assert rep.get("熔断") is True or "熔断" in str(rep.get("状态", "")), (
        f"返回必须带熔断/旁路命运：{rep}"
    )


def test_甲_熔断旁路票可在复位后投递成功():
    """旁路不是吞票：复位合闸后，原被旁路事件可重新投递成功，主链可续。"""
    键 = _新端点(阈值=2)
    _打满熔断(键, 2)
    ev = 入队.建事件({"事件类型": "invoice.ok", "端点键": 键, "载荷摘要": "held"})
    blocked = 投递.投递一次(ev["事件号"])
    assert blocked.get("状态") != "成功"

    复位 = getattr(端点, "熔断复位", None)
    assert callable(复位), "端点服务缺少 熔断复位 接线"
    复位(键)

    ok = 投递.投递一次(ev["事件号"])
    assert ok["状态"] == "成功"
    assert 签名目录.事件一条(ev["事件号"])["状态"] == "已投递"
