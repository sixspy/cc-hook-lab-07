#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hook-07 收尾：并发双请求打同一把端点锁。

两种打法：

1) 默认进程内模式（不需要起服务）：
   - 证明 ``熔断.端点锁(键)`` 对同一端点返回同一把锁、不同端点不同锁；
   - 夹具式连续强制失败打满熔断阈后，用 Barrier 让两票同时撞投递临界区，
     断言不会“双成”：成功返回 < 2，熔断后成功行增量 = 0。

2) 活服务 HTTP 模式（收尾核账用）：
       python scripts/并发双请求.py --base-url http://127.0.0.1:8507
   脚本自注册唯一端点、设熔断阈值=3、连造 3 次强制失败打满熔断，
   再入两票正常事件，两个线程同时带登录 Cookie POST /api/投递，
   校验 HTTP 侧同样不双成、流水成功行增量为 0。

退出码 0 = 同锁/拒双成成立；1 = 发现双成或断言失败；2 = 环境/连接问题。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# 进程内模式
# ---------------------------------------------------------------------------

def 跑进程内() -> int:
    临时目录 = tempfile.mkdtemp(prefix="hook07_同锁_")
    os.environ["数据目录"] = 临时目录
    os.environ.setdefault("夹具目录", str(_BACKEND.parent / "fixtures"))

    import app.db as db
    db.数据目录 = Path(临时目录)
    db.库路径 = Path(临时目录) / "同锁.db"

    from app import seed
    seed.初始化库()
    from app.services import 端点, 入队, 投递, 熔断, 签名目录

    print("=" * 68)
    print("【进程内】同一端点同一把锁 + 熔断后并发双请求不得双成")
    print("=" * 68)

    # 1) 同锁身份
    键锁 = "ep-lock-proof"
    a = 熔断.端点锁(键锁)
    b = 熔断.端点锁(键锁)
    c = 熔断.端点锁("ep-other-proof")
    assert a is b, "同一端点键必须返回同一把锁"
    assert a is not c, "不同端点必须是不同锁"
    print(f"[同锁] 同一端点两次取锁 is 同一把：{a is b}；异端点异锁：{a is not c}")

    # 互斥性实测
    got = threading.Event()
    release = threading.Event()

    def _持锁():
        with 熔断.端点锁("ep-mutex-proof"):
            got.set()
            release.wait(timeout=2)

    t = threading.Thread(target=_持锁, daemon=True)
    t.start()
    got.wait(2)
    主线程锁 = 熔断.端点锁("ep-mutex-proof")
    抢不到 = 主线程锁.acquire(timeout=0.3) is False
    release.set()
    t.join(2)
    print(f"[互斥] 后台持锁时，另一线程 0.3s 内抢不到同一把锁：{抢不到}")
    assert 抢不到

    # 2) 造事故：连续强制失败打满熔断阈
    键 = "ep-lock-" + uuid.uuid4().hex[:8]
    端点.登记({
        "端点键": 键, "名称": "同锁双请端点",
        "回调URL": f"https://lock.local/{键}", "密钥版本": "v1",
    })
    签名目录.改签名规({"熔断阈值": "3"})
    for i in range(3):
        ev = 入队.建事件({"事件类型": f"lock{i}.fail", "端点键": 键, "载荷摘要": f"lf{i}"})
        rep = 投递.投递一次(ev["事件号"], 强制失败=True)
        print(f"[造事故] 第 {i + 1} 次强制失败投递 -> {rep.get('状态')}")
        assert rep.get("状态") == "失败"

    对账 = 端点.名册对账()
    我的 = next(e for e in 对账["端点"] if e["端点键"] == 键)
    print(f"[熔断] 灯={我的['灯']} 熔断={我的['熔断']} 来源库/封条="
          f"{我的['库熔断']}/{我的['封条熔断']} 结论：{我的['对账结论']}")
    assert 我的["熔断"] is True

    # 3) 熔断后两票正常事件，Barrier 同时撞临界区
    e1 = 入队.建事件({"事件类型": "invoice.lock.a", "端点键": 键, "载荷摘要": "la"})
    e2 = 入队.建事件({"事件类型": "invoice.lock.b", "端点键": 键, "载荷摘要": "lb"})

    def _成功数() -> int:
        return sum(1 for r in 签名目录.投递列表()
                   if r["端点键"] == 键 and r["状态"] == "成功")

    before = _成功数()
    barrier = threading.Barrier(2)
    outs: list[dict] = []
    errs: list[BaseException] = []

    def _work(eid: str):
        barrier.wait(timeout=5)
        try:
            outs.append(投递.投递一次(eid))
        except BaseException as e:  # noqa: BLE001
            errs.append(e)

    t1 = threading.Thread(target=_work, args=(e1["事件号"],))
    t2 = threading.Thread(target=_work, args=(e2["事件号"],))
    t1.start(); t2.start()
    t1.join(10); t2.join(10)

    after = _成功数()
    成功返回 = [r for r in outs if r.get("状态") == "成功"]
    旁路返回 = [r for r in outs if r.get("熔断") or r.get("状态") == "熔断旁路"]

    print("-" * 68)
    for r in outs:
        print(f"[并发返回] 事件={r.get('事件号')} 状态={r.get('状态')} "
              f"熔断={r.get('熔断')} 旁路={r.get('旁路')} 码={r.get('响应码')}")
    for e in errs:
        print(f"[并发异常] {e!r}")
    print(f"[核账] 成功返回 {len(成功返回)} 个（必须 < 2）；"
          f"旁路/拒投返回 {len(旁路返回)} 个；成功行 {before} -> {after}（增量须 0）")

    ok = (
        not t1.is_alive() and not t2.is_alive()
        and len(outs) + len(errs) == 2
        and len(成功返回) < 2
        and after - before == 0
    )
    print("=" * 68)
    print("进程内结论：", "✅ 通过——同一端点同一把锁，熔断后并发双请求未双成" if ok
          else "⛔ 失败——仍存在双成/悬挂")
    print("=" * 68)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 活服务 HTTP 模式
