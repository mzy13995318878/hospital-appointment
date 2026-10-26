"""并发用例 —— 这是加分项 B「并发控制」的正式证据。

**本文件里的每个用例都跑在真的 uvicorn 进程上**，不用 TestClient。
理由写在 `conftest.py` 的 `LiveServer` 类注释里，核心一句话：

> TestClient 会把并发串行化掉，用它测出来的"并发安全"是假象。

四个用例分别证明四件事：

1. `test_twenty_users_race_for_fifteen_slots` —— 20 人抢 15 个号，
   恰好 15 人拿到、5 人排队，**一个都不多**；序号连续无重复。
2. `test_many_users_race_for_the_last_slot` —— 多人抢**最后 1 个号**，
   反复多轮，每轮只有一个赢家。这是"不超卖"最锋利的一刀。
3. `test_concurrent_enqueue_gets_distinct_positions` —— 满员时大家同时排队，
   名次互不重复，不会因为并发而两人并列第 3 位。
4. `test_concurrent_cancel_promotes_distinct_slots` —— 三人同时退号，
   三个候补各接一个，接到的号互不相同，不会两人拿到同一个号。

一条共同的主线：**并发下的正确性不靠"代码写得小心"，靠数据库那把锁**。
"""

from __future__ import annotations


# ------------------------------------------------------------
# 小工具
# ------------------------------------------------------------


def _tasks(schedule_id: int, user_ids: list[int]) -> list[tuple]:
    """把「这些人去约这个排班」变成 LiveServer.race 能吃的任务列表。"""
    return [
        ("POST", "/api/appointments", {"schedule_id": schedule_id}, uid)
        for uid in user_ids
    ]


def _split(results: list[dict]) -> tuple[list[dict], list[dict]]:
    """把并发结果按 type 分成「拿到号」与「去排队」两组。

    顺带把请求本身失败（500 / 网络错）的情况单独暴露出来：
    断言时把这种结果单独列出来，比笼统地说"结果不对"好排查得多。
    """
    booked: list[dict] = []
    waitlisted: list[dict] = []
    for r in results:
        if r["error"] is not None or r["payload"] is None:
            raise AssertionError(f"请求本身失败了：{r}")
        if r["payload"].get("ok") is not True:
            raise AssertionError(f"返回了业务错误：{r['payload']}")
        data = r["payload"]["data"]
        if data["type"] == "booked":
            booked.append(data)
        elif data["type"] == "waitlisted":
            waitlisted.append(data)
        else:
            raise AssertionError(f"判别联合出现了第三种形态：{data}")
    return booked, waitlisted


def _spread_ms(results: list[dict]) -> float:
    """各请求"真正发出"的时刻之间最大间隔（毫秒）。

    用来证明 Barrier 确实把起跑线对齐了：
    如果这个数字很大，说明请求是先后错开到达的，
    那这个用例就没在测并发，测的只是排队。
    """
    starts = [r["started"] for r in results if r.get("started")]
    return (max(starts) - min(starts)) * 1000


def _report(title: str, results: list[dict], extra: list[str]) -> None:
    """打印一份可直接粘进 docs/CONCURRENCY.md 的实测摘要。

    刻意打印真实数字而不是"应该没问题"：
    文档里的时间会因机器而异，但**计数必须恒等**，那才是要证明的东西。
    """
    # 用 .get("type") 而不是 ["type"]：
    # 只有预约接口返回的是判别联合（带 type），退号接口返回的没有这个字段。
    # 一个报告工具不该要求调用方都长一个样。
    seqs = sorted(
        r["payload"]["data"]["seq_no"]
        for r in results
        if r["payload"] and r["payload"]["data"].get("type") == "booked"
    )
    lines = [
        f"--- {title} ---",
        f"并发请求数     : {len(results)}",
        f"起跑间隔(最大) : {_spread_ms(results):.2f} ms",
        f"耗时(最慢)     : {max(r['elapsed'] for r in results) * 1000:.1f} ms",
        f"拿到号的序号   : {seqs if seqs else '（无）'}",
    ]
    lines.extend(extra)
    print("\n".join(lines))


# ------------------------------------------------------------
# 用例
# ------------------------------------------------------------


