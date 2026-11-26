# 投递台

| 项 | 值 |
|----|-----|
| 领域 | **应用开发 / 全栈开发** |
| 产品线 | cc-hook-lab |
| 题号前缀 | hook-* |
| 直跑端口 | **8500**（可用环境变量 `端口` 覆盖；收尾核账对齐冒烟用 **8507**） |
| compose 对外 | **8507** → 容器内 8500 |
| 登录 | `/login` · admin / 123456 |

## 主链

**端点登记 → 事件入队 → 签名投递 → 失败重试与死信 → 端点熔断拒投/旁路 → 投递账互证**

页面：投递台 · 端点名册（熔断灯卡片） · 事件队列 · 事件详情 · 投递流水卡片 · 死信匣详情 · 签名规策略 · 投递互证

> 题材声明：Webhook 回调签名投递、重试与死信联调控制台。**不是**工单客服 / OA / CRM / 预约 / 电商订单（官方雷同禁区）。**不是**数据工程采集入库。

## hook-07-端点熔断拒投

修的是“端点连续失败已达熔断阈，投递仍继续打该端点并写出成功壳”的假绿，空桩「端点熔断」已真正接线（`backend/app/services/熔断.py`）。

工程形态（整仓只兑现这一边）：**物化指纹封条 + 拒熔断后回刷**。

- 熔断当刻把 `端点键｜回调URL｜密钥版本` 的 SHA256 指纹原子写到 `数据目录/熔断封条/<端点>.封条.json`（tmp + `os.replace`）。
- 同一端点键在进程内始终取同一把 `threading.RLock`（`熔断.端点锁`）。
- 投递临界区在锁内 `BEGIN IMMEDIATE`，把“读熔断闸 + 写投递尝试/事件/熔断行”收成同一事务：**读熔断态与写尝试同成败**，熔断瞬间并发双请求不会双成。
- 闸开后只写 `状态='熔断旁路'`、HTTP 503 的流水与中文旁路说明，**不打端点、不写成功壳**；事件命运置 `熔断旁路`，复位后可续投。
- 端点名册 `LEFT JOIN 端点熔断 ⋈ 投递尝试`，用熔断当刻尝试的隐式 `rowid`（`端点熔断.熔断行id`）精确识别 `rowid > 熔断行id` 的成功回刷，判为 **假绿**、健康=False、对账撕裂；互证批次纳入该口径。

### 三个分服务红/绿测

```bash
cd backend
python -m pytest tests/test_hook_cb_a.py   # 投递服务：熔断后成功尝试数不得增
python -m pytest tests/test_hook_cb_b.py   # 端点名册对账：熔断后成功回刷=假绿
python -m pytest tests/test_hook_cb_c.py   # 同锁/争用：同一端点同一把锁，双请不得双成
python -m pytest -q                        # 全套 12 passed
```

### 并发双请求脚本（打同锁）

```bash
cd backend
# 进程内：同锁身份/互斥 + Barrier 双投，断言成功行增量 0
python scripts/并发双请求.py
# 活服务：登录后对 http://127.0.0.1:8507 同一端点并发双 POST /api/投递
python scripts/并发双请求.py --base-url http://127.0.0.1:8507
```

### 本地直跑（对齐冒烟端口 8507）

```bash
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
# 本环境 bash 不接受中文变量名做临时赋值前缀，用 env 传入：
env "端口=8507" python -m app.main     # threaded=True，支持真并发
# 浏览 http://127.0.0.1:8507/login （admin / 123456）
```

五页核账顺序：**事件队列列表 → 死信匣详情 → 端点名册卡片 → 投递台队列/投递流水卡片 → 签名规策略**。
演示事故：签名规设 `熔断阈值=3` → 端点名册登记 `ep-cb` → 事件队列“灌 hook-07 熔断事故样例” → 对三笔 `*.fail` 各投递一次打满熔断 → 再投 `invoice.paid.after-trip`，即见红灯、指纹封条与中文熔断旁路。

## 冒烟（Docker）

```bash
docker compose up -d --build
# compose 对外 http://127.0.0.1:8507/login （容器内 8500）
```
