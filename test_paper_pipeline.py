#!/usr/bin/env python3
"""test_paper_pipeline.py —— 真题管线(下载/验真/增量/判定/发布/可见性)测试。

全部使用本地夹具(临时仓库 + 本地 HTTP 服务), 不访问外网, 不触碰用户 PDF
与真实 data/。覆盖:
  - 文件名解析(常规/qp_et/反转/paper=0/gt 排除)
  - 来源发现: 非规则会话 slug、挂错年份的链接(按文件名定位年份)、重复链接、
    同卷 qp_et 多版本、solved/topical 排除
  - 下载与验真: %PDF+PyMuPDF、200+HTML、零字节、中断后续传(服务端先失败后成功)、
    同名内容变化→隔离区不覆盖、缺 MS 对账
  - --dry-run 零写入; 重复 --apply 零改动(目录树哈希不变)
  - 大纲版本更新 → sync 报告 migration_required 并跳过发布
  - 增量切题幂等(新卷→切; 未变→跳过; 源变→重切)
  - 新卷判定 IN/OUT/PENDING → 发布: classified 仅 IN、units 带 status、
    topics/search 数据重建、post_verify 通过
  - 可见性: 服务端搜题(server.SearchIndex)过滤 OUT/PENDING; mock_exam
    排除含 OUT/PENDING 小问的完整大题; merge_tags 缺标签不崩溃且上报

用法: /usr/bin/python3 test_paper_pipeline.py            # 全部
      /usr/bin/python3 test_paper_pipeline.py Download   # 按类名过滤
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
# 测试进程内发布不做"通知本机 8930 服务"(真实服务刷新与本仓库夹具无关,
# 且会拖慢/污染测试); 服务内刷新钩子路径由 test_pipeline_service 覆盖。
os.environ.setdefault("ZJ_PUBLISH_NOTIFY_SERVER", "0")
import fitz                      # noqa: E402
import paper_pipeline as pp      # noqa: E402
import migrate_syllabus as ms    # noqa: E402
import live_version as lv        # noqa: E402

UA = {"User-Agent": "test-agent"}
CODE = "9999"


# ============================ 夹具 PDF ============================
def make_qp_pdf(questions, cover="9999/01", variant_note=""):
    """构造最小 CAIE 风格 QP: 封面页 + 内容页(题号在左列 x≈50)。"""
    doc = fitz.open()
    p0 = doc.new_page()
    p0.insert_text((200, 300), f"Cambridge International {cover}")
    p0.insert_text((200, 330), variant_note or "Mock Paper for tests")
    p1 = doc.new_page()
    y = 90
    for q, text in questions:
        p1.insert_text((50, y), f"{q} {text}")
        y += 24
    p1.insert_text((50, y + 10), "END OF PAPER")
    data = doc.tobytes()
    doc.close()
    return data


def make_ms_pdf(pairs):
    doc = fitz.open()
    p0 = doc.new_page()
    p0.insert_text((200, 300), "Mark Scheme")
    p1 = doc.new_page()
    y = 90
    for label, text in pairs:
        p1.insert_text((50, y), f"{label} {text}")
        y += 24
    data = doc.tobytes()
    doc.close()
    return data


# ============================ 夹具 HTTP 服务 ============================
class FixtureSite:
    """按 path → bytes 提供文件; 支持 first_n_fail(path, n) 模拟中断。"""

    def __init__(self):
        self.files = {}
        self.fail_counts = {}
        self.hits = {}
        self.server = None
        self.thread = None

    def start(self):
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_HEAD(self):
                n = site.fail_counts.get(self.path, 0)
                if n > 0:
                    self.send_response(500)
                    self.end_headers()
                    return
                data = site.files.get(self.path)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()

            def do_GET(self):
                site.hits[self.path] = site.hits.get(self.path, 0) + 1
                n = site.fail_counts.get(self.path, 0)
                if n > 0:
                    site.fail_counts[self.path] = n - 1
                    self.send_response(500)
                    self.end_headers()
                    return
                data = site.files.get(self.path)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type",
                                 "text/html" if not data.startswith(b"%PDF")
                                 else "application/pdf")
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def url(self, path):
        return self.base + path


def a_href(url, label="x"):
    return f'<a href="{url}">{label}</a>'


def build_site(site):
    """构造覆盖各陷阱的来源站。"""
    up = "/directories/CAIE/CAIE-pastpapers/upload/"
    # --- 科目主页: 常规 slug + 非规则 slug(年份月份颠倒) + solved/topical(排除) ---
    subj = "\n".join([
        a_href(site.url("/papers/caie/test-subject-9999-2024-may-june")),
        a_href(site.url("/papers/caie/test-subject-9999-2024-june-2024-may")),   # 非规则
        a_href(site.url("/papers/caie/test-subject-9999-2023-oct-nov")),
        a_href(site.url("/papers/caie/test-subject-9999-solved-past-papers")),
        a_href(site.url("/papers/caie/test-subject-9999-topical-past-papers")),
    ])
    site.files["/papers/caie/test-subject-9999"] = subj.encode()

    q1 = [("1", "State the law of demand and explain two assumptions "
                "underlying it. [4]"),
          ("2", "Define the concept of opportunity cost in economics. [2]")]
    q2 = [("1", "Distinguish between movement along and shift of a demand "
                "curve. [6]")]
    q3 = [("1", "Analyse how the price mechanism allocates scarce resources "
                "in a market economy. [8]")]
    ms1 = [("1", "law of demand: inverse relation price/quantity"),
           ("2", "opportunity cost: next best alternative forgone")]

    def put(name, data):
        site.files[up + name] = data

    # --- 2024 may-june 会话页: 挂错年份的链接(2023 文件) + 重复链接 + qp_et +
    #     反转命名 + gt/er 资料齐全 ---
    s24 = "\n".join([
        a_href(site.url(up + "9999_s24_qp_11.pdf")),
        a_href(site.url(up + "9999_s24_qp_11.pdf")),                    # 重复
        a_href(site.url(up + "9999_s24_ms_11.pdf")),
        a_href(site.url(up + "9999_s24_qp_12.pdf")),
        a_href(site.url(up + "9999_s24_ms_12.pdf")),
        a_href(site.url(up + "9999_s24_qp_et_12.pdf")),                 # ET 版
        a_href(site.url(up + "9999_s24_ms_et_12.pdf")),
        a_href(site.url(up + "9999_s23_qp_11.pdf")),                     # 挂错年份
        a_href(site.url(up + "9999_s23_ms_11.pdf")),
        a_href(site.url(up + "s24_9999_qp_13.pdf")),                     # 反转命名
        a_href(site.url(up + "9999_s24_gt.pdf")),                        # 非 QP/MS
        a_href(site.url(up + "9999_s24_er_11.pdf")),
        a_href(site.url(up + "9999_s24_ci_31.pdf")),
    ])
    site.files["/papers/caie/test-subject-9999-2024-may-june"] = s24.encode()
    site.files["/papers/caie/test-subject-9999-2024-june-2024-may"] = s24.encode()
    put("9999_s24_qp_11.pdf", make_qp_pdf(q1, variant_note="Paper 1 v1"))
    put("9999_s24_ms_11.pdf", make_ms_pdf(ms1))
    put("9999_s24_qp_12.pdf", make_qp_pdf(q2, variant_note="Paper 1 v2"))
    put("9999_s24_ms_12.pdf", make_ms_pdf(ms1))
    put("9999_s24_qp_et_12.pdf", make_qp_pdf(q2, variant_note="Paper 1 v2 ET"))
    put("9999_s24_ms_et_12.pdf", make_ms_pdf(ms1))
    put("9999_s23_qp_11.pdf", make_qp_pdf(q3, variant_note="Paper 1 2023"))
    put("9999_s23_ms_11.pdf", make_ms_pdf(ms1))
    put("s24_9999_qp_13.pdf", make_qp_pdf(q2, variant_note="reversed name"))
    put("9999_s24_gt.pdf", b"%PDF-not-really")
    put("9999_s24_er_11.pdf", b"<html>report</html>")

    # --- 2023 oct-nov 会话页: 200+HTML 伪 PDF / 零字节 / 缺 MS ---
    s23 = "\n".join([
        a_href(site.url(up + "9999_w23_qp_11.pdf")),
        a_href(site.url(up + "9999_w23_ms_11.pdf")),
        a_href(site.url(up + "9999_w23_qp_21.pdf")),      # 无 MS
        a_href(site.url(up + "9999_w23_qp_22.pdf")),      # 200+HTML
        a_href(site.url(up + "9999_w23_qp_23.pdf")),      # 零字节
        a_href(site.url(up + "9999_w23_qp_24.pdf")),      # 中断续传(500×2 后成功)
        a_href(site.url(up + "9999_w23_ms_24.pdf")),
    ])
    site.files["/papers/caie/test-subject-9999-2023-oct-nov"] = s23.encode()
    put("9999_w23_qp_11.pdf", b"<html>soft 404 page</html>")
    put("9999_w23_ms_11.pdf", b"")
    put("9999_w23_qp_21.pdf", make_qp_pdf(q1, variant_note="no MS paper"))
    put("9999_w23_qp_22.pdf", b"<html>also soft 404</html>")
    put("9999_w23_qp_23.pdf", b"")
    put("9999_w23_qp_24.pdf", make_qp_pdf(q1, variant_note="resumable"))
    put("9999_w23_ms_24.pdf", make_ms_pdf(ms1))
    site.fail_counts[up + "9999_w23_qp_24.pdf"] = 2


# ============================ 夹具仓库 ============================
TOPICS = {
    "subject": CODE, "name": "Fixture Studies", "zh": "夹具学",
    "source": "fixture", "syllabus_version": "2024-2026",
    "note": "test taxonomy",
    "levels": [{"level": "AS", "from": "1", "to": "2", "papers": [1, 2]}],
    "sections": [
        {"code": "1", "en": "Market mechanics", "zh": "市场机制",
         "kw": ["demand", "supply", "price"],
         "subs": [
             {"code": "1.1", "en": "Demand and supply",
              "zh": "供需", "kw": ["demand", "law of demand", "supply"]},
             {"code": "1.2", "en": "Price mechanism",
              "zh": "价格机制", "kw": ["price mechanism", "scarcity",
                                       "allocate resources"]}]},
        {"code": "2", "en": "Fundamental concepts", "zh": "基本概念",
         "kw": ["concept"],
         "subs": [
             {"code": "2.1", "en": "Scarcity and choice",
              "zh": "稀缺与选择", "kw": ["opportunity cost", "scarcity",
                                          "choice"]}]},
    ],
}


class TestRepo:
    """临时仓库: subjects.json + 空 units + topics/classified/tags。"""

    def __init__(self, site):
        self.dir = tempfile.mkdtemp(prefix="pp_test_repo_")
        self.site = site
        dd = os.path.join(self.dir, "9999（夹具）")
        os.makedirs(os.path.join(dd, "2024", "May-Jun"))
        os.makedirs(os.path.join(self.dir, "data", "units", CODE))
        os.makedirs(os.path.join(self.dir, "data", "loc", CODE))
        os.makedirs(os.path.join(self.dir, "data", "msloc", CODE))
        os.makedirs(os.path.join(self.dir, f"tags_{CODE}"))
        with open(os.path.join(self.dir, "subjects.json"), "w") as f:
            json.dump({"subjects": {
                CODE: {"code": CODE, "name": "Fixture Studies", "zh": "夹具",
                       "level": "AS & A Level", "data_dir": "9999（夹具）",
                       "source": {"kind": "papacambridge",
                                  "slug": "test-subject-9999"},
                       "caie_page": site.url("/caie/fixture-9999"),
                       "paper_names": {}}}}, f)
        with open(os.path.join(self.dir, f"topics_{CODE}.json"), "w") as f:
            json.dump(TOPICS, f, ensure_ascii=False)
        with open(os.path.join(self.dir, f"classified_{CODE}.json"), "w") as f:
            json.dump({"subject": CODE, "generated": "2026-01-01",
                       "papers": []}, f)
        pp._apply_root(self.dir)
        ms._apply_root(self.dir)

    def destroy(self):
        pp._apply_root(REPO)
        ms._apply_root(REPO)
        shutil.rmtree(self.dir, ignore_errors=True)

    def tree_hash(self, *skip_prefixes):
        """目录树内容哈希(路径+内容), 跳过指定前缀(如 runs/)。"""
        h = hashlib.sha256()
        for dp, dns, fs in os.walk(self.dir):
            dns[:] = sorted(dns)
            for f in sorted(fs):
                p = os.path.join(dp, f)
                rel = os.path.relpath(p, self.dir)
                if any(rel.startswith(s) for s in skip_prefixes):
                    continue
                h.update(rel.encode())
                with open(p, "rb") as fh:
                    h.update(fh.read())
        return h.hexdigest()


class PipelineTestCase(unittest.TestCase):
    site = None

    @classmethod
    def setUpClass(cls):
        if PipelineTestCase.site is None:
            site = FixtureSite()
            site.start()          # 先起服务拿到 base, 再构造含绝对链接的页面
            build_site(site)
            PipelineTestCase.site = site

    def setUp(self):
        self.repo = TestRepo(PipelineTestCase.site)

    def tearDown(self):
        self.repo.destroy()


# ============================ 解析与发现 ============================
class ParseNames(unittest.TestCase):
    def test_regular(self):
        r = pp.parse_pdf_name("9999_s24_qp_11.pdf", CODE)
        self.assertEqual(r[0:5], ("qp", 1, 1, 2024, "May-Jun"))

    def test_qp_et(self):
        r = pp.parse_pdf_name("9999_s24_qp_et_12.pdf", CODE)
        self.assertEqual(r[0], "qp")

    def test_reversed(self):
        r = pp.parse_pdf_name("s24_9999_qp_13.pdf", CODE)
        self.assertEqual(r[0:5], ("qp", 1, 3, 2024, "May-Jun"))
        self.assertTrue(r[5])

    def test_wrong_subject_excluded(self):
        self.assertIsNone(pp.parse_pdf_name("8888_s24_qp_11.pdf", CODE))

    def test_paper0_excluded(self):
        self.assertIsNone(pp.parse_pdf_name("9999_s08_qp_01.pdf", CODE))

    def test_gt_not_matched(self):
        self.assertIsNone(pp.parse_pdf_name("9999_s24_gt.pdf", CODE))


class Discovery(PipelineTestCase):
    def run_discovery(self):
        files, spages = pp.papacambridge_discover(
            CODE, slug="test-subject-9999",
            source_url=PipelineTestCase.site.url("/papers/caie/test-subject-9999"),
            log=lambda *a: None)
        return files, spages

    def test_counts_and_filtering(self):
        files, spages = self.run_discovery()
        names = set(files)
        self.assertIn("9999_s24_qp_11.pdf", names)
        self.assertIn("9999_s24_qp_et_12.pdf", names)     # ET 版保留
        self.assertIn("9999_s24_ms_et_12.pdf", names)
        self.assertIn("9999_s23_qp_11.pdf", names)        # 挂错年份的按文件名归 2023
        self.assertNotIn("s24_9999_qp_13.pdf", names)     # 反转命名视为重复源
        self.assertNotIn("9999_s24_gt.pdf", names)        # 非 QP/MS
        self.assertNotIn("9999_s24_er_11.pdf", names)
        self.assertNotIn("9999_s24_ci_31.pdf", names)
        # solved/topical 会话被排除: 只有 2 个真会话页被抓(可能各出现两次 slug)
        real = [s for s in spages if "solved" not in s["slug"]
                and "topical" not in s["slug"]]
        self.assertTrue(real)
        # 挂错年份归位: 2023 文件的 year 字段来自文件名
        self.assertEqual(files["9999_s23_qp_11.pdf"].year, 2023)
        # s 码 = May-Jun: 年份/考季一律来自文件名, 不信会话页
        self.assertEqual(files["9999_s23_qp_11.pdf"].session, "May-Jun")
        self.assertEqual(files["9999_s24_qp_11.pdf"].year, 2024)

    def test_missing_ms_reported(self):
        state = pp.load_state()
        report = {"targets": [], "failures": []}
        files, _ = self.run_discovery()
        pp.plan_and_download(CODE, pp.load_registry()[CODE], files,
                            (2023, 2023), False, state, report,
                            log=lambda *a: None)
        names = {e["name"]: e for e in report["targets"]}
        self.assertIn("9999_w23_qp_21.pdf", names)        # 缺 MS 的 QP 在清单
        self.assertEqual(names["9999_w23_qp_21.pdf"]["status"],
                         "missing-locally")
        qp_names = {n for n, e in names.items() if e["kind"] == "qp"}
        self.assertIn("9999_w23_ms_11.pdf",
                      {n.replace("_qp", "_ms") for n in qp_names})


# ============================ 下载/验真/幂等 ============================
class Download(PipelineTestCase):
    def apply_2023(self):
        state = pp.load_state()
        report = {"targets": [], "failures": []}
        files, _ = pp.papacambridge_discover(
            CODE, slug="test-subject-9999",
            source_url=PipelineTestCase.site.url("/papers/caie/test-subject-9999"),
            log=lambda *a: None)
        pp.plan_and_download(CODE, pp.load_registry()[CODE], files,
                            (2023, 2023), True, state, report,
                            log=lambda *a: None)
        return report

    def test_validation_and_failures(self):
        report = self.apply_2023()
        st = {e["name"]: e["status"] for e in report["targets"]}
        self.assertEqual(st["9999_w23_qp_11.pdf"], "failed:200-html")
        self.assertEqual(st["9999_w23_ms_11.pdf"], "failed:zero-bytes")
        self.assertEqual(st["9999_w23_qp_22.pdf"], "failed:200-html")
        self.assertEqual(st["9999_w23_qp_23.pdf"], "failed:zero-bytes")
        self.assertEqual(st["9999_w23_qp_24.pdf"], "downloaded")   # 500×2 后续传成功
        dest = os.path.join(self.repo.dir, "9999（夹具）", "2023", "Oct-Nov",
                            "9999_w23_qp_24.pdf")
        self.assertTrue(os.path.exists(dest))
        with open(dest, "rb") as f:
            self.assertEqual(f.read(5), b"%PDF-")
        self.assertIn("9999_w23_qp_21.pdf",
                      [m["qp"] for m in report["missing_ms"]])

    def test_second_apply_zero_change(self):
        self.apply_2023()
        h1 = self.repo.tree_hash()
        self.apply_2023()
        h2 = self.repo.tree_hash()
        self.assertEqual(h1, h2, "重复 apply 不得改写未变化文件")

    def test_conflict_quarantined_not_overwritten(self):
        self.apply_2023()
        dest = os.path.join(self.repo.dir, "9999（夹具）", "2023", "Oct-Nov",
                            "9999_w23_qp_24.pdf")
        # 模拟远端内容变化(同文件名不同内容): 服务器替换内容
        up = "/directories/CAIE/CAIE-pastpapers/upload/"
        self.site.files[up + "9999_w23_qp_24.pdf"] = make_qp_pdf(
            [("9", "totally different content [2]")])
        before = open(dest, "rb").read()
        report = self.apply_2023()
        st = {e["name"]: e["status"] for e in report["targets"]}
        self.assertEqual(st["9999_w23_qp_24.pdf"], "conflict-quarantined")
        self.assertEqual(before, open(dest, "rb").read())   # 不覆盖
        q = os.path.join(pp.QUAR_DIR, CODE, "9999_w23_qp_24.pdf")
        self.assertTrue(os.path.exists(q))


class DryRunNoWrite(PipelineTestCase):
    def test_dry_run_writes_nothing(self):
        before = self.repo.tree_hash()
        state = pp.load_state()
        report = {"targets": [], "failures": []}
        files, _ = pp.papacambridge_discover(
            CODE, slug="test-subject-9999",
            source_url=PipelineTestCase.site.url("/papers/caie/test-subject-9999"),
            log=lambda *a: None)
        pp.plan_and_download(CODE, pp.load_registry()[CODE], files,
                            (2024, 2024), False, state, report,
                            log=lambda *a: None)
        self.assertEqual(self.repo.tree_hash(), before)
        self.assertFalse(os.path.exists(pp.RUNS_DIR))
        # 计划仍然完整: 展示待下载项
        self.assertTrue(any(e["status"] == "missing-locally"
                            for e in report["targets"]))


class SyllabusVersionChange(PipelineTestCase):
    def test_sync_flags_migration_required(self):
        state = pp.load_state()
        st = pp.subj_state(state, CODE)
        st["syllabus"] = {"version": "2024-2026",
                          "pdf": {"url": "x", "sha256": "y"}}
        pp.save_state(state)
        import unittest.mock as mock
        fake = {"version": "2030-2032",
                "url": self.site.url("/Images/999.pdf"),
                "label": "2030-2032 Syllabus", "update": None}
        with mock.patch.object(pp, "resolve_latest_syllabus", return_value=fake):
            report = {"targets": [], "failures": [],
                      "cmd": "sync", "code": CODE}
            sub = pp.load_registry()[CODE]
            files, _ = pp.papacambridge_discover(
                CODE, slug="test-subject-9999",
                source_url=self.site.url("/papers/caie/test-subject-9999"),
                log=lambda *a: None)
            pp.plan_and_download(CODE, sub, files, (2024, 2024), False,
                                 pp.load_state(), report, log=lambda *a: None)
            target = pp.resolve_latest_syllabus(sub["caie_page"])
            report["syllabus"] = {
                "version": target["version"], "url": target["url"],
                "recorded": "2024-2026", "migration_required": True}
        self.assertTrue(report["syllabus"]["migration_required"])


# ============================ 增量切题 ============================
class ExtractIncrement(PipelineTestCase):
    def setUp(self):
        super().setUp()
        up = "/directories/CAIE/CAIE-pastpapers/upload/"
        self.site.files[up + "9999_s24_qp_11.pdf"] = make_qp_pdf(
            [("1", "State the law of demand and explain two assumptions "
                   "underlying it. [4]"),
             ("2", "Define the concept of opportunity cost in economics. [2]")],
            variant_note="Paper 1 v1")
        self.site.files[up + "9999_s24_ms_11.pdf"] = make_ms_pdf(
            [("1", "law of demand"), ("2", "opportunity cost")])
        state = pp.load_state()
        report = {"targets": [], "failures": []}
        files, _ = pp.papacambridge_discover(
            CODE, slug="test-subject-9999",
            source_url=PipelineTestCase.site.url("/papers/caie/test-subject-9999"),
            log=lambda *a: None)
        pp.plan_and_download(CODE, pp.load_registry()[CODE], files,
                            (2024, 2024), True, state, report,
                            log=lambda *a: None)

    def test_incremental(self):
        r1 = pp.cmd_extract(pp.argparse.Namespace(code=CODE, apply=True,
                                                  papers=None))
        # qp_et/ms_et 已下载入档但按既有约定不切题(见 data/reconcile.json 规则排除)
        self.assertEqual(r1["extracted"], 4)      # 2 卷 × (qp+ms)
        work = os.path.join(pp.PIPE_DIR, "work", CODE, "units")
        qp = json.load(open(os.path.join(work, "s24_11_qp.json")))
        self.assertTrue(qp["units"], "夹具 QP 应切出单元")
        # 未变化 → 跳过
        r2 = pp.cmd_extract(pp.argparse.Namespace(code=CODE, apply=True,
                                                  papers=None))
        self.assertEqual(r2["extracted"], 0)
        # 源变化 → 重切
        up = "/directories/CAIE/CAIE-pastpapers/upload/"
        self.site.files[up + "9999_s24_qp_11.pdf"] = make_qp_pdf(
            [("1", "Changed question about supply curves. [3]")])
        src = os.path.join(self.repo.dir, "9999（夹具）", "2024", "May-Jun",
                           "9999_s24_qp_11.pdf")
        # 直接改本地文件模拟源更新(哈希变化)
        with open(src, "wb") as f:
            f.write(self.site.files[up + "9999_s24_qp_11.pdf"])
        r3 = pp.cmd_extract(pp.argparse.Namespace(code=CODE, apply=True,
                                                  papers=None))
        # 该卷的 qp+ms 成对重切
        self.assertEqual(r3["extracted"], 2)


# ============================ 判定/发布/可见性 ============================
def seed_published_paper(repo, code="s24_11", statuses=None):
    """在临时仓库放一卷已发布单元(status: {uid: (status, tags)})。"""
    units_dir = os.path.join(repo.dir, "data", "units", CODE)
    qp_rel = f"9999（夹具）/2024/May-Jun/9999_s24_qp_11.pdf"
    src = os.path.join(repo.dir, "9999（夹具）", "2024", "May-Jun",
                       "9999_s24_qp_11.pdf")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    if not os.path.exists(src):
        with open(src, "wb") as f:
            f.write(make_qp_pdf(
                [("1", "State the law of demand and explain two assumptions "
                       "underlying it. [4]"),
                 ("2", "Define the concept of opportunity cost. [2]")]))
    units = []
    for uid, q, part, text in (
            ("1", 1, None, "State the law of demand and explain two "
                            "assumptions underlying it. [4]"),
            ("2", 2, None, "Define the concept of opportunity cost "
                           "in economics. [2]")):
        st, tags = (statuses or {}).get(uid, ("IN", ["1.1"]))
        u = {"id": uid, "q": q, "part": part, "page": 2, "marks": 4,
             "text": text, "topics": tags if st == "IN" else None,
             "in_old_index": st == "IN", "text_source": "id", "status": st}
        units.append(u)
    rec = {"subject": CODE, "code": code, "year": 2024, "session": "May-Jun",
           "paper": 1, "variant": 1, "qp_pdf": qp_rel, "ms_pdf": None,
           "ms_pages": {}, "units": units,
           "syllabus_version": "2024-2026"}
    with open(os.path.join(units_dir, code + ".json"), "w") as f:
        json.dump(rec, f, ensure_ascii=False)
    # 合法 loc(供 mock 候选核验)
    loc = {"units": {uid: {"status": "exact", "seg": [[2, 40, 70, 550, 700]]}
                     for uid, *_ in [(u["id"],) for u in units]},
           "questions": {"1": {"status": "exact", "seg": [[2, 40, 70, 550, 700]]},
                         "2": {"status": "exact", "seg": [[2, 40, 70, 550, 700]]}}}
    with open(os.path.join(repo.dir, "data", "loc", CODE, code + ".json"),
              "w") as f:
        json.dump(loc, f)
    with open(os.path.join(repo.dir, "data", "msloc", CODE, code + ".json"),
              "w") as f:
        json.dump({"ms_status": "missing", "letters": {}, "units": {},
                   "questions": {}}, f)
    return rec


class Visibility(unittest.TestCase):
    """不依赖 HTTP 夹具的可见性测试(直接构造 data/units)。"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="pp_vis_")
        os.makedirs(os.path.join(cls.dir, "data", "units", CODE))
        os.makedirs(os.path.join(cls.dir, "data", "loc", CODE))
        os.makedirs(os.path.join(cls.dir, "data", "msloc", CODE))
        with open(os.path.join(cls.dir, f"topics_{CODE}.json"), "w") as f:
            json.dump(TOPICS, f, ensure_ascii=False)
        pp._apply_root(cls.dir)
        ms._apply_root(cls.dir)

    @classmethod
    def tearDownClass(cls):
        pp._apply_root(REPO)
        ms._apply_root(REPO)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def seed(self, statuses):
        shutil.rmtree(os.path.join(self.dir, "data", "units", CODE))
        os.makedirs(os.path.join(self.dir, "data", "units", CODE))
        return seed_published_paper(self, statuses=statuses)

    def test_server_search_filters_out_pending(self):
        import server
        self.seed({"1": ("IN", ["1.1"]), "2": ("PENDING", None)})
        old_units = server.UNITS_DIR
        server.UNITS_DIR = os.path.join(self.dir, "data", "units")
        try:
            idx = server.SearchIndex()
            idx.build()
            res = idx.search("opportunity cost")
            self.assertEqual(res, [], "PENDING 单元不得出现在服务端搜题")
            res2 = idx.search("law of demand")
            self.assertEqual(len(res2), 1)
            self.assertEqual(res2[0]["uid"], "1")
            self.assertEqual(res2[0]["status"], "IN")
        finally:
            server.UNITS_DIR = old_units

    def test_mock_exam_excludes_mixed_question(self):
        import mock_exam as me
        # Q1 全 IN, Q2 混 IN/PENDING → 只 Q1 进候选
        self.seed({"1": ("IN", ["1.1"]), "2": ("PENDING", None)})
        old = (me.UNITS_DIR, me.LOC_DIR, me.MSLOC_DIR, me.ROOT)
        me.ROOT = self.dir
        me.UNITS_DIR = os.path.join(self.dir, "data", "units")
        me.LOC_DIR = os.path.join(self.dir, "data", "loc")
        me.MSLOC_DIR = os.path.join(self.dir, "data", "msloc")
        try:
            idx = me.MockIndex()
            idx._build()
            idx.ready = True
            qs = idx.subjects[CODE]["questions"]
            self.assertEqual([q["q"] for q in qs], [1])
            excl = idx.subjects[CODE]["papers"][1]["excluded"]
            self.assertEqual(excl.get("out-or-pending"), 1)
        finally:
            me.ROOT, me.UNITS_DIR, me.LOC_DIR, me.MSLOC_DIR = old

    def test_mock_exam_excludes_out_question(self):
        import mock_exam as me
        self.seed({"1": ("OUT", None), "2": ("IN", ["2.1"])})
        old = (me.UNITS_DIR, me.LOC_DIR, me.MSLOC_DIR, me.ROOT)
        me.ROOT = self.dir
        me.UNITS_DIR = os.path.join(self.dir, "data", "units")
        me.LOC_DIR = os.path.join(self.dir, "data", "loc")
        me.MSLOC_DIR = os.path.join(self.dir, "data", "msloc")
        try:
            idx = me.MockIndex()
            idx._build()
            idx.ready = True
            qs = idx.subjects[CODE]["questions"]
            self.assertEqual([q["q"] for q in qs], [2])
            excl = idx.subjects[CODE]["papers"][1]["excluded"]
            self.assertEqual(excl.get("out-or-pending"), 1)
        finally:
            me.ROOT, me.UNITS_DIR, me.LOC_DIR, me.MSLOC_DIR = old

    def test_rescore_flags_out_or_pending(self):
        import mock_exam as me
        rec = {"units": [
            {"id": "1", "q": 1, "part": None, "page": 2, "marks": 4,
             "text": "all fine [4]", "topics": ["1.1"], "status": "IN"},
            {"id": "1a", "q": 1, "part": "a", "page": 2, "marks": 2,
             "text": "sub part (a) [2]", "topics": ["1.1"], "status": "IN"},
            {"id": "1b", "q": 1, "part": "b", "page": 2, "marks": 2,
             "text": "removed-topic sub part [2]", "topics": None,
             "status": "OUT"},
        ], "paper": 1}
        qs, why = me.rescore_paper(rec)
        self.assertIsNone(why)
        self.assertEqual(qs[0]["out_or_pending"], 1)
        self.assertEqual(qs[0]["unclassified"], 0)


