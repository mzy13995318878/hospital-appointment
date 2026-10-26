"""后端应用包。

包本身只做容器，具体模块说明见各自文件：

    app/db.py       数据库连接与初始化（R1）
    app/main.py     FastAPI 入口（R2）
    app/deps.py     从请求头解析当前用户（R2）
    app/schemas.py  请求与响应的数据模型（R2）
    app/routers/    路由层：收 HTTP 请求、调服务、返响应（R2/R3）
    app/services/   服务层：业务规则与事务（R2/R3）
"""
