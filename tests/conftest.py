"""pytest 公共夹具。

两条原则：

1. **每个测试用独立的临时数据库**，绝不共用 `data/clinic.db`。
   那是演示用的库，被测试写进一堆「占位 1 号退号」的脏数据后，演示就穿帮了。
2. **测试自建最小数据集，不依赖 `seed.sql`**。
   种子数据是为了演示好看，会随演示需要随时调整；
   若测试依赖它，改一次演示数据就要改一批测试，本末倒置。

于是每个测试拿到的是一个只有表结构、没有任何数据的空库，
要什么数据由测试自己造 —— 这样读测试时，一行就能看出当前在构造什么场景。

连接依赖的替换方式见下方 `client` 夹具的注释，那里有个 Python 默认参数的坑。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# pytest 只会把 tests/ 插进 sys.path，项目根并不在其中。
# 没有下面这几行，`from app import db` 会直接 ModuleNotFoundError。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import sqlite3  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import clock  # noqa: E402


class Fixtures:
    """最小数据集的构造器。

    所有方法都直接操作「当前测试的那条连接」，
    因此造数据的人与被测代码看到的是同一份数据、同一个事务 ——
    测完直接查库就能断言，不必再走一遍 HTTP。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._n = 0

    # ---- 基础对象 ----

    def date(self, offset_days: int) -> str:
        """相对今天的日期。

        用 SQL 算而不是 Python 的 `date.today()`，
        是为了与 `guards` 判定「今天」时用**同一个时钟** ——
        否则换一台时区不同的机器，测试会莫名其妙地变红。
        """
        # f"{n:+d} day" 能把 3 变成 "+3 day"、-1 变成 "-1 day"，
        # 负数时不会拼出 "+-1 day" 这种 SQLite 认不出来的东西。
        return self.conn.execute(
            "SELECT date('now','localtime',?)", (f"{offset_days:+d} day",)
        ).fetchone()[0]

    def user(self, name: str = "测试用户") -> int:
        """造一个用户。

        三要素必须各不相同，否则撞 `users` 上的三列唯一索引。
        这里用一个自增序号同时生成身份证后 4 位与手机号，保证不重复。
        """
        self._n += 1
        return self.conn.execute(
            "INSERT INTO users(name, id_card_last4, phone) VALUES (?, ?, ?)",
            (name, f"{self._n:04d}", f"138{self._n:08d}"),
        ).lastrowid

    def doctor(self, name: str = "测试医生", department: str = "测试科") -> int:
        return self.conn.execute(
            "INSERT INTO doctors(name, title, department) VALUES (?, '主任医师', ?)",
            (name, department),
        ).lastrowid

    def schedule(
        self,
        doctor_id: int,
        offset_days: int = 1,
        period: str = "morning",
        total: int = 10,
        is_closed: int = 0,
    ) -> int:
        """造一个排班。日期默认放在明天，落到"过期"的坑里。"""
        return self.conn.execute(
            "INSERT INTO schedules(doctor_id, work_date, period, total_slots,"
            " is_closed) VALUES (?, ?, ?, ?, ?)",
            (doctor_id, self.date(offset_days), period, total, is_closed),
        ).lastrowid

    # ---- 造场景用的旁路手段 ----

    def fill(self, schedule_id: int, up_to: int) -> list[tuple[int, int]]:
        """把排班的 1..up_to 号占满。

        返回 `[(预约 id, 持号人 id), ...]` —— 两个都得给，
        因为退号必须由持号人本人发起，测试后面要用得到这个人。

        绕过业务层直接插，只用于**造场景**（比如"让这个排班没号了"）。
        记账必须一并同步，否则 `booked_count` 与实占数对不上，
        后面依赖它的断言会全线崩掉 —— 而崩掉的是测试，不是被测代码。
        """
        pairs = []
        for seq in range(1, up_to + 1):
            holder = self.user(f"占位{seq}")
            appt = self.conn.execute(
                "INSERT INTO appointments(user_id, schedule_id, seq_no)"
                " VALUES (?, ?, ?)",
                (holder, schedule_id, seq),
            ).lastrowid
            pairs.append((appt, holder))
        self.conn.execute(
            "UPDATE schedules SET booked_count = ? WHERE id = ?",
            (up_to, schedule_id),
        )
        return pairs

    def release(self, appointment_id: int) -> None:
        """直接释放一个序号（**不触发递补**），用于造出「有空号」的场景。

        刻意不走业务层：cancel_booking 会顺带递补队首，
        而造场景时我们要的只是"这个号空着"，至于谁来接由被测代码决定。
        """
        self.conn.execute(
            "UPDATE appointments SET status = 'cancelled' WHERE id = ?",
            (appointment_id,),
        )
        self.conn.execute(
            "UPDATE schedules SET booked_count = booked_count - 1"
            " WHERE id = (SELECT schedule_id FROM appointments WHERE id = ?)",
            (appointment_id,),
        )

    def enqueue(self, user_id: int, schedule_id: int, position: int = 1) -> int:
        """直接插一条候补记录，用于构造「他已经在排队」的初始状态。"""
        return self.conn.execute(
            "INSERT INTO waitlist(user_id, schedule_id, position)"
            " VALUES (?, ?, ?)",
            (user_id, schedule_id, position),
        ).lastrowid

    def occupy(self, user_id: int, schedule_id: int, seq_no: int) -> int:
        """让某个人直接持有一个序号，用于精确构造数据（不同步计数）。"""
        return self.conn.execute(
            "INSERT INTO appointments(user_id, schedule_id, seq_no)"
            " VALUES (?, ?, ?)",
            (user_id, schedule_id, seq_no),
        ).lastrowid

    # ---- 断言用的查询 ----

    def booked_count(self, schedule_id: int) -> int:
        return self.conn.execute(
            "SELECT booked_count FROM schedules WHERE id = ?", (schedule_id,)
        ).fetchone()[0]

    def real_occupied(self, schedule_id: int) -> int:
        """数一数库里真正占着号的人头数。

        与 `booked_count` 对比，就是「记账是否对得上」的检查。

        状态清单取自 `db.ACTIVE_STATUS_CLAUSE` 而不是在这里抄一遍：
        R5 新增 `pending` 时，这两处硬编码的字符串没跟着改，
        于是「记账对不上」的断言**算错了** ——
        三个待确认的号被当成"没人占"，报出 `assert 3 == 6`。
        测试工具自身写错，比被测代码写错更难发现：
        它会给出一条看起来完全合理的失败信息，把人往错误的方向引。
        """
        return self.conn.execute(
            "SELECT COUNT(*) FROM appointments "
            f" WHERE schedule_id = ? AND {db.ACTIVE_STATUS_CLAUSE}",
            (schedule_id,),
        ).fetchone()[0]

    def seq_nos(self, schedule_id: int) -> list[int]:
        """当前所有被占用的序号，升序。

        与 `real_occupied` 同理，状态清单统一取自 db，避免各抄一份。
        """
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT seq_no FROM appointments "
                f" WHERE schedule_id = ? AND {db.ACTIVE_STATUS_CLAUSE}"
                " ORDER BY seq_no",
                (schedule_id,),
            )
        ]

    def waitlist_status(self, waitlist_id: int) -> str:
        return self.conn.execute(
            "SELECT status FROM waitlist WHERE id = ?", (waitlist_id,)
        ).fetchone()[0]

    def raw_position(self, waitlist_id: int) -> int:
        """库里存的排序键（与对外的名次不是一回事）。"""
        return self.conn.execute(
            "SELECT position FROM waitlist WHERE id = ?", (waitlist_id,)
        ).fetchone()[0]


