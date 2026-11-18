#!/usr/bin/env python3
"""mock_exam.py —— 真题模拟组卷核心: 核分索引 / 候选过滤 / 精确总分选题 /
试题 PDF + 同一冻结选题的官方答案 PDF 生成。

被 server.py 导入(后台线程构建索引), 也可独立命令行运行生成样张:
  /usr/bin/python3 mock_exam.py 9702 1,2 2016 2026 40 --topics 7,8 --seed abc

设计要点(对应需求):
  1. 核分不信任 units 里逐单元的 marks(抽取脚本只记单元最后一个 [n], 如
     w20_42 Q2b 含 [2][2] 却记 2)。对整道大题重新核分: 题面明确 [Total: N] 优先;
     否则求和给分标记(处理 [2][2]/多罗马小问); 全卷无任何标记且题数≥20 → MCQ
     每题 1 分。代码数组下标 Data[1]/Mix[4](前邻是字母数字)不算给分标记;
     无 Total 且标记和 >40 或单标记 >15 视为异常(表格行号/题号丢失混入)保守排除。
  2. 最小选题单位 = 完整大题, 用 data/loc 的 questions[q].seg(含共享题干/图表/
     跨页); 定位 fail/fuzzy、缺 seg、原 PDF 缺失的题排除。卷内题号非连续重复
     (重编号/OPTIONS)时按题号的映射无法唯一定位 → 整卷排除。
     **全部裁切段(QP 与 MS)在候选阶段逐段核验**: 页码在页数内、与页面矩形相交
     非空且非碎屑、跨页顺序不回退; 任一段无效该题直接出局(计入排除统计),
     渲染阶段再遇无效段则整份拒绝(render-failed), 绝不静默省略输出残缺试题。
  3. 章节过滤: 候选题的所有已分类标签都必须落在所选小节集合内(多标签全列出);
     存在未分类/OUT 单元的大题整体排除。topics_<code>.json 的 note 随预览返回展示。
  4. 年份含起止; paper 是 1/2/3 试卷类型(非 variant), 多选为并集, 空集/跨科编号
     返回可读错误。精确求和用位掩码后缀 DP + 保可行顺序贪心: 每一步只在"加入后
     剩余候选仍能精确补满"时才选入, 因此只要存在精确子集必然给出, 不会"有解报
     无解"; 复杂度 O(n·T/w)(n=候选数, T=目标分, w=机器字长), 数千候选 T≤200
     在几十毫秒内。随机种子决定抽题顺序, 同参数同种子可复现, 换种子换一套。
     require_each_paper 用**带约束的后缀 DP**(状态=剩余还需覆盖的 Paper 子集 ×
     可达和位掩码), 同样具备完备性 —— 不会把"精确分值且每种 Paper 都出现"的
     有解误报为无解。
  5. 答案(第二轮): 预览时逐题确认官方 MS 可用性 —— 结构题用 msloc.questions[q]
     .seg(ms_status==ok + ms_pdf 存在 + 全段坐标有效), MCQ 用 units[uid].letter
     且与 letters 表交叉一致; "试卷+答案"模式在**选题之前**过滤掉无答案的题,
     无精确解时明确报错。试题与答案 PDF 来自同一冻结选题: 预览返回 selection_id
     (规范化参数+所选题 ID/分值+源文件版本的指纹), 两个下载端点在服务端重算并
     比对指纹, 过期/源文件变化返回 409, 不信任浏览器传来的题目/分值/路径。
"""
import hashlib
import io
import json
import os
import random
import re
import threading
import time
import zipfile

import fitz

ROOT = os.path.dirname(os.path.abspath(__file__))
UNITS_DIR = os.path.join(ROOT, "data", "units")
LOC_DIR = os.path.join(ROOT, "data", "loc")
MSLOC_DIR = os.path.join(ROOT, "data", "msloc")
MAX_TARGET = 200          # 目标总分上限(限制工作量)
MAX_MARKS_TOTAL = 60      # 单题总分合理上限
MAX_MARKS_SUM = 40        # 无 [Total] 时标记求和的保守上限
MAX_MARKER = 15           # 单个给分标记上限([15] 是 9702 P5 规划题, 更大视为异常)
MCQ_MIN_QUESTIONS = 20    # 全卷无任何给分标记且题数≥20 → MCQ 每题 1 分
MAX_REQ_PAPERS = 6        # require_each_paper 约束的 Paper 数上限(2^k 状态空间)
SESSION_ORDER = {"Feb-Mar": 0, "May-Jun": 1, "Oct-Nov": 2}
MODES = ("answers", "exam_only")      # answers=试卷+答案(默认), exam_only=仅试题

MARK_RE = re.compile(r"\[(\d{1,2})\]")
TOTAL_RE = re.compile(r"\[Total:?\s*(\d{1,3})\s*\]", re.I)
# 需要额外器材/附件/上机操作, PDF 无法完整呈现 → 保守排除(检查题面前 600 字符)
MATERIALS_RE = re.compile(
    r"(you are provided|graph paper|the insert|answer booklet|"
    r"copy and paste|save your program)", re.I)
# 仅提示不排除: 题面要求查 Data Booklet(9701)
DATABOOKLET_RE = re.compile(r"data booklet", re.I)

EXCL_ZH = {"outside-topics": "知识点不在所选范围", "unclassified": "未分类/OUT",
           "out-or-pending": "含 OUT/PENDING 单元(最新大纲口径)",
           "no-marks": "无法核分", "marks-anomaly": "分值异常保守排除",
           "needs-materials": "需器材/附件/上机", "no-loc": "题面定位失败",
           "loc-fuzzy": "定位低置信", "loc-fail": "定位失败",
           "renumbered-q": "卷内题号重编号", "missing-pdf": "原卷 PDF 缺失",
           "empty-paper": "空卷/扫描件", "dup-in-paper": "卷内重复",
           "qp-seg-invalid": "题面裁切段坐标无效",
           "ms-missing": "缺 MS 文件", "ms-no-text": "MS 无文字层",
           "ms-no-loc": "MS 无法定位该题答案",
           "ms-seg-invalid": "MS 裁切段坐标无效",
           "ms-letter-missing": "MCQ 官方字母缺失",
           "ms-letter-mismatch": "MCQ 字母与答案表不一致"}


