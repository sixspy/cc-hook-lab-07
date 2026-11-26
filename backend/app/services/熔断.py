"""端点熔断（hook-07-端点熔断拒投）。

工程形态（整仓只兑现这一边）：**物化指纹封条**。
熔断落下的当刻，把端点指纹（端点键｜回调URL｜密钥版本 的 SHA256）
原子物化到 ``数据目录/熔断封条/<端点>.封条.json``；此后该端点的回刷投递
在同一把端点锁 + 同一个 SQLite 写事务里读到“闸开”，一律旁路/拒写成功壳。
人工复位合闸后，封条被改写为“已复位”（留痕不删指纹），投递方可恢复。

并发正确性：
* :func:`端点锁` 对同一端点键返回进程内同一把 ``RLock``，收尾“并发双请求打同锁”即此锁；
* 投递临界区在锁内用 ``BEGIN IMMEDIATE`` 把“读熔断态 + 写尝试/事件/熔断行”
  收成同一事务，读闸与写尝试同成败，熔断瞬间不会双成。

注意：本模块只依赖 app.db，避免与 投递/端点 服务形成循环导入。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path

from app import db
from app.services.时间 import 此刻

# ---- 同一端点同一把锁（进程内“同锁”） -----------------------------------

_锁表: dict[str, threading.RLock] = {}
_锁表闸 = threading.Lock()


def 端点锁(端点键: str) -> threading.RLock:
    """同一端点键始终返回同一把可重入锁；不同端点不同锁。"""
    with _锁表闸:
        lk = _锁表.get(端点键)
        if lk is None:
            lk = threading.RLock()
            _锁表[端点键] = lk
        return lk


# ---- 阈值 / 指纹 / 封条文件 ----------------------------------------------

默认阈值 = 5


def 读阈值(conn) -> int:
    try:
        row = conn.execute("SELECT 值 FROM 签名规 WHERE 键='熔断阈值'").fetchone()
        if row is None:
            row = conn.execute("SELECT 值 FROM 签名规 WHERE 键='熔断阈'").fetchone()
        return max(1, int(row["值"])) if row else 默认阈值
    except (TypeError, ValueError):
        return 默认阈值


def 端点指纹(端点键: str, 回调URL: str, 密钥版本: str) -> str:
    raw = f"{端点键}|{回调URL}|{密钥版本}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _安全文件名(端点键: str) -> str:
    s = re.sub(r"[^0-9A-Za-z一-鿿._-]+", "_", str(端点键)).strip("._")
    if not s:
        s = hashlib.sha1(str(uuid.uuid4()).encode()).hexdigest()[:12]
    return f"{s}.封条.json"


def 封条目录() -> Path:
    # 动态读取 db.数据目录，测试 monkeypatch 后封条也随之进临时目录
    d = Path(db.数据目录) / "熔断封条"
    d.mkdir(parents=True, exist_ok=True)
    return d


def 封条路径(端点键: str) -> Path:
    return 封条目录() / _安全文件名(端点键)


def 写封条(端点键: str, 载荷: dict) -> Path:
    """原子落盘封条（tmp + os.replace），封住熔断当刻指纹。"""
    p = 封条路径(端点键)
    tmp = p.with_suffix(p.suffix + ".tmp")
    body = dict(载荷)
    body.setdefault("端点键", 端点键)
    body.setdefault("封条版本", "hook-07-v1")
    body.setdefault("工程形态", "物化指纹封条·拒熔断后回刷")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)
    return p


def 读封条(端点键: str) -> dict | None:
    p = 封条路径(端点键)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 封条损坏也按“闸开”从严处理，避免绕过
        return {"端点键": 端点键, "封": "熔断", "损坏": True}


def 复位封条(端点键: str) -> None:
    旧 = 读封条(端点键) or {"端点键": 端点键}
    旧["封"] = "已复位"
    旧["复位时"] = 此刻()
    写封条(端点键, 旧)


# ---- 数据库熔断态 + 封条联查 ---------------------------------------------

def 闸态(conn, 端点键: str, ep=None) -> dict:
    """在投递事务内调用：读数据库熔断行并联查物化封条。

    返回 ``{"熔断": bool, "来源": ..., "行": Row|None, "封条": dict|None, "指纹": str}``。
    数据库熔断行=熔断 或 封条封=熔断，任一成立即闸开（物化封条可独立拒回刷）。
    """
    row = conn.execute(
        "SELECT * FROM 端点熔断 WHERE 端点键=?", (端点键,)
    ).fetchone()
    seal = 读封条(端点键)
    db_open = bool(row is not None and str(row["状态"]) == "熔断")
    seal_open = bool(isinstance(seal, dict) and seal.get("封") == "熔断")

    指纹 = ""
    if ep is not None:
        指纹 = 端点指纹(端点键, ep["回调URL"], ep["密钥版本"])
    elif row is not None and "指纹" in row.keys():
        指纹 = row["指纹"] or ""

    指纹一致 = True
    if seal and 指纹 and seal.get("指纹"):
        指纹一致 = (seal["指纹"] == 指纹)

    return {
        "熔断": db_open or seal_open,
        "库熔断": db_open,
        "封条熔断": seal_open,
        "来源": "库+封条" if db_open and seal_open else ("库" if db_open else ("封条" if seal_open else "无")),
        "行": row,
        "封条": seal,
        "指纹": 指纹,
        "指纹一致": 指纹一致,
    }


def 记失败并可能熔断(conn, *, 端点键: str, ep, 阈值: int, t: str,
                    尝试号: str, 尝试行id: int) -> dict:
    """在投递事务内累加端点连续失败；打满阈则把熔断行置为熔断。

    返回 ``{"熔断": bool, "连续失败": int, "指纹": str, "封条载荷": dict|None}``。
    封条文件在事务提交后由调用方按返回的 ``封条载荷`` 物化（锁仍持有）。
    """
    指纹 = 端点指纹(端点键, ep["回调URL"], ep["密钥版本"])
    row = conn.execute("SELECT * FROM 端点熔断 WHERE 端点键=?", (端点键,)).fetchone()
    if row is None:
        连续 = 1
        conn.execute(
            "INSERT INTO 端点熔断(端点键,状态,灯,连续失败,阈值,指纹,封条路径,熔断时,"
            "熔断尝试号,熔断行id,复位时,更新时) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (端点键, "正常", "黄" if 连续 < 阈值 else "红", 连续, 阈值, 指纹,
             str(封条路径(端点键).name), "", "", 0, "", t),
        )
    else:
        连续 = int(row["连续失败"] or 0) + 1
        conn.execute(
            "UPDATE 端点熔断 SET 连续失败=?, 阈值=?, 指纹=?, 更新时=? WHERE 端点键=?",
            (连续, 阈值, 指纹, t, 端点键),
        )

    tripped = 连续 >= 阈值
    封条载荷 = None
    if tripped:
        conn.execute(
            "UPDATE 端点熔断 SET 状态='熔断', 灯='红', 熔断时=?, 熔断尝试号=?, 熔断行id=? "
            "WHERE 端点键=?",
            (t, 尝试号, int(尝试行id or 0), 端点键),
        )
        封条载荷 = {
            "封": "熔断",
            "端点键": 端点键,
            "指纹": 指纹,
            "回调URL": ep["回调URL"],
            "密钥版本": ep["密钥版本"],
            "阈值": 阈值,
            "连续失败": 连续,
            "熔断时": t,
            "熔断尝试号": 尝试号,
            "熔断行id": int(尝试行id or 0),
        }
    else:
        conn.execute(
            "UPDATE 端点熔断 SET 灯='黄' WHERE 端点键=?", (端点键,)
        )
    return {"熔断": tripped, "连续失败": 连续, "指纹": 指纹, "封条载荷": 封条载荷}


def 记成功复位(conn, *, 端点键: str, t: str, 阈值: int | None = None) -> None:
    """真实成功：闭合连续失败计数（闸本就闭合）。无行则补一条正常行。"""
    阈 = int(阈值) if 阈值 else 默认阈值
    row = conn.execute("SELECT 端点键 FROM 端点熔断 WHERE 端点键=?", (端点键,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO 端点熔断(端点键,状态,灯,连续失败,阈值,指纹,封条路径,熔断时,"
            "熔断尝试号,熔断行id,复位时,更新时) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (端点键, "正常", "绿", 0, 阈, "", "", "", "", 0, "", t),
        )
    else:
        conn.execute(
            "UPDATE 端点熔断 SET 连续失败=0, 灯='绿', 阈值=?, 更新时=? WHERE 端点键=?",
            (阈, t, 端点键),
        )
