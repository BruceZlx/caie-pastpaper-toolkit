#!/usr/bin/env python3
"""test_mock_exam.py —— 真题模拟组卷功能测试(第二轮: 试题+官方答案)。

覆盖: 核分([Total]/标记求和/MCQ/代码下标/多罗马小问/异常排除)、章节·年份·Paper
过滤、精确总分有解/无解、require_each 的**带约束完备**求解(与暴力对账)、
参数校验(空/跨科/类型/穿越)、QP/MS **全部裁切段坐标**候选阶段核验、
答案可用性在选题前过滤(MS 缺失/无文字层/定位失败/字母缺失与不一致)、
冻结选题指纹 selection_id(参数/种子/源文件变化 → 409)、试题与答案 PDF 一一
对账(9702 P1+2 混排、9709/9618 结构题、9701 MCQ)、server HTTP 接口与既有
接口回归。前端失效/晚到响应行为已在浏览器中验证(见交付说明)。

用法: /usr/bin/python3 test_mock_exam.py            # 全部
      /usr/bin/python3 test_mock_exam.py Rescore Answers  # 按类名过滤
依赖: /usr/bin/python3(含 PyMuPDF); server 用例自动拉起临时进程。
"""
import itertools
import json
import os
import random
import re
import subprocess
import sys
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fitz                       # noqa: E402
import mock_exam as me            # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8981
BASE = f"http://127.0.0.1:{PORT}"

IDX = None


def setUpModule():
    global IDX
    IDX = me.MockIndex()
    IDX.build()
    assert IDX.ready, "索引构建失败"


def idx_q(subject, code, q):
    for it in IDX.subjects[subject]["questions"]:
        if it["code"] == code and it["q"] == q:
            return it
    return None


def unit_file(subject, code):
    with open(os.path.join(ROOT, "data", "units", subject, code + ".json")) as f:
        return json.load(f)


def msloc_file(subject, code):
    with open(os.path.join(ROOT, "data", "msloc", subject, code + ".json")) as f:
        return json.load(f)


def http(method, path, body=None, timeout=90):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def pv_body(**kw):
    b = {"subject": "9702", "papers": [1, 2], "topics": ["3", "4"],
         "year_from": 2016, "year_to": 2026, "target": 40, "seed": "t",
         "mode": "answers"}
    b.update(kw)
    return b


# ============================ 核分 ============================
class Rescore(unittest.TestCase):
    def test_w20_42_q2_total_priority(self):
        """9702 w20_42 Q2: 题面 [Total: 10] 优先于单元 marks(2b 记 2 而实际 4)。"""
        it = idx_q("9702", "w20_42", 2)
        self.assertIsNotNone(it)
        self.assertEqual(it["marks"], 10)
        self.assertEqual(it["basis"], "total")

    def test_unit_marks_trap(self):
        """数据陷阱实证: 2b/2c 的单元 marks 只记最后一个 [n], 不得直接相加。"""
        rec = unit_file("9702", "w20_42")
        u2b = next(u for u in rec["units"] if u["id"] == "2b")
        u2c = next(u for u in rec["units"] if u["id"] == "2c")
        self.assertEqual(u2b["marks"], 2)          # 题面含 (i)[2] (ii)[2] → 实际 4
        self.assertEqual(u2c["marks"], 1)          # 题面含 (i)[3] (ii)[1] → 实际 4
        self.assertEqual(me.mark_markers(u2b["text"]), [2, 2])
        self.assertEqual(me.mark_markers(u2c["text"]), [3, 1])

    def test_sum_basis_without_total(self):
        found = False
        for it in IDX.subjects["9702"]["questions"]:
            if it["basis"] == "sum" and it["paper"] == 4:
                found = True
                break
        self.assertTrue(found, "9702 P4 应存在 sum 依据的题")

    def test_mcq_each_1_mark(self):
        """9702 m23_12 (Paper 1 MCQ): 40 题每题 1 分。"""
        qs = [it for it in IDX.subjects["9702"]["questions"] if it["code"] == "m23_12"]
        self.assertEqual(len(qs), 40)
        self.assertTrue(all(it["marks"] == 1 and it["basis"] == "mcq" for it in qs))

    def test_cs_array_index_not_counted(self):
        """9618 s24_21 Q5: 数组下标 Data[1]..Data[3] 不算给分标记。
        真实标记 [6]+[1]+[1] = 8; 抽取脚本给单元 5 记的 marks=3 恰是 Data[3]。"""
        rec = unit_file("9618", "s24_21")
        q5_units = [u for u in rec["units"] if u["q"] == 5]
        self.assertEqual([u["marks"] for u in q5_units], [3, 6, 1])
        text = "\n".join(u["text"] for u in q5_units)
        self.assertIn("Data[1]", text)
        self.assertEqual(me.mark_markers(text), [6, 1, 1])
        it = idx_q("9618", "s24_21", 5)
        self.assertIsNotNone(it)
        self.assertEqual(it["marks"], 8)
        self.assertEqual(it["basis"], "sum")

    def test_math_multi_roman(self):
        """9709 数学多罗马小问: 标记求和正确。"""
        chosen = None
        for code in sorted(os.listdir(os.path.join(ROOT, "data/units/9709"))):
            r = unit_file("9709", code[:-5])
            for g in me.group_questions(r["units"]):
                t = "\n".join(u["text"] for u in g["units"])
                if "(i)" in t and "(ii)" in t and "[Total" not in t \
                        and me.mark_markers(t):
                    chosen = (r, g, t)
                    break
            if chosen:
                break
        self.assertIsNotNone(chosen, "应存在多罗马小问题")
        r, g, t = chosen
        it = idx_q("9709", r["code"], g["q"])
        self.assertEqual(it["marks"], sum(me.mark_markers(t)))

    def test_anomaly_excluded(self):
        """9618 s25_33 Q13 (标记和 114) 保守排除; 9702 P5 规划题 [15] 保留。"""
        self.assertIsNone(idx_q("9618", "s25_33", 13))
        kept = [it for it in IDX.subjects["9702"]["questions"] if it["paper"] == 5]
        self.assertTrue(kept and any(it["marks"] == 15 for it in kept))

    def test_materials_excluded(self):
        for it in IDX.subjects["9702"]["questions"]:
            if it["code"] == "m18_33":
                self.assertNotEqual(it["q"], 1)

    def test_renumbered_paper_excluded(self):
        rec = unit_file("9702", "w20_42")
        rec = dict(rec, units=list(rec["units"]))
        rec["units"].append(dict(rec["units"][0], q=1))
        qs, why = me.rescore_paper(rec)
        self.assertEqual(why, "renumbered-q")
        self.assertEqual(qs, [])


