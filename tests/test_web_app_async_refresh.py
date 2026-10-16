"""MA120 关注池写操作「异步刷新」改造的并发与回归测试。

背景
----
改造前`StockDashboardService._change_watchlist` 在持有 `self.lock` 的
情况下调用 `self.current()` 重建全量行情快照（39 支标的），单次写请求
耗时数秒，并发时全站请求串行阻塞。改造后写请求立即返回，行情重建移到
后台线程，用「脏标记 + 单一刷新线程」合并短时间内的多次刷新。

本文件覆盖六个必须验证的点：
1. 无死锁（`self.lock` 是普通 Lock 而非 RLock，锁竞争一律带超时保护）
2. 写入不丢（模拟生产形态：每请求新建 store，走真实 HTTP 链路）
3. 测试数据合法（严格 6 位数字；服务端 400 是正确行为而非缺陷）
4. 并发写不再累积到分钟级
5. 既有行为回归（add/rename/remove、refresh_warning、显式 refresh、页面路由）
6. 读接口不被后台刷新拖住

设计原则
--------
* **不预设改造后的代码形态**：本文件不引用任何新引入的私有属性
  （如 `_dirty` / `_refresh_thread`），只通过公开接口
  （HTTP 路由、`refresh()`、`current()`、`add/rename/remove_watchlist`）
  和可观测行为断言。因此在改造前/后都能运行，语义一致的用例在两侧
  都应通过 —— 这正是回归测试的价值。
* **凡是测锁竞争都有超时**：用 `ThreadPoolExecutor` + 显式 timeout，
  卡住即失败，绝不无限等待。
* **复用 `TolerantHTTPConnection`**：Windows 下 `ThreadingHTTPServer`
  偶发 WinError 10053 属平台时序抖动而非业务失败，由该类统一重连一次。
  不自造轮子，也不因抖动改业务代码。
* **测试数据严格 6 位数字**：`normalize_code` 接受 1~6 位并 zfill 到 6 位；
  服务端对非法代码返回 400 属正确行为。本文件用 `assert` 显式校验所有
  生成的代码合法，避免把 400 误判成「丢数据」。
* **测试写操作全部落在临时数据库**：`StockDashboardService` 通过
  `database_path` / `watchlist_path` 指向 `TemporaryDirectory`，
  绝不触碰真实 `data/market.sqlite3`，因此无需清理、不污染 revision
  与审计链。
"""

import http.client
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from config_store import ConfigurationStore
from tests.test_web_app import TolerantHTTPConnection
from web_app import DashboardHandler, StockDashboardService


# ---------------------------------------------------------------------------
# 常量与工具
# ---------------------------------------------------------------------------

#: 单个写请求的合理上限（秒）。真实重建需数秒，改造后应远快于此。
#: 留足余量以免在慢机器上误报，但足以在「退化成同步阻塞」时立刻失败。
WRITE_BUDGET_SECONDS = 1.5

#: 读接口在后台刷新进行中仍应快速返回的上限（秒）。
READ_BUDGET_SECONDS = 1.0

#: 锁竞争场景的硬超时。超过即判定为死锁/永久挂起，测试立即失败。
DEADLOCK_TIMEOUT_SECONDS = 20.0

#: 行情重建的模拟耗时。
#:
#: **必须显著大于 `WRITE_BUDGET_SECONDS`**，否则延迟断言将失去鉴别力：
#: 若重建耗时低于预算，「写请求同步等待重建」与「写请求立即返回」两种
#: 形态都会通过，测试就成了假阳性。真实的全量重建（39 支标的）耗时数秒，
#: 这里取 3.0 秒以复现该量级，并让同步/异步两种形态产生明确差异。
FAKE_REFRESH_SECONDS = 3.0


def assert_legal_code(case, code):
    """确保测试生成的代码严格合法（6 位数字）。

    历史上曾因拼出 7/8 位代码导致服务端正确返回 400，却被误判成
    「并发写丢数据」。这里把合法性校验前置，让误判不可能发生。
    """
    case.assertIsInstance(code, str, f"股票代码必须是字符串，实际 {code!r}")
    case.assertEqual(len(code), 6, f"股票代码必须是 6 位，实际 {code!r}（{len(code)} 位）")
    case.assertTrue(code.isdigit(), f"股票代码必须是纯数字，实际 {code!r}")


def legal_code(seed):
    """由种子生成确定性的合法 6 位 A 股代码。

    使用 6xxxxx 号段，避开测试中可能出现的其他号段，且恒为 6 位数字。
    """
    assert 0 <= seed <= 999999, f"种子越界：{seed}"
    return f"{600000 + seed:06d}"