# ------------------------------------------------------------
# 夹具
# ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clock_is_never_left_frozen():
    """每个用例跑完都把时钟解冻（autouse，无需声明）。

    为什么必须做成 autouse：冻结是**模块级全局状态**，
    一个用例忘了 unfreeze，后面所有用例都会跑在错误的时间上，
    而失败信息会是"没有 pending 过期""冷却期没生效"这种
    看起来像业务逻辑写错的话 —— 真正的元凶却在第一条用例里。

    这类"状态泄漏"用 autouse 是唯一稳妥的解法：
    指望每个写测试的人都记得在末尾清理，等于把可靠性寄托在记忆力上。
    """
    yield
    clock.unfreeze()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """一个只有表结构、没有任何数据的临时库。"""
    path = tmp_path / "clinic.db"
    # 刻意不用 db.init_db()：它会把 seed.sql 一起灌进来（理由见文件头）。
    conn = db.connect(path)
    try:
        conn.executescript((db.SQL_DIR / "schema.sql").read_text(encoding="utf-8"))
    finally:
        conn.close()
    return path


@pytest.fixture()
def conn(db_path: Path):
    """测试自己用的那条直连：造数据、做断言。"""
    c = db.connect(db_path)
    yield c
    c.close()


@pytest.fixture()
def fx(conn: sqlite3.Connection) -> Fixtures:
    return Fixtures(conn)