# ============================ 过滤 ============================
class Filters(unittest.TestCase):
    def test_topic_expansion(self):
        subs = IDX.expand_topics("9702", ["8"])
        self.assertEqual(subs, {"8.1", "8.2", "8.3", "8.4"})
        self.assertEqual(IDX.expand_topics("9702", ["8.4"]), {"8.4"})
        self.assertEqual(IDX.expand_topics("9702", ["8", "8.4"]),
                         {"8.1", "8.2", "8.3", "8.4"})
        with self.assertRaises(me.MockError):
            IDX.expand_topics("9702", ["99"])
        with self.assertRaises(me.MockError):
            IDX.expand_topics("9702", [])

    def test_paper_year_topic_filter(self):
        params = me.validate_params(IDX, {
            "subject": "9702", "papers": [1, 2],
            "topics": ["3", "4"], "year_from": 2016, "year_to": 2020,
            "target": 40, "seed": "t", "mode": "exam_only"})
        cands, stats, notes, _ = me.build_candidates(IDX, params)
        self.assertTrue(cands)
        for c in cands:
            self.assertIn(c["paper"], (1, 2))
            self.assertTrue(2016 <= c["year"] <= 2020)
            self.assertTrue(set(c["labels"]) <= params["topics"])
            self.assertIsNone(me.check_segs(c["seg"], IDX.geom.geom(c["qp_pdf"])))

    def test_labels_all_within_strict(self):
        """多标签题: 所有标签都必须在所选范围内(选 4.1 不带出 4.3)。"""
        params = me.validate_params(IDX, {
            "subject": "9702", "papers": [1], "topics": ["4.1"],
            "year_from": 2002, "year_to": 2026, "target": 20, "seed": "t",
            "mode": "exam_only"})
        cands, *_ = me.build_candidates(IDX, params)
        self.assertTrue(cands)
        for c in cands:
            self.assertEqual(set(c["labels"]), {"4.1"})

    def test_paper_empty_note(self):
        params = me.validate_params(IDX, {
            "subject": "9702", "papers": [1, 6], "topics": ["3"],
            "year_from": 2016, "year_to": 2026, "target": 20, "seed": "t",
            "mode": "exam_only"})
        cands, stats, notes, _ = me.build_candidates(IDX, params)
        self.assertTrue(any("Paper 6" in n and "没有候选题" in n for n in notes))
        self.assertTrue(all(c["paper"] == 1 for c in cands))