# ---------------------------------------------------------------------------

def _http(base: str, method: str, path: str, body: dict | None = None,
           cookie: str = "") -> tuple[int, dict, str]:
    path = urllib.parse.quote(path, safe="/?=&%")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {}), resp.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw), e.headers.get("Set-Cookie", "")
        except json.JSONDecodeError:
            return e.code, {"错误": raw}, ""


def 跑http(base: str, 账号: str, 密码: str) -> int:
    print("=" * 68)
    print(f"【活服务 HTTP】{base} 登录后并发双 POST /api/投递 打同锁")
    print("=" * 68)

    st, login, set_cookie = _http(base, "POST", "/api/登录",
                                  {"账号": 账号, "密码": 密码})
    if st != 200 or not login.get("ok"):
        print(f"[登录失败] HTTP {st} {login}")
        return 2
    # 从 Set-Cookie 抽 session 段，构造两个线程共用的 Cookie 头
    cookie = set_cookie.split(";", 1)[0]
    if "=" not in cookie:
        print("[登录失败] 未取到会话 Cookie：", set_cookie)
        return 2
    print(f"[登录] {账号} 成功，取得会话 Cookie")

    键 = "ep-http-lock-" + uuid.uuid4().hex[:8]
    st, rep, _ = _http(base, "POST", "/api/端点", {
        "端点键": 键, "名称": "HTTP同锁端点",
        "回调URL": f"https://http-lock.local/{键}", "密钥版本": "v1",
    }, cookie)
    if st not in (200, 201):
        print(f"[注册端点失败] HTTP {st} {rep}")
        return 2
    _http(base, "POST", "/api/签名规", {"熔断阈值": "3"}, cookie)
    print(f"[准备] 唯一端点 {键}，熔断阈值=3")

    # 连续 3 次强制失败打满熔断
    for i in range(3):
        _, ev, _ = _http(base, "POST", "/api/事件/入队", {
            "事件类型": f"http-lock{i}.fail", "端点键": 键, "载荷摘要": f"h{i}",
        }, cookie)
        eid = ev.get("事件号")
        st, r, _ = _http(base, "POST", "/api/投递",
                         {"事件号": eid, "强制失败": True}, cookie)
        print(f"[造事故] 第 {i + 1} 次强败 {eid} -> HTTP {st} {r.get('状态')}")

    # 两票正常事件
    票 = []
    for i in range(2):
        _, ev, _ = _http(base, "POST", "/api/事件/入队", {
            "事件类型": f"invoice.http-lock.{i}", "端点键": 键, "载荷摘要": f"hp{i}",
        }, cookie)
        票.append(ev["事件号"])
    print("[准备] 熔断后两票正常事件：", 票)

    _, before_rows, _ = _http(base, "GET", "/api/投递", cookie=cookie)
    before = sum(1 for r in before_rows if r.get("端点键") == 键 and r.get("状态") == "成功")

    barrier = threading.Barrier(2)
    results: list[tuple[int, dict]] = []

    def _worker(eid: str):
        barrier.wait(timeout=5)
        st, r, _ = _http(base, "POST", "/api/投递", {"事件号": eid}, cookie)
        results.append((st, r))

    ths = [threading.Thread(target=_worker, args=(e,)) for e in 票]
    for t in ths: t.start()
    for t in ths: t.join(15)

    for st, r in results:
        print(f"[并发HTTP返回] HTTP {st} 事件={r.get('事件号')} 状态={r.get('状态')} "
              f"熔断={r.get('熔断')} 旁路={r.get('旁路')}")

    _, after_rows, _ = _http(base, "GET", "/api/投递", cookie=cookie)
    after = sum(1 for r in after_rows if r.get("端点键") == 键 and r.get("状态") == "成功")
    成功返回 = sum(1 for st, r in results if r.get("状态") == "成功")
    旁路返回 = sum(1 for st, r in results if r.get("熔断") or r.get("状态") == "熔断旁路")

    # 名册侧核账
    _, roster, _ = _http(base, "GET", "/api/端点/对账", cookie=cookie)
    mine = next((e for e in roster.get("端点", []) if e.get("端点键") == 键), {})
    print(f"[名册核账] 灯={mine.get('灯')} 熔断={mine.get('熔断')} "
          f"熔断后成功数={mine.get('熔断后成功数')} 旁路数={mine.get('熔断后旁路数')} "
          f"健康={mine.get('健康')}")
    print(f"[流水核账] 成功返回 {成功返回}（须 < 2），旁路返回 {旁路返回}；"
          f"该端点成功行 {before} -> {after}（增量须 0）")

    ok = (len(results) == 2 and 成功返回 < 2 and after - before == 0
          and int(mine.get("熔断后成功数", 0)) == 0)
    print("=" * 68)
    print("HTTP 结论：", "✅ 通过——活服务 threaded 下同一端点同锁串行，未双成" if ok
          else "⛔ 失败——活服务仍双成或名册出现熔断后成功")
    print("=" * 68)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="hook-07 同锁并发双请求验证")
    ap.add_argument("--base-url", default="", help="例如 http://127.0.0.1:8507；不填则跑进程内模式")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="123456")
    args = ap.parse_args()
    if args.base_url:
        try:
            return 跑http(args.base_url, args.user, args.password)
        except urllib.error.URLError as e:
            print(f"[连接失败] {args.base_url}：{e}")
            return 2
    return 跑进程内()


if __name__ == "__main__":
    raise SystemExit(main())