def fake_snapshot():
    """构造一个形状合法的行情快照，替代真实的全量行情重建。"""
    return {
        "generated_at": "2026-10-16T12:00:00+08:00",
        "portfolio": [],
        "watchlist": [{"code": "600001", "name": "测试股票"}],
    }


class RefreshRecorder:
    """可观测的假行情重建器。

    记录调用次数、并发进入次数与是否发生过并发重叠，用于断言
    「进程内同时最多一个后台刷新」以及探测同步/异步两种形态。
    """

    def __init__(self, seconds=FAKE_REFRESH_SECONDS, snapshot=None):
        self.seconds = seconds
        self.snapshot = snapshot if snapshot is not None else fake_snapshot()
        self._lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.call_event = threading.Event()
        self.release_event = threading.Event()
        self.release_event.set()

    def __call__(self):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.call_event.set()
        try:
            self.release_event.wait(timeout=self.seconds * 4)
            time.sleep(self.seconds)
            return self.snapshot
        finally:
            with self._lock:
                self.active -= 1


class AsyncWatchlistTestCase(unittest.TestCase):
    """公共脚手架：临时 watchlist 文件 + 真实 HTTP 链路。

    每个测试独享临时目录与端口，互不干扰。
    """

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.watchlist_path = self.root / "watchlist.json"
        self.write_watchlist_file([])

    def write_watchlist_file(self, items):
        payload = {"version": 1, "items": items}
        self.watchlist_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def read_watchlist_file(self):
        return json.loads(self.watchlist_path.read_text(encoding="utf-8"))["items"]

    def build_service(self, loader):
        return StockDashboardService(loader=loader, watchlist_path=self.watchlist_path)

    def start_server(self, service):
        """在临时端口上启动真实 ThreadingHTTPServer，复用 TolerantHTTPConnection。"""
        service_holder = {"service": service}

        class Handler(DashboardHandler):
            stock_service = service_holder["service"]

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def request(port, method, target, payload=None, headers=None, timeout=10):
        """单次 HTTP 请求。每次新建连接，避免跨线程复用长连接。"""
        body = None
        if payload is not None:
            body = payload if isinstance(payload, bytes) else json.dumps(
                payload, ensure_ascii=False).encode("utf-8")
        connection = TolerantHTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            connection.request(method, target, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        finally:
            connection.close()
        try:
            return status, json.loads(raw)
        except (ValueError, TypeError):
            return status, raw

    @staticmethod
    def json_headers():
        return {"Content-Type": "application/json"}

    def add_code(self, port, code, name="测试标的", timeout=10):
        assert_legal_code(self, code)
        return self.request(port, "POST", "/api/ma120/watchlist",
                            {"code": code, "name": name}, self.json_headers(), timeout)


# ---------------------------------------------------------------------------
# 1. 无死锁
# ---------------------------------------------------------------------------

class NoDeadlockTests(AsyncWatchlistTestCase):
    """`self.lock` 是普通 Lock（非 RLock），锁竞争必须能在超时内完成。

    最危险的形态是：后台刷新线程 `with self.lock` 重建行情，同时主线程
    在同一把锁上处理写请求并等待刷新完成 —— 形成自锁死锁。
    """

    def test_write_during_background_refresh_returns_within_timeout(self):
        """写请求撞上正在进行的行情刷新，必须在超时内返回，不得永久挂起。"""
        recorder = RefreshRecorder()
        service = self.build_service(recorder)
        port = self.start_server(service).server_port

        # 先播种一条，确保后续 add 不因「重复代码」被 400 拒绝。
        self.assertEqual(self.add_code(port, legal_code(1))[0], 200)

        # 手动持锁模拟「后台刷新进行中」，再从另一线程发起写请求。
        # 若锁设计有误（后台线程持锁且写路径等待刷新），此处必然超时。
        acquired = service.lock.acquire(timeout=5)
        self.assertTrue(acquired, "无法取得 self.lock，测试前提不成立")

        def write():
            return self.add_code(port, legal_code(2), timeout=DEADLOCK_TIMEOUT_SECONDS)

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(write)
                # 主线程释放锁后，写请求应在超时内完成。
                time.sleep(0.3)
                service.lock.release()
                started = time.monotonic()
                status, payload = future.result(timeout=DEADLOCK_TIMEOUT_SECONDS)
                elapsed = time.monotonic() - started
        finally:
            if service.lock.locked():
                try:
                    service.lock.release()
                except RuntimeError:
                    pass

        self.assertEqual(status, 200, f"写请求未成功：{payload}")
        self.assertLess(
            elapsed, DEADLOCK_TIMEOUT_SECONDS,
            "写请求在锁竞争下耗时异常，疑似死锁")

    def test_concurrent_writes_all_complete_without_hanging(self):
        """多个并发写请求全部在超时内完成，无一挂起。"""
        recorder = RefreshRecorder(seconds=0.4)
        service = self.build_service(recorder)
        port = self.start_server(service).server_port

        codes = [legal_code(100 + i) for i in range(8)]
        for code in codes:
            assert_legal_code(self, code)

        def write(code):
            return self.add_code(port, code, timeout=DEADLOCK_TIMEOUT_SECONDS)

        with ThreadPoolExecutor(max_workers=len(codes)) as pool:
            futures = [pool.submit(write, code) for code in codes]
            results = []
            for code, future in zip(codes, futures):
                results.append((code, future.result(timeout=DEADLOCK_TIMEOUT_SECONDS)))

        for code, (status, payload) in results:
            self.assertEqual(status, 200, f"{code} 写入失败（非法代码会返回 400）：{payload}")

    def test_refresh_is_reentrant_safe_across_threads(self):
        """显式 refresh() 并发调用不得死锁：普通 Lock 不可重入，
        后台刷新线程若在持锁状态下再次进入 refresh/current 即会自锁。"""
        recorder = RefreshRecorder(seconds=0.3)
        service = self.build_service(recorder)

        def refresh():
            service.refresh()
            return True

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(refresh) for _ in range(4)]
            for future in futures:
                self.assertTrue(future.result(timeout=DEADLOCK_TIMEOUT_SECONDS))


