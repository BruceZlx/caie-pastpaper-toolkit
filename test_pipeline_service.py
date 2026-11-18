#!/usr/bin/env python3
"""test_pipeline_service.py —— 题库管理业务层与管理 API 测试。

覆盖(JOB-01 / SAFE-01 / UI-01 的服务端基础):
  - TaskManager: 创建/串行执行/重复提交拒绝/取消/重试/重启恢复(interrupted)
  - 参数校验: 未知动作/未知参数/非法值 → 结构化错误
  - 判定草稿(adjudication_update): overrides 落盘 + 计数重算 + 非法标签拒绝
  - server 管理 API(真实 HTTP): overview/tasks 创建与查询/预览零写入/
    LAN 写保护(--lan 模式下非本机 403, 本机放行; 只读 GET 不受影响)
  - 发布成功后同进程索引刷新钩子(LIVE-01 基础): data_version 递增, 索引换新

用法: /usr/bin/python3 test_pipeline_service.py
"""
import json
import os
import re
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
# 服务层测试在本进程内以钩子路径验证刷新; 不做"通知本机 8930 服务"的外呼
os.environ.setdefault("ZJ_PUBLISH_NOTIFY_SERVER", "0")
import fitz                      # noqa: E402
import paper_pipeline as pp      # noqa: E402
import migrate_syllabus as ms    # noqa: E402
import pipeline_service as ps    # noqa: E402

CODE = "9999"


def make_qp_pdf(questions):
    doc = fitz.open()
    p0 = doc.new_page()
    p0.insert_text((200, 300), "Cambridge International 9999/01")
    p1 = doc.new_page()
    y = 90
    for q, text in questions:
        p1.insert_text((50, y), f"{q} {text}")
        y += 24
    data = doc.tobytes()
    doc.close()
    return data


TOPICS = {
    "subject": CODE, "name": "Fixture Studies", "zh": "夹具学",
    "source": "fixture", "syllabus_version": "2024-2026",
    "note": "test taxonomy",
    "levels": [{"level": "AS", "from": "1", "to": "2", "papers": [1, 2]}],
    "sections": [
        {"code": "1", "en": "Market mechanics", "zh": "市场机制", "kw": ["demand"],
         "subs": [
             {"code": "1.1", "en": "Demand and supply", "zh": "供需",
              "kw": ["demand", "law of demand", "supply"]},
             {"code": "1.2", "en": "Price mechanism", "zh": "价格机制",
              "kw": ["price mechanism", "scarcity"]}]}],
}


def build_repo():
    d = tempfile.mkdtemp(prefix="ps_test_repo_")
    os.makedirs(os.path.join(d, "9999（夹具）", "2024", "May-Jun"))
    os.makedirs(os.path.join(d, "data", "units", CODE))
    os.makedirs(os.path.join(d, "data", "loc", CODE))
    os.makedirs(os.path.join(d, "data", "msloc", CODE))
    os.makedirs(os.path.join(d, f"tags_{CODE}"))
    with open(os.path.join(d, "subjects.json"), "w") as f:
        json.dump({"subjects": {
            CODE: {"code": CODE, "name": "Fixture Studies", "zh": "夹具",
                   "level": "AS & A Level", "data_dir": "9999（夹具）",
                   "source": {"kind": "papacambridge", "slug": None},
                   "caie_page": "", "paper_names": {}}}}, f)
    with open(os.path.join(d, f"topics_{CODE}.json"), "w") as f:
        json.dump(TOPICS, f, ensure_ascii=False)
    with open(os.path.join(d, f"classified_{CODE}.json"), "w") as f:
        json.dump({"subject": CODE, "generated": "2026-01-01", "papers": []}, f)
    return d


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = build_repo()
        pp._apply_root(self.dir)
        ms._apply_root(self.dir)
        ps.apply_root(self.dir)
        import server
        server.apply_root(self.dir)        # server 索引也指向临时仓库
        ps.TM = None                      # 每个用例独立任务管理器

    def tearDown(self):
        pp._apply_root(REPO)
        ms._apply_root(REPO)
        ps.apply_root(REPO)
        ps.TM = None
        ms._REFRESH_HOOKS.clear()      # 测试注册的钩子不外溢到后续用例
        import server
        server.apply_root(REPO)
        server.LAN_MODE = False
        shutil.rmtree(self.dir, ignore_errors=True)

    def tree_hash(self, *skip):
        import hashlib
        h = hashlib.sha256()
        for dp, dns, fs in os.walk(self.dir):
            dns[:] = sorted(dns)
            for f in sorted(fs):
                rel = os.path.relpath(os.path.join(dp, f), self.dir)
                if any(rel.startswith(s) for s in skip):
                    continue
                h.update(rel.encode())
                h.update(open(os.path.join(dp, f), "rb").read())
        return h.hexdigest()


class ParamValidation(ServiceTestCase):
    def test_unknown_action(self):
        with self.assertRaises(ps.ParamError):
            ps.get_manager().create("rm_rf", {})

    def test_unknown_param(self):
        with self.assertRaises(ps.ParamError):
            ps.get_manager().create("publish", {"code": CODE, "evil": 1})

    def test_bad_code(self):
        with self.assertRaises(ps.ParamError):
            ps.get_manager().create("publish", {"code": "../etc"})
        with self.assertRaises(ps.ParamError):
            ps.get_manager().create("sync", {"code": "9999", "years": "20-3"})