class PublishNewPaper(PipelineTestCase):
    """sync --apply 的判定→发布贯通(夹具新卷)。P0-2(2026-11-07): 无 AI 草稿的
    新卷自动结果只作候选(PENDING); 提供 AI 草稿/人工复核后才能 IN 并发布。"""

    def _seed_new_paper(self):
        seed_published_paper(self.repo)
        up = "/directories/CAIE/CAIE-pastpapers/upload/"
        self.site.files[up + "9999_w23_qp_21.pdf"] = make_qp_pdf(
            [("1", "Explain how the price mechanism allocates scarce "
                   "resources. [6]"),
             ("2", "Place the exam attendance sticker on your answer "
                   "script. [0]")],
            variant_note="Paper 1 new")
        state = pp.load_state()
        report = {"targets": [], "failures": []}
        files, _ = pp.papacambridge_discover(
            CODE, slug="test-subject-9999",
            source_url=self.site.url("/papers/caie/test-subject-9999"),
            log=lambda *a: None)
        pp.plan_and_download(CODE, pp.load_registry()[CODE], files,
                            (2023, 2023), True, state, report,
                            log=lambda *a: None)
        pp.cmd_extract(pp.argparse.Namespace(code=CODE, apply=True,
                                             papers=None))

    def test_adjudicate_and_publish(self):
        self._seed_new_paper()
        res = ms.adjudicate_new_papers(CODE, log=lambda *a: None)
        self.assertTrue(res["ok"], res.get("problems"))
        self.assertIn("w23_21", res["new_papers"])
        adj = json.load(open(os.path.join(pp.PIPE_DIR, "adjudication",
                                          f"{CODE}.json")))
        pa = adj["units"]["w23_21"]
        # P0-2: 自动关键词/近邻只作候选, 不得直接 IN
        self.assertEqual(pa["1"]["status"], "PENDING", pa["1"])
        self.assertTrue(pa["1"].get("candidate_tags"),
                        "强命中须记录候选标签供复核")
        self.assertEqual(pa["2"]["status"], "PENDING")
        pub = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(pub["ok"], pub.get("problems"))
        cls = json.load(open(os.path.join(self.repo.dir,
                                          f"classified_{CODE}.json")))
        self.assertEqual([(p["code"], u["id"]) for p in cls["papers"]
                          for u in p["units"]], [],
                         "无草稿的新卷不得自动进入 classified(IN)")
        # 复核路径(网页判定复核 = overrides)→ IN → 再发布
        ms.record_override(CODE, "w23_21", "1",
                           {"status": "IN", "tags": ["1.2"],
                            "reason": "复核: price mechanism → 1.2(官方条款)",
                            "reviewed_at": "test"})
        adj2 = json.load(open(os.path.join(pp.PIPE_DIR, "adjudication",
                                           f"{CODE}.json")))
        self.assertEqual(adj2["units"]["w23_21"]["1"]["status"], "IN")
        pub2 = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(pub2["ok"], pub2.get("problems"))
        cls2 = json.load(open(os.path.join(self.repo.dir,
                                           f"classified_{CODE}.json")))
        all_units = [(p["code"], u["id"]) for p in cls2["papers"]
                     for u in p["units"]]
        self.assertIn(("w23_21", "1"), all_units)
        for f in (f"topics_data_{CODE}.js", f"search_data_{CODE}.js",
                  "index.html"):
            self.assertTrue(os.path.exists(os.path.join(self.repo.dir, f)))