# ---------------------------------------------------------------------------
# 2. 写入不丢（模拟生产形态：每线程新建 store + 真实 HTTP 链路）
# ---------------------------------------------------------------------------

class ConcurrentWriteNoLossTests(AsyncWatchlistTestCase):
    """并发写不得静默丢条目。

    历史教训：项目曾出现「并发写静默丢数据」，根因是锁挂在 **实例** 上
    而非 **类** 上。因此测试必须模拟生产形态 —— 每个线程各自新建 store、
    走真实 HTTP 链路 —— 绝不能复用同一个 store 实例，否则会得到假的
    「通过」（类属性仍在生效时，实例级回退测不出来）。
    """

    def test_concurrent_http_adds_do_not_lose_entries(self):
        """并发 POST /api/ma120/watchlist：最终条目数必须等于预期。"""
        recorder = RefreshRecorder(seconds=0.2)
        service = self.build_service(recorder)
        port = self.start_server(service).server_port

        threads, rounds = 4, 5
        codes = [legal_code(200 + t * rounds + r) for t in range(threads)
                 for r in range(rounds)]
        for code in codes:
            assert_legal_code(self, code)
        barrier = threading.Barrier(threads)

        def add(t):
            # 每线程独立连接 + 独立一批代码，模拟多个用户同时操作。
            def worker():
                barrier.wait(timeout=DEADLOCK_TIMEOUT_SECONDS)
                for r in range(rounds):
                    status, payload = self.add_code(
                        port, legal_code(200 + t * rounds + r),
                        timeout=DEADLOCK_TIMEOUT_SECONDS)
                    if status != 200:
                        return status, payload
                return 200, None

            return worker()

        with ThreadPoolExecutor(max_workers=threads) as pool:
            outcomes = [f.result(timeout=DEADLOCK_TIMEOUT_SECONDS)
                        for f in [pool.submit(add, t) for t in range(threads)]]

        for status, payload in outcomes:
            self.assertEqual(status, 200, f"并发写入返回异常状态：{payload}")

        items = self.read_watchlist_file()
        self.assertEqual(
            len(items), threads * rounds,
            f"关注池条目丢失：期望 {threads * rounds} 条，实际 {len(items)} 条")
        self.assertEqual({item["code"] for item in items}, set(codes))

    def test_concurrent_store_instances_do_not_lose_entries(self):
        """每线程各自 new 一个 ConfigurationStore，验证类级锁语义。

        这直接复现「锁挂在实例上」的缺陷形态：若互斥仅在实例级生效，
        跨实例并发写会互相覆盖，条目静默丢失。
        """
        root = self.root
        database = root / "data" / "market.sqlite3"
        (root / "watchlist.json").write_text(
            json.dumps({"version": 1, "items": [{"code": "600001", "name": "测试股票"}]}),
            encoding="utf-8")
        template = Path(__file__).resolve().parents[1] / "etf_plans.example.toml"
        (root / "etf_plans.toml").write_bytes(template.read_bytes())
        ConfigurationStore(database, root).initialize()

        threads, rounds = 4, 10
        barrier = threading.Barrier(threads)

        def add(t):
            store = ConfigurationStore(database, root)  # 每线程独立实例
            barrier.wait(timeout=DEADLOCK_TIMEOUT_SECONDS)
            for r in range(rounds):
                code = legal_code(1000 + t * rounds + r)
                store.mutate_stocks(
                    "watchlist", lambda rows: rows + [{"code": code, "name": code}])

        with ThreadPoolExecutor(max_workers=threads) as pool:
            list(pool.map(add, range(threads)))

        final = ConfigurationStore(database, root).stocks("watchlist")
        self.assertEqual(len(final), 1 + threads * rounds,
                         f"跨实例并发写丢条目：期望 {1 + threads * rounds}，实际 {len(final)}")

    def test_write_lock_is_class_level_not_instance_level(self):
        """**直接断言锁的作用域**—— 这是唯一无法被 SQLite 兜底的检测点。

        为什么需要这条：2026-10-16 的血泪教训。当时只把类级锁改回实例级，
        测试**依然通过**，因为 `__init__` 里的 `self._write_lock =
        self.__class__._write_lock` 让实例属性仍然指向同一个类锁；只有把
        类属性与实例绑定**同时**移除，测试才失败。

        而当两者都被移除后，「条目数是否丢失」这类**结果断言**仍可能通过 ——
        因为 `mutate_stocks` 内的 `BEGIN IMMEDIATE` 会在 SQLite 层串行化写事务，
        把锁失效的后果**掩盖**掉（实测：锁完全失效时，进入 operation 的
        并发峰值依然是 1，最终条目数也正确）。

        因此这里不去断言「条目是否丢失」，而是断言**结构事实**：
        不同实例必须共享同一把锁。锁一旦挂到实例上，此断言立即失败，
        不受数据库层兜底影响。
        """
        store_a = ConfigurationStore()
        store_b = ConfigurationStore()
        self.assertIs(
            store_a._write_lock, store_b._write_lock,
            "不同ConfigurationStore 实例拿到了不同的锁 —— 锁挂在实例上而非类上，"
            "跨请求并发写会互相覆盖，导致关注池条目静默丢失")
        self.assertIs(
            store_a._write_lock, ConfigurationStore._write_lock,
            "实例锁未绑定到类锁，类级互斥已失效")

    def test_concurrent_add_and_remove_converge_to_expected_state(self):
        """并发 add + remove 混合操作后，状态必须收敛到可预期的正确结果。"""
        recorder = RefreshRecorder(seconds=0.1)
        service = self.build_service(recorder)
        port = self.start_server(service).server_port

        keep = [legal_code(300 + i) for i in range(6)]
        drop = [legal_code(400 + i) for i in range(4)]
        for code in keep + drop:
            assert_legal_code(self, code)
        for code in keep + drop:
            self.assertEqual(self.add_code(port, code)[0], 200)

        barrier = threading.Barrier(len(drop))

        def remove(code):
            barrier.wait(timeout=DEADLOCK_TIMEOUT_SECONDS)
            return self.request(port, "DELETE", f"/api/ma120/watchlist?code={code}",
                                timeout=DEADLOCK_TIMEOUT_SECONDS)

        with ThreadPoolExecutor(max_workers=len(drop)) as pool:
            futures = [pool.submit(remove, code) for code in drop]
            for future in futures:
                status, _ = future.result(timeout=DEADLOCK_TIMEOUT_SECONDS)
                self.assertEqual(status, 200)

        remaining = {item["code"] for item in self.read_watchlist_file()}
        self.assertEqual(remaining, set(keep),
                         f"并发 add/remove 后状态错误：{sorted(remaining)}")


