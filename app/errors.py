"""业务异常与统一响应。

为什么要单独抽这个文件：

    路由层只负责「发现规则被违反了」，然后抛异常；
    至于这个违规对应哪个 HTTP 状态码、响应体长什么样，一律由这里统一转换。

好处有两个：
    1. 路由里不会出现 return JSONResponse(status_code=409, ...) 这种
       把协议细节和业务逻辑搅在一起的代码；
    2. HTTP 状态码与业务码的分工得以强制落实 ——
       状态码表达「哪一类问题」（404 找不到、409 冲突、403 被禁），
       code 表达「具体是哪一条规则」（号满了、重复预约、当天限一个）。
       前端靠 code 决定文案，靠状态码决定要不要跳登录页。

docstring 里的分工说明可以直接拿去答辩讲，这是 RESTful 设计里
最容易被糊弄过去的一个点。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# 业务错误码 → HTTP 状态码。
# 这张表就是「分工」的全部内容，新增错误码必须先在这里登记。
ERROR_HTTP_STATUS = {
    # 401：没带身份或身份失效 → 前端跳登录页
    "UNAUTHORIZED": 401,
    # 400：请求本身就是坏的（字段格式不对）→ 表单红字提示
    "INVALID_IDENTITY": 400,
    "INVALID_PARAM": 400,
    # 404：要操作的东西不存在
    "SCHEDULE_NOT_FOUND": 404,
    "DOCTOR_NOT_FOUND": 404,
    "APPOINTMENT_NOT_FOUND": 404,
    "WAITLIST_NOT_FOUND": 404,
    # 409：请求没问题，但和当前状态冲突
    "SCHEDULE_CLOSED": 409,
    "SCHEDULE_EXPIRED": 409,
    "ALREADY_BOOKED_THIS_SLOT": 409,
    "DAILY_DEPT_LIMIT": 409,
    "ALREADY_WAITING": 409,
    "ALREADY_CANCELLED": 409,
    "WAITLIST_ALREADY_LEFT": 409,
    "CANCEL_WINDOW_CLOSED": 409,
    # ---- R5 五个加分项新增 ----
    "COOLDOWN_ACTIVE": 409,
    "CONFIRM_TIMEOUT": 409,
    "NOT_PENDING": 409,
    "CHECKIN_NOT_OPEN": 409,
    "CHECKIN_WINDOW_CLOSED": 409,
    "ALREADY_CHECKED_IN": 409,
    # 403：用户自身状态不允许操作
    "USER_BLACKLISTED": 403,
}

# 每个错误码的默认文案。抛出时若传了 message 就用传入的，
# 没传就用这里的，保证不会出现「有 code 没话说」的响应。
ERROR_MESSAGES = {
    "UNAUTHORIZED": "请先登录",
    "INVALID_IDENTITY": "身份信息格式有误",
    "INVALID_PARAM": "筛选参数取值非法",
    "SCHEDULE_NOT_FOUND": "该排班不存在",
    "DOCTOR_NOT_FOUND": "该医生不存在",
    "APPOINTMENT_NOT_FOUND": "该预约不存在",
    "WAITLIST_NOT_FOUND": "该候补记录不存在",
    "SCHEDULE_CLOSED": "该时段医生已停诊",
    "SCHEDULE_EXPIRED": "该排班日期已过，无法预约",
    "ALREADY_BOOKED_THIS_SLOT": "你已约过该时段",
    "DAILY_DEPT_LIMIT": "同一天同一科室限约 1 个号",
    "ALREADY_WAITING": "你已在候补队列中",
    "ALREADY_CANCELLED": "该预约已取消，无需重复操作",
    "WAITLIST_ALREADY_LEFT": "该候补已放弃或已转正",
    "CANCEL_WINDOW_CLOSED": "临近开诊，已不能取消",
    "COOLDOWN_ACTIVE": "刚退掉这个时段的号，请稍后再试",
    "CONFIRM_TIMEOUT": "确认已超时，号已顺延给下一位",
    "NOT_PENDING": "该预约不处于待确认状态",
    "CHECKIN_NOT_OPEN": "就诊当天才开放签到",
    "CHECKIN_WINDOW_CLOSED": "就诊日期已过，不能再签到",
    "ALREADY_CHECKED_IN": "你已签到，无需重复操作",
    "USER_BLACKLISTED": "账号已被限制，请联系门诊办",
}


class BizError(Exception):
    """业务规则被违反时抛出。

    用法：raise BizError("SLOT_FULL") 只这个的话 SLOT_FULL 其实不是错误，
    见 docs/API.md 第 3 节末尾的说明 —— 号满走候补，是正常业务流。

    Args:
        code: 业务错误码，必须是 ERROR_HTTP_STATUS 里登记过的。
        message: 覆盖默认文案。用于需要带动态信息的场景，
            比如「你已在候补第 3 位」。
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, code)
        self.http_status = ERROR_HTTP_STATUS.get(code, 400)
        super().__init__(f"{code}: {self.message}")


async def biz_error_handler(request: Request, exc: BizError) -> JSONResponse:
    """把业务异常翻译成统一响应体。

    响应体形状固定为 {"ok": false, "error": {"code": ..., "message": ...}}，
    与成功时的 {"ok": true, "data": ...} 对称。
    前端只需要判断 ok 的真假来决定取 data 还是取 error。
    """
    return JSONResponse(
        status_code=exc.http_status,
        content={"ok": False, "error": {"code": exc.code, "message": exc.message}},
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """把框架自己抛的 422 收编进统一响应形状。

    FastAPI 默认对「字段缺失 / 类型不对」返回 422 与
    {"detail": [{"type": "int_parsing", "loc": ["body","schedule_id"], ...}]}，
    这和我们的 {"ok": false, "error": {...}} 是两套结构。
    契约规定对外只有一种错误形状（见 docs/API.md 第 2 节），
    因此这里统一降级为 400 INVALID_PARAM。

    分工不变：本处理器只管「请求结构本身是坏的」，
    业务规则冲突仍旧由 BizError 走各自的 409 / 403 / 404。

    刻意**不**把 exc.errors() 的细节塞进 message：
    那会把内部字段名（loc=["body","schedule_id"]）暴露到对外响应里，
    前端用不上，反而可能被拿来探测接口结构。
    """
    return JSONResponse(
        status_code=400,
        content={
            "ok": False,
            "error": {
                "code": "INVALID_PARAM",
                "message": "请求参数格式有误",
            },
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """在 app 上挂好异常处理器。

    放在这里而不是 main.py，是为了让 main.py 只保留「装配」这一件事：
    挂静态目录、注册路由、挂异常处理器，读起来一目了然。
    """
    app.add_exception_handler(BizError, biz_error_handler)
    # 后注册者覆盖先注册者 —— 这里就是要盖掉 FastAPI 自带的 422 处理器。
    app.add_exception_handler(RequestValidationError, validation_error_handler)