# ============================ P0-2(2026-11-07): 反例题面作为新卷不得自动 IN ============================
class NewPaperCounterexamples(unittest.TestCase):
    """Codex 复现脚本: 用真实 9701 taxonomy + 两个已确认反例的原题面作为
    新卷, 无 AI 草稿调用真实 adjudicate_new_papers → 不得 IN(只可候选/PENDING)。"""

    TEXT_6C = ("An individual enzyme operates best at a specific pH. Different "
               "enzymes operate best under conditions of different pH. The "
               "graph shows how the activity of three digestive enzymes "
               "(amylase, pepsin, trypsin) varies with pH. [3]")
    TEXT_7B = ("DNA is responsible for encoding the amino acid sequence to "
               "produce proteins. Ribosome, tRNA and mRNA are all involved in "
               "protein synthesis. Describe how the base sequence of DNA "
               "codes for the amino acid sequence of a protein. [3]")

    def test_counterexamples_not_auto_in(self):
        d = tempfile.mkdtemp(prefix="pp_cx_")
        self.addCleanup(shutil.rmtree, d, True)
        self.addCleanup(ms._apply_root, REPO)
        self.addCleanup(pp._apply_root, REPO)
        os.makedirs(os.path.join(d, "data", "units", "9701"))
        work = os.path.join(d, "data", "pipeline", "work", "9701", "units")
        os.makedirs(work)
        # 真实 taxonomy(含 38.x 实操技能分类)
        shutil.copy(os.path.join(REPO, "topics_9701.json"), d)
        for pc, uid, text in (("s99_41", "6c", self.TEXT_6C),
                              ("s99_42", "7b", self.TEXT_7B)):
            with open(os.path.join(work, f"{pc}_qp.json"), "w") as f:
                json.dump({"code": pc, "year": 2099, "session": "May-Jun",
                           "paper": 4, "variant": 1, "qp_pdf": "x.pdf",
                           "ms_pdf": None,
                           "units": [{"id": uid, "q": 6, "part": "c",
                                      "page": 13, "marks": 3,
                                      "text": text}]}, f)
        pp._apply_root(d)
        ms._apply_root(d)
        res = ms.adjudicate_new_papers("9701", log=lambda *a: None)
        self.assertTrue(res["ok"], res.get("problems"))
        self.assertEqual(res["new_papers"], ["s99_41", "s99_42"])
        adj = json.load(open(os.path.join(d, "data", "pipeline",
                                          "adjudication", "9701.json")))
        for pc, uid in (("s99_41", "6c"), ("s99_42", "7b")):
            e = adj["units"][pc][uid]
            self.assertEqual(e["status"], "PENDING",
                             f"{pc}#{uid} 不得自动 IN: {e}")
            self.assertNotEqual(e.get("basis"), "auto-in")