# ============================ 精确总分选题(含 require_each 完备性) ============================
class Selection(unittest.TestCase):
    def test_exact_solvable(self):
        pv = me.run_preview(IDX, pv_body())
        self.assertTrue(pv["ok"])
        self.assertEqual(pv["total"], 40)
        self.assertEqual(sum(q["marks"] for q in pv["questions"]), 40)

    def test_reproducible_and_reshuffle(self):
        a = me.run_preview(IDX, pv_body(seed="same"))
        b = me.run_preview(IDX, pv_body(seed="same"))
        self.assertEqual([q["id"] for q in a["questions"]],
                         [q["id"] for q in b["questions"]])
        self.assertEqual(a["selection_id"], b["selection_id"])
        c = me.run_preview(IDX, pv_body(seed="other"))
        self.assertNotEqual([q["id"] for q in a["questions"]],
                            [q["id"] for q in c["questions"]])
        self.assertNotEqual(a["selection_id"], c["selection_id"])

    def test_unsolvable_target(self):
        p5 = next(it for it in IDX.subjects["9702"]["questions"] if it["paper"] == 5)
        with self.assertRaises(me.MockError) as cm:
            me.run_preview(IDX, {"subject": "9702", "papers": [5],
                                 "topics": sorted(set(p5["labels"])),
                                 "year_from": 2007, "year_to": 2026,
                                 "target": 16, "seed": "x", "mode": "exam_only"})
        self.assertIn("凑出恰好", cm.exception.message)

    def test_no_greedy_false_negative(self):
        """[3,3,7] 凑 10 → 必须取 3+7(朴素贪心会失败)。"""
        def c(m, i):
            return {"id": f"x{i}", "marks": m, "paper": 1, "variant": 1,
                    "year": 2020, "session": "May-Jun", "q": i,
                    "labels": ["1.1"], "code": f"fake{i}", "subject": "9702"}
        sel = me.select_subset([c(3, 1), c(3, 2), c(7, 3)], 10, "s")
        self.assertIsNotNone(sel)
        self.assertEqual(sorted(x["marks"] for x in sel), [3, 7])

    def test_require_each_paper(self):
        pv = me.run_preview(IDX, pv_body(require_each_paper=True))
        used = {str(q["paper"]) for q in pv["questions"]}
        self.assertEqual(used, {"1", "2"})
        self.assertEqual(pv["total"], 40)

    def test_total_never_off(self):
        for target in (7, 13, 25, 40, 58):
            pv = me.run_preview(IDX, pv_body(target=target, seed=f"s{target}"))
            self.assertEqual(pv["total"], target, f"target={target}")

    def test_constrained_dp_matches_bruteforce(self):
        """require_each 的完备性: 与暴力枚举在随机实例上逐一对账。"""
        rnd = random.Random(11)

        def c(m, p, i):
            return {"id": f"x{i}", "marks": m, "paper": p, "variant": 1,
                    "year": 2020, "session": "May-Jun", "q": i,
                    "labels": ["1.1"], "code": f"c{i}", "subject": "s"}
        n_feasible = 0
        for trial in range(300):
            n = rnd.randint(1, 9)
            items = [c(rnd.randint(1, 5), rnd.randint(1, 3), i + 1)
                     for i in range(n)]
            target = rnd.randint(1, 20)
            req = sorted({it["paper"] for it in items})
            bf = any(sum(it["marks"] for it in combo) == target and
                     set(req) <= {it["paper"] for it in combo}
                     for r in range(1, n + 1)
                     for combo in itertools.combinations(items, r))
            dp = me.select_subset(items, target, f"s{trial}",
                                  require_papers=req) is not None
            n_feasible += bf
            self.assertEqual(bf, dp, f"trial={trial} items="
                             f"{[(i['marks'], i['paper']) for i in items]} "
                             f"target={target}")
        self.assertGreater(n_feasible, 50, "随机实例应有相当数量可行例")

    def test_require_each_realdata_multi_paper(self):
        """真实数据: 选 3 种 Paper + require_each, 三种都出现且总分精确。"""
        body = {"subject": "9702", "papers": [1, 2, 4], "topics": ["3", "4", "12"],
                "year_from": 2016, "year_to": 2026, "target": 40, "seed": "m3",
                "mode": "answers", "require_each_paper": True}
        pv = me.run_preview(IDX, body)
        used = {q["paper"] for q in pv["questions"]}
        self.assertEqual(used, {1, 2, 4})
        self.assertEqual(pv["total"], 40)


# ============================ 参数校验(核心层) ============================
class Validate(unittest.TestCase):
    def call(self, **kw):
        body = {"subject": "9702", "papers": [1], "topics": ["3"],
                "year_from": 2016, "year_to": 2026, "target": 20, "mode": "answers"}
        body.update(kw)
        return me.validate_params(IDX, body)

    def assert_msg(self, patch, frag):
        with self.assertRaises(me.MockError) as cm:
            self.call(**patch)
        self.assertIn(frag, cm.exception.message)

    def test_valid(self):
        p = self.call()
        self.assertEqual(p["papers"], [1])
        # 断言与当前 topics_9702.json 同步: 章节 "3" 展开为该章全部小节
        # (2028-2030 大纲第 3 章 Dynamics = 3.1-3.3; 旧大纲此处曾是 {3.1})
        tax = json.load(open(os.path.join(ROOT, "topics_9702.json")))
        sec3 = next(s for s in tax["sections"] if s["code"] == "3")
        self.assertEqual(p["topics"], {x["code"] for x in sec3["subs"]})
        self.assertEqual(p["mode"], "answers")

    def test_papers_invalid(self):
        self.assert_msg({"papers": []}, "至少选择一个 Paper")
        self.assert_msg({"papers": ["1"]}, "整数")
        self.assert_msg({"papers": [1, 1]}, "重复")
        self.assert_msg({"papers": [7]}, "不属于科目 9702")   # 9709 才有 P7 → 跨科
        self.assert_msg({"papers": [11]}, "不属于科目 9702")  # variant 编号不是 Paper
        self.assert_msg({"papers": [0]}, "不属于科目 9702")
        self.assert_msg({"papers": [-1]}, "不属于科目 9702")

    def test_subject_invalid(self):
        with self.assertRaises(me.MockError) as cm:
            me.validate_params(IDX, {"subject": "9999", "papers": [1],
                                     "topics": ["3"], "year_from": 2016,
                                     "year_to": 2026, "target": 10,
                                     "mode": "answers"})
        self.assertIn("没有可用真题数据", cm.exception.message)
        for bad in ("../../9702", "9702/../9701", "abcd", "", None, 9702):
            with self.assertRaises(me.MockError):
                me.validate_params(IDX, {"subject": bad, "papers": [1],
                                         "topics": ["3"], "year_from": 2016,
                                         "year_to": 2026, "target": 10,
                                         "mode": "answers"})

    def test_topics_invalid(self):
        self.assert_msg({"topics": []}, "至少选择一个章节")
        self.assert_msg({"topics": ["99"]}, "知识点编号无效")
        self.assert_msg({"topics": ["3", "xx"]}, "知识点编号格式无效")

    def test_years_target_mode_invalid(self):
        self.assert_msg({"year_from": 2030, "year_to": 2020}, "起始年份不能大于结束年份")
        self.assert_msg({"year_from": "2016"}, "整数年份")
        self.assert_msg({"target": 0}, "正整数")
        self.assert_msg({"target": -5}, "正整数")
        self.assert_msg({"target": 9999}, "过大")
        self.assert_msg({"target": "40"}, "正整数")
        self.assert_msg({"mode": "both"}, "mode 应为")

    def test_selection_id_format(self):
        for bad in (None, "", "xyz", "0123456789abc", 123):
            with self.assertRaises(me.MockError) as cm:
                me._validate_selection_id({"selection_id": bad})
            self.assertEqual(cm.exception.code, "bad-selection-id")