class MockError(Exception):
    """输入无效/无解等可读错误, server 转成 400 JSON。"""

    def __init__(self, message, code="invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


# ============================ 核分 ============================
def mark_markers(text):
    """题面给分标记 [n]; 前邻字母/数字的(数组下标 Data[1]/Mix[4])不算。"""
    out = []
    for m in MARK_RE.finditer(text):
        i = m.start()
        if i > 0 and text[i - 1].isalnum():
            continue
        out.append(int(m.group(1)))
    return out


def group_questions(units):
    """按存储顺序把单元聚成连续同题号的完整大题。"""
    groups = []
    for u in units:
        if groups and groups[-1]["q"] == u["q"]:
            groups[-1]["units"].append(u)
        else:
            groups.append({"q": u["q"], "units": [u]})
    return groups


def has_renumbered_q(groups):
    """题号非连续重复(重编号/OPTIONS) → questions 按题号映射无法唯一定位。"""
    seen, prev = set(), None
    for g in groups:
        if g["q"] != prev:
            if g["q"] in seen:
                return True
            seen.add(g["q"])
            prev = g["q"]
    return False


def rescore_paper(rec):
    """对一整卷重新核分。返回 (questions, paper_excl)。

    questions: [{q, units, marks, basis, labels, unclassified, warn}]
      basis: 'total'([Total: N]) | 'sum'(标记求和) | 'mcq'(每题 1 分)
    paper_excl: 整卷排除原因, 否则 None。
    """
    units = rec.get("units") or []
    if not units:
        return [], "empty-paper"
    groups = group_questions(units)
    if has_renumbered_q(groups):
        return [], "renumbered-q"
    texts = ["\n".join(u["text"] for u in g["units"]) for g in groups]
    any_marks = any(mark_markers(t) or TOTAL_RE.search(t) for t in texts)
    is_mcq = (not any_marks) and len(groups) >= MCQ_MIN_QUESTIONS
    out = []
    for g, text in zip(groups, texts):
        labels, unclassified, out_pending = set(), 0, 0
        discounted = False
        for u in g["units"]:
            st = u.get("status")
            if u.get("discounted"):
                # 官方 MS 标注作废(0 分)的题: 保留原卷, 不进入计分组卷/答案候选
                discounted = True
                continue
            t = u.get("topics") or []
            if st == "IN" and t:
                labels.update(t)
            elif st == "CONTEXT":
                pass                    # 无分值共享材料: 题干上下文, 不阻断大题
            elif st in ("OUT", "PENDING"):
                out_pending += 1        # 最新大纲口径: 任一实质小问 OUT/PENDING
            else:
                unclassified += 1       # 无状态字段(旧数据/未迁移)按未分类处理
        head = " ".join(text[:600].lower().split())
        warn = "data-booklet" if DATABOOKLET_RE.search(head) else None
        mk = mark_markers(text)
        total = TOTAL_RE.search(text)
        marks, basis, bad = None, None, None
        if is_mcq:
            marks, basis = 1, "mcq"
        elif total:
            marks, basis = int(total.group(1)), "total"
        elif mk:
            marks, basis = sum(mk), "sum"
        else:
            bad = "no-marks"
        if not bad and basis != "mcq":
            if any(v > MAX_MARKER for v in mk):
                bad = "marks-anomaly"          # 表格行号/代码下标混入
            elif basis == "sum" and marks > MAX_MARKS_SUM:
                bad = "marks-anomaly"          # 无 Total 而标记和过大 → 题号丢失
        if not bad and not (1 <= marks <= MAX_MARKS_TOTAL):
            bad = "marks-anomaly"
        out.append({"q": g["q"], "units": g["units"], "text": text,
                    "marks": marks, "basis": basis, "bad": bad,
                    "labels": sorted(labels), "unclassified": unclassified,
                    "out_or_pending": out_pending,
                    "discounted": discounted,
                    "warn": warn})
    return out, None


# ============================ 索引 ============================
def _valid_seg(seg):
    """seg: [[page,x0,y0,x1,y1],...] 结构性校验(页码/正矩形)。"""
    if not isinstance(seg, list) or not seg:
        return False
    for s in seg:
        if not (isinstance(s, (list, tuple)) and len(s) == 5):
            return False
        try:
            p, x0, y0, x1, y1 = int(s[0]), *map(float, s[1:5])
        except (TypeError, ValueError):
            return False
        if p < 1 or x1 <= x0 or y1 <= y0 or x0 < 0 or y0 < 0:
            return False
    return True


def _text_key(text):
    """跨 variant 近乎相同题去重键: 题面开头 200 字符规范化。"""
    return re.sub(r"\s+", " ", (text or "")[:200]).strip().lower()


# ============================ 源 PDF 几何缓存(逐段坐标核验用) ============================
class DocGeomCache:
    """rel_path → (页数, 各页矩形)。按 mtime+size 缓存, 多线程安全。
    打开失败返回 None(调用方按"源文件不可用"处理, 绝不静默通过)。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}

    def geom(self, rel_path):
        try:
            st = os.stat(os.path.join(ROOT, rel_path))
        except OSError:
            return None
        key = (rel_path, st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._cache.get(rel_path)
            if hit and hit[0] == key:
                return hit[1]
        try:
            doc = fitz.open(os.path.join(ROOT, rel_path))
            rects = [doc[i].rect for i in range(doc.page_count)]
            doc.close()
        except Exception:
            rects = None
        with self._lock:
            self._cache[rel_path] = (key, rects)
        return rects


def check_segs(seg, rects):
    """全部裁切段逐段核验(候选阶段调用, 渲染前拒绝无效题)。
    返回 None(全部有效) 或 拒绝原因。检查: 段结构、页码范围、与页面矩形
    相交非空且非碎屑(≥4pt)、跨页顺序不回退、段不重复。"""
    if not isinstance(seg, list) or not seg:
        return "empty"
    prev_page = 0
    seen = set()
    for s in seg:
        if not (isinstance(s, (list, tuple)) and len(s) == 5):
            return "bad-shape"
        try:
            p, x0, y0, x1, y1 = int(s[0]), *map(float, s[1:5])
        except (TypeError, ValueError):
            return "bad-num"
        if p < 1 or rects is None or p > len(rects):
            return "page-range"
        if x1 <= x0 or y1 <= y0 or x0 < 0 or y0 < 0:
            return "bad-rect"
        if p < prev_page:
            return "order"
        key = (p, round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2))
        if key in seen:
            return "dup"
        seen.add(key)
        prev_page = p
        r = rects[p - 1]
        ix, iy = min(x1, r.width), min(y1, r.height)
        wx, wy = max(0.0, ix - max(0.0, x0)), max(0.0, iy - max(0.0, y0))
        if wx < 4 or wy < 4:
            return "clipped-away"
    return None


class MockIndex:
    """启动时后台构建: 每科 → 试卷统计 + 重新核分后的完整大题(定位/标签/排除)。"""

    def __init__(self):
        self.subjects = {}       # subj -> {"papers": {paper: info}, "questions": [...]}
        self.topics = {}         # subj -> topics_<code>.json (缓存)
        self.ready = False
        self.error = None
        self.geom = DocGeomCache()
        self.base_root = None    # 版本钉住根(P0-2): units/loc/msloc/topics 同版本

    # ---------- topics ----------
    def load_topics(self, subject):
        tp = self.topics.get(subject)
        if tp is None:
            path = os.path.join(self.base_root or ROOT,
                                f"topics_{subject}.json")
            if not os.path.isfile(path):
                raise MockError(f"科目 {subject} 缺少 topics_{subject}.json")
            with open(path) as f:
                tp = json.load(f)
            self.topics[subject] = tp
        return tp

    def expand_topics(self, subject, codes):
        """章节号展开为小节集合; 未知 code 报可读错误。"""
        tp = self.load_topics(subject)
        sec_subs = {s["code"]: [x["code"] for x in s["subs"]]
                    for s in tp["sections"]}
        all_subs = set()
        for v in sec_subs.values():
            all_subs.update(v)
        out, unknown = set(), []
        for c in codes:
            if c in sec_subs:
                out.update(sec_subs[c])
            elif c in all_subs:
                out.add(c)
            else:
                unknown.append(c)
        if unknown:
            raise MockError(f"知识点编号无效: {', '.join(unknown[:8])}"
                            f"（共 {len(unknown)} 个），请在大纲树中重新选择")
        if not out:
            raise MockError("所选章节下没有小节，无法组卷")
        return out

    # ---------- build ----------
    def build(self, base=None):
        """base: 显式版本根(P0-2, 如 data/pipeline/versions/<vid>) —— units/
        loc/msloc/topics 一律从该根读取, 与搜题索引/静态文件同一可见版本;
        None 时回退模块目录常量(整次构建仍钉住同一路径)。"""
        t0 = time.time()
        self.base_root = base
        try:
            n_q = self._build()
            self.ready = True
            print(f"[mock-index] {len(self.subjects)} subjects, {n_q} questions, "
                  f"{time.time()-t0:.1f}s (base={base or 'root'})")
        except Exception as e:             # 索引失败要让 wait_ready 立刻可见
            self.error = repr(e)
            raise

    def _build(self):
        n_q = 0
        root = self.base_root or ROOT
        units_base = (os.path.join(root, "data", "units") if self.base_root
                      else os.path.realpath(UNITS_DIR))  # 版本钉住: 同版本
        loc_base = (os.path.join(root, "data", "loc") if self.base_root
                    else LOC_DIR)
        msloc_base = (os.path.join(root, "data", "msloc") if self.base_root
                      else MSLOC_DIR)
        for subject in sorted(os.listdir(units_base)):
            sd = os.path.join(units_base, subject)
            if not os.path.isdir(sd):
                continue
            entry = {"papers": {}, "questions": []}
            for f in sorted(os.listdir(sd)):
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(sd, f)) as fh:
                        rec = json.load(fh)
                except Exception:
                    continue
                code = f[:-5]
                paper = rec.get("paper")
                pinfo = entry["papers"].setdefault(
                    paper, {"count": 0, "papers": 0, "excluded": {},
                            "years": [None, None]})
                pinfo["papers"] += 1
                qs, paper_excl = rescore_paper(rec)
                pdf_rel = rec.get("qp_pdf")
                pdf_ok = bool(pdf_rel) and os.path.isfile(
                    os.path.join(ROOT, pdf_rel))
                if paper_excl or not pdf_ok:
                    why = paper_excl or "missing-pdf"
                    pinfo["excluded"][why] = pinfo["excluded"].get(why, 0) + 1
                    continue
                loc = {}
                locp = os.path.join(loc_base, subject, f)
                if os.path.isfile(locp):
                    try:
                        with open(locp) as fh:
                            loc = json.load(fh)
                    except Exception:
                        loc = {}
                qlocs = loc.get("questions") or {}
                msloc = {}
                msp = os.path.join(msloc_base, subject, f)
                if os.path.isfile(msp):
                    try:
                        with open(msp) as fh:
                            msloc = json.load(fh)
                    except Exception:
                        msloc = {}
                ms_pdf = rec.get("ms_pdf")
                ms_pdf_ok = bool(ms_pdf) and os.path.isfile(
                    os.path.join(ROOT, ms_pdf))
                ms_status = msloc.get("ms_status") if ms_pdf_ok else "missing"
                ms_letters = msloc.get("letters") or {}
                ms_qlocs = msloc.get("questions") or {}
                ms_ulocs = msloc.get("units") or {}
                dup_seen = set()
                for qi in qs:
                    if qi["bad"]:
                        pinfo["excluded"][qi["bad"]] = \
                            pinfo["excluded"].get(qi["bad"], 0) + 1
                        continue
                    # 官方作废题(Discounted, 0 分): 不进入计分组卷/答案候选
                    if qi.get("discounted"):
                        pinfo["excluded"]["officially-discounted"] = \
                            pinfo["excluded"].get("officially-discounted", 0) + 1
                        continue
                    # 最新大纲口径: 完整大题中任一实质小问 OUT/PENDING → 整题排除
                    # (CONTEXT 共享材料不阻断)
                    if qi.get("out_or_pending"):
                        pinfo["excluded"]["out-or-pending"] = \
                            pinfo["excluded"].get("out-or-pending", 0) + 1
                        continue
                    if qi["unclassified"] or not qi["labels"]:
                        pinfo["excluded"]["unclassified"] = \
                            pinfo["excluded"].get("unclassified", 0) + 1
                        continue
                    if MATERIALS_RE.search(" ".join(qi["text"][:600].lower().split())):
                        pinfo["excluded"]["needs-materials"] = \
                            pinfo["excluded"].get("needs-materials", 0) + 1
                        continue
                    q = qi["q"]
                    qloc = qlocs.get(str(q))
                    seg = (qloc or {}).get("seg")
                    if not qloc or qloc.get("status") != "exact" or not _valid_seg(seg):
                        why = "no-loc" if (not qloc or not seg) else \
                            "loc-" + str(qloc.get("status"))
                        pinfo["excluded"][why] = pinfo["excluded"].get(why, 0) + 1
                        continue
                    tk = _text_key(qi["units"][0]["text"])
                    dk = (paper, rec.get("session"), q, tk)
                    if dk in dup_seen:
                        pinfo["excluded"]["dup-in-paper"] = \
                            pinfo["excluded"].get("dup-in-paper", 0) + 1
                        continue
                    dup_seen.add(dk)
                    y = rec.get("year")
                    first_uid = qi["units"][0]["id"]
                    mu = ms_ulocs.get(first_uid) or {}
                    entry["questions"].append({
                        "id": f"{code}:Q{q}", "subject": subject, "code": code,
                        "q": q, "marks": qi["marks"], "basis": qi["basis"],
                        "labels": qi["labels"], "warn": qi["warn"],
                        "n_units": len(qi["units"]), "paper": paper,
                        "variant": rec.get("variant"), "year": y,
                        "session": rec.get("session"),
                        "page": qi["units"][0].get("page"),
                        "qp_pdf": rec["qp_pdf"], "seg": seg, "_tk": tk,
                        # 官方 MS 信息(候选阶段按需核验)
                        "ms_pdf": ms_pdf if ms_pdf_ok else None,
                        "ms_status": ms_status,
                        "ms_q_seg": (ms_qlocs.get(str(q)) or {}).get("seg"),
                        "ms_letter": mu.get("letter"),
                        "ms_letter_table": ms_letters.get(str(q)),
                    })
                    pinfo["count"] += 1
                    ys = pinfo["years"]
                    if ys[0] is None or y < ys[0]:
                        ys[0] = y
                    if ys[1] is None or y > ys[1]:
                        ys[1] = y
            # 跨 variant 近乎相同题: 同 (paper,year,session,q) 且题面开头一致 → 只留
            # 排序靠前(低 variant)的一道
            seen, deduped = set(), []
            for it in entry["questions"]:
                k = (it["paper"], it["year"], it["session"], it["q"], it["_tk"])
                if k in seen:
                    continue
                seen.add(k)
                deduped.append(it)
            entry["dup_variant"] = len(entry["questions"]) - len(deduped)
            entry["questions"] = deduped
            self.subjects[subject] = entry
            n_q += len(entry["questions"])
        return n_q

    def wait_ready(self, timeout=120):
        t0 = time.time()
        while not self.ready and time.time() - t0 < timeout:
            if self.error:
                raise MockError("题库索引构建失败: " + self.error, code="index-error")
            time.sleep(0.2)
        if not self.ready:
            raise MockError("题库索引尚未构建完成，请稍后重试", code="not-ready")

    # ---------- meta ----------
    def meta(self, subject):
        """组卷页元数据: 可用 Paper(含候选数/年份范围) + 大纲树 + 适配说明。"""
        self.wait_ready()
        if subject not in self.subjects:
            raise MockError(f"科目 {subject} 没有真题数据（可用: "
                            f"{', '.join(sorted(self.subjects))}）")
        tp = self.load_topics(subject)
        papers = []
        for p, info in sorted(self.subjects[subject]["papers"].items()):
            papers.append({"paper": p, "candidates": info["count"],
                           "source_papers": info["papers"],
                           "years": info["years"]})
        return {"subject": subject, "zh": tp.get("zh") or subject,
                "name": tp.get("name") or subject,
                "papers": papers,
                "topics": {"note": tp.get("note"),
                           "levels": tp.get("levels") or [],
                           "sections": [
                               {"code": s["code"], "en": s.get("en"),
                                "zh": s.get("zh"),
                                "subs": [{"code": x["code"], "en": x.get("en"),
                                          "zh": x.get("zh")} for x in s["subs"]]}
                               for s in tp["sections"]]},
                "limits": {"max_target": MAX_TARGET}}


_IDX = None


def get_index():
    return _IDX


def init_index(background=True):
    """构建全局索引(server 启动时调用)。"""
    global _IDX
    _IDX = MockIndex()
    th = threading.Thread(target=_IDX.build, daemon=True)
    th.start()
    if not background:
        th.join()
    return _IDX


def refresh_index(background=True):
    """发布后重建索引: 新索引构建完成后才原子替换(期间旧索引继续服务)。"""
    def _build_and_swap():
        global _IDX
        new = MockIndex()
        new.build()
        _IDX = new
    th = threading.Thread(target=_build_and_swap, daemon=True)
    th.start()
    if not background:
        th.join()
    return _IDX


# ============================ 参数校验 ============================
def _validate_subject(subject):
    if not isinstance(subject, str) or not re.fullmatch(r"\d{4}", subject or ""):
        raise MockError("subject 应为 4 位科目代码，如 9702")
    return subject


def _validate_papers(idx, subject, papers):
    if not isinstance(papers, list) or not papers:
        raise MockError("至少选择一个 Paper 编号（papers 为非空数组，如 [1,2]）")
    out = []
    for p in papers:
        if isinstance(p, bool) or not isinstance(p, int):
            raise MockError(f"Paper 编号必须是整数，收到: {p!r}"
                            "（1/2/3…是试卷类型，不是 11/21 等 variant）")
        out.append(p)
    if len(set(out)) != len(out):
        raise MockError("Paper 编号重复")
    valid = set(idx.subjects.get(subject, {}).get("papers") or {})
    if not valid:
        raise MockError(f"科目 {subject} 没有可用真题数据")
    bad = [p for p in out if p not in valid]
    if bad:
        raise MockError(f"Paper {', '.join(map(str, bad))} 不属于科目 {subject}"
                        f"（该科可用: {', '.join('P' + str(v) for v in sorted(valid))}）")
    return sorted(set(out))


def validate_params(idx, body):
    """严格校验请求体; 返回规范化参数 dict。任何缺失/越界/跨科值 → MockError。"""
    if not isinstance(body, dict):
        raise MockError("请求体应为 JSON 对象")
    subject = _validate_subject(body.get("subject"))
    idx.wait_ready()
    papers = _validate_papers(idx, subject, body.get("papers"))
    year_from, year_to = body.get("year_from"), body.get("year_to")
    for name, v in (("year_from", year_from), ("year_to", year_to)):
        if isinstance(v, bool) or not isinstance(v, int):
            raise MockError(f"{name} 应为整数年份")
    if year_from < 1990 or year_to > 2100:
        raise MockError("年份超出合理范围 (1990-2100)")
    if year_from > year_to:
        raise MockError("起始年份不能大于结束年份")
    target = body.get("target")
    if isinstance(target, bool) or not isinstance(target, int):
        raise MockError("target 应为正整数目标总分")
    if target < 1:
        raise MockError("目标总分必须为正整数")
    if target > MAX_TARGET:
        raise MockError(f"目标总分过大（上限 {MAX_TARGET} 分）")
    topics_raw = body.get("topics")
    if not isinstance(topics_raw, list) or not topics_raw:
        raise MockError("至少选择一个章节/小节（topics 为非空数组）")
    for t in topics_raw:
        if not isinstance(t, str) or \
                not re.fullmatch(r"\d{1,2}(\.\d{1,3})?", t or ""):
            raise MockError(f"知识点编号格式无效: {t!r}（应为如 \"8\" 或 \"8.4\"）")
    topics = idx.expand_topics(subject, topics_raw)
    seed = body.get("seed")
    if seed is None or seed == "":
        seed = "1"
    seed = str(seed)[:64]
    require_each = bool(body.get("require_each_paper"))
    if require_each and len(papers) < 2:
        require_each = False              # 单 Paper 无意义, 忽略
    if require_each and len(papers) > MAX_REQ_PAPERS:
        raise MockError(f"require_each_paper 最多支持 {MAX_REQ_PAPERS} 种 Paper")
    mode = body.get("mode") or "answers"
    if mode not in MODES:
        raise MockError(f"mode 应为 {' 或 '.join(MODES)}（answers=试卷+答案, "
                        "exam_only=仅试题）, 收到: " + repr(mode))
    return {"subject": subject, "papers": papers, "topics_raw": topics_raw,
            "topics": topics, "year_from": year_from, "year_to": year_to,
            "target": target, "seed": seed, "require_each_paper": require_each,
            "mode": mode}


def _validate_selection_id(body):
    sid = body.get("selection_id")
    if not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{16}", sid or ""):
        raise MockError("缺少有效的 selection_id —— 请先「生成预览」, 再用同一份"
                        "预览下载(服务端将校验选题指纹)", code="bad-selection-id")
    return sid


# ============================ 候选过滤 ============================
def _answer_state(idx, it):
    """判定一题的官方答案可用性(试卷+答案模式)。
    返回 (ok, reason, info): ok=False 时 reason 计入排除统计;
    info = {"kind": "mcq"|"structured", "letter"/"seg", "ms_pdf"}。"""
    st = it.get("ms_status")
    if st == "missing" or not it.get("ms_pdf"):
        return False, "ms-missing", {"kind": None}
    if st != "ok":                         # no_text_layer / no_units / 未知
        return False, "ms-no-text" if st == "no_text_layer" else "ms-no-loc", \
            {"kind": None}
    if it["basis"] == "mcq":
        # MCQ: units[uid].letter 与 letters 表都要有且一致; 不用整行裁切(可能含
        # 其他题号) —— 只在答案 PDF 里输出"新题号→字母"表。
        a, b = it.get("ms_letter"), it.get("ms_letter_table")
        if not a or not re.fullmatch(r"[A-E]", str(a)):
            return False, "ms-letter-missing", {"kind": "mcq"}
        if b is not None and str(b).strip().upper() != str(a).upper():
            return False, "ms-letter-mismatch", {"kind": "mcq"}
        return True, None, {"kind": "mcq", "letter": str(a).upper(),
                            "ms_pdf": it["ms_pdf"]}
    # 结构题: msloc.questions[q].seg = 该题首个行标 → 下一题首个行标, 完整官方
    # 评分标准(含全部小问/跨页), 不会并入下一题答案; 全段坐标逐段核验。
    seg = it.get("ms_q_seg")
    if not seg:
        return False, "ms-no-loc", {"kind": "structured"}
    rects = idx.geom.geom(it["ms_pdf"])
    if rects is None:
        return False, "ms-missing", {"kind": "structured"}
    why = check_segs(seg, rects)
    if why:
        return False, "ms-seg-invalid", {"kind": "structured"}
    return True, None, {"kind": "structured", "seg": seg, "ms_pdf": it["ms_pdf"]}


def build_candidates(idx, params):
    """返回 (candidates, stats, notes, topics_json)。

    试卷+答案模式: 逐题核验 QP 裁切段与官方 MS 可用性(MS 状态/整题定位/字母
    一致性/全部段坐标), 不满足者在**进入选题算法之前**剔除并计入统计。"""
    subject, papers = params["subject"], set(params["papers"])
    topic_set, y0, y1 = params["topics"], params["year_from"], params["year_to"]
    mode = params["mode"]
    entry = idx.subjects[subject]
    tp = idx.load_topics(subject)
    cands = []
    stats = {"excluded": {"outside-topics": 0}, "per_paper": {},
             "mode": mode}
    notes = []
    for p in params["papers"]:
        info = entry["papers"][p]
        stats["per_paper"][str(p)] = {
            "paper": p, "source_papers": info["papers"], "candidates": 0,
            "used": 0, "years": info["years"],
            "excluded_paper": dict(info.get("excluded") or {})}
    qp_geom_cache = {}
    for it in entry["questions"]:
        if it["paper"] not in papers:
            continue
        if not (y0 <= it["year"] <= y1):
            continue
        if not set(it["labels"]) <= topic_set:
            stats["excluded"]["outside-topics"] += 1
            continue
        # QP 全段坐标核验(缓存几何, 避免重复打开同一份卷)
        if it["qp_pdf"] not in qp_geom_cache:
            qp_geom_cache[it["qp_pdf"]] = idx.geom.geom(it["qp_pdf"])
        rects = qp_geom_cache[it["qp_pdf"]]
        why = check_segs(it["seg"], rects)
        if why:
            stats["excluded"]["qp-seg-invalid"] = \
                stats["excluded"].get("qp-seg-invalid", 0) + 1
            continue
        ans_ok, ans_why, ans_info = (True, None, {"kind": None}) \
            if mode == "exam_only" else _answer_state(idx, it)
        if not ans_ok:
            stats["excluded"][ans_why] = stats["excluded"].get(ans_why, 0) + 1
            continue
        c = dict(it)
        c["answer"] = ans_info if mode == "answers" else \
            {"kind": None, "note": "exam_only 模式未核验答案"}
        stats["per_paper"][str(it["paper"])]["candidates"] += 1
        cands.append(c)
    stats["candidates"] = len(cands)
    stats["dup_variant"] = entry.get("dup_variant", 0)
    for p in params["papers"]:
        pp = stats["per_paper"][str(p)]
        yrs = pp["years"]
        if pp["candidates"] == 0:
            if yrs[0] is not None and (yrs[1] < y0 or yrs[0] > y1):
                notes.append(f"Paper {p} 在 {y0}–{y1} 年没有候选题"
                             f"（该 Paper 候选题年份为 {yrs[0]}–{yrs[1]}）")
            else:
                notes.append(f"Paper {p} 在所选知识点/年份/答案要求下没有候选题")
    return cands, stats, notes, tp


# ============================ 精确求和选题 ============================
def _suffix_masks(items, target):
    """suffix[i] 的第 s 位 = 用 items[i:] 能凑出和 s(位掩码 DP, O(n·T/w))。"""
    n = len(items)
    suffix = [0] * (n + 1)
    suffix[n] = 1                       # 和 0 恒可达
    full = (1 << (target + 1)) - 1
    for i in range(n - 1, -1, -1):
        m = items[i]["marks"]
        nxt = suffix[i + 1]
        suffix[i] = nxt | ((nxt << m) & full) if m <= target else nxt
    return suffix


def _constrained_suffix(items, target, req_bits):
    """带 Paper 覆盖约束的后缀 DP(完备, 不靠启发式重试)。

    g[i] = {mask: bitset}: 用 items[i:] 选中若干题可凑出和 s(bit s), 且这批
    选中题**覆盖** mask 中的每一种 Paper(每种至少一题)。倒序转移 —— 选 i 时
    整体覆盖需求 mask = 剩余需求 Q ∪ {p_i}(即需求向后生长):
      g[i][Q]       ⊇ g[i+1][Q]                    (不选 i)
      g[i][Q∪{p_i}] ⊇ g[i+1][Q] << m_i             (选 i, p_i∈req 时)
    基态 g[n] = {∅: bit0}。状态数 n·2^|req|, 转移是 T 位整数位移 —— 数千候选
    + 4 种 Paper 在百毫秒级。可行性 = g[0][full] 含 bit target; 贪心抽取每步
    检查 g[i+1][need_after], 不变量: need 非空的 g 不含 bit0(覆盖非空需求至少
    要 1 分), 故 cur==target 时必然已无剩余需求, 双约束同时达成。"""
    n = len(items)
    full_mask = (1 << len(req_bits)) - 1 if req_bits else 0
    bit_of = {p: 1 << k for k, p in enumerate(req_bits)}
    g = [dict() for _ in range(n + 1)]
    g[n][0] = 1                                  # 无剩余需求, 和 0 可达
    mask_full = (1 << (target + 1)) - 1
    for i in range(n - 1, -1, -1):
        m = items[i]["marks"]
        pbit = bit_of.get(items[i]["paper"], 0)
        nxt, cur = g[i + 1], g[i]
        for mask, bits in nxt.items():
            cur[mask] = cur.get(mask, 0) | bits
            if m <= target:
                nm = mask | pbit   # 选 i: 剩余只需覆盖 Q = mask (p_i 已被 i 覆盖)
                cur[nm] = cur.get(nm, 0) | ((bits << m) & mask_full)
        # 向下闭包: "覆盖 {1,2}" 的选法同时算作 "覆盖 {2}"/"{1}"/"∅" ——
        # 贪心判断 g[k+1][need] 中的 need 是"至少要覆盖"的需求集合
        for mask in range(full_mask, -1, -1):
            bits = cur.get(mask, 0)
            if not bits:
                continue
            mm = mask
            while mm:
                low = mm & -mm
                cur[mask ^ low] = cur.get(mask ^ low, 0) | bits
                mm ^= low
    return g, full_mask


def _diversity_order(cands, seed):
    """多样性顺序: 优先覆盖不同来源卷(年·考季·Paper·variant)与不同知识点。
    每题的排序键 = 来源组内序号 + 知识点组内序号 + 种子抖动(都归一到 0..1),
    同参数同种子可复现, 换种子得到不同的套卷。"""
    rnd = random.Random(f"mock/{seed}")
    n = len(cands)

    def group_ranks(keyfn):
        groups = {}
        for i in range(n):
            groups.setdefault(keyfn(cands[i]), []).append(i)
        keys = list(groups)
        rnd.shuffle(keys)
        rank = {}
        for gi, k in enumerate(keys):
            lst = groups[k]
            rnd.shuffle(lst)
            m = max(1, len(lst) - 1)
            for pos, i in enumerate(lst):
                rank[i] = (gi + pos / m) / max(1, len(keys))
        return rank
    ry = group_ranks(lambda c: (c.get("year"), c.get("session"),
                                        c.get("paper"), c.get("variant")))
    rt = group_ranks(lambda c: c["labels"][0])
    jitter = [rnd.random() * 0.3 for _ in range(n)]
    return sorted(range(n), key=lambda i: ry[i] + rt[i] + jitter[i])


def select_subset(cands, target, seed, require_papers=None):
    """精确总分选题(完备)。只要存在和恰为 target 的子集必然返回一个;
    require_papers 非空时进一步要求"每种所列 Paper 至少一题"—— 同样完备
    (带约束后缀 DP), 不存在满足两者的一组才返回 None。见 _constrained_suffix
    与模块 docstring 的不变量论证。"""
    if not cands or target < 1:
        return None
    require_papers = sorted(set(require_papers or ()))
    if len(require_papers) > MAX_REQ_PAPERS:
        return None
    order = _diversity_order(cands, seed)
    items = [cands[i] for i in order]
    if require_papers:
        # 覆盖约束下的多样性顺序: 每种待覆盖 Paper 的一道候选题前置(优先入选)
        front, rest, covered = [], [], set()
        for i in order:
            c = cands[i]
            if c["paper"] in require_papers and c["paper"] not in covered:
                front.append(i)
                covered.add(c["paper"])
            else:
                rest.append(i)
        order = front + rest
        items = [cands[i] for i in order]
        g, full_mask = _constrained_suffix(items, target, require_papers)
        if not (g[0].get(full_mask, 0) >> target) & 1:
            return None                 # 精确+覆盖 双约束下确定无解
        pick, cur, mask = [], 0, full_mask
        for k, c in enumerate(items):
            m = c["marks"]
            want = target - cur
            if m > want or m <= 0:
                continue
            pbit = 1 << require_papers.index(c["paper"]) \
                if c["paper"] in require_papers else 0
            nm = mask & ~pbit
            if (g[k + 1].get(nm, 0) >> (want - m)) & 1:
                pick.append(k)
                cur += m
                mask = nm
                if cur == target and mask == 0:
                    break
        if not (cur == target and mask == 0):
            return None                 # 理论不可达(不变量被破坏才走到这里)
    else:
        suffix = _suffix_masks(items, target)
        if not (suffix[0] >> target) & 1:
            return None
        pick, cur = [], 0
        for k, c in enumerate(items):
            m = c["marks"]
            want = target - cur
            if m > want or m <= 0:
                continue
            if (suffix[k + 1] >> (want - m)) & 1:
                pick.append(k)
                cur += m
                if cur == target:
                    break
        if cur != target:
            return None
    sel = [items[k] for k in sorted(pick)]
    # 试卷内自然排序: paper → 年 → 考季 → 题号(与分值/覆盖约束无关, 只定顺序)
    sel.sort(key=lambda c: (c["paper"], c["year"],
                            SESSION_ORDER.get(c["session"], 9), c["q"]))
    return sel


# ============================ PDF 生成 ============================
A4_W, A4_H = 595, 842
MARGIN_X, MARGIN_TOP, MARGIN_BOT = 46, 54, 44
FONT_LATIN, FONT_CJK = "helv", "china-s"
INK, MUTED, ACCENT = (0.1, 0.12, 0.16), (0.45, 0.47, 0.52), (0.08, 0.2, 0.45)
LINE = (0.85, 0.87, 0.9)


def _has_cjk(t):
    return any(ord(ch) > 0x2E80 for ch in t)


def _fit_line(text, max_w, size=10.0, font=FONT_LATIN):
    """单行自适应: 用 base-14 精确宽度逐步缩字号, 仍超宽则截断加省略号。
    题头必须纯 ASCII(中文留给 CJK 字体的多行块), 避免全宽拉丁溢出页边。"""
    s = size
    while s > 7.5 and fitz.get_text_length(text, font, s) > max_w:
        s -= 0.5
    if fitz.get_text_length(text, font, s) <= max_w:
        return text, s
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fitz.get_text_length(text[:mid] + "…", font, s) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…", s


def _text_width(t, size, cjk_font=False):
    """保守宽度估算; 需要精确值时用 fitz.get_text_length(仅 base-14)。"""
    w = 0.0
    for ch in t:
        w += _char_w(ch, size, cjk_font)
    return w


def _char_w(ch, size, cjk_font):
    """单字符宽度: china-s 的拉丁字符也是全宽(实测 10pt 字号下每字符 10pt),
    helv 拉丁按 0.52em 保守估计(helv 实测均值 ≈0.46em)。"""
    if ord(ch) > 0x2E80:
        return size
    return size if cjk_font else size * 0.52


def _wrap(text, size, max_w, cjk_font=False):
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if _text_width(cur, size, cjk_font) + _char_w(ch, size, cjk_font) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    lines.append(cur)
    return lines


def _fit_text(page, text, rect, font, size, color=INK):
    """在 rect 内自动缩字号写多行文本(按字体实测宽度换行), 返回消耗的底部 y。"""
    max_w = rect.width
    cjk = font == FONT_CJK
    s = size
    while s >= 6:
        lines = _wrap(text, s, max_w, cjk)
        h = len(lines) * (s * 1.5)
        if h <= rect.height + 0.01:
            y = rect.y0
            for ln in lines:
                if ln:
                    page.insert_text((rect.x0, y + s), ln, fontname=font,
                                     fontsize=s, color=color)
                y += s * 1.5
            return y
        s -= 0.5
    return rect.y0


def _footer(doc, label):
    n = doc.page_count
    for pno in range(n):
        page = doc[pno]
        txt = f"{label} - {pno + 1} / {n}"
        page.insert_text(((A4_W - _text_width(txt, 8.5)) / 2, A4_H - 22), txt,
                         fontname=FONT_LATIN, fontsize=8.5, color=MUTED)


class _Flow:
    """A4 页面顺序排版器: 题头与首段同页(keep-with-next), 逐段矢量嵌入,
    全段有效(候选阶段已核验) —— 渲染层再遇无效段直接抛错, 不静默省略。"""

    def __init__(self, doc):
        self.doc = doc
        self.content_w = A4_W - 2 * MARGIN_X
        self.top = MARGIN_TOP + 8
        self.bot = A4_H - MARGIN_BOT
        self.fresh_h = self.bot - self.top
        self.page = doc.new_page(width=A4_W, height=A4_H)
        self.y = self.top

    def clips_for(self, src_doc, seg, what, normalize_rotation=False):
        """计算并**逐段硬校验**裁切; 任一段页码越界/空矩形 → MockError。"""
        clips = []
        for seg_i, s in enumerate(seg):
            pno, x0, y0, x1, y1 = int(s[0]) - 1, *map(float, s[1:5])
            if pno < 0 or pno >= src_doc.page_count:
                raise MockError(f"{what} 第 {seg_i + 1} 段页码越界"
                                f"({s[0]}/{src_doc.page_count}), 拒绝生成",
                                code="render-failed")
            sp = src_doc[pno]
            if normalize_rotation and sp.rotation:
                sp.remove_rotation()
            clip = fitz.Rect(x0, y0, x1, y1) & sp.rect
            if clip.is_empty or clip.width < 4 or clip.height < 4:
                raise MockError(f"{what} 第 {seg_i + 1} 段裁切区域为空, 拒绝生成",
                                code="render-failed")
            clips.append((pno, clip))
        return clips

    def place(self, src_doc, clips, header):
        """题头 + 逐段顺序排版; 超高段按整页可用高度缩放, 跨页不断尾。"""
        HEADER_H = 22
        sized = []
        for pno, clip in clips:
            w = self.content_w
            h = w * clip.height / clip.width
            maxh = self.fresh_h if sized else self.fresh_h - HEADER_H - 6
            if h > maxh:
                h = maxh
                w = h * clip.width / clip.height
            sized.append((pno, clip, w, h))
        if self.y + HEADER_H + sized[0][3] > self.bot:
            self.page = self.doc.new_page(width=A4_W, height=A4_H)
            self.y = self.top
        self.page.draw_line(fitz.Point(MARGIN_X - 8, self.y - 7),
                            fitz.Point(A4_W - MARGIN_X + 8, self.y - 7),
                            color=LINE, width=0.6)
        htxt, hsize = _fit_line(header, self.content_w)
        self.page.insert_text((MARGIN_X, self.y + 8), htxt,
                              fontname=FONT_LATIN, fontsize=hsize, color=ACCENT)
        self.y += HEADER_H
        for pno, clip, w, h in sized:
            if self.y + h > self.bot:
                self.page = self.doc.new_page(width=A4_W, height=A4_H)
                self.y = self.top
            x = MARGIN_X + (self.content_w - w) / 2
            self.page.show_pdf_page(fitz.Rect(x, self.y, x + w, self.y + h),
                                    src_doc, pno, clip=clip,
                                    keep_proportion=True)
            self.y += h + 10
        self.y += 6


def build_pdf(idx, params, selection):
    """用原卷 PDF 的矢量裁切排版生成模拟卷 PDF, 返回 bytes。"""
    subject = params["subject"]
    tp = idx.load_topics(subject)
    zh = tp.get("zh") or subject
    total = sum(c["marks"] for c in selection)
    doc = fitz.open()
    content_w = A4_W - 2 * MARGIN_X

    # ---- 封面 ----
    cover = doc.new_page(width=A4_W, height=A4_H)
    y = 110
    cover.insert_text((MARGIN_X, y), "真题模拟卷 · Mock Exam", fontname=FONT_CJK,
                      fontsize=25, color=ACCENT)
    y += 30
    cover.insert_text((MARGIN_X, y), f"{zh} {subject} · {tp.get('name') or subject}",
                      fontname=FONT_CJK, fontsize=14, color=MUTED)
    y += 36
    papers_s = " + ".join("Paper " + str(p) for p in params["papers"])
    rows = [
        ("科目 Subject", f"{zh} {subject}"),
        ("Paper 编号 Papers", papers_s),
        ("年份范围 Years", f"{params['year_from']} - {params['year_to']}"),
        ("知识点 Topics", ", ".join(params["topics_raw"])),
        ("总分 Total marks", f"{total} 分（与目标分值精确相等）"),
        ("题数 Questions", f"{len(selection)}"),
        ("随机种子 Seed", params["seed"]),
    ]
    for k, v in rows:
        cover.insert_text((MARGIN_X, y), k, fontname=FONT_CJK, fontsize=10.5,
                          color=MUTED)
        _fit_text(cover, v, fitz.Rect(MARGIN_X + 150, y - 11, A4_W - MARGIN_X,
                                      y + 26), FONT_CJK, 10.5)
        y += 20
    y += 12
    cover.draw_line(fitz.Point(MARGIN_X, y), fitz.Point(A4_W - MARGIN_X, y),
                    color=LINE, width=0.8)
    y += 22
    cover.insert_text((MARGIN_X, y), "试题清单 Question list", fontname=FONT_CJK,
                      fontsize=11.5, color=INK)
    y += 14
    for t, x in (("No.", MARGIN_X), ("Marks", MARGIN_X + 34),
                 ("Source", MARGIN_X + 88), ("Topics", MARGIN_X + 300)):
        cover.insert_text((x, y), t, fontname=FONT_LATIN, fontsize=8.5,
                          color=MUTED)
    y += 6
    cover.draw_line(fitz.Point(MARGIN_X, y), fitz.Point(A4_W - MARGIN_X, y),
                    color=LINE, width=0.6)
    y += 14
    for i, c in enumerate(selection, 1):
        if y > A4_H - 130:
            cover.insert_text((MARGIN_X, y),
                              f"… (共 {len(selection)} 题, 完整来源见末页附录)",
                              fontname=FONT_CJK, fontsize=9, color=MUTED)
            break
        src = (f"{c['year']} {c['session']} P{c['paper']}"
               + (f" v{c['variant']}" if (c["variant"] or 1) > 1 else "")
               + f" Q{c['q']}")
        cover.insert_text((MARGIN_X, y), str(i), fontname=FONT_LATIN, fontsize=9.5)
        cover.insert_text((MARGIN_X + 34, y), str(c["marks"]),
                          fontname=FONT_LATIN, fontsize=9.5)
        cover.insert_text((MARGIN_X + 88, y), src, fontname=FONT_LATIN, fontsize=9.5)
        cover.insert_text((MARGIN_X + 300, y), ", ".join(c["labels"]),
                          fontname=FONT_LATIN, fontsize=9.5)
        y += 14.5
    _fit_text(cover,
              "试题逐题裁自本地真题原卷（矢量排版，含共享题干、图表与跨页内容），"
              "各题来源与原卷文件见末页附录。",
              fitz.Rect(MARGIN_X, A4_H - 66, A4_W - MARGIN_X, A4_H - 34),
              FONT_CJK, 9, color=MUTED)

    # ---- 试题区(每题: 新序号+分值题头, 原卷矢量裁切; 题间顺序排版) ----
    flow = _Flow(doc)
    for i, c in enumerate(selection, 1):
        src_doc = fitz.open(os.path.join(ROOT, c["qp_pdf"]))
        try:
            head = (f"Question {i}    [{c['marks']} marks]    "
                    f"{c['year']} {c['session']} Paper {c['paper']} Q{c['q']}"
                    + (f" (variant {c['variant']})" if (c["variant"] or 1) > 1 else ""))
            clips = flow.clips_for(src_doc, c["seg"], f"题 {c['id']} 题面")
            flow.place(src_doc, clips, head)
        finally:
            src_doc.close()

    # ---- 附录: 来源清单 ----
    ap = doc.new_page(width=A4_W, height=A4_H)
    ap.insert_text((MARGIN_X, 84), "附录 · 试题来源清单 Appendix: Sources",
                   fontname=FONT_CJK, fontsize=14, color=ACCENT)
    y = 108
    for t, x in (("No.", MARGIN_X), ("Marks", MARGIN_X + 34),
                 ("Year", MARGIN_X + 86), ("Session", MARGIN_X + 118),
                 ("Paper", MARGIN_X + 190), ("Q", MARGIN_X + 230),
                 ("Topics", MARGIN_X + 252), ("Source PDF", MARGIN_X + 348)):
        ap.insert_text((x, y), t, fontname=FONT_LATIN, fontsize=8.5, color=MUTED)
    y += 6
    ap.draw_line(fitz.Point(MARGIN_X, y), fitz.Point(A4_W - MARGIN_X, y),
                 color=LINE, width=0.6)
    y += 15
    for i, c in enumerate(selection, 1):
        if y > A4_H - 56:
            ap = doc.new_page(width=A4_W, height=A4_H)
            y = MARGIN_TOP + 8
        row = [(str(i), MARGIN_X), (str(c["marks"]), MARGIN_X + 34),
               (str(c["year"]), MARGIN_X + 86), (c["session"], MARGIN_X + 118),
               (f"P{c['paper']}v{c['variant']}", MARGIN_X + 190),
               (str(c["q"]), MARGIN_X + 230),
               (", ".join(c["labels"]), MARGIN_X + 252)]
        for t, x in row:
            ap.insert_text((x, y), t, fontname=FONT_LATIN, fontsize=9)
        name = os.path.basename(c["qp_pdf"])
        maxw = A4_W - MARGIN_X - 348
        while name and _text_width(name + "…", 9) > maxw:
            name = name[:-1]
        ap.insert_text((MARGIN_X + 348, y), name + ("…" if len(name) <
                       len(os.path.basename(c["qp_pdf"])) else ""),
                       fontname=FONT_LATIN, fontsize=9)
        y += 14.5
    y += 8
    _fit_text(ap, f"合计 {total} 分 · {len(selection)} 题 · 题目裁自上述原卷 PDF。",
              fitz.Rect(MARGIN_X, y, A4_W - MARGIN_X, y + 40), FONT_CJK, 9,
              color=MUTED)

    _footer(doc, f"Mock Exam {subject} "
                 f"P{'+'.join(map(str, params['papers']))} m{total}")
    doc.set_metadata({
        "title": (f"Mock exam {subject} papers "
                  f"{'+'.join(map(str, params['papers']))} "
                  f"{params['year_from']}-{params['year_to']} {total} marks"),
        "producer": "zhenjuan mock_exam", "creator": "server.py"})
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data
# ============================ 答案 PDF ============================
def answers_pdf_filename(params, total):
    """与试题一致的文件名主干 + answers 后缀。"""
    return pdf_filename(params, total)[:-4] + "_answers.pdf"


def _draw_mcq_table(flow, start_no, items):
    """MCQ 官方答案紧凑表: 新题号 → 官方字母(每行 5 对)。items = [(no, letter)]。"""
    COLS = 5
    col_w = (flow.content_w) / COLS
    head = (f"Questions {items[0][0]}-{items[-1][0]}    MCQ Official Key"
            f"    [{len(items)} x 1 mark]")
    if flow.y + 60 > flow.bot:
        flow.page = flow.doc.new_page(width=A4_W, height=A4_H)
        flow.y = flow.top
    flow.page.draw_line(fitz.Point(MARGIN_X - 8, flow.y - 7),
                        fitz.Point(A4_W - MARGIN_X + 8, flow.y - 7),
                        color=LINE, width=0.6)
    htxt, hsize = _fit_line(head, flow.content_w)
    flow.page.insert_text((MARGIN_X, flow.y + 8), htxt,
                          fontname=FONT_LATIN, fontsize=hsize, color=ACCENT)
    flow.y += 22
    rows = (len(items) + COLS - 1) // COLS
    row_h = 22
    y0 = flow.y
    for r in range(rows):
        if flow.y + row_h > flow.bot:
            flow.page = flow.doc.new_page(width=A4_W, height=A4_H)
            flow.y = flow.top
            y0 = flow.y
        for c in range(COLS):
            k = r * COLS + c
            if k >= len(items):
                break
            no, letter = items[k]
            x = MARGIN_X + c * col_w
            flow.page.draw_rect(fitz.Rect(x, flow.y, x + col_w, flow.y + row_h),
                                color=LINE, width=0.5)
            flow.page.insert_text((x + 10, flow.y + 15),
                                  f"Q{no}", fontname=FONT_LATIN,
                                  fontsize=10.5, color=INK)
            flow.page.insert_text((x + col_w - 34, flow.y + 15), letter,
                                  fontname=FONT_LATIN, fontsize=12,
                                  color=(0.08, 0.45, 0.2))
        flow.y += row_h
    flow.y += 8


MS_HDR_ROW_RE = re.compile(r"(Question\s+Answer(\s+Marks)?(\s+Guidance)?"
                            r"|Question\s+Answer)", re.I)
MS_TAIL_NOISE_RE = re.compile(
    r"(Question\s+Answer(\s+Marks)?(\s+Guidance)?"
    r"|©?\s*Cambridge\s+University\s+Press[^,]*,?\s*\d{4}(\s+Page\s+\d+\s+of\s+\d+)?"
    r"|©\s*UCLES.*"
    r"|Page\s+\d+\s+of\s+\d+"
    r"|PUBLISHED)", re.I)


def _trim_ms_tails(src_doc, clips, max_trim=60.0):
    """清理 MS 裁切段尾部的非内容带: 并入的下一源页空表头行
    ("Question Answer Marks/Guidance")、源页页脚(© Cambridge/UCLES、
    Page N of M、PUBLISHED)与纯空白带。逐 14pt 带从底回收(总高≤max_trim),
    只在带内文字完整命中噪声模式或为空时回收 —— 正文评分行不会被截掉;
    裁完高度 <12pt 的残片段整段丢弃。"""
    out = []
    for pno, clip in clips:
        page = src_doc[pno]
        c = fitz.Rect(clip)
        trimmed = 0.0
        while c.height > 10 and trimmed < max_trim:
            band_h = min(14.0, max_trim - trimmed)
            band = fitz.Rect(c.x0, c.y1 - band_h, c.x1, c.y1)
            txt = " ".join(page.get_text(clip=band).split())
            if not txt or (txt and MS_TAIL_NOISE_RE.fullmatch(txt)):
                c.y1 -= band_h
                trimmed += band_h
            else:
                break
        if c.height >= 12:
            out.append((pno, c))
    return out


def build_answers_pdf(idx, params, selection):
    """同一冻结选题的官方答案 PDF: 结构题 = msloc.questions[q].seg 完整官方
    评分标准(矢量, 含全部小问与跨页, MS 旋转页已归一); MCQ = 新题号→官方字母
    紧凑表(不裁整行, 避免混入其他题号)。封面/题头/附录与试题卷一一对应。"""
    subject = params["subject"]
    tp = idx.load_topics(subject)
    zh = tp.get("zh") or subject
    total = sum(c["marks"] for c in selection)
    doc = fitz.open()
    content_w = A4_W - 2 * MARGIN_X

    # ---- 封面 ----
    cover = doc.new_page(width=A4_W, height=A4_H)
    y = 110
    cover.insert_text((MARGIN_X, y), "真题模拟卷 · 官方答案 Answers",
                      fontname=FONT_CJK, fontsize=24, color=(0.08, 0.35, 0.24))
    y += 30
    cover.insert_text((MARGIN_X, y), f"{zh} {subject} · {tp.get('name') or subject}",
                      fontname=FONT_CJK, fontsize=14, color=MUTED)
    y += 36
    n_mcq = sum(1 for c in selection if c["answer"]["kind"] == "mcq")
    n_struct = len(selection) - n_mcq
    rows = [
        ("科目 Subject", f"{zh} {subject}"),
        ("Paper 编号 Papers", " + ".join("Paper " + str(p) for p in params["papers"])),
        ("年份范围 Years", f"{params['year_from']} - {params['year_to']}"),
        ("知识点 Topics", ", ".join(params["topics_raw"])),
        ("总分 Total marks", f"{total} 分（与试卷精确一致）"),
        ("题数 Questions", f"{len(selection)}（MCQ {n_mcq} · 结构题 {n_struct}）"),
        ("选题指纹 selection_id", params["_selection_id"]),
    ]
    for k, v in rows:
        cover.insert_text((MARGIN_X, y), k, fontname=FONT_CJK, fontsize=10.5,
                          color=MUTED)
        _fit_text(cover, v, fitz.Rect(MARGIN_X + 175, y - 11, A4_W - MARGIN_X,
                                      y + 26), FONT_CJK, 10.5)
        y += 20
    y += 12
    cover.draw_line(fitz.Point(MARGIN_X, y), fitz.Point(A4_W - MARGIN_X, y),
                    color=LINE, width=0.8)
    y += 22
    cover.insert_text((MARGIN_X, y), "答案清单 Answer list", fontname=FONT_CJK,
                      fontsize=11.5, color=INK)
    y += 14
    for t, x in (("No.", MARGIN_X), ("Marks", MARGIN_X + 34),
                 ("Answer", MARGIN_X + 88), ("Source", MARGIN_X + 190)):
        cover.insert_text((x, y), t, fontname=FONT_LATIN, fontsize=8.5, color=MUTED)
    y += 14
    for i, c in enumerate(selection, 1):
        if y > A4_H - 120:
            cover.insert_text((MARGIN_X, y),
                              f"… (共 {len(selection)} 题, 完整来源见末页附录)",
                              fontname=FONT_CJK, fontsize=9, color=MUTED)
            break
        ans = c["answer"]
        a_txt = "MCQ " + ans["letter"] if ans["kind"] == "mcq" else "官方评分标准"
        src = (f"{c['year']} {c['session']} P{c['paper']} Q{c['q']} · "
               + os.path.basename(ans.get("ms_pdf") or ""))
        cover.insert_text((MARGIN_X, y), str(i), fontname=FONT_LATIN, fontsize=9.5)
        cover.insert_text((MARGIN_X + 34, y), str(c["marks"]),
                          fontname=FONT_LATIN, fontsize=9.5)
        cover.insert_text((MARGIN_X + 88, y), a_txt, fontname=FONT_CJK, fontsize=9.5)
        if _text_width(src, 9) > A4_W - MARGIN_X - 190:
            while src and _text_width(src + "…", 9) > A4_W - MARGIN_X - 190:
                src = src[:-1]
            src += "…"
        cover.insert_text((MARGIN_X + 190, y), src, fontname=FONT_LATIN, fontsize=9.5)
        y += 14.5
    _fit_text(cover, "答案裁自各来源卷官方评分标准（矢量排版）。本答案卷与试题卷"
              "来自同一冻结选题，指纹见上表；仅试题模式不生成此卷。",
              fitz.Rect(MARGIN_X, A4_H - 66, A4_W - MARGIN_X, A4_H - 34),
              FONT_CJK, 9, color=MUTED)

    # ---- 答案区(按试题顺序: 连续 MCQ 合并为紧凑答案表, 结构题逐题裁 MS) ----
    flow = _Flow(doc)
    i = 0
    n = len(selection)
    while i < n:
        c = selection[i]
        if c["answer"]["kind"] == "mcq":
            j = i
            block = []
            while j < n and selection[j]["answer"]["kind"] == "mcq":
                block.append((j + 1, selection[j]["answer"]["letter"]))
                j += 1
            _draw_mcq_table(flow, i + 1, block)
            i = j
            continue
        src_doc = fitz.open(os.path.join(ROOT, c["answer"]["ms_pdf"]))
        try:
            head = (f"Question {i + 1}    [{c['marks']} marks]    "
                    f"Official Mark Scheme    {c['year']} {c['session']} "
                    f"Paper {c['paper']} Q{c['q']}"
                    + (f" v{c['variant']}" if (c["variant"] or 1) > 1 else ""))
            clips = flow.clips_for(src_doc, c["answer"]["seg"],
                                   f"answer of {c['id']}", normalize_rotation=True)
            clips = _trim_ms_tails(src_doc, clips)
            if not clips:
                raise MockError(f"题 {c['id']} 的答案区域裁切后为空, 拒绝生成",
                                code="render-failed")
            flow.place(src_doc, clips, head)
        finally:
            src_doc.close()
        i += 1

    # ---- 附录: QP/MS 来源 ----
    ap = doc.new_page(width=A4_W, height=A4_H)
    ap.insert_text((MARGIN_X, 84), "附录 · 试题与答案来源 Appendix: Sources",
                   fontname=FONT_CJK, fontsize=14, color=ACCENT)
    y = 108
    for t, x in (("No.", MARGIN_X), ("Marks", MARGIN_X + 34), ("Year", MARGIN_X + 86),
                 ("Session", MARGIN_X + 118), ("Paper", MARGIN_X + 190),
                 ("Q", MARGIN_X + 230), ("Question PDF", MARGIN_X + 252),
                 ("Mark Scheme PDF", MARGIN_X + 400)):
        ap.insert_text((x, y), t, fontname=FONT_LATIN, fontsize=8.5, color=MUTED)
    y += 6
    ap.draw_line(fitz.Point(MARGIN_X, y), fitz.Point(A4_W - MARGIN_X, y),
                 color=LINE, width=0.6)
    y += 15

    def _short(name, maxw):
        while name and _text_width(name + "…", 8.5) > maxw:
            name = name[:-1]
        return name + ("…" if len(name) < len(os.path.basename("x")) else "")

    for i, c in enumerate(selection, 1):
        if y > A4_H - 56:
            ap = doc.new_page(width=A4_W, height=A4_H)
            y = MARGIN_TOP + 8
        qp = os.path.basename(c["qp_pdf"])
        ms = os.path.basename(c["answer"].get("ms_pdf") or "") or "-"
        for nm, x, mw in ((qp, MARGIN_X + 252, 140), (ms, MARGIN_X + 400, 150)):
            full = nm
            while nm and _text_width(nm + "…", 8.5) > mw:
                nm = nm[:-1]
            ap.insert_text((x, y), nm + ("…" if nm != full else ""),
                           fontname=FONT_LATIN, fontsize=8.5)
        row = [(str(i), MARGIN_X), (str(c["marks"]), MARGIN_X + 34),
               (str(c["year"]), MARGIN_X + 86), (c["session"], MARGIN_X + 118),
               (f"P{c['paper']}v{c['variant']}", MARGIN_X + 190),
               (str(c["q"]), MARGIN_X + 230)]
        for t, x in row:
            ap.insert_text((x, y), t, fontname=FONT_LATIN, fontsize=9)
        y += 14.5
    y += 8
    _fit_text(ap, f"合计 {total} 分 · {len(selection)} 题 · 选题指纹 "
              f"{params['_selection_id']} · 题目与答案分别裁自上列原卷。",
              fitz.Rect(MARGIN_X, y, A4_W - MARGIN_X, y + 40), FONT_CJK, 9,
              color=MUTED)

    _footer(doc, f"Mock Exam Answers {subject} "
                 f"P{'+'.join(map(str, params['papers']))} m{total}")
    doc.set_metadata({
        "title": (f"Mock exam answers {subject} papers "
                  f"{'+'.join(map(str, params['papers']))} "
                  f"{params['year_from']}-{params['year_to']} {total} marks "
                  f"sel {params['_selection_id']}"),
        "producer": "zhenjuan mock_exam", "creator": "server.py"})
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


# ============================ 对外入口 ============================
def pdf_filename(params, total):
    seed = re.sub(r"[^0-9A-Za-z_-]", "", params["seed"])[:16] or "s"
    return (f"mock_{params['subject']}_p{'-'.join(map(str, params['papers']))}_"
            f"{params['year_from']}-{params['year_to']}_m{total}_{seed}.pdf")


def _fmt_excl(excl):
    if not excl:
        return "无"
    return ", ".join(f"{EXCL_ZH.get(k, k)} {v}" for k, v in sorted(excl.items()))


def _file_versions(selection):
    """选题涉及的源文件版本(路径/mtime/size), 排序去重 —— 进入选题指纹。"""
    rels = sorted({c["qp_pdf"] for c in selection} |
                  {c["answer"]["ms_pdf"] for c in selection
                   if c.get("answer", {}).get("ms_pdf")})
    out = []
    for rel in rels:
        try:
            st = os.stat(os.path.join(ROOT, rel))
            out.append([rel, st.st_mtime_ns, st.st_size])
        except OSError:
            out.append([rel, "missing"])
    return out


def selection_fingerprint(params, selection):
    """冻结选题指纹: 规范化参数 + 所选题 ID/分值/核分依据 + 源文件版本。"""
    canon = {
        "v": 2,
        "subject": params["subject"], "papers": params["papers"],
        "topics": sorted(set(params["topics_raw"])),
        "year_from": params["year_from"], "year_to": params["year_to"],
        "target": params["target"], "seed": params["seed"],
        "require_each_paper": params["require_each_paper"],
        "mode": params["mode"],
        "sel": [[c["id"], c["marks"], c["basis"]] for c in selection],
        "files": _file_versions(selection),
    }
    h = hashlib.sha1()
    h.update(json.dumps(canon, sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()[:16]


def _solve(idx, body):
    """校验 → 候选(含答案过滤与全段核验) → 精确选题 → 指纹。
    返回 (params, selection, stats, notes, tp, selection_id)。"""
    params = validate_params(idx, body)
    cands, stats, notes, tp = build_candidates(idx, params)
    if not cands:
        raise MockError(
            f"没有可用候选题（{params['year_from']}–{params['year_to']} 年 · "
            f"Paper {'/'.join(map(str, params['papers']))} · 所选知识点"
            + (" · 试卷+答案模式已剔除无官方答案的题" if params["mode"] == "answers"
               else "") + f"）。已排除: "
            f"{_fmt_excl({k: v for k, v in stats['excluded'].items() if v})}",
            code="no-candidates")
    req = params["papers"] if params["require_each_paper"] else None
    selection = select_subset(cands, params["target"], params["seed"],
                              require_papers=req)
    if selection is None:
        sums = sorted({c["marks"] for c in cands})
        raise MockError(
            f"无法从 {len(cands)} 道候选题中凑出恰好 {params['target']} 分"
            + ("且每种所选 Paper 至少一题" if req else "")
            + ("（试卷+答案模式下仅含有官方答案的题；可切换「仅试题」模式或"
               "调整选项后重试）" if params["mode"] == "answers" else "")
            + f"。候选单题分值: {', '.join(map(str, sums[:18]))}"
            + ("…" if len(sums) > 18 else "")
            + "。请调整目标总分、年份范围或知识点。", code="no-solution")
    total = sum(c["marks"] for c in selection)
    if total != params["target"]:        # 不变量: 恰好等于目标
        raise MockError("内部错误: 选题总分不等于目标分值，已拒绝生成",
                        code="invariant")
    if req and not {c["paper"] for c in selection} >= set(req):
        raise MockError("内部错误: Paper 覆盖约束未满足，已拒绝生成",
                        code="invariant")
    sid = selection_fingerprint(params, selection)
    params["_selection_id"] = sid
    return params, selection, stats, notes, tp, sid


def run_preview(idx, body):
    """预览: 服务端核分/答案核验/精确选题, 返回逐题清单+冻结指纹。不生成 PDF。"""
    params, selection, stats, notes, tp, sid = _solve(idx, body)
    total = sum(c["marks"] for c in selection)
    for c in selection:
        stats["per_paper"][str(c["paper"])]["used"] += 1
    n_ans = sum(1 for c in selection if c["answer"].get("kind"))
    return {
        "ok": True,
        "selection_id": sid,
        "request": {"subject": params["subject"], "papers": params["papers"],
                    "topics": params["topics_raw"],
                    "year_from": params["year_from"], "year_to": params["year_to"],
                    "target": params["target"], "seed": params["seed"],
                    "require_each_paper": params["require_each_paper"],
                    "mode": params["mode"]},
        "total": total,
        "filename": pdf_filename(params, total),
        "answers_filename": answers_pdf_filename(params, total),
        "zip_filename": pdf_filename(params, total)[:-4] + ".zip",
        "answers_covered": (n_ans if params["mode"] == "answers" else None),
        "questions": [{
            "no": i, "id": c["id"], "subject": c["subject"],
            "code": c["code"], "q": c["q"],
            "marks": c["marks"], "basis": c["basis"], "labels": c["labels"],
            "warn": c["warn"], "n_units": c["n_units"], "year": c["year"],
            "session": c["session"], "paper": c["paper"],
            "variant": c["variant"], "page": c["page"], "qp_pdf": c["qp_pdf"],
            "answer": {"kind": c["answer"].get("kind"),
                       "letter": c["answer"].get("letter"),
                       "ms_pdf": os.path.basename(c["answer"]["ms_pdf"])
                       if c["answer"].get("ms_pdf") else None},
        } for i, c in enumerate(selection, 1)],
        "stats": stats,
        "notes": notes,
        "topics_note": tp.get("note"),
    }


def _resolve_frozen(idx, body):
    """下载入口公共部分: 服务端重验参数并**重新**核分选题(绝不信任浏览器传来的
    题目/分值/路径), 重算指纹并与请求的 selection_id 比对 —— 表单已变、数据或
    源文件已变化都会得到可读的 409。"""
    validate_params(idx, body)            # 参数错误优先于指纹错误(可读性)
    sid = _validate_selection_id(body)
    params, selection, stats, notes, tp, sid2 = _solve(idx, body)
    if sid != sid2:
        raise MockError("预览已过期：选题条件或源文件已变化，与确认的 selection_id "
                        "不一致。请重新「生成预览」后再下载。", code="stale")
    return params, selection


def run_qp_pdf(idx, body):
    """试题 PDF 下载(冻结选题)。返回 (filename, bytes)。"""
    params, selection = _resolve_frozen(idx, body)
    total = sum(c["marks"] for c in selection)
    return pdf_filename(params, total), build_pdf(idx, params, selection)


def run_answers_pdf(idx, body):
    """官方答案 PDF 下载(同一冻结选题)。仅 answers 模式可下载。"""
    params, selection = _resolve_frozen(idx, body)
    if params["mode"] != "answers":
        raise MockError("仅试题模式不生成答案；请切换「试卷+答案」模式重新预览",
                        code="no-answers-mode")
    total = sum(c["marks"] for c in selection)
    return answers_pdf_filename(params, total), build_answers_pdf(idx, params,
                                                                   selection)


def run_zip(idx, body):
    """一键打包试题 + 答案两份 PDF(可选便利入口; 两个独立下载仍是主要方式)。"""
    fn1, qp = run_qp_pdf(idx, body)
    fn2, ans = run_answers_pdf(idx, body)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(fn1, qp)
        z.writestr(fn2, ans)
    return fn1[:-4] + ".zip", buf.getvalue()


# ---------------- CLI(测试/样张) ----------------
def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="生成一份真题模拟卷(试题+答案) PDF")
    ap.add_argument("subject")
    ap.add_argument("papers", help="逗号分隔, 如 1,2")
    ap.add_argument("year_from", type=int)
    ap.add_argument("year_to", type=int)
    ap.add_argument("target", type=int)
    ap.add_argument("--topics", required=True, help="逗号分隔章节/小节, 如 7,8.4")
    ap.add_argument("--seed", default="cli")
    ap.add_argument("--out", default="", help="输出前缀(默认用规范文件名)")
    ap.add_argument("--require-each", action="store_true")
    ap.add_argument("--exam-only", action="store_true")
    a = ap.parse_args()
    idx = MockIndex()
    idx.build()
    body = {"subject": a.subject, "papers": [int(x) for x in a.papers.split(",")],
            "topics": [t for t in a.topics.split(",") if t],
            "year_from": a.year_from, "year_to": a.year_to, "target": a.target,
            "seed": a.seed, "require_each_paper": a.require_each,
            "mode": "exam_only" if a.exam_only else "answers"}
    pv = run_preview(idx, body)
    body["selection_id"] = pv["selection_id"]
    fn1, qp = run_qp_pdf(idx, body)
    out1 = (a.out + ".pdf") if a.out else fn1
    with open(out1, "wb") as f:
        f.write(qp)
    print(f"total={pv['total']} questions={len(pv['questions'])} "
          f"answers={pv['answers_covered']}/{len(pv['questions'])} "
          f"sel={pv['selection_id']} -> {out1}")
    if pv["answers_covered"] is not None:
        fn2, ans = run_answers_pdf(idx, body)
        out2 = (a.out + "_answers.pdf") if a.out else fn2
        with open(out2, "wb") as f:
            f.write(ans)
        print(f"answers -> {out2}")
    for q in pv["questions"]:
        ans = q["answer"]
        a_txt = (ans["letter"] or "") if ans["kind"] == "mcq" else \
            ("MS" if ans["kind"] else "-")
        print(f"  Q{q['no']:>3} {q['marks']:>3}m ({q['basis']:<5}) "
              f"{q['year']} {q['session']:<8} P{q['paper']} Q{q['q']:<3} "
              f"{','.join(q['labels'])} ans={a_txt}")


if __name__ == "__main__":
    _cli()