def _make_client(db_path: Path) -> TestClient:
    """把 app 的数据库依赖改指向指定文件。"""

    def _fake_get_conn():
        c = db.connect(db_path)
        try:
            yield c
        finally:
            c.close()

    # 用依赖覆盖，而不是去改 app.db.DB_PATH：
    # connect() 的默认参数 `db_path=DB_PATH` 在 import 的那一刻就求值绑定了，
    # 事后改模块变量不会影响它 —— 这是 Python 默认参数只求值一次的坑。
    # 依赖覆盖是 FastAPI 官方留的口子，改的是注册表中的可调用对象，没有这个坑。
    app.dependency_overrides[db.get_conn] = _fake_get_conn
    # 刻意不用 with TestClient(app)：那会触发 lifespan，
    # 而 lifespan 里调的是 db.init_db()，会把真实库也建出来。
    return TestClient(app)


@pytest.fixture()
def client(db_path: Path):
    c = _make_client(db_path)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def seeded_path(tmp_path: Path) -> Path:
    """灌了演示数据的临时库，用于验证种子数据本身没坏。"""
    path = tmp_path / "seeded.db"
    # 这里是唯一用 init_db 的地方：就是要连种子一起灌
    db.init_db(path)
    return path


@pytest.fixture()
def seeded_conn(seeded_path: Path):
    c = db.connect(seeded_path)
    yield c
    c.close()


@pytest.fixture()
def seeded_client(seeded_path: Path):
    c = _make_client(seeded_path)
    yield c
    app.dependency_overrides.clear()


# ------------------------------------------------------------
# 真 uvicorn 服务（R5 并发测试专用）
#
# 本文件上半部分的 TestClient 用于常规用例，
# 但**并发用例必须用这里的东西**，理由写在 LiveServer 的类注释里。
# ------------------------------------------------------------


def _free_port() -> int:
    """向操作系统要一个当前空闲的端口。

    不让测试写死 8001：机器上可能同时开着演示服务，也可能并行跑别的测试，
    写死端口撞车时报的是 "Address already in use"，
    看起来像代码有 bug，实际只是端口被占，排查很费时间。
    """
    # bind 到端口 0 = 请系统自己挑一个空闲的
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