class MergeTagsFix(unittest.TestCase):
    def test_missing_tag_no_crash(self):
        d = tempfile.mkdtemp(prefix="pp_mt_")
        self.addCleanup(shutil.rmtree, d, True)
        units = os.path.join(d, "units")
        tags = os.path.join(d, "tags")
        os.makedirs(units)
        os.makedirs(tags)
        with open(os.path.join(units, "t01_qp.json"), "w") as f:
            json.dump({"code": "t01", "year": 2024, "session": "May-Jun",
                       "paper": 1, "variant": 1, "qp_pdf": "x.pdf",
                       "ms_pdf": None,
                       "units": [
                           {"id": "1", "q": 1, "part": None, "page": 2,
                            "marks": 2, "text": "demand question [2]"},
                           {"id": "2", "q": 2, "part": None, "page": 2,
                            "marks": 2, "text": "no tag for me [2]"}]}, f)
        with open(os.path.join(tags, "t01.json"), "w") as f:
            json.dump({"code": "t01", "tags": {"1": ["9.9"]}}, f)  # 非法码+缺标签
        with open(os.path.join(d, f"topics_{CODE}.json"), "w") as f:
            json.dump(TOPICS, f, ensure_ascii=False)
        import subprocess
        r = subprocess.run(["/usr/bin/python3", os.path.join(REPO, "merge_tags.py"),
                            CODE, units, tags, d], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("NO TAG", r.stdout)          # 缺标签被上报而非崩溃
        self.assertIn("invalid codes ['9.9']", r.stdout)   # 非法码漏报已修
        out = json.load(open(os.path.join(d, f"classified_{CODE}.json")))
        self.assertEqual(out["papers"][0]["units"], [])     # 两单元都被拦下
        self.assertNotEqual(out["generated"], "2026-08-31")  # 生成日期不再固定


# ============================ P0-1/P0-3: 基线隔离 + 提交各阶段完整回滚 ============================
class BaselineIsolationAndRollback(PipelineTestCase):
    """复现 9709 场景: 已有正式卷(带 status) + workdir 同名积压 + 1 个真新卷。

    旧实现失败方式: 待发布以 published.json 为准且判定前就写 data/units,
    862 份既有正式卷被当新卷重判并改写; gen_* 失败后静态索引不回滚。
    """

    def _publish_seed(self):
        seed_published_paper(self.repo)            # 既有正式卷 s24_11(IN)
        res = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(res["ok"], res)
        return res

    def _add_new_paper(self):
        up = "/directories/CAIE/CAIE-pastpapers/upload/"
        self.site.files[up + "9999_w23_qp_21.pdf"] = make_qp_pdf(
            [("1", "Explain how the price mechanism allocates scarce "
                   "resources. [6]")], variant_note="Paper 1 new")
        self.site.files[up + "9999_w23_ms_21.pdf"] = make_ms_pdf(
            [("1", "price mechanism answer")])
        state = pp.load_state()
        report = {"targets": [], "failures": []}
        files, _ = pp.papacambridge_discover(
            CODE, slug="test-subject-9999",
            source_url=self.site.url("/papers/caie/test-subject-9999"),
            log=lambda *a: None)
        pp.plan_and_download(CODE, pp.load_registry()[CODE], files,
                            (2023, 2023), True, state, report,
                            log=lambda *a: None)
        pp.cmd_extract(pp.argparse.Namespace(code=CODE, apply=True, papers=None))

    def test_baseline_and_isolation(self):
        self._publish_seed()
        self._add_new_paper()
        # P0-1a: 已发布基线安全初始化 —— 只登记带正式 status 的卷
        added = ms.ensure_published_baseline(CODE, log=lambda *a: None)
        self.assertEqual(added, 0, "publish 后基线已登记, 不应重复初始化")
        self.assertIn("s24_11", ms.published_papers(CODE))
        # P0-1b: 待发布 = 真新卷 only(workdir 中的 s24_11 积压不算;
        # s23_11/w23_21/w23_24 是本次新下载且从未发布的卷)
        pend = ms.pending_papers(CODE, log=lambda *a: None)
        self.assertEqual(pend, ["s23_11", "w23_21", "w23_24"])
        # P0-1c: 判定阶段不写正式 data/units(字节不变)
        h_before = self.repo.tree_hash("data/pipeline", "data/cache")
        res = ms.adjudicate_new_papers(CODE, log=lambda *a: None)
        self.assertEqual(res["new_papers"], ["s23_11", "w23_21", "w23_24"])
        self.assertEqual(self.repo.tree_hash("data/pipeline", "data/cache"),
                         h_before, "判定不得改写正式数据/索引")
        # P0-1d: 发布只并入新卷; 既有 s24_11 字节不变
        units_dir = os.path.join(self.repo.dir, "data", "units", CODE)
        s24_before = open(os.path.join(units_dir, "s24_11.json"), "rb").read()
        pub = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(pub["ok"], pub)
        self.assertEqual(open(os.path.join(units_dir, "s24_11.json"),
                              "rb").read(), s24_before,
                         "新增一卷不得重判/改写其余既有正式卷")
        self.assertTrue(os.path.exists(os.path.join(units_dir, "w23_21.json")))
        cls = json.load(open(os.path.join(self.repo.dir,
                                          f"classified_{CODE}.json")))
        self.assertEqual(sorted(p["code"] for p in cls["papers"]),
                         ["s23_11", "s24_11", "w23_21", "w23_24"])
        # 幂等: 全部已发布后 pending 为 0
        self.assertEqual(ms.pending_papers(CODE, log=lambda *a: None), [])

    def test_publish_failure_rolls_back_every_stage(self):
        import unittest.mock as mock
        self._publish_seed()
        self._add_new_paper()
        ms.adjudicate_new_papers(CODE, log=lambda *a: None)

        def formal_hashes():
            rels = ms._formal_artifacts(CODE)
            return rels, ms._hash_formal(rels)

        rels, before = formal_hashes()
        self.assertIn(f"topics_data_{CODE}.js", rels)
        self.assertIn(f"search_data_{CODE}.js", rels)
        self.assertIn("index.html", rels)

        import gen_topics, gen_search
        failures = {
            "gen_topics": mock.patch.object(
                gen_topics, "run", side_effect=RuntimeError("boom-topics")),
            "gen_search": mock.patch.object(
                gen_search, "build_subject", return_value=False),
            "gen_index": mock.patch.object(
                ms.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 1, "", "boom")),
            "post_switch": mock.patch.object(
                ms, "_post_verify_root",
                return_value=["注入: 切换后复核失败"]),
        }
        for name, patcher in failures.items():
            with patcher:
                res = ms.publish(CODE, log=lambda *a: None)
            self.assertFalse(res["ok"], f"{name} 注入应失败: {res.get('summary')}")
            self.assertIn(res.get("stage"), ("build-version", "switch"),
                          "生成/切换阶段失败")
            self.assertTrue(res.get("rollback_verified"),
                            f"{name} 后必须完整回滚")
            rels2, after = formal_hashes()
            self.assertEqual(after, before,
                             f"{name} 失败后正式产物必须与发布前逐字节一致")
            self.assertEqual(rels2, rels)
            # 待发布卷保持待发布(未登记), 可重试
            for pc in ("s23_11", "w23_21", "w23_24"):
                self.assertNotIn(pc, ms.published_papers(CODE))
        # 解除注入后重发成功
        res = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res.get("pending_published"),
                         ["s23_11", "w23_21", "w23_24"])
        cls = json.load(open(os.path.join(self.repo.dir,
                                          f"classified_{CODE}.json")))
        self.assertIn("w23_21", [p["code"] for p in cls["papers"]])


# ============================ P0-2: 关键词命中不得把旧 OUT 升为 IN ============================
class KeywordNoAutoIn(unittest.TestCase):
    def test_content_kw_never_publishes_in(self):
        """题面命中内容关键词(即使 kw≥15)也只能 PENDING, 不得自动 IN。"""
        d = tempfile.mkdtemp(prefix="pp_kw_")
        self.addCleanup(shutil.rmtree, d, True)
        self.addCleanup(ms._apply_root, REPO)
        os.makedirs(os.path.join(d, "data", "units", "9701"))
        units = [{"id": "6c", "q": 6, "part": "c", "page": 3, "marks": 3,
                  "text": "amino acids and condensation polymerisation "
                          "keywords repeated: amino acids, condensation "
                          "polymerisation, amino acids [3]"}]
        rec = {"subject": "9701", "code": "s11_42", "year": 2011,
               "session": "May-Jun", "paper": 4, "variant": 2,
               "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
               "units": units}
        with open(os.path.join(d, "data/units/9701/s11_42.json"), "w") as f:
            json.dump(rec, f)
        topics = {"sections": [
            {"code": "34", "en": "Amino acids", "zh": "", "kw": [],
             "subs": [{"code": "34.4", "en": "Amino acids", "zh": "",
                       "kw": ["amino acids", "condensation polymerisation"]}]},
            {"code": "38", "en": "Practical skills", "zh": "", "kw": [],
             "subs": [{"code": "38.1", "en": "Manipulation", "zh": "",
                       "kw": ["titration", "burette"]}]}],
            "levels": []}
        with open(os.path.join(d, "topics_9701.json"), "w") as f:
            json.dump(topics, f)
        ms._apply_root(d)
        adj = {"s11_42": {"6c": {"status": "OUT", "reason": "旧分类考纲外",
                                 "basis": "old-out"}}}
        old_out = [("s11_42", "6c", units[0]["text"], units[0])]
        review = ms._revalidate_practical_and_context(
            "9701", adj, topics, old_out, {("s11_42", "6c"): 4},
            ms.Judge([], {}), Counter(), log=lambda *a: None)
        self.assertEqual(adj["s11_42"]["6c"]["status"], "PENDING",
                         "内容关键词命中不得自动 IN(P0-2)")
        self.assertEqual(review[0]["new"], "PENDING")
        self.assertGreater(review[0]["kw"], 0)     # 词汇命中被记录但不作为依据


# ============================ P0-3a: 新科目首次发布失败, 新增根产物一并删除 ============================
class FirstPublishRollback(PipelineTestCase):
    """新科目首次发布(正式 topics/units 尚不存在), 在每个生成阶段注入失败:
    受管路径的完整集合与哈希必须恢复到发布前(即新增文件全部被删除)。"""

    def _seed_new_subject(self):
        # 正式库为空: 移除 TestRepo 预置的正式 topics/classified, 模拟新科目
        for r in (f"topics_{CODE}.json", f"classified_{CODE}.json",
                  f"topics_data_{CODE}.js", f"search_data_{CODE}.js"):
            p = os.path.join(self.repo.dir, r)
            if os.path.exists(p):
                os.remove(p)
        with open(os.path.join(self.repo.dir, f"topics_{CODE}.json"), "w") as f:
            json.dump(TOPICS, f, ensure_ascii=False)
        work = os.path.join(pp.PIPE_DIR, "work", CODE, "units")
        os.makedirs(work, exist_ok=True)
        with open(os.path.join(work, "w23_11_qp.json"), "w") as f:
            json.dump({"code": "w23_11", "year": 2023, "session": "Oct-Nov",
                       "paper": 1, "variant": 1, "qp_pdf": "x.pdf",
                       "ms_pdf": None,
                       "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                                  "marks": 4,
                                  "text": "demand question [4]"}]}, f)
        with open(os.path.join(self.repo.dir, "x.pdf"), "wb") as f:
            f.write(b"%PDF-1.4\n%fixture\n")
        os.makedirs(os.path.join(self.repo.dir, f"{CODE}（夹具）", "2023",
                                 "Oct-Nov"), exist_ok=True)
        os.makedirs(os.path.join(self.repo.dir, f"tags_{CODE}"), exist_ok=True)
        with open(os.path.join(self.repo.dir, f"tags_{CODE}",
                               "w23_11.json"), "w") as f:
            json.dump({"code": "w23_11", "tags": {"1": ["1.1"]}}, f)
        ms.adjudicate_new_papers(CODE, log=lambda *a: None)

    def _formal_snapshot(self):
        rels = ms._formal_artifacts(CODE)
        return rels, ms._hash_formal(rels)

    def test_first_publish_failure_cleans_new_root_artifacts(self):
        import unittest.mock as mock
        self._seed_new_subject()
        rels, before = self._formal_snapshot()
        self.assertFalse(any(os.path.exists(os.path.join(self.repo.dir, r))
                             for r in (f"classified_{CODE}.json",
                                       f"topics_data_{CODE}.js",
                                       f"search_data_{CODE}.js",
                                       f"papers_data_{CODE}.js")))
        import gen_topics, gen_search
        failures = {
            "gen_topics": mock.patch.object(
                gen_topics, "run", side_effect=RuntimeError("boom-topics")),
            "gen_search": mock.patch.object(
                gen_search, "build_subject", return_value=False),
            "gen_index": mock.patch.object(
                ms.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 1, "", "boom")),
            "post_verify": mock.patch.object(
                ms, "_post_verify_root",
                return_value=["注入: 复核失败"]),
        }
        for name, patcher in failures.items():
            with patcher:
                res = ms.publish(CODE, log=lambda *a: None)
            self.assertFalse(res["ok"], f"{name}: {res.get('summary')}")
            self.assertIn(res.get("stage"), ("build-version", "switch"))
            self.assertTrue(res.get("rollback_verified"),
                            f"{name} 后回滚必须通过")
            rels2, after = self._formal_snapshot()
            self.assertEqual(after, before,
                             f"{name} 失败后受管路径集合与哈希必须与发布前一致")
            self.assertEqual(rels2, rels)
        # 解除注入后首次发布成功
        res = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(res["ok"], res)
        for r in (f"topics_{CODE}.json", f"classified_{CODE}.json",
                  f"topics_data_{CODE}.js", f"search_data_{CODE}.js",
                  f"papers_data_{CODE}.js"):
            self.assertTrue(os.path.exists(os.path.join(self.repo.dir, r)), r)