class TaskLifecycle(ServiceTestCase):
    def _wait(self, tm, tid, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = tm.get(tid)
            if cur["status"] in ps.TERMINAL_STATES:
                return cur
            time.sleep(0.05)
        return tm.get(tid)

    def test_run_and_succeed(self):
        tm = ps.get_manager()
        t = tm.create("refresh_index", {})
        cur = self._wait(tm, t["id"])
        self.assertEqual(cur["status"], "succeeded", cur.get("error"))

    def test_duplicate_rejected(self):
        tm = ps.get_manager()
        lock = threading.Event()

        def slow(p, t):
            lock.wait(5)
            return {"ok": True}
        tm._act_refresh_index = slow
        t1 = tm.create("refresh_index", {})
        time.sleep(0.3)                    # 等任务进入 running
        with self.assertRaises(ps.ParamError):
            tm.create("refresh_index", {})
        lock.set()
        cur = self._wait(tm, t1["id"])
        self.assertEqual(cur["status"], "succeeded")
        t2 = tm.create("refresh_index", {})   # 终态后允许再次创建
        self.assertNotEqual(t1["id"], t2["id"])
        self._wait(tm, t2["id"])

    def test_retry_after_terminal(self):
        tm = ps.get_manager()
        boom = {"n": 0}

        def flaky(p, t):
            boom["n"] += 1
            if boom["n"] == 1:
                raise RuntimeError("boom")
            return {"ok": True}
        tm._act_refresh_index = flaky
        t1 = tm.create("refresh_index", {})
        cur = self._wait(tm, t1["id"])
        self.assertEqual(cur["status"], "failed")
        self.assertIn("boom", cur["error"])
        t2 = tm.retry(t1["id"])
        cur = self._wait(tm, t2["id"])
        self.assertEqual(cur["status"], "succeeded")

    def test_interrupted_recovery(self):
        """服务重启: running 任务 → interrupted, 可 retry 原参数。"""
        tm = ps.get_manager()
        lock = threading.Event()

        def slow(p, t):
            lock.wait(10)
            return {"ok": True}
        tm._act_refresh_index = slow
        t1 = tm.create("refresh_index", {})
        time.sleep(0.3)
        # 模拟重启: 丢弃内存态, 重新加载持久化任务
        ps.TM = None
        tm2 = ps.TaskManager()             # 不自动 start_worker
        cur = tm2.get(t1["id"])
        self.assertEqual(cur["status"], "interrupted")
        lock.set()

    def test_cancel_queued(self):
        tm = ps.get_manager()
        lock = threading.Event()
        tm._act_refresh_index = lambda p, t: (lock.wait(5), {"ok": True})[1]
        t1 = tm.create("refresh_index", {})
        t2 = tm.create("syllabus_check", {"code": CODE})  # 排队中的第二个
        tm.cancel(t2["id"])
        self.assertEqual(tm.get(t2["id"])["status"], "cancelled")
        lock.set()
        cur = self._wait(tm, t1["id"])
        self.assertEqual(cur["status"], "succeeded")


class AdjudicationDraft(ServiceTestCase):
    def seed_adjudication(self):
        os.makedirs(os.path.join(pp.PIPE_DIR, "adjudication"), exist_ok=True)
        with open(os.path.join(pp.PIPE_DIR, "adjudication", f"{CODE}.json"),
                  "w") as f:
            json.dump({"subject": CODE, "counts": {"in": 1, "out": 1,
                                                   "pending": 0, "context": 0},
                       "target": {"version": "2024-2026"},
                       "units": {"s24_11": {
                           "1": {"status": "IN", "tags": ["1.1"], "basis": "x"},
                           "2": {"status": "OUT", "reason": "r", "basis": "x"}}},
                       "papers": {}}, f)

    def test_update_and_recount(self):
        self.seed_adjudication()
        res = ps.adjudication_update(CODE, [
            {"paper": "s24_11", "uid": "2", "status": "CONTEXT",
             "tags": [], "reason": "无分值共享材料"}])
        self.assertTrue(res["ok"], res)
        adj = json.load(open(os.path.join(pp.PIPE_DIR, "adjudication",
                                          f"{CODE}.json")))
        self.assertEqual(adj["units"]["s24_11"]["2"]["status"], "CONTEXT")
        self.assertEqual(adj["counts"], {"in": 1, "out": 0, "pending": 0,
                                         "context": 1})
        ov = ms.load_overrides()
        self.assertEqual(ov[CODE]["s24_11"]["2"]["status"], "CONTEXT")

    def test_invalid_tags_rejected(self):
        self.seed_adjudication()
        with self.assertRaises(ps.ParamError):
            ps.adjudication_update(CODE, [
                {"paper": "s24_11", "uid": "2", "status": "IN",
                 "tags": ["9.9"], "reason": ""}])
        with self.assertRaises(ps.ParamError):
            ps.adjudication_update(CODE, [
                {"paper": "s24_11", "uid": "2", "status": "OUT",
                 "tags": [], "reason": ""}])   # OUT 缺理由


class AdminAPI(ServiceTestCase):
    """真实 HTTP 管理接口(本机回环)。"""

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_overview(self):
        st, j = self.req("GET", "/api/pipeline/overview")
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])
        codes = [s["code"] for s in j["subjects"]]
        self.assertIn(CODE, codes)

    def test_task_create_and_get(self):
        st, j = self.req("POST", "/api/pipeline/tasks",
                         {"action": "refresh_index", "params": {}})
        self.assertEqual(st, 201)
        tid = j["task"]["id"]
        for _ in range(200):
            st2, j2 = self.req("GET", f"/api/pipeline/tasks/{tid}")
            if j2["task"]["status"] in ps.TERMINAL_STATES:
                break
            time.sleep(0.05)
        self.assertEqual(j2["task"]["status"], "succeeded")

    def test_bad_action_400(self):
        st, j = self.req("POST", "/api/pipeline/tasks",
                         {"action": "nope", "params": {}})
        self.assertEqual(st, 400)
        self.assertFalse(j["ok"])

    def test_preview_zero_write(self):
        """预览(只读)不得写题库/索引/正式数据(任务记账除外)。"""
        before = self.tree_hash("data/pipeline/tasks")
        st, j = self.req("POST", "/api/pipeline/tasks",
                         {"action": "preview_sync",
                          "params": {"code": CODE, "latest": True}})
        self.assertEqual(st, 201)
        tid = j["task"]["id"]
        for _ in range(600):
            st2, j2 = self.req("GET", f"/api/pipeline/tasks/{tid}")
            if j2["task"]["status"] in ps.TERMINAL_STATES:
                break
            time.sleep(0.1)
        # 预览对空来源会 failed(empty-source) — 两种终态都不允许写仓库
        self.assertIn(j2["task"]["status"], ("succeeded", "failed"))
        self.assertEqual(self.tree_hash("data/pipeline/tasks"), before,
                         "预览必须零题库写入(仅任务记账)")

    def test_lan_write_guard(self):
        """--lan 模式: 非本机 IP 的管理写请求 → 403; 本机放行。"""
        srv = self.server
        old = srv.LAN_MODE
        srv.LAN_MODE = True
        try:
            h = srv.Handler.__new__(srv.Handler)
            h.client_address = ("192.168.1.77", 12345)
            self.assertFalse(h._admin_write_allowed())
            h2 = srv.Handler.__new__(srv.Handler)
            h2.client_address = ("127.0.0.1", 12345)
            self.assertTrue(h2._admin_write_allowed())
            h3 = srv.Handler.__new__(srv.Handler)
            h3.client_address = ("::1", 12345)
            self.assertTrue(h3._admin_write_allowed())
        finally:
            srv.LAN_MODE = old
        h4 = srv.Handler.__new__(srv.Handler)     # 非 lan(默认绑定)回环放行
        h4.client_address = ("127.0.0.1", 1)
        self.assertTrue(h4._admin_write_allowed())

    def test_adjudication_endpoint(self):
        os.makedirs(os.path.join(pp.PIPE_DIR, "adjudication"), exist_ok=True)
        with open(os.path.join(pp.PIPE_DIR, "adjudication", f"{CODE}.json"),
                  "w") as f:
            json.dump({"subject": CODE, "counts": {"in": 1, "out": 0,
                                                   "pending": 0, "context": 0},
                       "target": {"version": "2024-2026"},
                       "units": {"s24_11": {"1": {"status": "IN",
                                                  "tags": ["1.1"]}}},
                       "papers": {}}, f)
        with open(os.path.join(pp.ROOT, "data", "units", CODE, "s24_11.json"),
                  "w") as f:
            json.dump({"subject": CODE, "code": "s24_11", "year": 2024,
                       "session": "May-Jun", "paper": 1, "variant": 1,
                       "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
                       "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                                  "marks": 4, "text": "demand text [4]"}]}, f)
        st, j = self.req("GET", f"/api/pipeline/adjudication?subject={CODE}")
        self.assertEqual(st, 200)
        self.assertEqual(j["data"]["total"], 1)
        self.assertEqual(j["data"]["rows"][0]["uid"], "1")
        self.assertTrue(any(t["code"] == "1.1" for t in j["data"]["tag_options"]))
        st, j = self.req("POST", "/api/pipeline/adjudication",
                         {"subject": CODE, "updates": [
                             {"paper": "s24_11", "uid": "1", "status": "PENDING",
                              "tags": [], "reason": "待复核"}]})
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])