class LiveServer:
    """一个跑在**独立进程**里的真 uvicorn 服务。

    R5 的并发用例为什么不能用 TestClient：

    1. **TestClient 会把并发串行化掉**。它在一个线程里发请求、
       在另一个线程里处理，但请求是逐个构造、逐个走完的，
       两个请求根本不会重叠 —— 用它能测出"并发安全"，那是假象。
    2. **它不经过网络层**，请求对象是直接构造出来的，
       socket 排队、连接复用这些都观察不到。
    3. **它共用当前进程的内存**，所以能用依赖覆盖把数据库指到临时文件；
       独立进程跨不了这道边界，只能靠 `CLINIC_DB` 环境变量
       （见 `app/db.py` 的 resolve_db_path）。

    换成真进程之后，还有一件更关键的事：**并发是真的发生了吗？**
    我们的路由全部是同步 `def`（不是 `async def`），
    Starlette 会把同步路由丢进线程池执行，于是请求之间才会真正重叠。
    若当初写成 `async def`，它们会挤在同一个事件循环里串行执行，
    SQLite 那把锁永远不会被竞争 —— 那样测出来的"没有超卖"毫无说服力。
    """

    # 启动后等待服务就绪的最长时间（秒）
    READY_TIMEOUT = 30.0

    def __init__(self, port: int, db_path: Path, process: subprocess.Popen) -> None:
        self.port = port
        self.db_path = db_path
        self._proc = process
        self.base_url = f"http://127.0.0.1:{port}"
        # 测试自己用的直连：造场景、以及并发之后直接查库做断言。
        # 刻意**不走 HTTP** 去断言 —— 并发测试关心的是"库里最终是什么样"，
        # 而不是"某个接口说自己成功了"。
        self.conn = db.connect(db_path)
        self.fx = Fixtures(self.conn)

    # ---- 生命周期 ----

    def wait_ready(self) -> None:
        """轮询一个真实业务接口，直到服务能应答。

        不做这一步，第一批请求会直接 ConnectionRefused，
        报出来的错是网络问题，很容易被误判成代码的 bug。

        探针用 `/api/departments` 而不是专门的 `/api/health`：
        本项目没有健康检查接口，而临时加一个要先改契约 —— 不值得。
        用真实业务接口反而**更好**：它证明的不只是"端口通了"，
        而是路由已注册、静态目录已挂载、数据库连接已可用，
        整个 app 真正装配完成了。
        一个只返回 `{"status":"ok"}` 的假探针证明不了这些，
        服务还没准备好它就会先返回 200。
        """
        deadline = time.monotonic() + self.READY_TIMEOUT
        url = f"{self.base_url}/api/departments"
        while time.monotonic() < deadline:
            # 进程自己退出了就没必要再等，直接把死因报出来
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"uvicorn 进程启动失败，退出码 {self._proc.returncode}"
                )
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                pass
            time.sleep(0.05)
        raise RuntimeError(f"uvicorn 在 {self.READY_TIMEOUT}s 内没有就绪")

    def stop(self) -> None:
        """停掉服务进程。先礼后兵：terminate 不行就 kill。"""
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        self.conn.close()

    # ---- 发请求 ----

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """发一个请求，返回 `{"status", "payload", "elapsed", "started"}`。

        刻意不抛异常：并发测试要的是"收集所有结果再统一断言"，
        单个请求抛异常会让整个 race 提前中断，其他线程的结果全丢了。
        """
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if user_id is not None:
            req.add_header("X-User-Id", str(user_id))

        started = time.monotonic()
        # urllib 与 fetch 一样：**HTTP 4xx/5xx 不抛异常，只有网络层失败才抛**。
        # 必须自己看状态码，否则 409 会被当成成功。
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read()
        except Exception as e:  # 网络层的失败（连接被拒、超时）
            return {
                "status": None,
                "payload": None,
                "error": repr(e),
                "elapsed": time.monotonic() - started,
                "started": started,
            }

        payload = None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = None
        return {
            "status": status,
            "payload": payload,
            "error": None,
            "elapsed": time.monotonic() - started,
            "started": started,
        }

    def race(self, tasks: list[tuple], barrier_timeout: float = 30.0) -> list[dict]:
        """让一批请求**在同一刻**发出，返回全部结果。

        tasks 的每一项是 `(method, path, body, user_id)`。

        为什么要用 Barrier 对齐起跑线：
        不用 Barrier 的话，线程池是"提交一个、立刻开跑一个"，
        20 个请求会前后错开 —— 很可能第一个已经写完提交了，最后一个才刚发出，
        于是它们根本没在抢，只是排队而已。那样的测试**测不出超卖**，
        因为它制造的场景本身就没有任何竞争。

        用 Barrier 让所有线程先到齐、再一起冲出去，才是真的"同时抢"。
        """
        n = len(tasks)
        barrier = threading.Barrier(n)
        results: list[dict] = [None] * n  # type: ignore[list-item]

        def _one(index: int, task: tuple) -> None:
            method, path, body, user_id = task
            try:
                # 起跑线：到齐之前一个都不许先跑
                barrier.wait(timeout=barrier_timeout)
            except threading.BrokenBarrierError:
                # 有同伴没赶上（比如超时），那就自己先跑，
                # 降级为"不齐也要测"，总比整个用例挂掉强
                pass
            results[index] = self.request(method, path, body, user_id)

        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(_one, i, t) for i, t in enumerate(tasks)]
            for f in futures:
                f.result(timeout=60.0)
        return results


@pytest.fixture()
def live(tmp_path: Path):
    """一个专属的真 uvicorn 服务：独立进程 + 独立端口 + 独立临时库。

    返回的是 LiveServer 对象，测试通过 `live.fx` 造场景、
    通过 `live.race()` 发并发请求、通过 `live.conn` 直接查库做断言。
    """
    path = tmp_path / "live.db"

    # 必须**先把文件建出来**再去起 uvicorn。
    # 服务启动时 lifespan 会调 db.init_db()，它只在文件不存在时灌种子；
    # 若空着手启动，演示数据就被灌进来了，并发场景就不可控了。
    boot = db.connect(path)
    try:
        boot.executescript((db.SQL_DIR / "schema.sql").read_text(encoding="utf-8"))
    finally:
        boot.close()

    port = _free_port()
    env = os.environ.copy()
    env[db.DB_PATH_ENV] = str(path)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        # 丢弃 uvicorn 的输出：它每来一个请求就打一行访问日志，
        # 20 个并发请求会把测试输出冲得没法看。
        # 排查问题时把这两行删掉，就能直接看到 traceback。
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    server = LiveServer(port, path, proc)
    try:
        server.wait_ready()
        yield server
    finally:
        server.stop()