# ============================ P0-3b: 发布期间正式路径不暴露混合版本 ============================
class NoMixedVersionDuringPublish(PipelineTestCase):
    """在 gen_search 暂停点并发读取正式 topics/search/classified/units:
    必须全为旧版; 发布完成后全为新版。旧实现(先写数据再逐个生成)必失败。"""

    def test_reads_consistent_during_generator_pause(self):
        import unittest.mock as mock
        seed_published_paper(self.repo)
        res = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(res["ok"], res)
        # 新增一卷(带 AI 草稿 IN) → 再次发布会改变 topics_data/search_data
        up = "/directories/CAIE/CAIE-pastpapers/upload/"
        self.site.files[up + "9999_w23_qp_21.pdf"] = make_qp_pdf(
            [("1", "Explain how the price mechanism allocates scarce "
                   "resources. [6]")])
        state = pp.load_state()
        report = {"targets": [], "failures": []}
        files, _ = pp.papacambridge_discover(
            CODE, slug="test-subject-9999",
            source_url=self.site.url("/papers/caie/test-subject-9999"),
            log=lambda *a: None)
        pp.plan_and_download(CODE, pp.load_registry()[CODE], files,
                            (2023, 2023), True, state, report,
                            log=lambda *a: None)
        pp.cmd_extract(pp.argparse.Namespace(code=CODE, apply=True,
                                             papers=None))
        os.makedirs(os.path.join(self.repo.dir, f"tags_{CODE}"), exist_ok=True)
        with open(os.path.join(self.repo.dir, f"tags_{CODE}",
                               "w23_21.json"), "w") as f:
            json.dump({"code": "w23_21", "tags": {"1": ["1.2"]}}, f)
        ms.adjudicate_new_papers(CODE, log=lambda *a: None)

        def read_all():
            root = self.repo.dir
            out = {}
            for r in (f"topics_{CODE}.json", f"classified_{CODE}.json",
                      f"search_data_{CODE}.js", f"topics_data_{CODE}.js",
                      f"data/units/{CODE}/w23_21.json"):
                p = os.path.join(root, r)
                out[r] = (os.path.exists(p) and
                          hashlib.sha256(open(p, "rb").read()).hexdigest())
            return out

        before = read_all()
        observed = []
        stop = threading.Event()
        import gen_search
        real_build = gen_search.build_subject
        paused = threading.Event()

        def pausing_build(code, *a, **kw):
            paused.set()                       # 生成阶段已到达
            while not stop.is_set():
                time.sleep(0.01)               # 模拟生成中的暂停点
            return real_build(code, *a, **kw)

        with mock.patch.object(gen_search, "build_subject",
                               side_effect=pausing_build):
            th = threading.Thread(
                target=lambda: ms.publish(CODE, log=lambda *a: None),
                daemon=True)
            th.start()
            self.assertTrue(paused.wait(15), "发布未到达生成阶段")
            for _ in range(5):                 # 暂停期间并发读取
                observed.append(read_all())
                time.sleep(0.02)
            stop.set()
            th.join(60)
        after = read_all()
        # 暂停期间: 与发布前完全一致(旧版) —— 不允许 topics 新而 search 旧
        for obs in observed:
            self.assertEqual(obs, before,
                             "生成暂停点读到了混合版本(部分新部分旧)")
        # 发布完成后: 内容确实变化(新版)
        self.assertNotEqual(after, before, "发布后应切换到新版本")
        changed = [r for r in after if after[r] != before.get(r)]
        self.assertIn(f"search_data_{CODE}.js", changed)
        self.assertIn(f"data/units/{CODE}/w23_21.json", changed)


class JudgeSanity(unittest.TestCase):
    def test_kw_and_nn(self):
        anchors = [("the law of demand states inverse relation between "
                    "price and quantity demanded", ["1.1"]),
                   ("opportunity cost is the next best alternative forgone",
                    ["2.1"])]
        j = ms.Judge(anchors, {"1.1": ["law of demand"], "2.1": ["opportunity cost"]})
        codes, conf, det = j.judge("Explain the law of demand and its assumptions")
        self.assertEqual(codes[0], "1.1")
        self.assertGreater(conf, 0.5)


# ============================ 大纲版本(单年版/停发) ============================
class SyllabusResolve(PipelineTestCase):
    """P0-1: 官方页同时含范围版与单年版时选单年最新版; 解析失败停发。"""

    CAIE = "/caie/fixture-9999"

    def setUp(self):
        super().setUp()
        self._orig_caie = self.site.files.get(self.CAIE)

    def tearDown(self):
        # 夹具站点跨用例共享: 还原, 不污染后续 sync 流程测试
        if self._orig_caie is None:
            self.site.files.pop(self.CAIE, None)
        else:
            self.site.files[self.CAIE] = self._orig_caie
        super().tearDown()

    def _caie_page(self, links):
        # 官方页使用相对链接 /Images/...(resolve_latest_syllabus 只解析不下载)
        html = "\n".join(f'<a href="{u}">Syllabus PDF</a>' for u in links)
        self.site.files[self.CAIE] = html.encode()

    def _good_caie(self):
        self._caie_page(["/Images/111-2024-2026-syllabus.pdf"])

    def test_single_year_beats_range(self):
        self._caie_page([
            "/Images/697423-2026-2028-syllabus.pdf",
            "/Images/748950-2026-2028-syllabus-update.pdf",
            "/Images/764392-2029-syllabus.pdf",
        ])
        t = pp.resolve_latest_syllabus(self.site.url("/caie/fixture-9999"),
                                       log=lambda *a: None)
        self.assertIsNotNone(t)
        self.assertEqual(t["version"], "2029")           # 单年版被识别且最新
        self.assertEqual(t["url"],
                         "https://www.cambridgeinternational.org/Images/764392-2029-syllabus.pdf")
        # update 属于旧版 → 不跟随
        self.assertIsNone(t["update"])

    def test_range_only(self):
        self._caie_page([
            "/Images/697423-2026-2028-syllabus.pdf",
            "/Images/748950-2026-2028-syllabus-update.pdf",
        ])
        t = pp.resolve_latest_syllabus(self.site.url("/caie/fixture-9999"),
                                       log=lambda *a: None)
        self.assertEqual(t["version"], "2026-2028")
        self.assertIsNotNone(t["update"])               # 同版 update 跟随

    def test_unresolvable_returns_none(self):
        self.site.files["/caie/fixture-9999"] = b"<html>no syllabus links here</html>"
        t = pp.resolve_latest_syllabus(self.site.url("/caie/fixture-9999"),
                                       log=lambda *a: None)
        self.assertIsNone(t)

    def test_sync_stops_publish_when_unresolvable(self):
        """官方页不可确认 → sync apply 停止发布, 报告 failure, 退出非零。"""
        self._caie_page([])                      # 无 syllabus 链接 → 不可确认
        rc = pp.cmd_sync(pp.argparse.Namespace(
            code=CODE, apply=True, latest=True, years=None,
            source_url=self.site.url("/papers/caie/test-subject-9999"),
            source_list=None))
        self.assertEqual(rc, 2)
        runs = sorted(f for f in os.listdir(pp.RUNS_DIR)
                      if f.endswith("_sync_9999.json"))
        rep = json.load(open(os.path.join(pp.RUNS_DIR, runs[-1])))
        self.assertTrue(any(f["name"] == "syllabus" and
                            f["status"] == "unresolvable"
                            for f in rep["failures"]))
        self.assertNotIn("publish", rep)                 # 未进入发布


# ============================ extract --dry-run 零写入 ============================
class ExtractDryRunZeroWrite(PipelineTestCase):
    def test_dry_run_no_write(self):
        up = "/directories/CAIE/CAIE-pastpapers/upload/"
        self.site.files[up + "9999_s24_qp_11.pdf"] = make_qp_pdf(
            [("1", "State the law of demand. [4]")])
        state = pp.load_state()
        report = {"targets": [], "failures": []}
        files, _ = pp.papacambridge_discover(
            CODE, slug="test-subject-9999",
            source_url=self.site.url("/papers/caie/test-subject-9999"),
            log=lambda *a: None)
        pp.plan_and_download(CODE, pp.load_registry()[CODE], files,
                            (2024, 2024), True, state, report, log=lambda *a: None)
        before = self.repo.tree_hash()
        r = pp.cmd_extract(pp.argparse.Namespace(code=CODE, apply=False,
                                                 papers=None))
        self.assertEqual(self.repo.tree_hash(), before, "dry-run 不得写任何文件")
        self.assertTrue(r.get("dry_run"))
        self.assertEqual(set(r["would_extract"]), {"s24_11", "s24_12"})
        self.assertFalse(os.path.exists(
            os.path.join(pp.PIPE_DIR, "work", CODE, "units")))


# ============================ 空发现 ≠ 成功 ============================
class EmptyDiscovery(PipelineTestCase):
    def test_empty_source_fails(self):
        page = "/papers/caie/test-subject-9999"
        original = self.site.files[page]      # 夹具站点跨用例共享: 改完必须还原
        # 页面可抓取(含站名)但没有任何会话/文件链接 → 来源为空 ≠ 没有新卷
        self.site.files[page] = b"<html>pastpapers 9999 directory is empty</html>"
        try:
            rc = pp.cmd_sync(pp.argparse.Namespace(
                code=CODE, apply=True, latest=True, years=None,
                source_url=self.site.url(page),
                source_list=None))
        finally:
            self.site.files[page] = original
        self.assertEqual(rc, 2)
        runs = sorted(f for f in os.listdir(pp.RUNS_DIR)
                      if f.endswith("_sync_9999.json"))
        rep = json.load(open(os.path.join(pp.RUNS_DIR, runs[-1])))
        self.assertTrue(any(f["status"] == "empty-source"
                            for f in rep["failures"]))


# ============================ 发布失败→重试(SYNC-01) ============================
class SyncPublishRetry(PipelineTestCase):
    def _do_sync(self):
        # 用受校验直链清单只取新卷 w23_21(避开夹具会话页里故意的坏文件)
        up = "/directories/CAIE/CAIE-pastpapers/upload/"
        lst = os.path.join(self.repo.dir, "direct_list.txt")
        with open(lst, "w") as f:
            f.write(self.site.url(up + "9999_w23_qp_21.pdf") + "\n")
            f.write(self.site.url(up + "9999_w23_ms_21.pdf") + "\n")
        return pp.cmd_sync(pp.argparse.Namespace(
            code=CODE, apply=True, latest=True, years=None,
            source_url=None, source_list=lst))

    def test_publish_failure_then_retry_succeeds(self):
        seed_published_paper(self.repo)          # 已发布锚点
        up = "/directories/CAIE/CAIE-pastpapers/upload/"
        self.site.files[up + "9999_w23_qp_21.pdf"] = make_qp_pdf(
            [("1", "Explain how the price mechanism allocates scarce "
                   "resources. [6]")], variant_note="Paper 1 new")
        self.site.files[up + "9999_w23_ms_21.pdf"] = make_ms_pdf(
            [("1", "price mechanism answer")])
        # 官方大纲可确认(与 topics 版本一致 → 不触发迁移, 允许发布)
        self.site.files["/caie/fixture-9999"] = (
            '<a href="/Images/111-2024-2026-syllabus.pdf">Syllabus</a>').encode()

        # ---- 注入发布失败: 第一次 sync 全链走到 publish 时失败 ----
        cls_path = os.path.join(self.repo.dir, f"classified_{CODE}.json")
        cls_before = open(cls_path, "rb").read()
        import unittest.mock as mock
        fake_pub = {"ok": False, "summary": "注入发布失败(模拟)", "stage": "swap"}
        with mock.patch.object(ms, "publish", return_value=fake_pub):
            rc = self._do_sync()
        self.assertEqual(rc, 2, "发布失败必须非零退出")
        runs = sorted(f for f in os.listdir(pp.RUNS_DIR)
                      if f.endswith("_sync_9999.json"))
        rep = json.load(open(os.path.join(pp.RUNS_DIR, runs[-1])))
        self.assertTrue(any(f["name"] == "publish" for f in rep["failures"]),
                        rep["failures"])
        self.assertEqual(rep.get("stage"), "publish")
        self.assertIn("extraction", rep)              # 报告含完整阶段链
        self.assertIn("adjudication", rep)
        self.assertEqual(open(cls_path, "rb").read(), cls_before,
                         "发布失败不得改动旧正式数据")
        cls = json.loads(cls_before)
        self.assertNotIn("w23_21", [p["code"] for p in cls["papers"]])
        # w23_21 未登记为已发布 → 仍是待发布卷
        self.assertNotIn("w23_21", ms.published_papers(CODE))

        # ---- 重试: 本次切题数为 0, 待发布卷仍被处理并成功发布 ----
        with mock.patch.object(pp, "cmd_extract",
                               wraps=pp.cmd_extract) as spy:
            rc2 = self._do_sync()
            ext_args = spy.call_args_list[-1][0][0]
            self.assertTrue(ext_args.apply)
        self.assertEqual(rc2, 0)
        cls = json.load(open(cls_path))
        self.assertIn("w23_21", [p["code"] for p in cls["papers"]])
        self.assertIn("w23_21", ms.published_papers(CODE))
        # 幂等: 再跑一次零待发布, 仍成功
        rc3 = self._do_sync()
        self.assertEqual(rc3, 0)


