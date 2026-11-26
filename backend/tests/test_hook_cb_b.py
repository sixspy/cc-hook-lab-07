"""hook-07 失败测乙（服务：端点服务 / 端点名册对账）。

事故：端点名册基线只有“启用”口径，熔断后仍混进成功投递（回刷/影子正式）时，
名册对账仍把该端点当成健康（假绿）。

本测在改主干前必须红：基线没有 名册对账，回退到现行“启用即健康”口径，
启用=1 于是 现行健康=True，断言其为 False 失败——正是“把熔断后仍投当成健康”。
救绿归属服务：app/services/端点.py 的 名册对账（联查 端点熔断 + 投递尝试）。
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

_DIR = tempfile.mkdtemp(prefix="hook_cb_b_")
os.environ["数据目录"] = _DIR
os.environ["夹具目录"] = str(_BACKEND.parent / "fixtures")

import pytest  # noqa: E402

from app import seed  # noqa: E402
import app.db as db  # noqa: E402
from app.services import 端点, 入队, 投递, 签名目录  # noqa: E402
from app.services.时间 import 此刻  # noqa: E402


@pytest.fixture(autouse=True)
def _独立库(monkeypatch):
    monkeypatch.setattr(db, "数据目录", Path(_DIR))
    monkeypatch.setattr(db, "库路径", Path(_DIR) / "cb_b.db")
    seed.初始化库()
    yield


def _事故端点(阈值: int = 3) -> str:
    键 = "ep-b-" + uuid.uuid4().hex[:8]
    端点.登记({
        "端点键": 键,
        "名称": "乙测端点",
        "回调URL": f"https://b.local/{键}",
        "密钥版本": "v2",
    })
    签名目录.改签名规({"熔断阈值": str(阈值)})
    for i in range(阈值):
        ev = 入队.建事件({
            "事件类型": f"cb{i}.fail",
            "端点键": 键,
            "载荷摘要": f"b-fail-{i}",
        })
        r = 投递.投递一次(ev["事件号"], 强制失败=True)
        assert r["状态"] == "失败"
    return 键


def _塞一条熔断后成功回刷(键: str) -> str:
    """绕过新投递闸，直接落一条‘熔断后成功壳’，模拟回刷/影子正式同票混写。"""
    ev = 入队.建事件({"事件类型": "shadow.flush", "端点键": 键, "载荷摘要": "backflush"})
    尝试号 = "TRY-BACKFLUSH-" + uuid.uuid4().hex[:10]
    conn = db.连接()
    try:
        conn.execute(
            "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (尝试号, ev["事件号"], 键, "sig-backflush", "成功", 200,
             "熔断后回刷成功壳（假绿）", 此刻()),
        )
        conn.commit()
    finally:
        conn.close()
    return 尝试号


def test_乙_名册对账必须把熔断后仍投判为不健康():
    键 = _事故端点(3)
    票号 = _塞一条熔断后成功回刷(键)

    对账 = getattr(端点, "名册对账", None)
    if not callable(对账):
        # —— 基线路径：现行端点名册只看启用位，启用即被当成健康 ——
        row = next(r for r in 签名目录.端点列表() if r["端点键"] == 键)
        现行健康 = int(row["启用"]) == 1
        assert 现行健康 is False, (
            "端点名册对账缺少熔断口径，把熔断后仍投（票 %s）当成健康（假绿）" % 票号
        )
        return

    rep = 对账()
    assert isinstance(rep, dict) and "端点" in rep, "名册对账必须返回端点明细"
    ep = next(x for x in rep["端点"] if x["端点键"] == 键)

    assert ep.get("熔断") is True, f"该端点应处于熔断态：{ep}"
    assert int(ep.get("熔断后成功数", 0)) >= 1, "联查必须数出熔断后成功回刷行"
    assert ep.get("健康") is False, f"熔断后仍有成功投递，不得判健康：{ep}"
    assert rep.get("对齐") is False, "存在假绿回刷时，名册对账总体必须不对齐"


def test_乙_干净熔断端点判为熔断红灯而非健康假绿():
    键 = _事故端点(2)  # 不塞回刷
    对账 = getattr(端点, "名册对账", None)
    assert callable(对账), "端点服务缺少 名册对账 接线"
    rep = 对账()
    ep = next(x for x in rep["端点"] if x["端点键"] == 键)
    assert ep.get("熔断") is True
    assert int(ep.get("熔断后成功数", 0)) == 0
    assert ep.get("灯") in ("红", "熔断") or ep.get("健康") is False