class PublishRefreshHook(ServiceTestCase):
    def test_version_bump_and_hook_fired(self):
        """发布钩子(publish 成功后调用) → server 同进程刷新索引 + 版本递增。"""
        fired = []
        import server
        # 种入 1 个 IN 单元(空索引按 P0-4 守卫会显式失败, 不允许静默)
        with open(os.path.join(self.dir, "data", "units", CODE, "s24_11.json"),
                  "w") as f:
            json.dump({"subject": CODE, "code": "s24_11", "year": 2024,
                       "session": "May-Jun", "paper": 1, "variant": 1,
                       "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
                       "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                                  "marks": 4, "text": "demand text [4]",
                                  "topics": ["1.1"], "status": "IN"}]}, f)
        # 模拟 server.main() 的接线: 把 refresh_indexes 注册为发布钩子
        ms.register_refresh_hook(server.refresh_indexes)
        ms.register_refresh_hook(lambda code: fired.append(code))
        old_idx = server.IDX
        server.DATA_VERSION = 0
        errs = ms._fire_refresh_hooks("9999")       # publish 内部即此调用
        self.assertEqual(errs, [], errs)
        self.assertEqual(fired, ["9999"])
        deadline = time.time() + 10
        while server.DATA_VERSION < 1 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(server.DATA_VERSION, 1)
        deadline = time.time() + 10       # 等索引构建线程完成(空索引, 秒级)
        while server.IDX is old_idx and time.time() < deadline:
            time.sleep(0.1)
        self.assertIsNot(server.IDX, old_idx)


class DataVersionTiming(ServiceTestCase):
    """P0-4: data_version 不得早于索引就绪; 构建失败显式抛出。"""

    def _seed_unit(self):
        with open(os.path.join(self.dir, "data", "units", CODE, "s24_11.json"),
                  "w") as f:
            json.dump({"subject": CODE, "code": "s24_11", "year": 2024,
                       "session": "May-Jun", "paper": 1, "variant": 1,
                       "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
                       "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                                  "marks": 4, "text": "demand text [4]",
                                  "topics": ["1.1"], "status": "IN"}]}, f)

    def test_version_waits_for_indexes(self):
        import server
        self._seed_unit()
        old_idx = server.IDX
        server.DATA_VERSION = 0
        build_sleep = {"v": 0.8}

        class SlowSearch(server.SearchIndex):
            def build(self, *a, **k):
                time.sleep(build_sleep["v"])
                super().build(*a, **k)

        with unittest.mock.patch.object(server, "SearchIndex", SlowSearch):
            done = threading.Event()
            err = []

            def run():
                try:
                    server.refresh_indexes()
                except Exception as e:
                    err.append(e)
                finally:
                    done.set()
            th = threading.Thread(target=run, daemon=True)
            th.start()
            time.sleep(0.3)                 # 构建中: 版本与索引都不得提前变
            self.assertEqual(server.DATA_VERSION, 0,
                             "索引未就绪前不得递增 data_version")
            self.assertIs(server.IDX, old_idx, "索引未就绪前不得安装新实例")
            done.wait(10)
            th.join(10)
        self.assertFalse(err, err)
        self.assertEqual(server.DATA_VERSION, 1)      # 就绪后版本+新实例同批
        self.assertIsNot(server.IDX, old_idx)

    def test_build_failure_raises_and_version_unchanged(self):
        import server
        self._seed_unit()
        old_idx = server.IDX
        server.DATA_VERSION = 0

        class BoomSearch(server.SearchIndex):
            def build(self, *a, **k):
                raise RuntimeError("注入: 搜题索引构建失败")

        with unittest.mock.patch.object(server, "SearchIndex", BoomSearch):
            with self.assertRaises(RuntimeError):
                server.refresh_indexes()
        self.assertEqual(server.DATA_VERSION, 0)
        self.assertIs(server.IDX, old_idx)