class PublishFailureRollback(PipelineTestCase):
    """真实发布失败路径: 临时区复核失败 → 不触碰正式数据。"""

    def test_stage_verify_failure_keeps_old_data(self):
        seed_published_paper(self.repo)
        up = "/directories/CAIE/CAIE-pastpapers/upload/"
        self.site.files[up + "9999_w23_qp_21.pdf"] = make_qp_pdf(
            [("1", "Explain how the price mechanism allocates scarce "
                   "resources. [6]")])
        self.site.files[up + "9999_w23_ms_21.pdf"] = make_ms_pdf([("1", "ans")])
        state = pp.load_state()
        report = {"targets": [], "failures": []}
        files, _ = pp.papacambridge_discover(
            CODE, slug="test-subject-9999",
            source_url=self.site.url("/papers/caie/test-subject-9999"),
            log=lambda *a: None)
        pp.plan_and_download(CODE, pp.load_registry()[CODE], files,
                            (2023, 2023), True, state, report, log=lambda *a: None)
        pp.cmd_extract(pp.argparse.Namespace(code=CODE, apply=True, papers=None))
        ms.adjudicate_new_papers(CODE, log=lambda *a: None)
        # 注入: 移走新卷 QP → 临时区复核(qp_pdf 存在性)失败
        qp_new = os.path.join(self.repo.dir, "9999（夹具）", "2023", "Oct-Nov",
                              "9999_w23_qp_21.pdf")
        os.rename(qp_new, qp_new + ".bak")
        cls_path = os.path.join(self.repo.dir, f"classified_{CODE}.json")
        cls_before = open(cls_path, "rb").read()
        res = ms.publish(CODE, log=lambda *a: None)
        os.rename(qp_new + ".bak", qp_new)      # 还原, 不影响断言
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("stage"), "stage-verify")
        self.assertEqual(open(cls_path, "rb").read(), cls_before,
                         "发布失败不得改动旧正式数据")
        self.assertNotIn("w23_21", ms.published_papers(CODE))
        # 修复后真实重发成功
        res2 = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(res2["ok"], res2)
        cls = json.load(open(cls_path))
        self.assertIn("w23_21", [p["code"] for p in cls["papers"]])


# ============================ CONTEXT / 作废题 可见性 ============================
class ContextAndDiscounted(unittest.TestCase):
    """P0-2: 共享材料(CONTEXT)不阻断大题; 官方作废题不计分不进候选。"""

    def test_context_stem_does_not_block(self):
        import mock_exam as me
        d = tempfile.mkdtemp(prefix="pp_ctx_")
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "data", "units", CODE))
        os.makedirs(os.path.join(d, "data", "loc", CODE))
        os.makedirs(os.path.join(d, "data", "msloc", CODE))
        with open(os.path.join(d, f"topics_{CODE}.json"), "w") as f:
            json.dump(TOPICS, f, ensure_ascii=False)
        units = [
            {"id": "1", "q": 1, "part": None, "page": 2, "marks": None,
             "text": "Shared data-response source material about the economy",
             "topics": None, "status": "CONTEXT"},
            {"id": "1a", "q": 1, "part": "a", "page": 2, "marks": 4,
             "text": "Explain the law of demand sub part [4]",
             "topics": ["1.1"], "status": "IN"},
        ]
        with open(os.path.join(d, "data", "units", CODE, "s24_11.json"), "w") as f:
            json.dump({"subject": CODE, "code": "s24_11", "year": 2024,
                       "session": "May-Jun", "paper": 2, "variant": 1,
                       "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
                       "units": units}, f)
        with open(os.path.join(d, "data", "loc", CODE, "s24_11.json"), "w") as f:
            json.dump({"units": {}, "questions": {
                "1": {"status": "exact", "seg": [[2, 40, 70, 550, 700]]}}}, f)
        with open(os.path.join(d, "data", "msloc", CODE, "s24_11.json"), "w") as f:
            json.dump({"ms_status": "missing", "letters": {}, "units": {},
                       "questions": {}}, f)
        old = (me.UNITS_DIR, me.LOC_DIR, me.MSLOC_DIR, me.ROOT)
        me.ROOT = d
        me.UNITS_DIR = os.path.join(d, "data", "units")
        me.LOC_DIR = os.path.join(d, "data", "loc")
        me.MSLOC_DIR = os.path.join(d, "data", "msloc")
        try:
            qs, why = me.rescore_paper({"units": units, "paper": 2})
            self.assertIsNone(why)
            self.assertEqual(qs[0]["out_or_pending"], 0)
            self.assertEqual(qs[0]["unclassified"], 0)
            self.assertEqual(qs[0]["labels"], ["1.1"])
        finally:
            me.ROOT, me.UNITS_DIR, me.LOC_DIR, me.MSLOC_DIR = old

    def test_discounted_mcq_excluded(self):
        import mock_exam as me
        units = [
            {"id": "1", "q": 1, "part": None, "page": 2, "marks": 1,
             "text": "normal mcq one [1]", "topics": ["1.1"], "status": "IN"},
            {"id": "2", "q": 2, "part": None, "page": 2, "marks": None,
             "text": "discounted mcq question stem",
             "topics": None, "status": "PENDING", "discounted": True},
        ]
        qs, why = me.rescore_paper({"units": units, "paper": 1})
        self.assertIsNone(why)
        self.assertFalse(qs[0]["discounted"])
        self.assertTrue(qs[1]["discounted"])          # 官方作废 → 标记
        self.assertEqual(qs[1]["out_or_pending"], 0)  # 不按 OUT/PENDING 排除计数


# ============================ 实操技能复核(P0-3) ============================
class PracticalReview(unittest.TestCase):
    """9701/9702 旧 OUT 单元复核: 强实操信号→IN, 无证据→PENDING, 无分值→CONTEXT。"""

    def _run(self, code, paper, units, topics):
        d = tempfile.mkdtemp(prefix="pp_prac_")
        self.addCleanup(shutil.rmtree, d, True)
        self.addCleanup(ms._apply_root, REPO)
        os.makedirs(os.path.join(d, "data", "units", code))
        rec = {"subject": code, "code": "s24_53", "year": 2024,
               "session": "May-Jun", "paper": paper, "variant": 1,
               "qp_pdf": "x.pdf", "ms_pdf": None, "ms_pages": {},
               "units": units}
        with open(os.path.join(d, "data", "units", code, "s24_53.json"), "w") as f:
            json.dump(rec, f)
        with open(os.path.join(d, f"topics_{code}.json"), "w") as f:
            json.dump(topics, f, ensure_ascii=False)
        ms._apply_root(d)
        adj = {"s24_53": {u["id"]: {"status": "OUT",
                                    "reason": "旧分类阶段判定考纲外",
                                    "basis": "old-out"} for u in units}}
        old_out = [(rec["code"], u["id"], u["text"], u) for u in units]
        paper_of = {(rec["code"], u["id"]): paper for u in units}
        stats = Counter()
        judge = ms.Judge([], {"38.1": ["titration"], "23.4": ["d-block"]})
        review = ms._revalidate_practical_and_context(
            code, adj, topics, old_out, paper_of, judge, stats, log=lambda *a: None)
        return adj, review

    def test_9701_practical_paths(self):
        topics = json.load(open(os.path.join(REPO, "data", "pipeline",
                                             "migration",
                                             "9701_topics_new.json")))
        if not topics["sections"] or topics["sections"][-1]["code"] != "38":
            self.skipTest("需要 9701 迁移资产")
        units = [
            {"id": "1b", "q": 1, "part": "b", "page": 2, "marks": 1,
             "text": "Calculate the percentage error in the volume measured "
                     "with the burette during the titration. [1]"},
            {"id": "1c", "q": 1, "part": "c", "page": 2, "marks": 2,
             "text": "Describe a method to follow when carrying out the "
                     "titration with potassium manganate. [2]"},
            {"id": "1", "q": 1, "part": None, "page": 2, "marks": None,
             "text": "FA1 is a solution of sodium thiosulfate. Qualitative "
                     "notes: at each stage record the colour of the precipitate."},
            {"id": "9z", "q": 9, "part": "z", "page": 3, "marks": 2,
             "text": "Discuss the geopolitical significance of rare earth "
                     "mineral deposits in unrelated context. [2]"},
        ]
        adj, review = self._run("9701", 3, units, topics)
        self.assertEqual(adj["s24_53"]["1b"]["status"], "IN")
        self.assertTrue(any(t.startswith("38.") for t in adj["s24_53"]["1b"]["tags"]))
        self.assertTrue(any(t.startswith("38.") for t in adj["s24_53"]["1c"]["tags"]))
        self.assertEqual(adj["s24_53"]["1"]["status"], "CONTEXT")
        self.assertEqual(adj["s24_53"]["9z"]["status"], "PENDING")  # 无证据不得 OUT
        self.assertEqual(len(review), 4)

    def test_low_similarity_never_auto_out(self):
        """sync 新卷自动判定: 近零相似 → PENDING(不得自动 OUT)。"""
        d = tempfile.mkdtemp(prefix="pp_low_")
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "data", "units", CODE))
        with open(os.path.join(d, f"topics_{CODE}.json"), "w") as f:
            json.dump(TOPICS, f, ensure_ascii=False)
        pp._apply_root(d)
        ms._apply_root(d)
        work = os.path.join(pp.PIPE_DIR, "work", CODE, "units")
        os.makedirs(work)
        with open(os.path.join(work, "w23_21_qp.json"), "w") as f:
            json.dump({"code": "w23_21", "year": 2023, "session": "Oct-Nov",
                       "paper": 1, "variant": 1, "qp_pdf": "x.pdf",
                       "ms_pdf": None,
                       "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                                  "marks": 1,
                                  "text": "utterly unrelated gibberish zzz"}]}, f)
        with open(os.path.join(d, f"tags_{CODE}"), "w") as f:
            f.write("ignored")
        res = ms.adjudicate_new_papers(CODE, log=lambda *a: None)
        adj = json.load(open(os.path.join(pp.PIPE_DIR, "adjudication",
                                          f"{CODE}.json")))
        self.assertEqual(adj["units"]["w23_21"]["1"]["status"], "PENDING")
        self.assertIn("auto-low", adj["units"]["w23_21"]["1"]["basis"])
        try:
            pass
        finally:
            pp._apply_root(REPO)
            ms._apply_root(REPO)


