from __future__ import annotations

from app.db import 连接
from app.services import 校验
from app.services.时间 import 此刻
from app.services import 熔断


def 登记(body: dict) -> dict:
    端点键 = 校验.非空("端点键", body.get("端点键"))
    名称 = 校验.非空("名称", body.get("名称"))
    url = 校验.非空("回调URL", body.get("回调URL"))
    版本 = str(body.get("密钥版本") or "v1")
    说明 = str(body.get("说明") or "")
    conn = 连接()
    try:
        if conn.execute("SELECT 1 FROM 投递端点 WHERE 端点键=?", (端点键,)).fetchone():
            raise ValueError("端点键已存在")
        conn.execute(
            "INSERT INTO 投递端点(端点键,名称,回调URL,密钥版本,启用,说明) VALUES (?,?,?,?,1,?)",
            (端点键, 名称, url, 版本, 说明),
        )
        conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                     (此刻(), "登记端点", "", 端点键))
        conn.commit()
        return dict(conn.execute("SELECT * FROM 投递端点 WHERE 端点键=?", (端点键,)).fetchone())
    finally:
        conn.close()


def 改启用(端点键: str, 启用: bool) -> dict:
    conn = 连接()
    try:
        if not conn.execute("SELECT 1 FROM 投递端点 WHERE 端点键=?", (端点键,)).fetchone():
            raise ValueError("无此端点")
        conn.execute("UPDATE 投递端点 SET 启用=? WHERE 端点键=?", (1 if 启用 else 0, 端点键))
        conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                     (此刻(), "启用端点" if 启用 else "停用端点", "", 端点键))
        conn.commit()
        return dict(conn.execute("SELECT * FROM 投递端点 WHERE 端点键=?", (端点键,)).fetchone())
    finally:
        conn.close()