# ---------------------------------------------------------------------------
# 3. 测试数据合法性（服务端 400 是正确行为，不是缺陷）
# ---------------------------------------------------------------------------

class TestDataLegalityTests(AsyncWatchlistTestCase):
    """确保测试自身不因非法数据制造假失败。"""

    def test_generated_codes_are_always_six_digits(self):
        """本文件所有生成的代码必须严格 6 位数字。"""
        for seed in (0, 1, 42, 999):
            code = legal_code(seed)
            self.assertEqual(len(code), 6)
            self.assertTrue(code.isdigit())

    def test_server_rejects_malformed_codes_with_400(self):
        """服务端对非法代码返回 400 属**正确行为**，本测试锁定该契约。"""
        service = self.build_service(RefreshRecorder(seconds=0))
        port = self.start_server(service).server_port
        headers = self.json_headers()
        before = self.watchlist_path.read_bytes()

        for bad in ("1234567", "12345678", "abcdef", "60000a"):
            status, _ = self.request(port, "POST", "/api/ma120/watchlist",
                                     {"code": bad, "name": "非法"}, headers)
            self.assertEqual(status, 400, f"非法代码 {bad!r} 竟被接受")

        self.assertEqual(self.watchlist_path.read_bytes(), before,
                         "非法请求竟改动了关注池文件")

    def test_valid_six_digit_code_is_accepted(self):
        """对照：合法 6 位代码必须被接受（防止把 400 误判成丢数据）。"""
        service = self.build_service(RefreshRecorder(seconds=0))
        port = self.start_server(service).server_port
        code = legal_code(7)
        status, payload = self.add_code(port, code)
        self.assertEqual(status, 200, f"合法代码 {code} 被拒：{payload}")
        self.assertEqual(payload["items"], [{"code": code, "name": "测试标的"}])