# ============================ 第五轮: P0-1 五科首页配置 ============================
class FiveSubjectConfigAfterPublish(PipelineTestCase):
    """P0-1(第四轮回归): 发布构建根只含当前科目, gen_index 只扫目录会把
    window.WB_CONFIG 缩成单科。修复后 gen_index 以 subjects.json 登记表为
    全站科目权威来源 —— 只发布一科, 首页配置仍含全部五科, 未发布科目题数
    与 syllabus 配置不变。旧实现(gen_index 只扫构建根目录)在本测试必失败。"""

    CODES = ["9101", "9202", "9303", "9404", "9505"]

    def _build_five_subject_repo(self):
        """在 TestRepo 基础上扩展为五科登记(各科独立数据目录/种子卷)。"""
        d = self.repo.dir
        os.makedirs(os.path.join(d, "x"), exist_ok=True)
        os.makedirs(os.path.join(d, "syllabus"), exist_ok=True)
        reg = json.load(open(os.path.join(d, "subjects.json")))
        for c in self.CODES:
            zh = f"科目{c}"
            dd = f"{c}（{zh}）"
            for sub in (os.path.join(d, dd, "2024", "May-Jun"),
                        os.path.join(d, "data", "units", c),
                        os.path.join(d, "data", "loc", c),
                        os.path.join(d, "data", "msloc", c),
                        os.path.join(d, f"tags_{c}")):
                os.makedirs(sub, exist_ok=True)
            with open(os.path.join(d, "syllabus", f"{c}_2024-2026_syllabus.pdf"),
                      "wb") as f:
                f.write(b"%PDF-syllabus-fixture")
            reg["subjects"][c] = {
                "code": c, "name": f"Subject {c}", "zh": zh,
                "level": "AS & A Level", "data_dir": dd,
                "source": {"kind": "papacambridge", "slug": None},
                "caie_page": "", "paper_names": {}}
            tax = json.loads(json.dumps(TOPICS))
            tax["subject"] = c
            json.dump(tax, open(os.path.join(d, f"topics_{c}.json"), "w"),
                      ensure_ascii=False)
            json.dump({"subject": c, "generated": "2026-01-01", "papers": []},
                      open(os.path.join(d, f"classified_{c}.json"), "w"))
            # 各科种子正式卷(带 status IN → 可发布)
            with open(os.path.join(d, "x", c + ".pdf"), "wb") as f:
                f.write(b"%PDF-1.4\n%fixture-" + c.encode() + b"\n")
            json.dump({"subject": c, "code": "s24_11", "year": 2024,
                       "session": "May-Jun", "paper": 1, "variant": 1,
                       "qp_pdf": f"x/{c}.pdf", "ms_pdf": None, "ms_pages": {},
                       "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                                  "marks": 4, "text": f"demand text {c} [4]",
                                  "topics": ["1.1"], "status": "IN"}]},
                      open(os.path.join(d, "data", "units", c, "s24_11.json"),
                           "w"), ensure_ascii=False)
            json.dump({"units": {"1": {"status": "exact",
                                       "seg": [[2, 40, 70, 550, 700]]}},
                       "questions": {}},
                      open(os.path.join(d, "data", "loc", c, "s24_11.json"), "w"))
            json.dump({"ms_status": "missing", "letters": {}, "units": {},
                       "questions": {}},
                      open(os.path.join(d, "data", "msloc", c, "s24_11.json"),
                           "w"))
            json.dump({"code": "s24_11", "tags": {}},
                      open(os.path.join(d, f"tags_{c}", "s24_11.json"), "w"))
        json.dump(reg, open(os.path.join(d, "subjects.json"), "w"),
                  ensure_ascii=False)

    @staticmethod
    def _wb_config(repo_dir):
        cfg = open(os.path.join(repo_dir, "index.html"), encoding="utf-8").read()
        m = re.search(r"window\.WB_CONFIG = (\{.*?\});", cfg)
        return json.loads(m.group(1))

    def test_publish_one_subject_keeps_five_subject_config(self):
        self._build_five_subject_repo()
        # 每科先发布一次(种子转正式 + search_data 计数)
        for c in self.CODES:
            res = ms.publish(c, log=lambda *a: None)
            self.assertTrue(res["ok"], (c, res))
        expected_subjects = sorted(self.CODES + [CODE])   # TestRepo 已登记 9999
        wb0 = self._wb_config(self.repo.dir)
        self.assertEqual([s["code"] for s in wb0["subjects"]], expected_subjects)
        counts0 = dict(wb0["counts"])
        self.assertEqual(sorted(counts0), self.CODES)   # 9999 未发布无 search_data
        self.assertTrue(all(v == 1 for v in counts0.values()), counts0)
        self.assertEqual([x["code"] for x in wb0["syllabus"]], self.CODES,
                         "syllabus 配置不得被清空")
        # 只给 9101 增加一卷新题并单独发布
        target = "9101"
        work = os.path.join(pp.PIPE_DIR, "work", target, "units")
        os.makedirs(work, exist_ok=True)
        json.dump({"code": "w25_21", "year": 2025, "session": "Oct-Nov",
                   "paper": 2, "variant": 1, "qp_pdf": f"x/{target}.pdf",
                   "ms_pdf": None,
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 3,
                              "text": f"price mechanism new paper {target} [3]"}]},
                  open(os.path.join(work, "w25_21_qp.json"), "w"))
        json.dump({"code": "w25_21", "tags": {"1": ["1.2"]}},
                  open(os.path.join(self.repo.dir, f"tags_{target}",
                                    "w25_21.json"), "w"))
        ms.adjudicate_new_papers(target, log=lambda *a: None)
        res = ms.publish(target, log=lambda *a: None)
        self.assertTrue(res["ok"], res)
        wb1 = self._wb_config(self.repo.dir)
        self.assertEqual([s["code"] for s in wb1["subjects"]], expected_subjects,
                         "发布单科后全站科目仍是五科(登记全科)")
        for c in self.CODES:
            if c == target:
                self.assertEqual(wb1["counts"][c], counts0[c] + 1)
            else:
                self.assertEqual(wb1["counts"][c], counts0[c],
                                 f"未发布科目 {c} 题数不得变化")
        self.assertEqual([x["code"] for x in wb1["syllabus"]], self.CODES)


# ============================ 第五轮: P0-2 单一活动版本切换 ============================
class SingleFlipNoMixedVersion(PipelineTestCase):
    """P0-3b(第四轮阻断): 在切换序列的每个检查点(候选构建完成/入列/翻转前/
    翻转后/复核后)暂停并并发读取首页配置、搜题、知识点、题目详情、组卷数据
    —— 每个暂停点必须整体全旧或整体全新。旧实现(逐文件 os.replace 循环,
    无这些检查点)在本测试的等待处直接超时失败; 其等价"第一个文件替换后"
    暂停的读法由 CrashDuringSwitchRecovers 的翻转前/后断言覆盖。"""

    SURFACES = ("index.html", "search_data_{c}.js", "topics_data_{c}.js",
                "classified_{c}.json", "topics_{c}.json",
                "data/units/{c}/w23_21.json", "papers_data_{c}.js")
    ORDER = ["switch:build-done", "switch:staging", "switch:staged",
             "switch:pre-flip", "switch:flipped", "switch:post-verify",
             "switch:done"]
    PRE_FLIP = {"switch:build-done", "switch:staging", "switch:staged",
                "switch:pre-flip"}

    def _read_surfaces(self):
        out = {}
        for rel in self.SURFACES:
            r = rel.format(c=CODE)
            p = os.path.join(self.repo.dir, r)
            out[r] = (os.path.exists(p) and
                      hashlib.sha256(open(p, "rb").read()).hexdigest())
        return out

    def _prepare_new_paper(self):
        seed_published_paper(self.repo)
        res = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(res["ok"], res)
        work = os.path.join(pp.PIPE_DIR, "work", CODE, "units")
        os.makedirs(work, exist_ok=True)
        json.dump({"code": "w23_21", "year": 2023, "session": "Oct-Nov",
                   "paper": 2, "variant": 1,
                   "qp_pdf": "9999（夹具）/2024/May-Jun/9999_s24_qp_11.pdf",
                   "ms_pdf": None,
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 6,
                              "text": "price mechanism allocates scarce "
                                      "resources [6]"}]},
                  open(os.path.join(work, "w23_21_qp.json"), "w"))
        os.makedirs(os.path.join(self.repo.dir, f"tags_{CODE}"), exist_ok=True)
        json.dump({"code": "w23_21", "tags": {"1": ["1.2"]}},
                  open(os.path.join(self.repo.dir, f"tags_{CODE}",
                                    "w23_21.json"), "w"))
        ms.adjudicate_new_papers(CODE, log=lambda *a: None)

    def test_every_checkpoint_reads_consistent_version(self):
        self._prepare_new_paper()
        old = self._read_surfaces()
        gates = {cp: (threading.Event(), threading.Event()) for cp in self.ORDER}
        observed = {}

        def pause_fn(name):
            if name in gates:
                reached, release = gates[name]
                reached.set()
                release.wait(20)

        lv.set_pause(pause_fn)
        box = {}
        try:
            t = threading.Thread(
                target=lambda: box.update(res=ms.publish(CODE,
                                                         log=lambda *a: None)))
            t.start()
            for cp in self.ORDER:            # 逐检查点: 等阻塞 → 并发读 → 放行
                reached, release = gates[cp]
                self.assertTrue(reached.wait(30),
                                f"发布未到达检查点 {cp}(旧实现无此检查点)")
                rt = threading.Thread(target=lambda: observed.update(
                    {cp: self._read_surfaces()}))
                rt.start()
                rt.join(15)
                release.set()
            t.join(60)
            self.assertFalse(t.is_alive())
        finally:
            lv.clear_pause()
        self.assertTrue(box.get("res", {}).get("ok"), box.get("res"))
        new = self._read_surfaces()
        self.assertNotEqual(new, old, "发布完成后应读到新版")
        for cp in self.ORDER:
            if cp in self.PRE_FLIP:
                self.assertEqual(observed[cp], old,
                                 f"{cp}(翻转前): 并发读取必须整体=旧版")
            else:
                self.assertEqual(observed[cp], new,
                                 f"{cp}(翻转后): 并发读取必须整体=新版")
        # 发布成功且待发布清零(无钩子+跳过通知 → 直接晋升)
        self.assertEqual(ms.pending_papers(CODE, log=lambda *a: None), [])
        self.assertIn("w23_21", ms.published_papers(CODE))


# ============================ 第五轮: P0-2 切换中崩溃恢复 ============================
class CrashDuringSwitchRecovers(PipelineTestCase):
    """模拟发布切换序列中进程被杀(ZJ_CRASH_AT 硬退出, 不走任何 finally):
    崩溃后正式面只能是完整旧版(翻转前)或完整新版(翻转后); ensure_layout
    自愈; 待发布未清零; 重发成功。旧实现在逐文件 os.replace 循环中崩溃会
    留下混合版本(如新版 classified + 旧版 search_data), 本测试的完整性
    断言必失败。"""

    RUNNER = (
        "import json, os, sys\n"
        "sys.path.insert(0, {repo!r})\n"
        "os.environ['ZJ_PUBLISH_NOTIFY_SERVER'] = '0'\n"
        "import paper_pipeline as pp, migrate_syllabus as ms\n"
        "pp._apply_root({repo!r}); ms._apply_root({repo!r})\n"
        "res = ms.publish({code!r}, log=lambda *a: None)\n"
        "print('PUBLISH_RESULT', json.dumps({{'ok': res.get('ok'),"
        " 'stage': res.get('stage')}}))\n")
    OLD = (False, False, 2, False)     # (新卷 units / classified / 搜题计数 / papers_data)
    NEW = (True, True, 3, True)

    def _prepare_new_paper(self):
        seed_published_paper(self.repo)
        res = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(res["ok"], res)
        work = os.path.join(pp.PIPE_DIR, "work", CODE, "units")
        os.makedirs(work, exist_ok=True)
        json.dump({"code": "w23_21", "year": 2023, "session": "Oct-Nov",
                   "paper": 2, "variant": 1,
                   "qp_pdf": "9999（夹具）/2024/May-Jun/9999_s24_qp_11.pdf",
                   "ms_pdf": None,
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 6,
                              "text": "price mechanism crash test [6]"}]},
                  open(os.path.join(work, "w23_21_qp.json"), "w"))
        json.dump({"code": "w23_21", "tags": {"1": ["1.2"]}},
                  open(os.path.join(self.repo.dir, f"tags_{CODE}",
                                    "w23_21.json"), "w"))
        ms.adjudicate_new_papers(CODE, log=lambda *a: None)

    def _surface_state(self):
        """四个正式面的一致性状态(全旧/全新判定; 任何混合都既非 OLD 也非 NEW)。"""
        d = self.repo.dir
        has_units = os.path.exists(
            os.path.join(d, "data", "units", CODE, "w23_21.json"))
        cls_p = os.path.join(d, f"classified_{CODE}.json")
        cls_has = any(p["code"] == "w23_21"
                      for p in json.load(open(cls_p))["papers"]) \
            if os.path.exists(cls_p) else False
        m = re.search(r'"units":(\d+)',
                      open(os.path.join(d, f"search_data_{CODE}.js"),
                           encoding="utf-8").read(300))
        sd_n = int(m.group(1)) if m else -1
        td_p = os.path.join(d, f"topics_data_{CODE}.js")
        td_has = ("\"w23_21\"" in open(td_p, encoding="utf-8").read()
                  if os.path.exists(td_p) else False)
        return (has_units, cls_has, sd_n, td_has)

    def test_crash_at_each_checkpoint_then_restart_recovers(self):
        self._prepare_new_paper()
        self.assertEqual(self._surface_state(), self.OLD)
        for cp, expect in [("switch:build-done", "old"), ("switch:staging", "old"),
                           ("switch:staged", "old"), ("switch:pre-flip", "old"),
                           ("switch:flipped", "new"),
                           ("switch:post-verify", "new")]:
            with self.subTest(crash_at=cp):
                env = dict(os.environ, ZJ_CRASH_AT=cp,
                           ZJ_PUBLISH_NOTIFY_SERVER="0")
                r = subprocess.run(
                    [sys.executable, "-c",
                     self.RUNNER.format(repo=self.repo.dir, code=CODE)],
                    capture_output=True, text=True, env=env, timeout=180)
                self.assertEqual(r.returncode, 70,
                                 f"{cp}: 应在检查点硬崩溃(exit 70): "
                                 f"{r.stdout[-300:]} {r.stderr[-300:]}")
                state = self._surface_state()
                self.assertIn(state, (self.OLD, self.NEW),
                              f"{cp}: 崩溃后不得留下混合版本: {state}")
                if expect == "old":
                    self.assertEqual(state, self.OLD, f"{cp} 在翻转前, 应全旧")
                else:
                    self.assertEqual(state, self.NEW, f"{cp} 在翻转后, 应全新")
                # 崩溃在任何后续记账之前 → 待发布未清零
                self.assertIn("w23_21",
                              ms.pending_papers(CODE, log=lambda *a: None),
                              f"{cp}: 崩溃后待发布不得清零")
                # "重启"自愈: ensure_layout 幂等收尾, 正式面保持完整同版本
                lv.ensure_layout(log=lambda *a: None)
                self.assertEqual(self._surface_state(),
                                 self.OLD if expect == "old" else self.NEW)
        # 全部崩溃点验证后: 正常重发成功, 得到完整新版且不重复
        res = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self._surface_state(), self.NEW)
        self.assertIn("w23_21", ms.published_papers(CODE))
        self.assertEqual(ms.pending_papers(CODE, log=lambda *a: None), [])


