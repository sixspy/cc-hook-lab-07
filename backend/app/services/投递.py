from __future__ import annotations
import hashlib
import hmac
import uuid

from app.db import 连接
from app.services.时间 import 此刻
from app.services import 熔断


def _签名(密钥版本: str, 事件号: str, 摘要: str) -> str:
    raw = f"{密钥版本}|{事件号}|{摘要}".encode("utf-8")
    key = f"hook-secret-{密钥版本}".encode("utf-8")
    return hmac.new(key, raw, hashlib.sha256).hexdigest()[:24]


def _端点键Of(事件号: str) -> str:
    """锁前先定位端点键，以便打“同一端点同一把锁”。事件不存在在此即拒。"""
    conn = 连接()
    try:
        row = conn.execute("SELECT 端点键 FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
        if not row:
            raise ValueError("无此事件")
        return str(row["端点键"])
    finally:
        conn.close()


def 投递一次(事件号: str, 强制失败: bool = False) -> dict:
    """对事件执行一次签名投递。

    临界区 = 端点同一把 RLock + BEGIN IMMEDIATE 事务：
    在同一事务内“读熔断闸 → 写尝试/事件/熔断行”，读闸与写尝试同成败。
    熔断后不打端点、不写成功壳，改写一条 状态='熔断旁路' 的流水（中文旁路），
    事件置 '熔断旁路'，仍可在复位后由投递台/批量重试/死信重投续投。
    """
    端点键 = _端点键Of(事件号)
    with 熔断.端点锁(端点键):
        return _投递临界(事件号, bool(强制失败))


def _投递临界(事件号: str, 强制失败: bool) -> dict:
    t = 此刻()
    尝试号 = f"TRY-{uuid.uuid4().hex[:10]}"
    conn = 连接()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ev = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
        if not ev:
            raise ValueError("无此事件")
        if ev["状态"] in ("已投递", "死信"):
            raise ValueError(f"事件状态为{ev['状态']}，不可再投")

        键 = ev["端点键"]
        ep = conn.execute("SELECT * FROM 投递端点 WHERE 端点键=?", (键,)).fetchone()
        if not ep:
            raise ValueError("端点缺失")
        if int(ep["启用"]) != 1:
            raise ValueError("端点停用")

        阈 = 熔断.读阈值(conn)
        规 = conn.execute("SELECT 值 FROM 签名规 WHERE 键='最大重试'").fetchone()
        最大 = int(规["值"]) if 规 else 3
        sig = _签名(ep["密钥版本"], 事件号, ev["载荷摘要"])

        闸 = 熔断.闸态(conn, 键, ep)
        if 闸["熔断"]:
            连续 = int(闸["行"]["连续失败"]) if 闸["行"] is not None and "连续失败" in 闸["行"].keys() else 阈
            说明 = (
                f"端点 {键} 连续失败已达熔断阈（{连续}/{阈}，闸来源:{闸['来源']}），"
                "现行投递旁路拒投：未打端点、未写成功壳；请在端点名册核熔断灯/指纹后复位再投。"
            )
            conn.execute(
                "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (尝试号, 事件号, 键, sig, "熔断旁路", 503, 说明, t),
            )
            conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("熔断旁路", t, 事件号))
            conn.execute(
                "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                (t, "熔断旁路", 事件号, f"{键} {尝试号}"),
            )
            conn.commit()
            return {
                "事件号": 事件号,
                "尝试号": 尝试号, "状态": "熔断旁路", "事件状态": "熔断旁路",
                "熔断": True, "旁路": True, "响应码": 503, "签名摘要": sig, "说明": 说明,
            }

        失败次数 = conn.execute(
            "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 事件号=? AND 状态='失败'", (事件号,)
        ).fetchone()["c"]
        成功 = (not 强制失败) and (失败次数 < 1000)
        if 强制失败 or str(ev["事件类型"]).endswith(".fail"):
            成功 = False

        if 成功:
            conn.execute(
                "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (尝试号, 事件号, 键, sig, "成功", 200, "投递成功", t),
            )
            conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("已投递", t, 事件号))
            熔断.记成功复位(conn, 端点键=键, t=t, 阈值=阈)
            conn.execute(
                "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                (t, "投递成功", 事件号, 尝试号),
            )
            conn.commit()
            return {"事件号": 事件号, "尝试号": 尝试号, "状态": "成功", "事件状态": "已投递", "签名摘要": sig}

        # 失败
        cur = conn.execute(
            "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (尝试号, 事件号, 键, sig, "失败", 500, "投递失败（模拟连续强制失败）", t),
        )
        新失败 = 失败次数 + 1
        trip = 熔断.记失败并可能熔断(
            conn, 端点键=键, ep=ep, 阈值=阈, t=t, 尝试号=尝试号, 尝试行id=cur.lastrowid,
        )

        if 新失败 >= 最大:
            dl = f"DL-{uuid.uuid4().hex[:10]}"
            if not conn.execute("SELECT 1 FROM 死信 WHERE 事件号=?", (事件号,)).fetchone():
                conn.execute(
                    "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (dl, 事件号, 键, 新失败, "重试耗尽", "待处置", t),
                )
            conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("死信", t, 事件号))
            事件状态 = "死信"
        else:
            conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("投递中", t, 事件号))
            事件状态 = "投递中"

        详情 = f"失败{新失败}/{最大}；端点连续{trip['连续失败']}/{阈}"
        if trip["熔断"]:
            详情 += "；端点熔断落下并物化指纹封条"
        conn.execute(
            "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
            (t, "投递失败", 事件号, 详情),
        )
        conn.commit()
        if trip["封条载荷"]:
            try:
                熔断.写封条(键, trip["封条载荷"])
            except OSError:
                # 数据库熔断行已落下，闸仍拒投；封条落盘异常不吞投递结果
                pass
        return {
            "事件号": 事件号,
            "尝试号": 尝试号, "状态": "失败", "事件状态": 事件状态,
            "失败次数": 新失败, "签名摘要": sig,
            "端点连续失败": trip["连续失败"], "熔断": bool(trip["熔断"]),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
