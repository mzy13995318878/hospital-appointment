"""按 docs/DEMO.md 的演示路线真跑一遍，验证文档里的命令与预期输出不是编的。

用法（在项目根目录下执行）：

    python scripts/verify_demo.py

它回答的问题是：**答辩台本上写的那些"预期看到什么"，现在还成不成立。**
文档会过时，代码会改，而演示翻车往往就发生在"上次还好好的"那一步。

为什么值得单独做一个脚本：
    R6 写完 DEMO.md 后跑它，当场发现 docs/API.md 里 `promoted` 的示例
    还是 R3 时期的老样子，缺了 R5 新增的 `pending` 与 `confirm_deadline` ——
    **文档在骗人，而照着文档准备演示的人要到台上才会发现。**

两条隔离约定（都不许改）：

1. 用 `CLINIC_DB` 环境变量指向临时文件（照抄 tests/conftest.py 的做法），
   **绝不碰 data/clinic.db** —— 否则跑一次就把演示库弄脏了，
   那正是 DEMO 文档第 3.1 节列的头号翻车点。
2. 必须进入 TestClient 的 context（`with` 或 ExitStack），
   否则 lifespan 不跑、库不建立，第一个请求就会 `no such table`。
"""
import os
import sqlite3
import sys
import tempfile

# 脚本位于 scripts/ 下，项目根是它的上级目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmpdir = tempfile.mkdtemp(prefix="demo_verify_")
dbfile = os.path.join(tmpdir, "clinic.db")

from app import db as app_db  # noqa: E402

os.environ[app_db.DB_PATH_ENV] = dbfile

from contextlib import ExitStack  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

# 必须进入 context 才会触发 lifespan —— 不进的话 ensure_up_to_date() 不跑，
# 临时库是个空文件，第一个请求就会 no such table。
# 用 ExitStack 手动进出，省得把整个脚本缩进一层。
_stack = ExitStack()
c = _stack.enter_context(TestClient(app))
ok = 0
bad = 0


def step(title, resp, expect_code=None, check=None):
    global ok, bad
    body = resp.json()
    oke = body.get("ok")
    code = (body.get("error") or {}).get("code") if not oke else None
    good = resp.status_code == (expect_code or 200) and (check is None or check(body))
    print(f"[{'OK ' if good else 'FAIL'}] {title}")
    print(f"        HTTP {resp.status_code}  ok={oke}  error_code={code}")
    if not good:
        print(f"        !! 期望被违反，完整响应：{body}")
        bad += 1
    else:
        ok += 1
    return body.get("data") if oke else None


print("=" * 60)
print("DEMO 路线验证（临时库，不碰 data/clinic.db）")
print("=" * 60)

# --- 准备：找排班 1 -------------------------------------------------
scheds = c.get("/api/schedules").json()["data"]
s1 = next(s for s in scheds if s["schedule_id"] == 1)
print(f"\n排班 1：{s1['doctor_name']} {s1['work_date']} {s1['period_label']} "
      f"{s1['booked_count']}/{s1['total_slots']} 状态={s1['status']}/{s1['status_label']}")
assert s1["status"] == "waiting", "排班 1 应当是候补中"

# --- 第 2 步：排队 -------------------------------------------------
ua = c.post("/api/auth/login",
            json={"name": "演示员甲", "id_card_last4": "9991",
                  "phone": "13900000001"}).json()["data"]["user_id"]
ha = {"X-User-Id": str(ua)}
d = step("第2步 演示员甲约排班 1（已满）→ 应入候补第 3 位",
         c.post("/api/appointments", json={"schedule_id": 1}, headers=ha),
         check=lambda b: b["data"]["type"] == "waitlisted" and b["data"]["position"] == 3)

# --- 第 3 步：退号递补 → 队首拿到「待确认」-------------------------
conn = sqlite3.connect(dbfile)
row = conn.execute(
    "SELECT a.id, a.user_id, a.seq_no, u.name FROM appointments a "
    "JOIN users u ON u.id = a.user_id "
    "WHERE a.schedule_id = 1 AND a.status = 'booked' ORDER BY a.seq_no LIMIT 1"
).fetchone()
conn.close()
holder_appt_id, holder_uid, holder_seq, holder_name = row
print(f"\n排班 1 的持号人：{holder_name}（user_id={holder_uid}），序号 {holder_seq}")

d = step(f"第3步 {holder_name} 退号 → 队首应拿到「待确认」",
         c.delete(f"/api/appointments/{holder_appt_id}", headers={"X-User-Id": str(holder_uid)}))
promoted = (d or {}).get("promoted")
print(f"        promoted = {promoted}")
# 实测形状：promoted 里没有 status 字段，用 pending=True 表达"待确认"；
# 也没有 user_id，只有 user_name —— 确认接口要用 user_id，得回库里查。
if promoted and promoted.get("pending") is True and promoted.get("confirm_deadline"):
    print(f"        ✓ 递补者 {promoted['user_name']} 拿到 {promoted['seq_no']} 号，"
          f"pending=True，截止 {promoted['confirm_deadline']}")
    ok += 1
else:
    print("        !! 递补者不是 pending")
    bad += 1