class CancelRunning(ServiceTestCase):
    """P1-1: 运行中任务在安全边界取消 → cancelled; commit 阶段拒绝取消。"""

    def _wait(self, tm, tid, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = tm.get(tid)
            if cur["status"] in ps.TERMINAL_STATES:
                return cur
            time.sleep(0.05)
        return tm.get(tid)

    def test_cancel_running_task(self):
        tm = ps.get_manager()
        gates = []

        def slow_extract(p, t):
            for _ in range(30):
                tm.raise_if_cancelled(t["id"])      # 动作内的安全边界
                gates.append(1)
                time.sleep(0.1)
            return {"ok": True, "summary": "不应到达: 取消须生效"}
        tm._act_extract = slow_extract
        t1 = tm.create("extract", {"code": CODE})
        time.sleep(0.35)
        tm.cancel(t1["id"])
        cur = self._wait(tm, t1["id"])
        self.assertEqual(cur["status"], "cancelled",
                         f"运行中取消必须生效, 实际 {cur['status']}")
        self.assertEqual(cur["exit_code"], 4)

    def test_cancel_rejected_in_commit_stage(self):
        tm = ps.get_manager()
        lock = threading.Event()

        def slow_publish(p, t):
            with tm.lock:
                cur = tm.tasks[t["id"]]
                cur["stage"] = "commit"            # 进入不可中断提交阶段
                tm._save(cur)
            lock.wait(5)
            return {"ok": True, "summary": "committed"}
        tm._act_publish = slow_publish
        t1 = tm.create("publish", {"code": CODE})
        time.sleep(0.35)
        with self.assertRaises(ps.CancelRejected):
            tm.cancel(t1["id"])
        self.assertEqual(tm.get(t1["id"])["status"], "running")
        lock.set()
        cur = self._wait(tm, t1["id"])
        self.assertEqual(cur["status"], "succeeded")   # 提交阶段不可中断


class RefreshFailureRecovery(ServiceTestCase):
    """P0-4: 刷新钩子失败 → 发布 ok=False、待发布不清零、提示明确;
    刷新动作按真实结果返回; 修复后重发成功。"""

    def _seed_publishable(self, code=CODE):
        # 正式种子卷 + 一份待发布新卷(带 AI 草稿 IN)
        os.makedirs(os.path.join(self.dir, code + "（夹具）", "2024", "May-Jun"),
                    exist_ok=True)
        with open(os.path.join(self.dir, "x.pdf"), "wb") as f:
            f.write(b"%PDF-1.4\n%fixture\n")
        units_dir = os.path.join(self.dir, "data", "units", code)
        os.makedirs(units_dir, exist_ok=True)
        with open(os.path.join(units_dir, "s24_11.json"), "w") as f:
            json.dump({"subject": code, "code": "s24_11", "year": 2024,
                       "session": "May-Jun", "paper": 1, "variant": 1,
                       "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
                       "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                                  "marks": 4, "text": "demand text [4]",
                                  "topics": ["1.1"], "status": "IN"}]}, f)
        with open(os.path.join(self.dir, "data", "loc", code, "s24_11.json"),
                  "w") as f:
            json.dump({"units": {"1": {"status": "exact",
                                       "seg": [[2, 40, 70, 550, 700]]}},
                       "questions": {}}, f)
        with open(os.path.join(self.dir, "data", "msloc", code,
                               "s24_11.json"), "w") as f:
            json.dump({"ms_status": "missing", "letters": {}, "units": {},
                       "questions": {}}, f)
        work = os.path.join(pp.PIPE_DIR, "work", code, "units")
        os.makedirs(work, exist_ok=True)
        with open(os.path.join(work, "w23_21_qp.json"), "w") as f:
            json.dump({"code": "w23_21", "year": 2023, "session": "Oct-Nov",
                       "paper": 1, "variant": 1, "qp_pdf": "x.pdf",
                       "ms_pdf": None,
                       "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                                  "marks": 6,
                                  "text": "price mechanism question [6]"}]}, f)
        os.makedirs(os.path.join(self.dir, f"tags_{code}"), exist_ok=True)
        with open(os.path.join(self.dir, f"tags_{code}", "w23_21.json"),
                  "w") as f:
            json.dump({"code": "w23_21", "tags": {"1": ["1.1"]}}, f)

    def test_refresh_failure_reports_and_recovers(self):
        code = CODE
        self._seed_publishable(code)
        ms.adjudicate_new_papers(code, log=lambda *a: None)
        self.assertEqual(ms.pending_papers(code, log=lambda *a: None),
                         ["w23_21"])

        def boom(subject=None):
            raise RuntimeError("注入: 搜题索引构建失败")
        ms.register_refresh_hook(boom)
        res = ms.publish(code, log=lambda *a: None)
        self.assertFalse(res["ok"], "刷新失败不得假成功")
        self.assertEqual(res.get("stage"), "refresh-failed")
        self.assertIn("正式数据已提交、服务索引未就绪", res["summary"])
        self.assertIn("刷新服务索引", res["summary"])
        self.assertTrue(res.get("index_refresh_errors"))
        # 正式数据确实已提交(classified 含新卷) —— 只是索引未就绪
        cls = json.load(open(os.path.join(self.dir, f"classified_{code}.json")))
        self.assertIn("w23_21", [p["code"] for p in cls["papers"]])
        # 第五轮 P0-4: 刷新失败不清待发布(旧实现在此为 [], 本断言必失败);
        # 两阶段状态 pending_refresh 在登记/总览持久可见, 重启后仍在
        self.assertEqual(ms.pending_papers(code, log=lambda *a: None),
                         ["w23_21"], "刷新失败不得清待发布")
        self.assertEqual(ms.refresh_pending_papers(code),
                         {"s24_11", "w23_21"},
                         "本次发布提交的卷全部进入两阶段状态")
        self.assertNotIn("w23_21", ms.published_papers(code))
        ov = {s["code"]: s for s in ps.overview()}[code]
        self.assertEqual(ov["pending_refresh"], ["s24_11", "w23_21"])
        self.assertIn("w23_21", json.load(open(os.path.join(
            self.dir, "data", "pipeline", "published.json")))
            ["subjects"][code]["pending_refresh"])
        # 刷新动作按真实结果返回失败(坏钩子仍在)
        errs = ms._fire_refresh_hooks(None)
        self.assertTrue(errs)
        tm_out = ps.get_manager()._act_refresh_index({}, {"id": "t"})
        self.assertFalse(tm_out["ok"])
        self.assertIn("w23_21", ms.pending_papers(code, log=lambda *a: None))
        # 修复(移除坏钩子)后: 仅刷新即可恢复并晋升, 无需重发数据
        ms._REFRESH_HOOKS.remove(boom)
        errs2 = ms._fire_refresh_hooks(None)
        self.assertEqual(errs2, [])
        out2 = ps.get_manager()._act_refresh_index({}, {"id": "t2"})
        self.assertTrue(out2["ok"], out2)
        self.assertIn("晋升", out2["summary"])
        self.assertEqual(ms.pending_papers(code, log=lambda *a: None), [])
        self.assertIn("w23_21", ms.published_papers(code))

    def test_refresh_index_action_reports_failure(self):
        tm = ps.get_manager()

        def boom(subject=None):
            raise RuntimeError("注入: 组卷索引构建失败")
        ms.register_refresh_hook(boom)
        try:
            out = tm._act_refresh_index({}, {"id": "t"})
            self.assertFalse(out["ok"])
            self.assertEqual(out["exit_code"], 2)
            self.assertIn("索引刷新失败", out["summary"])
        finally:
            ms._REFRESH_HOOKS.remove(boom)