def test_twenty_users_race_for_fifteen_slots(live) -> None:
    """20 人同时抢 15 个号：15 人拿到，5 人排队，一个不多。

    这是 R3 那次预演的正式版。断言分三层，缺一层都不算证明没超卖：

    - **人数对**：15 + 5，总数守恒（没人被吞掉请求）
    - **序号对**：拿到的 26..40 互不重复，且全排班 1..40 无空洞
    - **记账对**：`booked_count` 与真正占着号的人数都是 40
    """
    doctor = live.fx.doctor("李建国", "口腔科")
    sch = live.fx.schedule(doctor, total=40)
    live.fx.fill(sch, 25)  # 25/40，只剩 15 个号

    racers = [live.fx.user(f"抢号{i:02d}") for i in range(1, 21)]
    results = live.race(_tasks(sch, racers))

    booked, waitlisted = _split(results)

    # ---- 第一层：人数 ----
    assert len(booked) == 15, f"拿到号的应该是 15 人，实际 {len(booked)}"
    assert len(waitlisted) == 5, f"排队的应该是 5 人，实际 {len(waitlisted)}"

    # ---- 第二层：序号 ----
    # 15 个赢家拿到的必须是 26..40 这 15 个号，一个不多一个不少
    got = sorted(d["seq_no"] for d in booked)
    assert got == list(range(26, 41)), f"序号不连续或有重复：{got}"
    # 加上原本占着的 25 个，全排班应当是 1..40 连号，没有空洞也没有越界
    assert live.fx.seq_nos(sch) == list(range(1, 41))

    # ---- 第三层：记账 ----
    assert live.fx.booked_count(sch) == 40
    assert live.fx.real_occupied(sch) == 40

    # ---- 附带：5 个排队者的名次互不重复 ----
    positions = sorted(d["position"] for d in waitlisted)
    assert positions == [1, 2, 3, 4, 5], f"名次出现重复：{positions}"

    _report(
        "20 人抢 15 个号",
        results,
        [
            f"拿到号 / 排队  : {len(booked)} / {len(waitlisted)}",
            f"排队名次       : {positions}",
            f"记账 vs 实占   : {live.fx.booked_count(sch)} / "
            f"{live.fx.real_occupied(sch)}",
        ],
    )


def test_many_users_race_for_the_last_slot(live) -> None:
    """10 人抢最后 1 个号，连抢 3 轮，每轮只有一个赢家。

    为什么要把"只剩 1 个号"单独拉出来做一个用例：
    剩 15 个号时，即便代码有竞态，也可能因为运气好而看不出问题 ——
    **竞争窗口越窄，有 bug 才越容易暴露**。
    只剩一个号时，任何"两个人都以为还有号"的写法都会当场现形。

    连抢 3 轮是因为并发 bug 往往是概率性的：
    只跑一轮通过，有可能是这次运气好。**跑三轮都只有一个赢家，才是结论。**
    """
    doctor = live.fx.doctor("张丽华", "儿科")

    for round_no in range(1, 4):
        # 每轮一个新排班：10 个号先占掉 9 个，只留最后 1 个
        sch = live.fx.schedule(doctor, offset_days=round_no, total=10)
        live.fx.fill(sch, 9)

        racers = [live.fx.user(f"第{round_no}轮{i:02d}") for i in range(1, 11)]
        results = live.race(_tasks(sch, racers))
        booked, waitlisted = _split(results)

        # 只有一个赢家 —— 多一个就是超卖
        assert len(booked) == 1, (
            f"第 {round_no} 轮有 {len(booked)} 人拿到了同一个号，发生超卖"
        )
        assert len(waitlisted) == 9

        # 赢家拿到的必然是唯一空着的 10 号
        assert booked[0]["seq_no"] == 10
        assert live.fx.booked_count(sch) == 10
        assert live.fx.real_occupied(sch) == 10

        if round_no == 1:
            _report(
                "10 人抢最后 1 个号（第 1 轮）",
                results,
                [
                    f"拿到号 / 排队  : {len(booked)} / {len(waitlisted)}",
                    f"赢家序号       : {booked[0]['seq_no']}",
                    f"记账 vs 实占   : {live.fx.booked_count(sch)} / "
                    f"{live.fx.real_occupied(sch)}",
                ],
            )