# ============================ 冻结选题指纹 ============================
class FrozenSelection(unittest.TestCase):
    def test_id_changes_with_each_input(self):
        base = me.run_preview(IDX, pv_body(seed="f1"))
        cases = [
            pv_body(seed="f2"),                                   # 种子
            pv_body(target=41),                                   # 目标总分
            pv_body(year_from=2017),                              # 年份
            pv_body(papers=[1]),                                  # Paper 集合
            pv_body(topics=["3"]),                                # 知识点
            pv_body(require_each_paper=True),                     # 覆盖约束
            pv_body(mode="exam_only"),                            # 模式
        ]
        ids = {base["selection_id"]}
        for b in cases:
            ids.add(me.run_preview(IDX, b)["selection_id"])
        self.assertEqual(len(ids), 1 + len(cases), "每个输入维度都应改变指纹")

    def test_fingerprint_sensitive_to_source_version(self):
        """源文件 mtime/size 变化 → 指纹变化 → 下载 409(不修改真实文件,
        用 mock 替换 _file_versions 的返回值模拟源变化)。"""
        body = pv_body(seed="fx")
        pv = me.run_preview(IDX, body)
        body["selection_id"] = pv["selection_id"]
        me.run_qp_pdf(IDX, body)                    # 正常路径先走通
        real = me._file_versions

        def bumped(sel):
            out = []
            for rel, mt, sz in real(sel):
                out.append([rel, mt + 1, sz])       # mtime 变了
            return out
        with unittest.mock.patch.object(me, "_file_versions", bumped):
            # build_answers_pdf 等经 _solve → selection_fingerprint → _file_versions
            with self.assertRaises(me.MockError) as cm:
                me.run_qp_pdf(IDX, body)
        self.assertEqual(cm.exception.code, "stale")
        self.assertIn("重新", cm.exception.message)

    def test_download_requires_valid_selection(self):
        body = pv_body(seed="dl")
        pv = me.run_preview(IDX, body)
        # 缺 selection_id
        with self.assertRaises(me.MockError) as cm:
            me.run_qp_pdf(IDX, pv_body(seed="dl"))
        self.assertEqual(cm.exception.code, "bad-selection-id")
        # 错误 selection_id
        bad = dict(body, selection_id="deadbeefdeadbeef")
        with self.assertRaises(me.MockError) as cm:
            me.run_qp_pdf(IDX, bad)
        self.assertEqual(cm.exception.code, "stale")
        # 表单已变(参数不同)但带旧 selection_id → 409 语义
        changed = dict(pv_body(seed="dl", target=41),
                       selection_id=pv["selection_id"])
        with self.assertRaises(me.MockError) as cm:
            me.run_qp_pdf(IDX, changed)
        self.assertEqual(cm.exception.code, "stale")

    def test_exam_only_has_no_answers(self):
        pv = me.run_preview(IDX, pv_body(mode="exam_only", seed="eo"))
        self.assertIsNone(pv["answers_covered"])
        self.assertTrue(all(q["answer"]["kind"] is None for q in pv["questions"]))
        body = dict(pv_body(mode="exam_only", seed="eo"),
                    selection_id=pv["selection_id"])
        me.run_qp_pdf(IDX, body)                    # 仅试题可下载试卷
        with self.assertRaises(me.MockError) as cm:
            me.run_answers_pdf(IDX, body)
        self.assertEqual(cm.exception.code, "no-answers-mode")