class RealSyncCancel(ServiceTestCase):
    """P1-1: 真实 _act_sync 在下载/切题之间被取消 → 终态 cancelled(退出码 4),
    不是 failed。夹具来源站对首个 QP 延迟响应制造运行窗口。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import http.server
        cls.slow_files = {}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, head=False):
                import fitz as _fitz
                doc = _fitz.open()
                p0 = doc.new_page()
                p0.insert_text((200, 300), "Cambridge International 9999/01")
                p1 = doc.new_page()
                p1.insert_text((50, 90), "1 demand question [4]")
                p1.insert_text((50, 114), "END OF PAPER")
                data = doc.tobytes()
                doc.close()
                if head:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", "application/pdf")
                self.end_headers()
                time.sleep(RealSyncCancel.slow_files.get(self.path, 0))
                self.wfile.write(data)

            def do_GET(self):
                self._serve()

            def do_HEAD(self):
                self._serve(head=True)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        # 会话页: 每个文件响应慢 1.2s → 下载阶段足够长, 可在运行中取消
        up = "/directories/CAIE/CAIE-pastpapers/upload/"
        slow_qp = up + "9999_w23_qp_21.pdf"
        cls.slow_files[slow_qp] = 1.2
        subj = "\n".join([
            "<html><body>pastpapers 9999",
            f'<a href="{cls.base}/papers/caie/fixture-9999-2023-oct-nov">oct</a>',
            "</body></html>"])
        sess = "\n".join([
            f'<a href="{cls.base}{slow_qp}">qp</a>',
        ])
        cls.httpd.RequestHandlerClass.directory_pages = None
        # 用类属性注入文件服务器内容
        cls.pages = {"/papers/caie/fixture-9999": subj.encode(),
                     "/papers/caie/fixture-9999-2023-oct-nov": sess.encode()}
        orig = H

        class H2(H):
            def _serve(self, head=False):
                body = RealSyncCancel.pages.get(self.path)
                if body is not None:
                    if head:
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return H._serve(self, head)
        cls.httpd.RequestHandlerClass = H2
        # 覆盖 _serve 绑定
        cls.base_url = cls.base
        # 官方大纲页(带 2023-2025 链接, 使 syllabus 检查通过)
        cls.pages["/caie/official-9999"] = (
            '<a href="/Images/111-2023-2025-syllabus.pdf">Syllabus</a>').encode()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        super().tearDownClass()

    def _wait(self, tm, tid, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = tm.get(tid)
            if cur["status"] in ps.TERMINAL_STATES:
                return cur
            time.sleep(0.05)
        return tm.get(tid)

    def test_running_cancel_yields_cancelled(self):
        # subjects.json 登记来源 slug + 官方大纲页 → syllabus 检查可解析
        reg = json.load(open(os.path.join(self.dir, "subjects.json")))
        reg["subjects"][CODE]["source"] = {"kind": "papacambridge",
                                           "slug": "fixture-9999"}
        reg["subjects"][CODE]["caie_page"] = f"{self.base}/caie/official-9999"
        with open(os.path.join(self.dir, "subjects.json"), "w") as f:
            json.dump(reg, f, ensure_ascii=False)
        tm = ps.get_manager()
        t = tm.create("sync", {"code": CODE, "years": "2023", "latest": False,
                               "source_url": f"{self.base}/papers/caie/fixture-9999"})
        # 等任务进入 running 且开始下载(1.2s 慢 QP)
        time.sleep(1.5)
        cur = tm.get(t["id"])
        self.assertEqual(cur["status"], "running")
        tm.cancel(t["id"])
        cur = self._wait(tm, t["id"])
        self.assertEqual(cur["status"], "cancelled",
                         f"真实 sync 取消必须终态 cancelled, 实际 "
                         f"{cur['status']}: {cur.get('summary')}")
        self.assertEqual(cur.get("exit_code"), 4)
        self.assertTrue(any("阶段" in ln or "已取消" in ln
                            for ln in cur.get("log", [])))


class ImportTopicsGuard(ServiceTestCase):
    """P1-2: import_topics 一律草稿化, 不得直改已有科目正式 taxonomy。"""

    def topics_hash(self):
        import hashlib
        return hashlib.sha256(
            open(os.path.join(self.dir, f"topics_{CODE}.json"), "rb").read()
        ).hexdigest()

    def test_existing_subject_import_is_draft_only(self):
        before = self.topics_hash()
        res = ps.get_manager()  # 无关, 仅确保模块就绪
        out = ps.TaskManager._act_import_topics(
            ps.get_manager(),
            {"code": CODE, "payload": {
                "subject": CODE, "syllabus_version": "2099-2101",
                "source": "fake official pdf",
                "sections": [{"code": "9", "en": "X", "zh": "", "kw": [],
                              "subs": [{"code": "9.1", "en": "Y", "zh": "",
                                        "kw": []}]}]}}, None)
        self.assertTrue(out["ok"], out)
        self.assertIn("正式 topics 未改动", out["summary"])
        self.assertEqual(self.topics_hash(), before)
        self.assertTrue(os.path.exists(ms.imported_topics_path(CODE)))
        # 清理草稿, 不影响后续用例
        os.remove(ms.imported_topics_path(CODE))

    def test_invalid_version_and_structure_rejected(self):
        before = self.topics_hash()
        for bad in (
            {"subject": CODE, "syllabus_version": "unknown", "source": "x",
             "sections": [{"code": "9", "en": "X", "subs": []}]},
            {"subject": CODE, "syllabus_version": "2028-2030", "source": "",
             "sections": [{"code": "9", "en": "X",
                           "subs": [{"code": "9.1", "en": "Y"}]}]},
            {"subject": "7777", "syllabus_version": "2028-2030", "source": "x",
             "sections": [{"code": "9", "en": "X",
                           "subs": [{"code": "9.1", "en": "Y"}]}]},
        ):
            out = ps.TaskManager._act_import_topics(
                ps.get_manager(), {"code": CODE, "payload": bad}, None)
            self.assertFalse(out["ok"], bad)
            self.assertEqual(out["exit_code"], 2)
        self.assertEqual(self.topics_hash(), before)
        self.assertFalse(os.path.exists(ms.imported_topics_path(CODE)))

    def test_new_subject_draft_used_by_publish(self):
        """新科目: 草稿由 publish 转正(正式文件在成功前不出现)。"""
        code2 = "7777"
        # qp_pdf 指向一个真实存在的文件(临时仓库根); gen_index 需要数据目录
        with open(os.path.join(self.dir, "x.pdf"), "wb") as f:
            f.write(b"%PDF-1.4\n%fixture\n")
        os.makedirs(os.path.join(self.dir, "7777（夹具）", "2024", "May-Jun"),
                    exist_ok=True)
        os.makedirs(os.path.join(self.dir, "data", "units", code2), exist_ok=True)
        work = os.path.join(pp.PIPE_DIR, "work", code2, "units")
        os.makedirs(work, exist_ok=True)
        with open(os.path.join(work, "n01_11_qp.json"), "w") as f:
            json.dump({"code": "n01_11", "year": 2024, "session": "May-Jun",
                       "paper": 1, "variant": 1, "qp_pdf": "x.pdf",
                       "ms_pdf": None,
                       "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                                  "marks": 4,
                                  "text": "demand question [4]"}]}, f)
        out = ps.TaskManager._act_import_topics(
            ps.get_manager(), {"code": code2, "payload": {
                "subject": code2, "syllabus_version": "2028-2030",
                "source": "fixture pdf",
                "levels": [{"level": "AS", "from": "1", "to": "1",
                            "papers": [1]}],
                "sections": [{"code": "1", "en": "Market", "zh": "",
                              "kw": ["demand"],
                              "subs": [{"code": "1.1", "en": "Demand", "zh": "",
                                        "kw": ["demand"]}]}]}}, None)
        self.assertTrue(out["ok"], out)
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, f"topics_{code2}.json")),
            "草稿不得直接变成正式 topics")
        # 判定(AI 草稿) → 发布 → 草稿转正
        os.makedirs(os.path.join(self.dir, f"tags_{code2}"), exist_ok=True)
        with open(os.path.join(self.dir, f"tags_{code2}", "n01_11.json"),
                  "w") as f:
            json.dump({"code": "n01_11", "tags": {"1": ["1.1"]}}, f)
        adj = ms.adjudicate_new_papers(code2, log=lambda *a: None)
        self.assertEqual(adj["new_papers"], ["n01_11"])
        self.assertFalse(os.path.exists(os.path.join(
            self.dir, "data", "units", code2, "n01_11.json")),
            "成功发布前不得写正式 data/units")
        res = ms.publish(code2, log=lambda *a: None)
        self.assertTrue(res["ok"], res)
        self.assertTrue(os.path.exists(
            os.path.join(self.dir, f"topics_{code2}.json")))
        self.assertFalse(os.path.exists(ms.imported_topics_path(code2)),
                         "转正后草稿应清除")


# ============================ 第六轮 P0-2: 真实 HTTP 对外版本一致性 ============================
class HttpVersionConsistency(ServiceTestCase):
    """Codex 第五轮 P0-2 复现场景: 真实 server.Handler HTTP 服务, 在发布
    翻转后/索引刷新前、索引构建中途、刷新失败后逐阶段请求真实端点:
      /api/health(搜题索引) · /search_data_<c>.js(静态) · /api/search ·
      /api/unit(详情) · /api/mock/meta(组卷)
    —— 任一时刻对外只能整体全旧或整体全新, 不得出现新版详情+旧版搜题。
    旧实现(静态/详情读 live、索引未刷新)在本测试 phase1 断言处必失败。"""

    CODE2 = "8888"          # 旁观科(用于注入损坏 units 制造刷新失败)

    def _build_repo_full(self):
        # ServiceTestCase.setUp 已建 9999 基础; 补旁观科与共享 PDF
        import fitz
        doc = fitz.open()
        p0 = doc.new_page(); p0.insert_text((200, 300), "Cambridge International 9999/11")
        p1 = doc.new_page(); p1.insert_text((50, 90), "1 Explain the demand question. [4]")
        open(os.path.join(self.dir, "x.pdf"), "wb").write(doc.tobytes())
        doc.close()
        os.makedirs(os.path.join(self.dir, "data", "units", self.CODE2), exist_ok=True)
        json.dump({"subject": self.CODE2, "code": "n01_11", "year": 2024,
                   "session": "May-Jun", "paper": 1, "variant": 1,
                   "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 4, "text": "bystander unit. [4]",
                              "topics": None, "status": "PENDING"}]},
                  open(os.path.join(self.dir, "data", "units", self.CODE2,
                                    "n01_11.json"), "w"))
        # 9999 种子正式卷(IN)
        json.dump({"subject": CODE, "code": "s24_11", "year": 2024,
                   "session": "May-Jun", "paper": 1, "variant": 1,
                   "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 4, "text": "demand seed [4]",
                              "topics": ["1.1"], "status": "IN"}]},
                  open(os.path.join(self.dir, "data", "units", CODE,
                                    "s24_11.json"), "w"), ensure_ascii=False)
        json.dump({"units": {"1": {"status": "exact",
                                   "seg": [[2, 40, 70, 550, 700]]}},
                   "questions": {}},
                  open(os.path.join(self.dir, "data", "loc", CODE,
                                    "s24_11.json"), "w"))
        json.dump({"ms_status": "missing", "letters": {}, "units": {},
                   "questions": {}},
                  open(os.path.join(self.dir, "data", "msloc", CODE,
                                    "s24_11.json"), "w"))

    def _add_new_paper(self, pc, text):
        work = os.path.join(pp.PIPE_DIR, "work", CODE, "units")
        os.makedirs(work, exist_ok=True)
        json.dump({"code": pc, "year": 2023, "session": "Oct-Nov", "paper": 2,
                   "variant": 1, "qp_pdf": "x.pdf", "ms_pdf": None,
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 6, "text": text}]},
                  open(os.path.join(work, f"{pc}_qp.json"), "w"))
        json.dump({"code": pc, "tags": {"1": ["1.2"]}},
                  open(os.path.join(self.dir, f"tags_{CODE}", f"{pc}.json"), "w"))
        ms.adjudicate_new_papers(CODE, log=lambda *a: None)

    def _start_http(self):
        import server
        from http.server import ThreadingHTTPServer
        server.DATA_VERSION = 0        # 每个用例独立计数(断言 1/2/3)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=15) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def _snapshot(self, tag):
        _, b_h = self._get("/api/health")
        h = json.loads(b_h)
        _, b_s = self._get("/api/search?q=" + urllib.parse.quote("price mechanism"))
        st_d, _ = self._get(f"/api/unit?subject={CODE}&code=w23_21&uid=1")
        _, b_t = self._get(f"/search_data_{CODE}.js")
        m = re.search(r'"units":(\d+)', b_t.decode())
        _, b_m = self._get(f"/api/mock/meta?subject={CODE}")
        return {"tag": tag, "units": h.get("units"), "dv": h.get("data_version"),
                "served": h.get("served_version"),
                "hits": len(json.loads(b_s)["results"]),
                "new_unit": st_d, "static": int(m.group(1)) if m else -1,
                "mock_ok": json.loads(b_m).get("ok")}

    def tearDown(self):
        if getattr(self, "httpd", None):
            self.httpd.shutdown()
        ms._REFRESH_HOOKS.clear()
        super().tearDown()

    def test_http_surface_all_old_or_all_new_each_phase(self):
        import server, live_version as lv
        self._build_repo_full()
        server.apply_root(self.dir)
        ms.register_refresh_hook(server.refresh_indexes)
        self._start_http()

        # phase0: 种子发布(hooks 安装 SERVED) → 对外 v_old
        r0 = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(r0["ok"], r0)
        v0 = self._snapshot("v0")
        self.assertEqual((v0["units"], v0["static"], v0["new_unit"],
                          v0["hits"], v0["dv"]), (1, 1, 404, 0, 1), v0)

        # phase1: 翻转后、索引刷新前(publish:pre-refresh 暂停) → 对外整体仍旧版
        self._add_new_paper("w23_21", "price mechanism allocates scarce [6]")
        reached, release = threading.Event(), threading.Event()

        def pause(name):
            if name == "publish:pre-refresh":
                reached.set()
                release.wait(30)
        lv.set_pause(pause)
        box = {}
        try:
            t = threading.Thread(target=lambda: box.update(
                res=ms.publish(CODE, log=lambda *a: None)))
            t.start()
            self.assertTrue(reached.wait(30), "未到达 publish:pre-refresh")
            v1 = self._snapshot("after_flip_before_refresh")
            # P0-2 核心: 静态=旧版(1), 搜题索引=旧版(1), 新版详情=404, dv 不提前
            self.assertEqual((v1["units"], v1["static"], v1["new_unit"],
                              v1["hits"], v1["dv"]), (1, 1, 404, 0, 1),
                             f"翻转后刷新前对外必须整体旧版: {v1}")
            self.assertEqual(v1["served"], v0["served"], "served 版本不得提前切换")
            release.set()
            t.join(60)
        finally:
            lv.clear_pause()
        self.assertTrue(box["res"]["ok"], box["res"])
        # phase2: 刷新完成 → 对外整体新版
        v2 = self._snapshot("after_refresh")
        self.assertEqual((v2["units"], v2["static"], v2["new_unit"],
                          v2["hits"], v2["dv"]), (2, 2, 200, 1, 2), v2)
        self.assertNotEqual(v2["served"], v1["served"])

    def test_http_surface_stays_old_while_refresh_failing(self):
        """刷新失败期间对外保持完整旧版; 修复后 reload 端点整体切新。"""
        import server, live_version as lv
        self._build_repo_full()
        server.apply_root(self.dir)
        ms.register_refresh_hook(server.refresh_indexes)
        self._start_http()
        r0 = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(r0["ok"], r0)
        self._add_new_paper("w23_21", "price mechanism allocates scarce [6]")
        r1 = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(r1["ok"], r1)
        v_a = self._snapshot("served_v1")
        self.assertEqual((v_a["units"], v_a["static"], v_a["new_unit"],
                          v_a["dv"]), (2, 2, 200, 2), v_a)
        # 注入: 旁观科损坏 units → 新一卷发布 → 翻转成功但刷新失败
        open(os.path.join(self.dir, "data", "units", self.CODE2,
                          "n01_11.json"), "w").write('{"subject": "8888", BROKEN')
        self._add_new_paper("w24_31", "law of demand second fixture [4]")
        r2 = ms.publish(CODE, log=lambda *a: None)
        self.assertFalse(r2["ok"], r2)
        self.assertEqual(r2.get("stage"), "refresh-failed",
                         f"实际错误: {r2.get('summary')} | problems={r2.get('problems')}")
        v_b = self._snapshot("refresh_failed")
        # 对外保持 v1: 静态/搜题/详情/组卷全部旧版, 不混合
        self.assertEqual((v_b["units"], v_b["static"], v_b["dv"]),
                         (2, 2, 2), v_b)
        st, _ = self._get(f"/api/unit?subject={CODE}&code=w24_31&uid=1")
        self.assertEqual(st, 404, "刷新失败期间新版详情不得可见")
        self.assertEqual(v_b["served"], v_a["served"])
        # 修复旁观科 → POST /api/pipeline/reload → 对外整体新版
        json.dump({"subject": self.CODE2, "code": "n01_11", "year": 2024,
                   "session": "May-Jun", "paper": 1, "variant": 1,
                   "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 4, "text": "bystander unit. [4]",
                              "topics": None, "status": "PENDING"}]},
                  open(os.path.join(self.dir, "data", "units", self.CODE2,
                                    "n01_11.json"), "w"))
        req = urllib.request.Request(self.base + "/api/pipeline/reload",
                                     method="POST", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            self.assertTrue(json.loads(r.read())["ok"])
        v_c = self._snapshot("after_reload")
        self.assertEqual((v_c["units"], v_c["static"], v_c["dv"]), (3, 3, 3), v_c)
        st, _ = self._get(f"/api/unit?subject={CODE}&code=w24_31&uid=1")
        self.assertEqual(st, 200)

    def test_http_surface_old_during_index_build(self):
        """索引构建中途(SlowSearch): 对外保持旧版直到安装完成。"""
        import server, live_version as lv
        self._build_repo_full()
        server.apply_root(self.dir)
        ms.register_refresh_hook(server.refresh_indexes)
        self._start_http()
        r0 = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(r0["ok"], r0)
        self._add_new_paper("w23_21", "price mechanism allocates scarce [6]")

        build_sleep = {"v": 1.2}

        class SlowSearch(server.SearchIndex):
            def build(self, *a, **k):
                time.sleep(build_sleep["v"])
                super().build(*a, **k)

        box = {}
        with unittest.mock.patch.object(server, "SearchIndex", SlowSearch):
            t = threading.Thread(target=lambda: box.update(
                res=ms.publish(CODE, log=lambda *a: None)))
            t.start()
            time.sleep(0.6)          # 翻转已发生, 搜题索引构建中
            v_mid = self._snapshot("during_build")
            t.join(60)
        self.assertTrue(box["res"]["ok"], box["res"])
        self.assertEqual((v_mid["units"], v_mid["static"], v_mid["new_unit"],
                          v_mid["dv"]), (1, 1, 404, 1),
                         f"构建中途对外必须整体旧版: {v_mid}")
        v_done = self._snapshot("after_build")
        self.assertEqual((v_done["units"], v_done["static"],
                          v_done["new_unit"], v_done["dv"]), (2, 2, 200, 2), v_done)


# ============================ 第七轮 P0: 连续刷新失败不得回收对外版本 ============================
class ServedVersionSurvivesGc(ServiceTestCase):
    """Codex 第六轮 P0 复现场景: 旧版正常服务后, 连续 ≥4 次「发布翻转 + 索引
    刷新失败」(混合同进程发布与独立子进程发布, 后者模拟 CLI 进程内的 GC)。
    每次都用真实 HTTP 验证 首页/静态搜题数据/搜题/旧卷详情/组卷 仍为完整旧版
    且 SERVED 版本目录**未被回收**; 修复刷新后整体切新版; 退役版本在宽限期内
    仍可访问(在途请求安全), 宽限期满并触发回收后才删除。
    旧实现(_gc_versions 不知情 SERVED, keep=2)在第 3 次失败发布后删除正在
    服务的版本 → 首页/详情 404, 本测试必失败。"""

    CODE2 = "8888"
    NEW_PAPERS = ["w25_21", "w26_31", "w27_41", "w28_51"]
    RUNNER = (
        "import json, os, sys\n"
        "sys.path.insert(0, {repo!r})\n"
        "os.environ['ZJ_PUBLISH_NOTIFY_SERVER'] = '0'\n"
        "import paper_pipeline as pp, migrate_syllabus as ms\n"
        "pp._apply_root({repo!r}); ms._apply_root({repo!r})\n"
        "res = ms.publish({code!r}, log=lambda *a: None)\n"
        "print('PUBLISH_RESULT', json.dumps({{'ok': res.get('ok'),"
        " 'stage': res.get('stage')}}))\n")

    def _build_repo_full(self):
        import fitz
        doc = fitz.open()
        p0 = doc.new_page(); p0.insert_text((200, 300), "Cambridge International 9999/11")
        p1 = doc.new_page(); p1.insert_text((50, 90), "1 Explain the demand question. [4]")
        open(os.path.join(self.dir, "x.pdf"), "wb").write(doc.tobytes())
        doc.close()
        os.makedirs(os.path.join(self.dir, "data", "units", self.CODE2), exist_ok=True)
        json.dump({"subject": self.CODE2, "code": "n01_11", "year": 2024,
                   "session": "May-Jun", "paper": 1, "variant": 1,
                   "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 4, "text": "bystander unit. [4]",
                              "topics": None, "status": "PENDING"}]},
                  open(os.path.join(self.dir, "data", "units", self.CODE2,
                                    "n01_11.json"), "w"))
        json.dump({"subject": CODE, "code": "s24_11", "year": 2024,
                   "session": "May-Jun", "paper": 1, "variant": 1,
                   "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 4, "text": "demand seed [4]",
                              "topics": ["1.1"], "status": "IN"}]},
                  open(os.path.join(self.dir, "data", "units", CODE,
                                    "s24_11.json"), "w"), ensure_ascii=False)
        json.dump({"units": {"1": {"status": "exact",
                                   "seg": [[2, 40, 70, 550, 700]]}},
                   "questions": {}},
                  open(os.path.join(self.dir, "data", "loc", CODE,
                                    "s24_11.json"), "w"))
        json.dump({"ms_status": "missing", "letters": {}, "units": {},
                   "questions": {}},
                  open(os.path.join(self.dir, "data", "msloc", CODE,
                                    "s24_11.json"), "w"))

    def _add_paper(self, pc):
        work = os.path.join(pp.PIPE_DIR, "work", CODE, "units")
        os.makedirs(work, exist_ok=True)
        json.dump({"code": pc, "year": 2023, "session": "Oct-Nov", "paper": 2,
                   "variant": 1, "qp_pdf": "x.pdf", "ms_pdf": None,
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 6,
                              "text": f"price mechanism gc cycle {pc} [6]"}]},
                  open(os.path.join(work, f"{pc}_qp.json"), "w"))
        json.dump({"code": pc, "tags": {"1": ["1.2"]}},
                  open(os.path.join(self.dir, f"tags_{CODE}", f"{pc}.json"), "w"))
        ms.adjudicate_new_papers(CODE, log=lambda *a: None)

    def _start_http(self):
        import server
        from http.server import ThreadingHTTPServer
        server.DATA_VERSION = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=15) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def _surface(self, tag, new_paper):
        """五端点快照: 首页/静态搜题数据/搜题/详情/组卷(全部应为同一版本)。"""
        st_i, b_i = self._get("/index.html")
        st_s, b_s = self._get(f"/search_data_{CODE}.js")
        m = re.search(r'"units":(\d+)', b_s.decode())
        st_q, b_q = self._get("/api/search?q=" + urllib.parse.quote("price mechanism"))
        st_old, _ = self._get(f"/api/unit?subject={CODE}&code=s24_11&uid=1")
        st_new, _ = self._get(f"/api/unit?subject={CODE}&code={new_paper}&uid=1")
        st_m, b_m = self._get(f"/api/mock/meta?subject={CODE}")
        return {"tag": tag, "index": st_i, "static_units": int(m.group(1)) if m else -1,
                "static_status": st_s, "hits": len(json.loads(b_q)["results"]),
                "old_unit": st_old, "new_unit": st_new,
                "mock_ok": json.loads(b_m).get("ok")}

    def tearDown(self):
        if getattr(self, "httpd", None):
            self.httpd.shutdown()
        ms._REFRESH_HOOKS.clear()
        super().tearDown()

    def test_four_failing_publishes_keep_served_version_http(self):
        import server, live_version as lv
        self._build_repo_full()
        server.apply_root(self.dir)
        ms.register_refresh_hook(server.refresh_indexes)
        self._start_http()
        # 旧版正常安装(SERVED v_old, retain 标记)
        r0 = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(r0["ok"], r0)
        served_vid = server.served_vid()
        self.assertTrue(served_vid)
        v_old = self._surface("served_old", "w25_21")
        self.assertEqual((v_old["index"], v_old["static_units"], v_old["old_unit"],
                          v_old["new_unit"], v_old["hits"]),
                         (200, 1, 200, 404, 0), v_old)
        served_dir = os.path.join(lv.VERSIONS_DIR, served_vid)
        self.assertTrue(os.path.isdir(served_dir))
        self.assertTrue(os.path.exists(served_dir + ".retain"))
        # 注入持续刷新失败(旁观科损坏 units → 服务端双索引构建抛错)
        open(os.path.join(self.dir, "data", "units", self.CODE2,
                          "n01_11.json"), "w").write('{"subject": "8888", BROKEN')
        for i, pc in enumerate(self.NEW_PAPERS):     # ≥4 次失败发布
            self._add_paper(pc)
            if i % 2 == 0:                            # 同进程发布(网页任务同链路)
                r = ms.publish(CODE, log=lambda *a: None)
                self.assertFalse(r["ok"], (pc, r))
                self.assertEqual(r.get("stage"), "refresh-failed",
                                 f"实际错误: {r.get('summary')}")
            else:                                     # 独立子进程发布(CLI 同构, GC 在另一进程)
                env = dict(os.environ, ZJ_PUBLISH_NOTIFY_SERVER="0")
                rp = subprocess.run(  # noqa: F405(subprocess 由 test_paper_pipeline 引入习惯; 本文件直接导入)
                    [sys.executable, "-c",
                     self.RUNNER.format(repo=self.dir, code=CODE)],
                    capture_output=True, text=True, env=env, timeout=180)
                self.assertEqual(rp.returncode, 0, rp.stdout[-300:])
            # 每次失败发布后: 五端点仍完整旧版, SERVED 版本目录仍在
            v = self._surface(f"cycle{i}_{pc}", pc)
            self.assertEqual((v["index"], v["static_units"], v["static_status"],
                              v["old_unit"], v["new_unit"], v["hits"], v["mock_ok"]),
                             (200, 1, 200, 200, 404, 0, True),
                             f"第{i + 1}次失败发布后对外必须仍是完整旧版: {v}")
            self.assertTrue(os.path.isdir(served_dir),
                            f"SERVED 版本 {served_vid} 不得被回收(第{i + 1}次)")
            self.assertTrue(os.path.exists(served_dir + ".retain"))
            lv._gc_versions(log=lambda *a: None)      # 手动再触发回收, 仍不得删除
            self.assertTrue(os.path.isdir(served_dir))
        # 恢复刷新 → 整体切新版(4 新卷全部可见)
        json.dump({"subject": self.CODE2, "code": "n01_11", "year": 2024,
                   "session": "May-Jun", "paper": 1, "variant": 1,
                   "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 4, "text": "bystander unit. [4]",
                              "topics": None, "status": "PENDING"}]},
                  open(os.path.join(self.dir, "data", "units", self.CODE2,
                                    "n01_11.json"), "w"))
        req = urllib.request.Request(self.base + "/api/pipeline/reload",
                                     method="POST", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            self.assertTrue(json.loads(r.read())["ok"])
        v_new = self._surface("after_reload", self.NEW_PAPERS[-1])
        self.assertEqual((v_new["index"], v_new["static_units"], v_new["old_unit"],
                          v_new["new_unit"], v_new["hits"]),
                         (200, 5, 200, 200, 4), v_new)
        self.assertNotEqual(server.served_vid(), served_vid)
        # 旧版退役但宽限期内仍在(在途请求安全), 之后(模拟到期)才可回收
        self.assertTrue(os.path.isdir(served_dir), "退役版本在宽限期内不得删除")
        with open(os.path.join(served_dir, "index.html"), "rb") as f:
            f.read(64)
        retired = served_dir + ".retired"
        self.assertTrue(os.path.exists(retired))
        past = time.time() - (lv.GC_GRACE + 60)
        os.utime(retired, (past, past))
        lv._gc_versions(log=lambda *a: None)
        self.assertFalse(os.path.isdir(served_dir), "宽限期满后应可回收")
        v_after = self._surface("after_gc", self.NEW_PAPERS[-1])
        self.assertEqual((v_after["index"], v_after["static_units"],
                          v_after["new_unit"]), (200, 5, 200),
                         "回收旧版不影响对外新版: {v_after}")

    def test_restart_keeps_boot_version_retained(self):
        """服务重启: BOOT(=活动版本)写保留标记; 旧进程的遗留标记被退役收敛。"""
        import server, live_version as lv
        self._build_repo_full()
        server.apply_root(self.dir)
        ms.register_refresh_hook(server.refresh_indexes)
        r0 = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(r0["ok"], r0)
        served = server.served_vid()
        # 模拟一次对外切换到新版本(旧 served 退役)后进程重启
        self._add_paper("w25_21")
        r1 = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(r1["ok"], r1)
        self.assertNotEqual(server.served_vid(), served)
        server.apply_root(self.dir)                  # 重启(状态重置)
        self.assertIsNone(server.BOOT_VID)
        boot = server._capture_boot_vid()
        self.assertEqual(boot, lv.active_version())
        self.assertTrue(os.path.exists(os.path.join(lv.VERSIONS_DIR,
                                                    boot + ".retain")))
        self.assertTrue(os.path.exists(os.path.join(lv.VERSIONS_DIR,
                                                   served + ".retired")),
                        "重启收敛: 上个进程的遗留 retain 标记转为退役")


if __name__ == "__main__":
    unittest.main(verbosity=1)