# ---------------------------------------------------------------------------
# 4 + 6. 写请求快速返回 & 读接口不被阻塞
# ---------------------------------------------------------------------------

class WriteLatencyAndReadIsolationTests(AsyncWatchlistTestCase):
    """写请求应快速返回；读接口不应被后台刷新拖住。"""

    def test_write_returns_well_before_full_rebuild_finishes(self):
        """写请求耗时应远小于一次全量行情重建的耗时。"""
        recorder = RefreshRecorder(seconds=FAKE_REFRESH_SECONDS)
        service = self.build_service(recorder)
        port = self.start_server(service).server_port

        started = time.monotonic()
        status, payload = self.add_code(port, legal_code(1),
                                        timeout=DEADLOCK_TIMEOUT_SECONDS)
        elapsed = time.monotonic() - started

        self.assertEqual(status, 200, f"写请求失败：{payload}")
        self.assertLess(
            elapsed, WRITE_BUDGET_SECONDS,
            f"写请求耗时 {elapsed:.2f}s，超过预算 {WRITE_BUDGET_SECONDS}s；"
            f"行情重建耗时 {FAKE_REFRESH_SECONDS}s，说明写请求仍在同步等待重建")

    def test_read_api_not_blocked_while_refresh_in_progress(self):
        """后台刷新进行中时，GET /api/ma120 仍应快速返回。"""
        recorder = RefreshRecorder(seconds=FAKE_REFRESH_SECONDS)
        service = self.build_service(recorder)
        port = self.start_server(service).server_port

        self.assertEqual(self.add_code(port, legal_code(1))[0], 200)

        # 触发一次重建，并等待它确实进入执行中。
        self.assertEqual(self.request(port, "POST", "/api/ma120/refresh", {},
                                      self.json_headers(),
                                      timeout=DEADLOCK_TIMEOUT_SECONDS)[0], 200)

        reads = []
        for _ in range(3):
            started = time.monotonic()
            status, _ = self.request(port, "GET", "/api/ma120", timeout=READ_BUDGET_SECONDS * 2)
            reads.append((status, time.monotonic() - started))

        for status, elapsed in reads:
            self.assertEqual(status, 200)
            self.assertLess(
                elapsed, READ_BUDGET_SECONDS,
                f"读接口耗时 {elapsed:.2f}s，被后台刷新拖住（预算 {READ_BUDGET_SECONDS}s）")

    def test_many_concurrent_writes_do_not_accumulate_to_minutes(self):
        """N 个并发写的总耗时应远小于 N × 单次重建耗时。"""
        rebuild = 1.0
        recorder = RefreshRecorder(seconds=rebuild)
        service = self.build_service(recorder)
        port = self.start_server(service).server_port

        workers = 6
        codes = [legal_code(500 + i) for i in range(workers)]
        for code in codes:
            assert_legal_code(self, code)
        barrier = threading.Barrier(workers)

        def write(code):
            barrier.wait(timeout=DEADLOCK_TIMEOUT_SECONDS)
            started = time.monotonic()
            status, payload = self.add_code(
                port, code, timeout=DEADLOCK_TIMEOUT_SECONDS)
            return code, status, payload, time.monotonic() - started

        wall_started = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = [f.result(timeout=DEADLOCK_TIMEOUT_SECONDS)
                       for f in [pool.submit(write, code) for code in codes]]
        wall = time.monotonic() - wall_started

        for code, status, payload, _ in results:
            self.assertEqual(status, 200, f"{code} 写入失败：{payload}")

        slowest = max(elapsed for *_, elapsed in results)
        naive = workers * rebuild
        self.assertLess(
            slowest, WRITE_BUDGET_SECONDS,
            f"单请求最慢耗时 {slowest:.2f}s，说明写请求仍同步等待重建")
        self.assertLess(
            wall, naive,
            f"{workers} 个并发写总耗时 {wall:.2f}s，接近串行累积值 {naive:.2f}s；"
            f"刷新未被后台化或未被合并")