# ============================ 答案可用性(选题前过滤, 构造异常) ============================
class AnswerFailures(unittest.TestCase):
    """monkeypatch 索引条目构造缺 MS/无文字层/seg 缺失/字母缺失/不一致/坐标越界,
    答案模式必须在选题前剔除; 仅试题模式不受 MS 影响。"""

    def candidates(self, **patch):
        params = me.validate_params(IDX, pv_body(**{"seed": "af"} | patch))
        return me.build_candidates(IDX, params)

    def with_patch(self, qid, patch, expect_reason, mode="answers"):
        qs = IDX.subjects["9702"]["questions"]
        it = next(x for x in qs if x["id"] == qid)
        saved = {k: it[k] for k in patch}
        it.update(patch)
        try:
            params = me.validate_params(IDX, pv_body(
                seed="af", mode=mode, year_from=it["year"], year_to=it["year"],
                topics=sorted(set(it["labels"])), papers=[it["paper"]]))
            cands, stats, _, _ = me.build_candidates(IDX, params)
            return cands, stats
        finally:
            it.update(saved)

    def test_ms_missing_excludes(self):
        it = next(x for x in IDX.subjects["9702"]["questions"]
                  if x["paper"] == 4 and x["basis"] != "mcq")
        cands, stats = self.with_patch(it["id"], {"ms_status": "missing"},
                                       "ms-missing")
        self.assertTrue(all(c["id"] != it["id"] for c in cands))
        self.assertGreaterEqual(stats["excluded"].get("ms-missing", 0), 1)

    def test_ms_no_text_layer_excludes(self):
        it = next(x for x in IDX.subjects["9702"]["questions"]
                  if x["paper"] == 4 and x["basis"] != "mcq")
        cands, stats = self.with_patch(it["id"], {"ms_status": "no_text_layer"},
                                       "ms-no-text")
        self.assertTrue(all(c["id"] != it["id"] for c in cands))
        self.assertGreaterEqual(stats["excluded"].get("ms-no-text", 0), 1)

    def test_ms_no_qseg_excludes(self):
        it = next(x for x in IDX.subjects["9702"]["questions"]
                  if x["paper"] == 4 and x["basis"] != "mcq")
        cands, stats = self.with_patch(it["id"], {"ms_q_seg": None}, "ms-no-loc")
        self.assertTrue(all(c["id"] != it["id"] for c in cands))
        self.assertGreaterEqual(stats["excluded"].get("ms-no-loc", 0), 1)

    def test_ms_seg_out_of_range_excludes(self):
        """MS 裁切段页码越界(构造) → 候选阶段拒绝, 不进入选题。"""
        it = next(x for x in IDX.subjects["9702"]["questions"]
                  if x["paper"] == 4 and x["basis"] != "mcq")
        bad_seg = [[9999, 0, 0, 100, 100]]
        cands, stats = self.with_patch(it["id"], {"ms_q_seg": bad_seg},
                                       "ms-seg-invalid")
        self.assertTrue(all(c["id"] != it["id"] for c in cands))
        self.assertGreaterEqual(stats["excluded"].get("ms-seg-invalid", 0), 1)

    def test_ms_seg_empty_rect_excludes(self):
        it = next(x for x in IDX.subjects["9702"]["questions"]
                  if x["paper"] == 4 and x["basis"] != "mcq")
        cands, stats = self.with_patch(
            it["id"], {"ms_q_seg": [[1, 0, 0, 0.1, 0.1]]}, "ms-seg-invalid")
        self.assertTrue(all(c["id"] != it["id"] for c in cands))

    def test_mcq_letter_missing_excludes(self):
        it = next(x for x in IDX.subjects["9702"]["questions"]
                  if x["paper"] == 1)
        cands, stats = self.with_patch(it["id"], {"ms_letter": None},
                                       "ms-letter-missing")
        self.assertTrue(all(c["id"] != it["id"] for c in cands))
        self.assertGreaterEqual(stats["excluded"].get("ms-letter-missing", 0), 1)

    def test_mcq_letter_mismatch_excludes(self):
        """units 字母与 letters 表不一致 → 排除(不冒充官方答案)。"""
        it = next(x for x in IDX.subjects["9702"]["questions"]
                  if x["paper"] == 1 and x.get("ms_letter"))
        other = "E" if it["ms_letter"] != "E" else "D"
        cands, stats = self.with_patch(it["id"], {"ms_letter_table": other},
                                       "ms-letter-mismatch")
        self.assertTrue(all(c["id"] != it["id"] for c in cands))
        self.assertGreaterEqual(stats["excluded"].get("ms-letter-mismatch", 0), 1)

    def test_qp_seg_invalid_excludes_even_exam_only(self):
        """QP 题面裁切段坐标无效 → 两种模式都拒绝该题。"""
        it = next(x for x in IDX.subjects["9702"]["questions"]
                  if x["paper"] == 4)
        for mode in ("answers", "exam_only"):
            cands, stats = self.with_patch(it["id"], {"seg": [[9999, 0, 0, 9, 9]]},
                                           "qp-seg-invalid", mode=mode)
            self.assertTrue(all(c["id"] != it["id"] for c in cands), mode)
            self.assertGreaterEqual(stats["excluded"].get("qp-seg-invalid", 0), 1,
                                    mode)

    def test_answer_filter_makes_no_solution_explicit(self):
        """答案过滤后无精确解 → 可读报错(不得默默输出缺答案的套卷)。"""
        # 9702 P5 全是 15 分规划题: 若 MS 不可用则 answers 模式无候选
        p5 = [x for x in IDX.subjects["9702"]["questions"] if x["paper"] == 5]
        saved = [(x, x["ms_status"]) for x in p5]
        try:
            for x in p5:
                x["ms_status"] = "missing"
            with self.assertRaises(me.MockError) as cm:
                me.run_preview(IDX, {"subject": "9702", "papers": [5],
                                     "topics": sorted(set(p5[0]["labels"])),
                                     "year_from": 2007, "year_to": 2026,
                                     "target": 15, "seed": "x",
                                     "mode": "answers"})
            self.assertIn("试卷+答案模式已剔除", cm.exception.message)
        finally:
            for x, st in saved:
                x["ms_status"] = st

    def test_check_segs_cases(self):
        rects = [fitz.Rect(0, 0, 595, 842), fitz.Rect(0, 0, 595, 842)]
        self.assertIsNone(me.check_segs([[1, 10, 10, 500, 800]], rects))
        self.assertIsNone(me.check_segs([[1, 10, 10, 500, 800],
                                         [2, 10, 10, 500, 800]], rects))
        self.assertIsNotNone(me.check_segs([[3, 10, 10, 500, 800]], rects))   # 页码越界
        self.assertIsNotNone(me.check_segs([[2, 10, 10, 500, 800],
                                            [1, 10, 10, 500, 800]], rects))  # 顺序回退
        self.assertIsNotNone(me.check_segs([[1, 10, 10, 500, 800],
                                            [1, 10, 10, 500, 800]], rects))  # 重复段
        self.assertIsNotNone(me.check_segs([[1, 594.5, 10, 600, 800]], rects))  # 相交<4pt
        self.assertIsNotNone(me.check_segs([[1, 10, 10, 500, 800],
                                            [1, 594.5, 10, 600, 800]], rects))  # 一段无效全拒
        self.assertIsNotNone(me.check_segs([], rects))
        self.assertIsNotNone(me.check_segs([[1, 10, 10, 500, 800]], None))   # 源不可用