# ============================ 第五轮: P0-4 刷新失败不清待发布 ============================
class RefreshFailureKeepsPending(PipelineTestCase):
    """第四轮 P0-4 差异: 刷新钩子失败时旧实现已把卷登记 published、待发布
    清零, 普通 sync 重试不会再处理。修复后为两阶段 pending_refresh:
    待发布保留、总览/登记可见、重启(disk)后保留、网页 refresh_index 或再次
    publish 都能安全恢复并晋升。旧实现在本测试的 pending 断言处必失败。"""

    def _seed_new_paper(self):
        seed_published_paper(self.repo)
        res = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(res["ok"], res)
        work = os.path.join(pp.PIPE_DIR, "work", CODE, "units")
        os.makedirs(work, exist_ok=True)
        json.dump({"code": "w23_21", "year": 2023, "session": "Oct-Nov",
                   "paper": 2, "variant": 1,
                   "qp_pdf": "9999（夹具）/2024/May-Jun/9999_s24_qp_11.pdf",
                   "ms_pdf": None,
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 6,
                              "text": "price mechanism refresh failure [6]"}]},
                  open(os.path.join(work, "w23_21_qp.json"), "w"))
        json.dump({"code": "w23_21", "tags": {"1": ["1.2"]}},
                  open(os.path.join(self.repo.dir, f"tags_{CODE}",
                                    "w23_21.json"), "w"))
        ms.adjudicate_new_papers(CODE, log=lambda *a: None)
        self.assertEqual(ms.pending_papers(CODE, log=lambda *a: None),
                         ["w23_21"])

    def test_refresh_failure_then_recover_keeps_pending(self):
        import pipeline_service as ps
        self._seed_new_paper()

        def boom(subject=None):
            raise RuntimeError("注入: 索引构建失败")
        ms.register_refresh_hook(boom)
        res = ms.publish(CODE, log=lambda *a: None)
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("stage"), "refresh-failed")
        # 核心: 待发布不清零 + 两阶段状态可见
        self.assertEqual(ms.pending_papers(CODE, log=lambda *a: None),
                         ["w23_21"], "刷新失败不得清待发布")
        self.assertEqual(ms.refresh_pending_papers(CODE),
                         {"s24_11", "w23_21"},
                         "本次发布提交的卷(含重发布的旧卷)全部进入两阶段状态")
        self.assertNotIn("w23_21", ms.published_papers(CODE))
        self.assertIn("w23_21", json.load(
            open(os.path.join(self.repo.dir, "data", "pipeline",
                              "published.json")))["subjects"][CODE]
            ["pending_refresh"], "pending_refresh 持久化(重启后仍在)")
        # 总览(管理页数据源)显示待刷新
        pp._apply_root(self.repo.dir)
        ms._apply_root(self.repo.dir)
        ps.apply_root(self.repo.dir)
        ov = {s["code"]: s for s in ps.overview()}[CODE]
        self.assertEqual(ov["pending_refresh"], ["s24_11", "w23_21"])
        # 网页路径恢复: refresh_index 动作(任务页「刷新服务索引」按钮)
        tm = ps.TaskManager()
        out = tm._act_refresh_index({}, {"id": "t"})
        self.assertFalse(out["ok"], "坏钩子仍在时刷新动作必须失败")
        self.assertIn("w23_21", ms.pending_papers(CODE, log=lambda *a: None))
        ms._REFRESH_HOOKS.remove(boom)
        out = tm._act_refresh_index({}, {"id": "t"})
        self.assertTrue(out["ok"], out)
        self.assertIn("晋升", out["summary"])
        self.assertEqual(ms.pending_papers(CODE, log=lambda *a: None), [])
        self.assertIn("w23_21", ms.published_papers(CODE))
        # 再次 sync/publish 幂等: 无待发布, 不重复计数
        res2 = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(res2["ok"], res2)
        cls = json.load(open(os.path.join(self.repo.dir,
                                          f"classified_{CODE}.json")))
        self.assertEqual([p["code"] for p in cls["papers"]].count("w23_21"), 1)


# ============================ 第六轮 P0-1: 翻转前失败不得改变已发布状态 ============================
class PreFlipFailureStateProtection(PipelineTestCase):
    """Codex 第五轮 P0-1 复现场景: 种子卷已发布后, 新卷发布分别在
    build-done / staged / pre-flip 检查点异常或硬崩溃。必须:
      - 旧卷 s24_11 保持已发布(意图不得提前移走 papers);
      - 新卷 w23_21 保持待发布, 正式单元文件不存在;
      - 「刷新服务索引」动作成功(旧版索引正常)也**不得**晋升过期意图;
      - 重启自愈后重发成功。
    翻转后崩溃(switch:flipped)则相反: 意图版本==活动版本, 刷新可安全晋升。
    旧实现(无条件晋升 pending_refresh)在本测试的刷新断言处必失败。"""

    RUNNER = (
        "import json, os, sys\n"
        "sys.path.insert(0, {repo!r})\n"
        "os.environ['ZJ_PUBLISH_NOTIFY_SERVER'] = '0'\n"
        "import paper_pipeline as pp, migrate_syllabus as ms\n"
        "pp._apply_root({repo!r}); ms._apply_root({repo!r})\n"
        "res = ms.publish({code!r}, log=lambda *a: None)\n"
        "print('PUBLISH_RESULT', json.dumps({{'ok': res.get('ok'),"
        " 'stage': res.get('stage')}}))\n")
    PRE_FLIP_POINTS = ["switch:build-done", "switch:staged", "switch:pre-flip"]

    def _fresh_repo(self):
        """subTest 每轮用全新夹具仓库(上一轮已把 w23_21 发布, 不可复用)。"""
        self.repo.destroy()
        self.repo = TestRepo(PipelineTestCase.site)

    def _seed_and_adjudicate_new(self):
        self._fresh_repo()
        seed_published_paper(self.repo)
        res = ms.publish(CODE, log=lambda *a: None)
        self.assertTrue(res["ok"], res)
        self.assertIn("s24_11", ms.published_papers(CODE))
        work = os.path.join(pp.PIPE_DIR, "work", CODE, "units")
        os.makedirs(work, exist_ok=True)
        json.dump({"code": "w23_21", "year": 2023, "session": "Oct-Nov",
                   "paper": 2, "variant": 1,
                   "qp_pdf": "9999（夹具）/2024/May-Jun/9999_s24_qp_11.pdf",
                   "ms_pdf": None,
                   "units": [{"id": "1", "q": 1, "part": None, "page": 2,
                              "marks": 6,
                              "text": "price mechanism pre-flip guard [6]"}]},
                  open(os.path.join(work, "w23_21_qp.json"), "w"))
        json.dump({"code": "w23_21", "tags": {"1": ["1.2"]}},
                  open(os.path.join(self.repo.dir, f"tags_{CODE}",
                                    "w23_21.json"), "w"))
        ms.adjudicate_new_papers(CODE, log=lambda *a: None)
        self.assertEqual(ms.pending_papers(CODE, log=lambda *a: None),
                         ["w23_21"])

    def _assert_uncommitted_state(self, cp):
        self.assertIn("s24_11", ms.published_papers(CODE),
                      f"{cp}: 旧卷必须保持已发布(意图不得提前移走)")
        self.assertNotIn("w23_21", ms.published_papers(CODE))
        self.assertEqual(ms.pending_papers(CODE, log=lambda *a: None),
                         ["w23_21"], f"{cp}: 新卷必须保持待发布")
        self.assertFalse(os.path.exists(
            os.path.join(self.repo.dir, "data", "units", CODE, "w23_21.json")),
            f"{cp}: 未提交的新卷不得有正式单元文件")
        self.assertEqual(ms.effective_refresh_pending(CODE), set(),
                         f"{cp}: 过期意图不得生效")
        self.assertEqual(ms.refresh_pending_papers(CODE),
                         {"s24_11", "w23_21"},
                         f"{cp}: 预备提交意图应留档(供审计/下次覆盖)")

    def _refresh_action(self):
        import pipeline_service as ps
        ps.apply_root(self.repo.dir)
        tm = ps.TaskManager()
        return tm._act_refresh_index({}, {"id": "t"}), tm

    def test_exception_at_each_pre_flip_point(self):
        for cp in self.PRE_FLIP_POINTS:
            with self.subTest(exception_at=cp):
                self._seed_and_adjudicate_new()

                def boom(name, _cp=cp):
                    if name == _cp:
                        raise RuntimeError(f"注入: {_cp} 异常")
                lv.set_pause(boom)
                try:
                    res = ms.publish(CODE, log=lambda *a: None)
                finally:
                    lv.clear_pause()
                self.assertFalse(res["ok"])
                self.assertEqual(res.get("stage"), "build-version")
                self._assert_uncommitted_state(cp)
                # 「刷新服务索引」动作成功(旧版索引正常)也不得晋升过期意图
                out, tm = self._refresh_action()
                self.assertTrue(out["ok"], out)
                self.assertNotIn("晋升", out["summary"])
                self._assert_uncommitted_state(cp + "+refresh")
                # 重启自愈后重发成功
                lv.ensure_layout(log=lambda *a: None)
                self._assert_uncommitted_state(cp + "+restart")
                res2 = ms.publish(CODE, log=lambda *a: None)
                self.assertTrue(res2["ok"], res2)
                self.assertIn("w23_21", ms.published_papers(CODE))
                self.assertTrue(os.path.exists(
                    os.path.join(self.repo.dir, "data", "units", CODE,
                                 "w23_21.json")))

    def test_hard_crash_at_each_pre_flip_point(self):
        for cp in self.PRE_FLIP_POINTS:
            with self.subTest(crash_at=cp):
                self._seed_and_adjudicate_new()
                env = dict(os.environ, ZJ_CRASH_AT=cp,
                           ZJ_PUBLISH_NOTIFY_SERVER="0")
                r = subprocess.run(
                    [sys.executable, "-c",
                     self.RUNNER.format(repo=self.repo.dir, code=CODE)],
                    capture_output=True, text=True, env=env, timeout=180)
                self.assertEqual(r.returncode, 70,
                                 f"{cp}: 应硬崩溃(exit 70): {r.stdout[-200:]}")
                self._assert_uncommitted_state(cp)
                out, tm = self._refresh_action()
                self.assertTrue(out["ok"], out)
                self.assertNotIn("晋升", out["summary"])
                self._assert_uncommitted_state(cp + "+refresh")
                res = ms.publish(CODE, log=lambda *a: None)
                self.assertTrue(res["ok"], res)
                self.assertIn("w23_21", ms.published_papers(CODE))

    def test_post_flip_crash_promotes_after_refresh(self):
        """翻转后崩溃: 意图版本==活动版本且文件存在 → 刷新动作安全晋升。"""
        self._seed_and_adjudicate_new()
        env = dict(os.environ, ZJ_CRASH_AT="switch:flipped",
                   ZJ_PUBLISH_NOTIFY_SERVER="0")
        r = subprocess.run(
            [sys.executable, "-c",
             self.RUNNER.format(repo=self.repo.dir, code=CODE)],
            capture_output=True, text=True, env=env, timeout=180)
        self.assertEqual(r.returncode, 70, r.stdout[-200:])
        self.assertTrue(os.path.exists(
            os.path.join(self.repo.dir, "data", "units", CODE, "w23_21.json")),
            "翻转后崩溃: 新卷已提交到活动版本")
        self.assertEqual(ms.effective_refresh_pending(CODE),
                         {"s24_11", "w23_21"})
        self.assertEqual(ms.pending_papers(CODE, log=lambda *a: None),
                         ["w23_21"], "索引未就绪前仍算待发布")
        out, tm = self._refresh_action()
        self.assertTrue(out["ok"], out)
        self.assertIn("晋升", out["summary"])
        self.assertEqual(ms.pending_papers(CODE, log=lambda *a: None), [])
        self.assertIn("w23_21", ms.published_papers(CODE))


if __name__ == "__main__":
    unittest.main(verbosity=1)