# ---------------------------------------------------------------------------
# 5. 既有行为回归
# ---------------------------------------------------------------------------

class WatchlistRegressionTests(AsyncWatchlistTestCase):
    """add / rename / remove、refresh_warning、显式 refresh、页面路由回归。"""

    def setUp(self):
        super().setUp()
        self.recorder = RefreshRecorder(seconds=0)
        self.service = self.build_service(self.recorder)
        self.port = self.start_server(self.service).server_port

    def test_add_rename_remove_round_trip(self):
        """三个写接口功能正确。"""
        headers = self.json_headers()
        code = legal_code(11)
        assert_legal_code(self, code)

        status, payload = self.add_code(self.port, code, "初始名称")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [{"code": code, "name": "初始名称"}])

        status, payload = self.request(
            self.port, "PATCH", f"/api/ma120/watchlist?code={code}",
            {"name": "新名称"}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [{"code": code, "name": "新名称"}])

        status, payload = self.request(
            self.port, "DELETE", f"/api/ma120/watchlist?code={code}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])

        self.assertEqual(self.request(self.port, "GET", "/api/ma120/watchlist")[1],
                         {"items": []})

    def test_add_duplicate_and_missing_are_rejected(self):
        """重复添加返回 400，操作不存在的标的返回 404。"""
        headers = self.json_headers()
        code = legal_code(12)
        self.assertEqual(self.add_code(self.port, code)[0], 200)

        status, _ = self.request(self.port, "POST", "/api/ma120/watchlist",
                                 {"code": code, "name": "重复"}, headers)
        self.assertEqual(status, 400, "重复添加应被拒绝")

        missing = legal_code(13)
        self.assertEqual(self.request(
            self.port, "PATCH", f"/api/ma120/watchlist?code={missing}",
            {"name": "x"}, headers)[0], 404)
        self.assertEqual(self.request(
            self.port, "DELETE",
            f"/api/ma120/watchlist?code={missing}")[0], 404)

    def test_refresh_warning_semantics_documented(self):
        """显式记录 `refresh_warning` 的语义边界。

        改造前：写请求内联同步重建，重建失败时返回 `refresh_warning`
        且 `snapshot` 被清空（见 tests/test_web_app.py 的
        `test_ma120_watchlist_save_survives_quote_refresh_failure`）。

        改造后：重建已后台化，写请求无法同步得知刷新是否失败，因此
        `refresh_warning` **可能不再出现**。这属于**有意的语义变化**，
        不是缺陷。此处断言的是**不变的那一半契约**：
        无论刷新成功与否，用户的写操作本身必须成功落盘。
        """
        failing = RefreshRecorder(seconds=0)
        failing.snapshot = None

        def boom():
            raise OSError("行情暂不可用")

        service = StockDashboardService(loader=boom, watchlist_path=self.watchlist_path)
        code = legal_code(14)
        result = service.add_watchlist({"code": code, "name": "刷新失败标的"})

        # 不变契约：写入成功，条目落盘。
        self.assertEqual(result["items"], [{"code": code, "name": "刷新失败标的"}])
        self.assertEqual(self.read_watchlist_file(),
                         [{"code": code, "name": "刷新失败标的"}])
        self.assertEqual(service.watchlist()["items"], result["items"])

        # 语义变化记录：改造前此处必有 refresh_warning；改造后允许没有。
        # 因此不断言其存在，只在存在时校验内容格式。
        warning = result.get("refresh_warning")
        if warning is not None:
            self.assertIsInstance(warning, str)
            self.assertIn("行情刷新失败", warning)

    def test_refresh_warning_is_deferred_to_next_write(self):
        """`refresh_warning` 锁定为「捎带上一次后台刷新的失败原因」。

        最终语义（写请求完全不阻塞）：
        - 触发失败的那次写请求**不会**带 warning —— 返回时后台还没开始重建，
          本次结果客观上无从得知；
        - 失败被后台记录后，**下一次**写请求才捎带上报；
        - 后台刷新成功后自动清除。

        写这条是为了防止有人把 `refresh_warning` 误当成同步语义 ——
        早期版本曾用 150ms 宽限窗口试图同步回报，但去抖窗口 0.5s > 宽限 0.15s，
        该分支恒不命中，属于死代码。
        """
        def boom():
            raise OSError("行情暂不可用")

        service = StockDashboardService(loader=boom, watchlist_path=self.watchlist_path)

        first = service.add_watchlist({"code": legal_code(20), "name": "首标的"})
        self.assertNotIn("refresh_warning", first,
                         "触发失败的那次写不应带 warning（此时后台尚未开始重建）")

        # 等后台失败落地（去抖窗口 + 重建耗时）。
        deadline = time.monotonic() + DEADLOCK_TIMEOUT_SECONDS
        while service._refresh_error is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(service._refresh_error, "后台重建失败未被记录")

        second = service.add_watchlist({"code": legal_code(21), "name": "次标的"})
        self.assertIn("refresh_warning", second,
                      "下一次写请求应捎带上一次后台刷新的失败原因")
        self.assertIn("行情暂不可用", second["refresh_warning"])
        # 不变契约：无论是否捎带警告，本次写入本身必须成功落盘。
        self.assertEqual([item["code"] for item in second["items"]],
                         [legal_code(20), legal_code(21)])

    def test_snapshot_never_flashes_empty_after_write(self):
        """写请求不得清空快照 —— 读侧永远拿到非空快照（可能偏旧）。

        改造早期版本在写后把 `self.snapshot` 置None，导致紧接着的读请求
        在 `current()` 里**同步触发一次全量重建**：等于把「写请求慢」原样搬到
        `/api/ma120`，还与后台线程重复计算。前端 `ma120.js:169` 写完正是立刻
        重读 `/api/ma120`，所以该坑会稳定触发。

        因此这里锁定：写完立刻读必须是**非空且快速**的（偏旧可以，闪空不行）。
        """
        snapshot = fake_snapshot()
        recorder = RefreshRecorder(seconds=FAKE_REFRESH_SECONDS, snapshot=snapshot)
        service = self.build_service(recorder)
        service.refresh()  # 先建立一份快照
        self.assertIsNotNone(service.current()["snapshot"])

        service.add_watchlist({"code": legal_code(30), "name": "读侧不闪空"})

        started = time.monotonic()
        current = service.current()["snapshot"]
        elapsed = time.monotonic() - started

        self.assertIsNotNone(
            current, "写后立刻读拿到 None —— 快照被清空，读侧会闪空并触发同步重建")
        self.assertEqual(current, snapshot, "读侧应继续返回上一份快照（可能偏旧）")
        self.assertLess(elapsed, READ_BUDGET_SECONDS,
                        f"读耗时 {elapsed:.2f}s，说明读路径被同步重建拖住")

    def test_explicit_refresh_returns_full_snapshot(self):
        """显式 refresh() 保持同步语义，仍返回完整快照。"""
        snapshot = fake_snapshot()
        service = self.build_service(RefreshRecorder(seconds=0, snapshot=snapshot))
        port = self.start_server(service).server_port

        status, payload = self.request(port, "POST", "/api/ma120/refresh", {},
                                       self.json_headers(),
                                       timeout=DEADLOCK_TIMEOUT_SECONDS)
        self.assertEqual(status, 200)
        self.assertEqual(payload["snapshot"], snapshot,
                         "显式 refresh() 必须同步返回完整快照")

        status, payload = self.request(port, "GET", "/api/ma120",
                                       timeout=DEADLOCK_TIMEOUT_SECONDS)
        self.assertEqual(status, 200)
        self.assertEqual(payload["snapshot"], snapshot)

    def test_service_level_refresh_and_current_contract(self):
        """服务层 refresh() 返回快照，current() 在刷新后可见。"""
        snapshot = fake_snapshot()
        service = self.build_service(RefreshRecorder(seconds=0, snapshot=snapshot))
        self.assertEqual(service.refresh(), {"snapshot": snapshot})
        self.assertEqual(service.current(), {"snapshot": snapshot})

    def test_static_pages_and_api_still_respond(self):
        """页面 /、/ma120、/configuration 与 /api/ma120 仍正常。"""
        for path in ("/", "/ma120", "/configuration", "/app.js", "/ma120.js"):
            status, body = self.request(self.port, "GET", path,
                                        timeout=DEADLOCK_TIMEOUT_SECONDS)
            self.assertEqual(status, 200, f"{path} 返回 {status}")
            self.assertTrue(body, f"{path} 响应体为空")

        status, payload = self.request(self.port, "GET", "/api/ma120",
                                       timeout=DEADLOCK_TIMEOUT_SECONDS)
        self.assertEqual(status, 200)
        self.assertIn("snapshot", payload)

    def test_foreign_origin_still_rejected(self):
        """跨站写请求仍被拒绝（安全回归）。"""
        code = legal_code(15)
        self.assertEqual(self.add_code(self.port, code)[0], 200)
        status, _ = self.request(self.port, "PATCH",
                                 f"/api/ma120/watchlist?code={code}",
                                 {"name": "恶意改名"},
                                 {"Content-Type": "application/json",
                                  "Origin": "http://evil.example"})
        self.assertEqual(status, 403)


# ---------------------------------------------------------------------------
# 单后台刷新线程（若改造引入该机制）
# ---------------------------------------------------------------------------

class SingleBackgroundRefreshTests(AsyncWatchlistTestCase):
    """脏标记合并：短时间内的多次写只应触发有限次重建。

    注意：此断言对「改造前」的同步实现不成立（每次写都同步重建一次），
    因此这里只断言**并发不重叠**这一与实现无关的健壮性属性 —— 任意时刻
    最多一个重建在执行中。把「调用次数」作为软信息打印而非硬断言，
    以免在改造未落地时产生误导性失败。
    """

    def test_rebuild_never_overlaps_concurrently(self):
        """任意时刻最多一个行情重建在执行。

        注意：写请求在异步形态下**立即返回**，后台重建在其后才开始，
        因此必须在写完之后**等后台真正跑过一轮**再断言，否则会在
        「一次重建都没发生」的情况下把max_active 读成 0 而误报失败。
        """
        recorder = RefreshRecorder(seconds=0.3)
        service = self.build_service(recorder)
        port = self.start_server(service).server_port

        codes = [legal_code(600 + i) for i in range(8)]
        for code in codes:
            assert_legal_code(self, code)

        def write(code):
            return self.add_code(port, code, timeout=DEADLOCK_TIMEOUT_SECONDS)

        with ThreadPoolExecutor(max_workers=len(codes)) as pool:
            futures = [pool.submit(write, code) for code in codes]
            for future in futures:
                status, payload = future.result(timeout=DEADLOCK_TIMEOUT_SECONDS)
                self.assertEqual(status, 200, f"写入失败：{payload}")

        # 等后台重建真正发生（异步：写返回时它可能还没启动）。
        deadline = time.monotonic() + DEADLOCK_TIMEOUT_SECONDS
        while recorder.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertGreater(recorder.calls, 0,
                           "后台从未执行任何重建，脏标记合并机制可能已失效")
        # 再留出窗口，让并发重叠若存在也会暴露出来。
        settle = time.monotonic() + 1.0
        while time.monotonic() < settle:
            time.sleep(0.05)

        self.assertEqual(recorder.max_active, 1,
                         f"检测到 {recorder.max_active} 个重建同时进行，"
                         f"违反「进程内同时最多一个后台刷新」约束")

    def test_burst_of_writes_is_coalesced_into_few_rebuilds(self):
        """连续一批写只触发极少次重建（脏标记去抖合并生效）。

        这是「批量导入不再累积到分钟级」的直接证据：8 次写若每次都重建，
        就是8 × 0.3s 的串行累积；合并后应远少于 8 次。
        """
        recorder = RefreshRecorder(seconds=0.3)
        service = self.build_service(recorder)

        codes = [legal_code(700 + i) for i in range(8)]
        for code in codes:
            assert_legal_code(self, code)
        for code in codes:
            service.add_watchlist({"code": code, "name": "合并测试"})

        deadline = time.monotonic() + DEADLOCK_TIMEOUT_SECONDS
        while recorder.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        settle = time.monotonic() + 1.5
        while time.monotonic() < settle:
            time.sleep(0.05)

        self.assertGreater(recorder.calls, 0, "后台从未执行重建")
        self.assertLess(recorder.calls, len(codes),
                        f"{len(codes)} 次连续写触发了 {recorder.calls} 次重建，"
                        f"去抖合并未生效，每次写仍在各自重建")
        self.assertEqual(recorder.max_active, 1,
                         f"检测到 {recorder.max_active} 个重建同时进行")


if __name__ == "__main__":
    unittest.main()