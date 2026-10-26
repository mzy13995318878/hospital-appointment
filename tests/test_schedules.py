"""查询接口的用例：筛选、以及「过去的排班默认不出现」。

这里测的是 R2 留下的两个接口，以及 R5 补上的 `only_future` 开关。
全部用 TestClient —— 这些是纯查询逻辑，不涉及并发，用不着真 uvicorn。
"""

from __future__ import annotations


def _ids(resp) -> list[int]:
    """从列表响应里抽出排班 id。"""
    return [s["schedule_id"] for s in resp.json()["data"]]


def _make_three(fx) -> tuple[int, int, int, int]:
    """造一个医生与他昨天 / 今天 / 明天三条排班。

    三条都造，是为了把边界一次测全：
    只造昨天和明天的话，「今天到底算不算未来」这个最容易错的点就漏掉了。
    """
    doc = fx.doctor("周医生", "骨科")
    past = fx.schedule(doc, offset_days=-1)
    today = fx.schedule(doc, offset_days=0)
    future = fx.schedule(doc, offset_days=1)
    return doc, past, today, future


def test_past_schedule_hidden_by_default(client, fx) -> None:
    """过去日期的排班默认不出现在列表里。

    同时验证**今天的不被一起藏掉**：
    判断条件若写成 `>` 而不是 `>=`，今天的号会在每天早上 8 点前消失
    （漏掉 localtime 时更是如此）。今天当天的号还能约，藏掉是错的。
    """
    _doc, past, today, future = _make_three(fx)

    ids = _ids(client.get("/api/schedules"))

    assert past not in ids, "昨天的排班不该出现在默认列表里"
    assert today in ids, "今天的号还能约，不能被一起藏掉"
    assert future in ids


def test_only_future_zero_brings_them_back(client, fx, conn) -> None:
    """`only_future=0` 能把过去的排班取回来，且记录本身没被删。

    后半句才是重点：**隐藏不等于删除**。
    数据必须还在库里，否则将来要做「这位医生上个月出诊几天」时无从下手。
    我们藏的只是"显示在列表里"这件事，不是数据本身。
    """
    _doc, past, today, future = _make_three(fx)

    ids = _ids(client.get("/api/schedules?only_future=0"))
    assert {past, today, future} <= set(ids), "开了开关却还是看不到过去的排班"

    # 记录本身一条都没少
    still_there = conn.execute(
        "SELECT COUNT(*) FROM schedules WHERE id IN (?, ?, ?)",
        (past, today, future),
    ).fetchone()[0]
    assert still_there == 3, "过去的排班记录被真的删掉了，历史数据就没了"


def test_doctor_detail_hides_past_too(client, fx) -> None:
    """医生详情页的内嵌排班同样默认过滤。

    两处必须一致：列表页看不到昨天的号、点进医生详情却看得到，
    用户会认为这两个页面在互相矛盾。
    """
    doc, past, today, future = _make_three(fx)

    ids = [s["schedule_id"] for s in client.get(f"/api/doctors/{doc}").json()["data"]["schedules"]]
    assert past not in ids
    assert {today, future} <= set(ids)

    ids_all = [
        s["schedule_id"]
        for s in client.get(f"/api/doctors/{doc}?only_future=0")
        .json()["data"]["schedules"]
    ]
    assert {past, today, future} <= set(ids_all)


def test_invalid_only_future_is_rejected(client, fx) -> None:
    """`only_future=abc` 必须是 400 INVALID_PARAM，不是空列表。

    与 `status` 同一个道理：静默返回空列表会把一个参数错误
    包装成「一个排班都没有」的业务事实，这类 bug 极难排查。
    """
    _make_three(fx)
    r = client.get("/api/schedules?only_future=abc")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PARAM"