# ============================ 试题 + 答案 PDF 对账(真实数据) ============================
class PdfBuild(unittest.TestCase):
    def make(self, body):
        pv = me.run_preview(IDX, body)
        dl = dict(body, selection_id=pv["selection_id"])
        fn1, qp = me.run_qp_pdf(IDX, dl)
        self.assertEqual(fn1, pv["filename"])
        docs = {"qp": fitz.open(stream=qp, filetype="pdf")}
        self.addCleanup(docs["qp"].close)
        if pv["answers_covered"] is not None:
            fn2, ans = me.run_answers_pdf(IDX, dl)
            self.assertEqual(fn2, pv["answers_filename"])
            self.assertTrue(fn2.endswith("_answers.pdf"))
            self.assertEqual(fn2[:-len("_answers.pdf")], fn1[:-len(".pdf")])
            docs["ans"] = fitz.open(stream=ans, filetype="pdf")
            self.addCleanup(docs["ans"].close)
        return pv, docs

    def texts(self, docs):
        return {k: "".join(d[p].get_text() for p in range(d.page_count))
                for k, d in docs.items()}

    def assert_common(self, pv, docs, body):
        """逐题对账: 顺序/新题号/分值/来源; 答案覆盖; 页脚; 附录; 无空白页。"""
        tx = self.texts(docs)
        qpt = tx["qp"]
        self.assertEqual(pv["total"], body["target"])
        self.assertEqual(sum(q["marks"] for q in pv["questions"]), body["target"])
        for q in pv["questions"]:
            self.assertIn(f"Question {q['no']}", qpt)
            self.assertIn(f"[{q['marks']} marks]", qpt)
            self.assertTrue(body["year_from"] <= q["year"] <= body["year_to"])
            self.assertIn(q["paper"], body["papers"])
        d = docs["qp"]
        self.assertIn(f"- 1 / {d.page_count}", qpt)
        for pno in range(d.page_count):
            pix = d[pno].get_pixmap(dpi=28)
            self.assertLess(pix.samples.count(255), len(pix.samples),
                            f"试卷第 {pno+1} 页为空白页")
        if "ans" not in docs:
            return
        anst = tx["ans"]
        self.assertEqual(pv["answers_covered"], len(pv["questions"]))
        for pno in range(docs["ans"].page_count):
            pix = docs["ans"][pno].get_pixmap(dpi=28)
            self.assertLess(pix.samples.count(255), len(pix.samples),
                            f"答案第 {pno+1} 页为空白页")
        # 附录: 每题的 QP 与 MS 来源文件
        for q in pv["questions"]:
            self.assertIn(os.path.basename(q["qp_pdf"])[:12], qpt)
            if q["answer"].get("ms_pdf"):
                self.assertIn(q["answer"]["ms_pdf"][:12], anst)
        self.assertIn(pv["selection_id"], anst)          # 封面写 selection_id

    def test_9702_p12_mixed_answers(self):
        """物理 9702 Paper 1+2 混排(MCQ+结构题), 两种 Paper 都出题。"""
        body = pv_body(seed="pdf12", require_each_paper=True)
        pv, docs = self.make(body)
        self.assertEqual({q["paper"] for q in pv["questions"]}, {1, 2})
        kinds = {q["answer"]["kind"] for q in pv["questions"]}
        self.assertIn("mcq", kinds)
        self.assertIn("structured", kinds)
        self.assert_common(pv, docs, body)
        # MCQ 字母逐题对照 msloc.units (原始数据源核验)
        anst = self.texts(docs)["ans"]
        for q in pv["questions"]:
            if q["answer"]["kind"] != "mcq":
                continue
            src = msloc_file("9702", q["code"])
            uid = next(u["id"] for u in unit_file("9702", q["code"])["units"]
                       if u["q"] == q["q"])
            letter = (src["units"].get(uid) or {}).get("letter")
            self.assertEqual(q["answer"]["letter"], letter)
            self.assertRegex(q["answer"]["letter"], r"^[A-D]$")
            self.assertIn(f"Q{q['no']}", anst)      # 答案表含新题号
        # 结构题: 使用 msloc.questions[q].seg(完整官方评分标准, 不并入下一题)
        for q in pv["questions"]:
            if q["answer"]["kind"] != "structured":
                continue
            src = msloc_file("9702", q["code"])
            self.assertTrue((src.get("questions") or {}).get(str(q["q"]), {})
                            .get("seg"))
            self.assertIn(f"Question {q['no']}    [{q['marks']} marks]", anst)

    def test_9702_p2_only(self):
        body = pv_body(papers=[2], topics=["3", "4", "5", "14", "15"],
                       year_from=2002, year_to=2026, target=20, seed="p2only")
        pv, docs = self.make(body)
        self.assertTrue(pv["questions"])
        self.assertTrue(all(q["paper"] == 2 for q in pv["questions"]))
        self.assert_common(pv, docs, body)

    def test_9709_structured_answers(self):
        """数学 9709 结构题官方评分标准。"""
        body = {"subject": "9709", "papers": [1], "topics": ["1"],
                "year_from": 2018, "year_to": 2026, "target": 45,
                "seed": "math", "mode": "answers"}
        pv, docs = self.make(body)
        self.assertTrue(all(q["answer"]["kind"] == "structured"
                            for q in pv["questions"]))
        self.assert_common(pv, docs, body)
        anst = self.texts(docs)["ans"]
        # 每道结构题的答案标题应存在于答案 PDF
        for q in pv["questions"]:
            self.assertIn(f"Question {q['no']}    [{q['marks']} marks]", anst)
        # 答案内容确实来自 MS 文字层(含典型评分符号)
        self.assertRegex(anst, r"(M1|A1|B1)")

    def test_9618_structured_answers(self):
        """CS 9618 P2 结构题。"""
        body = {"subject": "9618", "papers": [2], "topics": ["9", "10", "11"],
                "year_from": 2021, "year_to": 2026, "target": 30,
                "seed": "cs", "mode": "answers"}
        pv, docs = self.make(body)
        self.assert_common(pv, docs, body)

    def test_9701_mcq_answers(self):
        """化学 9701 Paper 1 MCQ 官方字母表。"""
        body = {"subject": "9701", "papers": [1], "topics": ["7"],
                "year_from": 2018, "year_to": 2026, "target": 20,
                "seed": "chem", "mode": "answers"}
        pv, docs = self.make(body)
        self.assertTrue(pv["questions"])
        for q in pv["questions"]:
            self.assertEqual(q["answer"]["kind"], "mcq")
            src = msloc_file("9701", q["code"])
            uid = next(u["id"] for u in unit_file("9701", q["code"])["units"]
                       if u["q"] == q["q"])
            self.assertEqual(q["answer"]["letter"],
                             (src["units"].get(uid) or {}).get("letter"))
        self.assert_common(pv, docs, body)
        self.assertIn("MCQ", self.texts(docs)["ans"])

    def test_exam_and_answers_same_selection(self):
        """同一冻结选题: 两份 PDF 的题数/顺序/分值/来源一一对应。"""
        body = pv_body(seed="pair")
        pv, docs = self.make(body)
        qpt, anst = self.texts(docs).values()
        # 答案封面/附录的分数合计与试卷一致
        self.assertIn(f"{pv['total']} 分", anst)
        for q in pv["questions"]:
            self.assertIn(f"Question {q['no']}", qpt)
        # 试卷与答案题头数量一致(结构题各自标题 + MCQ 表统一一段)
        n_struct = sum(1 for q in pv["questions"]
                       if q["answer"]["kind"] == "structured")
        self.assertEqual(anst.count("Official Mark Scheme"), n_struct)


