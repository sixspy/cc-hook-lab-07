from __future__ import annotations
from app.db import 连接
from app.services.时间 import 此刻
from app.services import 投递, 熔断


def 作废(事件号: str, 原因: str = "人工作废") -> dict:
    t = 此刻()
    conn = 连接()
    try:
        dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone()
        if not dl:
            raise ValueError("不在死信匣")
        if dl["状态"] != "待处置":
            raise ValueError("死信已处置")
        conn.execute("UPDATE 死信 SET 状态=? WHERE 事件号=?", ("已作废", 事件号))
        conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("已作废", t, 事件号))
        conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                     (t, "死信作废", 事件号, 原因))
        conn.commit()
        return dict(conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone())
    finally:
        conn.close()


def 重投(事件号: str) -> dict:
    # 先取端点键，打同一把端点锁，与现行投递/熔断闸同临界区
    conn = 连接()
    try:
        dl0 = conn.execute("SELECT 端点键,状态 FROM 死信 WHERE 事件号=?", (事件号,)).fetchone()
        if not dl0:
            raise ValueError("不在死信匣")
        if dl0["状态"] != "待处置":
            raise ValueError("死信已处置")
        键 = dl0["端点键"]
    finally:
        conn.close()

    with 熔断.端点锁(键):
        t = 此刻()
        conn = 连接()
        try:
            dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone()
            if not dl:
                raise ValueError("不在死信匣")
            if dl["状态"] != "待处置":
                raise ValueError("死信已处置")
            ep = conn.execute("SELECT * FROM 投递端点 WHERE 端点键=?", (键,)).fetchone()
            闸 = 熔断.闸态(conn, 键, ep) if ep is not None else {"熔断": False}
            if 闸["熔断"]:
                # 路径一“拒熔断后回刷”：死信票保持待处置，不刷成已重投
                conn.execute(
                    "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                    (t, "熔断拒投死信重投", 事件号, f"{键} 闸来源:{闸['来源']}，请先复位熔断"),
                )
                conn.commit()
                raise ValueError(
                    f"端点 {键} 熔断中，已拒绝死信回刷重投（死信仍待处置）；请先在端点名册复位合闸"
                )
            conn.execute("UPDATE 死信 SET 状态=? WHERE 事件号=?", ("已重投", 事件号))
            conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("待投", t, 事件号))
            conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                         (t, "死信重投", 事件号, "回待投"))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return 投递.投递一次(事件号)


def 列表():
    from app.services import 签名目录
    return 签名目录.死信列表()