# --- 第 4 步：确认 -------------------------------------------------
if promoted:
    pid = promoted["appointment_id"]
    conn = sqlite3.connect(dbfile)
    puid = conn.execute("SELECT user_id FROM appointments WHERE id = ?", (pid,)).fetchone()[0]
    conn.close()
    print(f"        （递补者 user_id={puid}）")
    d = step("第4步 递补者确认 → pending 应变 booked",
             c.post(f"/api/appointments/{pid}/confirm", headers={"X-User-Id": str(puid)}))
    print(f"        confirm 返回：{d}")
    # 用 my/appointments 复核这条记录的状态，比看 confirm 的返回体更可靠
    mine = c.get("/api/my/appointments", headers={"X-User-Id": str(puid)}).json()["data"]
    hit = [a for a in mine["appointments"] if a["id"] == pid]
    st = hit[0]["status"] if hit else None
    print(f"        复核：该预约在「我的预约」里状态 = {st}（{hit[0]['status_label'] if hit else '-'}）")
    if st == "booked":
        ok += 1
    else:
        print("        !! 确认后状态不是 booked")
        bad += 1

# --- 第 5 步：admin/settle ----------------------------------------
step("第5步 POST /api/admin/settle（手动结算）",
     c.post("/api/admin/settle"))

# --- 加分项 1：退号冷却 -------------------------------------------
ub = c.post("/api/auth/login",
            json={"name": "演示员乙", "id_card_last4": "9992",
                  "phone": "13900000002"}).json()["data"]["user_id"]
hb = {"X-User-Id": str(ub)}
uc = c.post("/api/auth/login",
            json={"name": "演示员丙", "id_card_last4": "9993",
                  "phone": "13900000003"}).json()["data"]["user_id"]
hc = {"X-User-Id": str(uc)}
d = step("加分项1-a 演示员乙约排班 2（可约）→ 应直接拿到号",
         c.post("/api/appointments", json={"schedule_id": 2}, headers=hb),
         check=lambda b: b["data"]["type"] == "booked")
appt_b = d["appointment_id"]
step("加分项1-b 立刻退号", c.delete(f"/api/appointments/{appt_b}", headers=hb))
step("加分项1-c 立刻再约排班 2 → 应 409 COOLDOWN_ACTIVE",
     c.post("/api/appointments", json={"schedule_id": 2}, headers=hb),
     expect_code=409,
     check=lambda b: (b.get("error") or {}).get("code") == "COOLDOWN_ACTIVE")

# --- 加分项 5：拉黑 -----------------------------------------------
u6 = c.post("/api/auth/login",
            json={"name": "周八", "id_card_last4": "6789",
                  "phone": "13800000006"}).json()["data"]["user_id"]
h6 = {"X-User-Id": str(u6)}
step("加分项5-a POST /api/admin/users/6/blacklist",
     c.post("/api/admin/users/6/blacklist"))
step("加分项5-b 被拉黑的用户去约号 → 应 403 USER_BLACKLISTED",
     c.post("/api/appointments", json={"schedule_id": 5}, headers=h6),
     expect_code=403,
     check=lambda b: (b.get("error") or {}).get("code") == "USER_BLACKLISTED")
step("加分项5-c 解除拉黑", c.delete("/api/admin/users/6/blacklist"))

# --- 加分项 3：签到 ----------------------------------------------
# 错误路径：排班 5 是后天，签到应被拒
d = step("加分项3-a 演示员丙约排班 5（后天）",
         c.post("/api/appointments", json={"schedule_id": 5}, headers=hc),
         check=lambda b: b["data"]["type"] == "booked")
appt_c = d["appointment_id"]
step("加分项3-b 还没到就诊当天就签到 → 应 409 CHECKIN_NOT_OPEN",
     c.post(f"/api/appointments/{appt_c}/check-in", headers=hc),
     expect_code=409,
     check=lambda b: (b.get("error") or {}).get("code") == "CHECKIN_NOT_OPEN")

# 成功路径：临时插一条今天的排班，约了再签到
conn = sqlite3.connect(dbfile)
today = conn.execute("SELECT date('now','localtime')").fetchone()[0]
conn.execute(
    "INSERT INTO schedules (doctor_id, work_date, period, total_slots, "
    "booked_count, is_closed) VALUES (2, ?, 'morning', 5, 0, 0)", (today,))
conn.commit()
sid_today = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
conn.close()
print(f"        （临时插入今天的排班 schedule_id={sid_today}）")

d = step("加分项3-c 约今天的排班",
         c.post("/api/appointments", json={"schedule_id": sid_today}, headers=hc),
         check=lambda b: b["data"]["type"] == "booked")
appt_today = d["appointment_id"]
step("加分项3-d 就诊当天签到 → 应成功，状态变 completed",
     c.post(f"/api/appointments/{appt_today}/check-in", headers=hc),
     check=lambda b: "签到" in b["data"]["message"])
step("加分项3-e 重复签到 → 应 409 ALREADY_CHECKED_IN",
     c.post(f"/api/appointments/{appt_today}/check-in", headers=hc),
     expect_code=409,
     check=lambda b: (b.get("error") or {}).get("code") == "ALREADY_CHECKED_IN")

print("\n" + "=" * 60)
print(f"通过 {ok} 项，失败 {bad} 项")
print("=" * 60)

import shutil  # noqa: E402

shutil.rmtree(tmpdir, ignore_errors=True)