# ============================ HTTP 接口与回归 ============================
class ServerApi(unittest.TestCase):
    proc = None

    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen(
            ["/usr/bin/python3", "server.py", "--port", str(PORT)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while time.time() - t0 < 90:
            try:
                s, _, _ = http("GET", "/api/health", timeout=5)
                if s == 200:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        cls.tearDownClass()
        raise RuntimeError("server 未启动")

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()

    # ---- mock 接口 ----
    def test_meta(self):
        s, _, b = http("GET", "/api/mock/meta?subject=9702")
        self.assertEqual(s, 200)
        d = json.loads(b)
        self.assertTrue(d["ok"])
        self.assertTrue(d["papers"])
        self.assertTrue(d["topics"]["sections"])
        self.assertTrue(d["topics"]["note"])
        s, _, b = http("GET", "/api/mock/meta?subject=9999")
        self.assertEqual(s, 400)
        self.assertIn("没有真题数据", json.loads(b)["error"])

    def test_preview_http(self):
        s, _, b = http("POST", "/api/mock/preview", pv_body(
            seed="http", require_each_paper=True))
        self.assertEqual(s, 200)
        d = json.loads(b)
        self.assertTrue(d["ok"])
        self.assertEqual(d["total"], 40)
        self.assertEqual({q["paper"] for q in d["questions"]}, {1, 2})
        self.assertEqual(d["answers_covered"], len(d["questions"]))
        self.assertRegex(d["selection_id"], r"^[0-9a-f]{16}$")
        self.assertTrue(d["answers_filename"].endswith("_answers.pdf"))
        for key in ("marks", "basis", "labels", "year", "session", "paper",
                    "variant", "code", "q", "answer"):
            self.assertIn(key, d["questions"][0])

    def test_qp_and_answers_and_zip_http(self):
        s, _, b = http("POST", "/api/mock/preview", pv_body(seed="httpdl"))
        sid = json.loads(b)["selection_id"]
        body = dict(pv_body(seed="httpdl"), selection_id=sid)
        s, h, b = http("POST", "/api/mock/pdf", body)
        self.assertEqual(s, 200)
        self.assertEqual(h.get("Content-Type"), "application/pdf")
        self.assertIn("attachment", h.get("Content-Disposition", ""))
        self.assertRegex(h.get("Content-Disposition", ""),
                         r'^attachment; filename="mock_9702_[a-z0-9_.-]+\.pdf"$')
        doc = fitz.open(stream=b, filetype="pdf")
        self.assertGreater(doc.page_count, 1)
        doc.close()
        s, h, b = http("POST", "/api/mock/answers", body)
        self.assertEqual(s, 200)
        self.assertEqual(h.get("Content-Type"), "application/pdf")
        self.assertIn("_answers.pdf", h.get("Content-Disposition", ""))
        doc = fitz.open(stream=b, filetype="pdf")
        self.assertGreater(doc.page_count, 0)
        doc.close()
        s, h, b = http("POST", "/api/mock/zip", body)
        self.assertEqual(s, 200)
        self.assertEqual(h.get("Content-Type"), "application/zip")
        self.assertEqual(b[:2], b"PK")
        import io
        import zipfile as zf
        z = zf.ZipFile(io.BytesIO(b))
        names = z.namelist()
        self.assertEqual(len(names), 2)
        self.assertTrue(names[1].endswith("_answers.pdf"))

    def test_stale_409_and_bad_id_400(self):
        s, _, b = http("POST", "/api/mock/preview", pv_body(seed="st409"))
        sid = json.loads(b)["selection_id"]
        # 表单已变但携带旧 selection_id → 409
        s, _, b = http("POST", "/api/mock/pdf",
                       dict(pv_body(seed="st409", target=41), selection_id=sid))
        self.assertEqual(s, 409)
        d = json.loads(b)
        self.assertEqual(d["code"], "stale")
        self.assertIn("重新", d["error"])
        # 缺 selection_id / 非法格式 → 400
        for bad in (None, "xyz", "0123456789abc"):
            body = pv_body(seed="st409")
            if bad is not None:
                body["selection_id"] = bad
            s, _, b = http("POST", "/api/mock/pdf", body)
            self.assertEqual(s, 400)
            self.assertEqual(json.loads(b)["code"], "bad-selection-id")
        # answers 端点在 exam_only 模式下 → 400
        s, _, b = http("POST", "/api/mock/preview", pv_body(seed="st409",
                                                            mode="exam_only"))
        sid2 = json.loads(b)["selection_id"]
        s, _, b = http("POST", "/api/mock/answers",
                       dict(pv_body(seed="st409", mode="exam_only"),
                            selection_id=sid2))
        self.assertEqual(s, 400)
        self.assertEqual(json.loads(b)["code"], "no-answers-mode")

    def test_invalid_params_400(self):
        base = {"subject": "9702", "papers": [1], "topics": ["3"],
                "year_from": 2016, "year_to": 2026, "target": 10,
                "mode": "answers"}
        cases = [
            ({"papers": []}, "至少选择一个 Paper"),
            ({"papers": [7]}, "不属于科目"),                    # 跨科编号(9709 的 P7)
            ({"papers": ["1"]}, "整数"),
            ({"papers": [1.5]}, "整数"),
            ({"subject": "9999"}, "没有可用真题数据"),
            ({"subject": "../../9702"}, "4 位科目代码"),        # 穿越
            ({"subject": "9702/../9701"}, "4 位科目代码"),
            ({"topics": []}, "至少选择一个章节"),
            ({"topics": ["99"]}, "知识点编号无效"),
            ({"year_from": 2030, "year_to": 2020}, "起始年份"),
            ({"target": 0}, "正整数"),
            ({"target": 99999}, "过大"),
            ({"target": "40"}, "正整数"),
            ({"mode": "both"}, "mode 应为"),
        ]
        for patch, frag in cases:
            body = dict(base, **patch)
            for ep in ("preview", "pdf"):
                s, _, b = http("POST", f"/api/mock/{ep}", body)
                self.assertEqual(s, 400, f"{ep} {patch} 应 400, got {s}")
                self.assertIn(frag, json.loads(b)["error"], f"{ep} {patch}")

    def test_body_too_large(self):
        s, _, b = http("POST", "/api/mock/preview", b'{"x":"' + b"a" * 70000 + b'"}')
        self.assertIn(s, (400, 413))
        self.assertFalse(json.loads(b).get("ok", False))

    def test_no_solution_400_readable(self):
        p5 = next(it for it in IDX.subjects["9702"]["questions"] if it["paper"] == 5)
        s, _, b = http("POST", "/api/mock/preview", {
            "subject": "9702", "papers": [5], "topics": sorted(set(p5["labels"])),
            "year_from": 2007, "year_to": 2026, "target": 16, "seed": "x",
            "mode": "exam_only"})
        self.assertEqual(s, 400)
        d = json.loads(b)
        self.assertEqual(d["code"], "no-solution")
        self.assertIn("凑出恰好 16", d["error"])

    def test_unknown_api(self):
        s, _, _ = http("POST", "/api/mock/other", {"a": 1})
        self.assertEqual(s, 404)

    # ---- 既有接口回归 ----
    def test_regression_search(self):
        s, _, b = http("GET", "/api/search?q=projectile&subject=9702")
        self.assertEqual(s, 200)
        d = json.loads(b)
        self.assertGreater(len(d["results"]), 0)
        self.assertTrue(all(r["subject"] == "9702" for r in d["results"]))

    def test_regression_unit(self):
        s, _, b = http("GET", "/api/unit?subject=9702&code=w20_42&uid=1a")
        self.assertEqual(s, 200)
        d = json.loads(b)
        self.assertEqual(d["unit"]["id"], "1a")
        self.assertEqual(d["unit"]["marks"], 2)
        self.assertTrue(d["qp_question"]["img"])

    def test_regression_crop(self):
        s, _, b = http("GET", "/api/crop?subject=9702&code=w20_42&uid=1a&kind=qpq")
        self.assertEqual(s, 200)
        self.assertEqual(b[:4], b"\x89PNG")

    def test_regression_static_and_security(self):
        s, _, b = http("GET", "/index.html")
        self.assertEqual(s, 200)
        self.assertIn(b"mock", b)                      # 侧栏含模拟组卷入口
        for path in ("/server.py", "/mock_exam.py", "/data/units/9702/w20_42.json",
                     "/topics_9702.json", "/../../../etc/passwd",
                     "/data/loc/9702/w20_42.json", "/data/msloc/9702/w20_42.json"):
            s, _, _ = http("GET", path)
            self.assertIn(s, (403, 404), f"{path} 不应可访问")
        pdf_path = urllib.parse.quote("/9702（物理）/2020/Oct-Nov/9702_w20_qp_42.pdf")
        s, _, b = http("GET", pdf_path)
        self.assertEqual(s, 200)                       # 原卷 PDF 仍可访问
        self.assertEqual(b[:5], b"%PDF-")


if __name__ == "__main__":
    names = sys.argv[1:]
    loader = unittest.defaultTestLoader
    if names:                                  # 按类名/方法名前缀过滤
        suite = unittest.TestSuite()
        for n in names:
            if "." in n:
                suite.addTests(loader.loadTestsFromName(
                    "test_mock_exam." + n, sys.modules["__main__"]))
            else:
                suite.addTests(loader.loadTestsFromTestCase(
                    globals()[n]) if n in globals() else [])
    else:
        suite = loader.loadTestsFromModule(sys.modules["__main__"])
    unittest.TextTestRunner(verbosity=2).run(suite)