def 熔断复位(端点键: str) -> dict:
    """人工复核后合闸：库熔断行归正常 + 物化封条改写为已复位（保留指纹留痕）。"""
    conn = 连接()
    try:
        ep = conn.execute("SELECT * FROM 投递端点 WHERE 端点键=?", (端点键,)).fetchone()
        if not ep:
            raise ValueError("无此端点")
        with 熔断.端点锁(端点键):
            t = 此刻()
            row = conn.execute("SELECT 端点键 FROM 端点熔断 WHERE 端点键=?", (端点键,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO 端点熔断(端点键,状态,灯,连续失败,阈值,指纹,封条路径,熔断时,"
                    "熔断尝试号,熔断行id,复位时,更新时) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (端点键, "正常", "绿", 0, 熔断.默认阈值, "", "", "", 0, t, t),
                )
            else:
                conn.execute(
                    "UPDATE 端点熔断 SET 状态='正常', 灯='绿', 连续失败=0, 复位时=?, 更新时=? "
                    "WHERE 端点键=?",
                    (t, t, 端点键),
                )
            conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                         (t, "熔断复位", "", f"{端点键} 人工合闸"))
            conn.commit()
        try:
            熔断.复位封条(端点键)
        except OSError:
            pass
        return 名册一条(端点键)
    finally:
        conn.close()


def _对账明细(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT e.*,
               c.状态   AS 熔断状态,
               c.灯     AS 库灯,
               c.连续失败 AS 熔断连续失败,
               c.阈值   AS 熔断阈值数,
               c.指纹   AS 熔断指纹,
               c.封条路径 AS 封条文件名,
               c.熔断时 AS 熔断时,
               c.熔断尝试号 AS 熔断尝试号,
               c.熔断行id AS 熔断行id,
               c.复位时 AS 复位时
          FROM 投递端点 e
          LEFT JOIN 端点熔断 c ON c.端点键 = e.端点键
         ORDER BY e.端点键
        """
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        键 = d["端点键"]
        seal = 熔断.读封条(键)
        行id = int(d.get("熔断行id") or 0)
        库熔断 = str(d.get("熔断状态") or "") == "熔断"
        封条熔断 = bool(isinstance(seal, dict) and seal.get("封") == "熔断")
        已熔断 = 库熔断 or 封条熔断

        熔断后成功数 = 0
        熔断后旁路数 = 0
        熔断后尝试数 = 0
        if 行id > 0 and 库熔断:
            熔断后成功数 = conn.execute(
                "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 端点键=? AND 状态='成功' AND rowid>?",
                (键, 行id),
            ).fetchone()["c"]
            熔断后旁路数 = conn.execute(
                "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 端点键=? AND 状态='熔断旁路' AND rowid>?",
                (键, 行id),
            ).fetchone()["c"]
            熔断后尝试数 = conn.execute(
                "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 端点键=? AND rowid>?",
                (键, 行id),
            ).fetchone()["c"]

        当前指纹 = 熔断.端点指纹(键, d["回调URL"], d["密钥版本"])
        封条指纹 = (seal or {}).get("指纹") or d.get("熔断指纹") or ""
        指纹一致 = (not 封条指纹) or (封条指纹 == 当前指纹)

        连续 = int(d.get("熔断连续失败") or 0)
        阈值数 = int(d.get("熔断阈值数") or 熔断.默认阈值)

        # 灯 / 健康 / 对账结论
        if 熔断后成功数 > 0:
            灯 = "假绿"
            健康 = False
            异常 = True
            结论 = (f"熔断后仍有 {熔断后成功数} 条成功投递（回刷/影子混写同票），"
                    "端点名册不得判健康")
        elif 已熔断:
            灯 = "红"
            健康 = False
            异常 = False
            结论 = (f"已熔断（连续失败 {连续}/{阈值数}）：现行投递旁路/拒投，"
                    f"已有 {熔断后旁路数} 条中文旁路记录；封条已封端点指纹")
        elif int(d["启用"]) != 1:
            灯 = "灰"
            健康 = True
            异常 = False
            结论 = "端点人工停用中：入队即拒/投递停用联拒，非熔断异常"
        elif 连续 > 0:
            灯 = "黄"
            健康 = True
            异常 = False
            结论 = f"连续失败 {连续}/{阈值数}，未达熔断阈，暂亮黄灯"
        else:
            灯 = "绿"
            健康 = True
            异常 = False
            结论 = "端点健康：未见连续失败与熔断后回刷"

        d.update({
            "熔断": 已熔断,
            "库熔断": 库熔断,
            "封条熔断": 封条熔断,
            "灯": 灯,
            "健康": 健康,
            "对账异常": 异常,
            "对账结论": 结论,
            "连续失败": 连续,
            "阈值": 阈值数,
            "当前指纹": 当前指纹,
            "指纹一致": 指纹一致,
            "封条存在": seal is not None,
            "封条封态": (seal or {}).get("封", ""),
            "熔断后成功数": int(熔断后成功数),
            "熔断后旁路数": int(熔断后旁路数),
            "熔断后尝试数": int(熔断后尝试数),
        })
        out.append(d)
    return out


def 名册对账() -> dict:
    """端点名册对账：联查 投递端点 ⋈ 端点熔断 ⋈ 投递尝试（同票），抓熔断后假绿。"""
    conn = 连接()
    try:
        eps = _对账明细(conn)
    finally:
        conn.close()
    异常数 = sum(1 for e in eps if e["对账异常"])
    熔断数 = sum(1 for e in eps if e["熔断"])
    对齐 = 异常数 == 0
    return {
        "时刻": 此刻(),
        "对齐": 对齐,
        "是否对齐": 对齐,
        "端点数": len(eps),
        "熔断端点数": 熔断数,
        "异常数": 异常数,
        "端点": eps,
        "说明": ("端点名册与投递流水同票联查，未见熔断后成功回刷，对账对齐"
                if 对齐 else
                f"有 {异常数} 个端点在熔断后仍增成功行（假绿），名册对账撕裂"),
    }


def 名册列表() -> list[dict]:
    """供 /api/端点 与端点名册卡片：原始列 + 熔断灯/健康/对账联查字段。"""
    return 名册对账()["端点"]


def 名册一条(端点键: str) -> dict:
    for e in 名册对账()["端点"]:
        if e["端点键"] == 端点键:
            return e
    raise ValueError("无此端点")