def test_concurrent_enqueue_gets_distinct_positions(live) -> None:
    """号满时 12 人同时排队：名次必须是 1..12，不能两人并列。

    这一条防的是另一种并发事故 —— 不是超卖，而是**名次撞车**。
    若 position 用「先查 MAX 再 +1」的写法，12 个并发请求会查到同一个 MAX，
    于是全都被插成第 6 位（撞唯一索引直接 500，或者更糟：索引没建时并列第 6）。

    我们用的是自增排序键 + 名次现算（见 `booking.py` 里 rank_in_waitlist 的注释），
    所以库里的 position 与对外的名次是两个东西，这里两个都查。
    """
    doctor = live.fx.doctor("陈静怡", "皮肤科")
    sch = live.fx.schedule(doctor, total=5)
    live.fx.fill(sch, 5)  # 5/5 已满，只能排队

    queuers = [live.fx.user(f"排队{i:02d}") for i in range(1, 13)]
    results = live.race(_tasks(sch, queuers))
    booked, waitlisted = _split(results)

    assert len(booked) == 0, "排班已满，不该有人拿到号"
    assert len(waitlisted) == 12

    # 对外名次：1..12 一人一位
    positions = sorted(d["position"] for d in waitlisted)
    assert positions == list(range(1, 13)), f"名次重复或缺失：{positions}"

    # 库里的排序键：同样互不重复（它只增不压缩，与名次是两个概念）
    # 先取出标量再排序：sqlite3.Row 对象之间不支持比较，
    # 直接 sorted(rows) 会抛 "not supported between instances of sqlite3.Row"。
    keys = sorted(
        r[0]
        for r in live.conn.execute(
            "SELECT position FROM waitlist WHERE schedule_id = ?", (sch,)
        ).fetchall()
    )
    assert len(set(keys)) == 12, f"排序键撞车：{keys}"

    _report(
        "12 人同时排队",
        results,
        [
            f"对外名次       : {positions[0]}..{positions[-1]}，共 {len(positions)} 人",
            f"库内排序键     : {keys[0]}..{keys[-1]}，互不重复",
        ],
    )


def test_concurrent_cancel_promotes_distinct_slots(live) -> None:
    """3 个持号人同时退号，3 个候补各接一个，接到的号互不相同。

    这是四个用例里最接近真实业务的一个：
    早高峰三个人同时退号，系统必须把三个号分给三个不同的人。
    若递补时"取队首"与"建预约"不在同一个事务里，
    两个并发事务会取到**同一个队首**，给同一个人发两个号，
    或者都去发 8 号，直接撞上唯一索引变成 500。

    断言的关键不是"递补成功了"，而是：
    三个转正者拿到的号是三个**不同**的号，且队列里不再有 waiting 状态的人。
    """
    doctor = live.fx.doctor("王志远", "心内科")
    sch = live.fx.schedule(doctor, total=6)
    holders = live.fx.fill(sch, 6)  # 6/6 满员

    # 三个候补先排上队（造场景，直接插库）
    promoted_users = [live.fx.user(f"候补{i}") for i in range(1, 4)]
    for i, uid in enumerate(promoted_users):
        live.fx.enqueue(uid, sch, position=i + 1)

    # 三个持号人同时退掉手里的号（他们退的是不同的号：1、2、3 号）
    tasks = [
        ("DELETE", f"/api/appointments/{appt}", None, holder)
        for appt, holder in holders[:3]
    ]
    results = live.race(tasks)

    # 三个退号请求都必须成功
    for r in results:
        assert r["status"] == 200, f"退号失败：{r}"
        assert r["payload"]["ok"] is True

    # 三个人拿到三个不同的号
    seqs = sorted(
        r["payload"]["data"]["promoted"]["seq_no"] for r in results
    )
    assert seqs == [1, 2, 3], f"递补的号撞车了：{seqs}"

    # 三个转正者各自手里有一个号
    promoted_ids = {r["payload"]["data"]["promoted"]["user_name"] for r in results}
    assert len(promoted_ids) == 3, f"有人被重复递补：{promoted_ids}"

    # 队列清空：三条 waiting 全部转为 promoted
    still_waiting = live.conn.execute(
        "SELECT COUNT(*) FROM waitlist WHERE schedule_id = ? AND status = 'waiting'",
        (sch,),
    ).fetchone()[0]
    assert still_waiting == 0

    # 记账：退 3 个、补 3 个，总数回到 6
    assert live.fx.booked_count(sch) == 6
    assert live.fx.real_occupied(sch) == 6

    _report(
        "3 人同时退号 → 3 人同时递补",
        results,
        [
            f"递补出的号     : {seqs}（互不相同）",
            f"递补人数       : {len(promoted_ids)}",
            f"记账 vs 实占   : {live.fx.booked_count(sch)} / "
            f"{live.fx.real_occupied(sch)}",
        ],
    )
