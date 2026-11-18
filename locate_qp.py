#!/usr/bin/env python3
"""locate_qp.py —— 为既有题目单元在 QP 文字层定位页内裁切坐标。

输入: data/units/<科目>/<卷代码>.json（persist_units.py 产物, 白名单单元）
输出: data/loc/<科目>/<卷代码>.json
  {"units":     {<unit_id>: {"status": "exact|fuzzy|fail", "seg": [[page,x0,y0,x1,y1],...]}},
   "questions": {"<q>":     {"status": "exact|fuzzy|fail", "seg": [...]}}}   # 大题级(含共享题干)

定位方法（只补展示位置, 不重新切题）:
  复用 extract_questions.py 的行提取与过滤(逐字同步), 得到每页带 PDF 坐标的行序列;
  按存储单元顺序游标推进, 用单元题面首行在页面上做 精确→后缀→模糊 匹配:
    - 题号/小问号在存储文本中已被剥掉, 匹配的是标签后的正文
      ("12 What is…" 行 命中存储文本 "What is…")
    - 命中行的上一行若是同题号裸数字行(左题号列), 起点上扩使裁图含题号
  单元终点 = 下一单元起点(同页截断)或该页最后一行 → 跨页题自动拆成多段 seg;
  大题级 seg = 该大题首单元起点 → 下一大题首单元起点(小问共享题干自动包含);
  卷尾单元截到 "END OF PAPER" 行或最后一页有内容的行。
状态: exact=整行/后缀/包含命中; fuzzy=词元重合≥0.75(界面会标注低置信); fail=未命中。

用法: /usr/bin/python3 locate_qp.py [科目...]     # 默认全部已切科目(动态发现)
      /usr/bin/python3 locate_qp.py 9709 --papers w25_12   # 只算指定卷(增量)
"""
import json, os, re, sys

import fitz

ROOT = os.path.dirname(os.path.abspath(__file__))


def discover_subjects():
    d = os.path.join(ROOT, "data", "units")
    if not os.path.isdir(d):
        return []
    return sorted(s for s in os.listdir(d)
                  if os.path.isdir(os.path.join(d, s)) and s.isdigit())

# ============ 行提取: 与 extract_questions.py 保持逐字同步(改动须双向同步) ============
NOISE_RES = [
    re.compile(r"^\s*\[?Turn over", re.I),
    re.compile(r"^\s*©?\s*UCLES"),
    re.compile(r"^\s*©?\s*Cambridge University Press"),
    re.compile(r"^\s*PapaCambridge", re.I),
    re.compile(r"^\s*BLANK PAGE\s*$", re.I),
]
END_RE = re.compile(r"^\s*(END\s+OF\s+(?:THE\s+)?PAPER|©\s*UCLES)", re.I)


def page_lines(page, subject):
    """行级提取(过滤同 extract_questions.page_lines), 额外保留每行 PDF bbox。
    返回 [(x_norm, text, plain, bbox_pdf)] 按 (y桶, x) 排序。"""
    d = page.get_text("dict")
    s = page.rect.width / 595.0
    keep = []
    for blk in d["blocks"]:
        if blk.get("type") != 0:
            continue
        bx0 = blk["bbox"][0]
        if bx0 < 45 * s or bx0 > 560 * s:
            continue
        for ln in blk["lines"]:
            x, y0 = ln["bbox"][0] / s, ln["bbox"][1] / s
            if y0 < 48 or y0 > 782:
                continue
            if y0 < 58 and x > 150:
                continue
            txt = "".join(sp["text"] for sp in ln["spans"])
            txt = txt.replace("\x07", " ").replace("\x08", " ").replace("\xa0", " ").strip()
            if not txt:
                continue
            if x > 500 and txt in ("For", "Examiner’s", "Examiner's", "Use",
                                   "for", "examiner’s"):
                continue
            if re.search(r"\*\s*00008\d+\s*\*", txt):
                continue
            odd = sum(1 for c in txt if ord(c) > 0x250 or not c.isprintable())
            if len(txt) > 10 and odd > 0.3 * len(txt):
                continue
            if any(r.search(txt) for r in NOISE_RES):
                continue
            if re.match(rf"^\s*{subject}/\d", txt):
                continue
            plain = bool(ln["spans"]) and all(
                "bold" not in sp["font"].lower() and re.search(r"arial|helvetica|times", sp["font"], re.I)
                for sp in ln["spans"])
            y = ln["spans"][0]["origin"][1] if ln["spans"] else y0
            keep.append((y, x, txt, plain, ln["bbox"]))
    keep.sort(key=lambda r: (round(r[0] / 4.0), r[1]))
    return [(x, t, pl, bb) for _, x, t, pl, bb in keep]

# ============================== 匹配与游标 ==============================
X0_N, X1_N = 38.0, 560.0        # 裁切横向范围(归一坐标, 覆盖图表)


def _norm(s):
    return " ".join(s.split()).casefold()


def _toks(s):
    return re.findall(r"[a-z0-9]+", _norm(s))


def match_lines(first_line, cand, truncated=False):
    """存储单元首行 vs 候选行 [(x, txt)]。返回 (k, status) 或 None。"""
    ft = _norm(first_line)
    if not ft:
        return None
    ft_toks = _toks(first_line)
    n_need = max(2, int(len(ft_toks) * 0.75 + 0.5))
    best = None
    for i, (x, txt) in enumerate(cand):
        lt = _norm(txt)
        if not lt:
            continue
        if len(ft) >= 8 and (lt == ft or lt.endswith(ft) or (" " + ft) in lt):
            return i, "exact"
        if truncated and len(ft) >= 12 and ft[:60] in lt:
            return i, "exact"
        if ft_toks:
            lt_toks = _toks(txt)
            tail = lt_toks[-len(ft_toks):] if len(lt_toks) >= len(ft_toks) else lt_toks
            inter = len(set(tail) & set(ft_toks))
            if inter >= n_need:
                score = inter / len(ft_toks)
                if best is None or score > best[1]:
                    best = (i, score)
    if best and best[1] >= 0.75:
        return best[0], "fuzzy"
    return None


def unit_lead_lines(unit_text, max_scan=16):
    """单元前若干行 + 其中的'实质行'(≥8字符且≥2词元, 跳过 (i)/•/坐标等短行)。
    短行开头的单元(小问罗马号、项目符号、图形标注)先用实质行命中, 再回溯短行定起点。"""
    lines = [l for l in unit_text.split("\n") if l.strip()]
    subs = []
    for i, l in enumerate(lines[:max_scan]):
        t = _norm(l)
        if len(t) >= 8 and len(_toks(l)) >= 2:
            subs.append((i, l))
            if len(subs) >= 3:
                break
    return lines, subs


def locate_paper(rec):
    subject, units = rec["subject"], rec["units"]
    doc = fitz.open(os.path.join(ROOT, rec["qp_pdf"]))
    pages, rects = [], []
    for pno in range(doc.page_count):
        pages.append([] if pno == 0 else page_lines(doc[pno], subject))
        rects.append(doc[pno].rect)
    doc.close()
    flat = [(pi, li) + (pages[pi][li][0],) + (pages[pi][li][1],)
            for pi in range(len(pages)) for li in range(len(pages[pi]))]
    # flat: (page_idx, line_idx, x, txt); bbox 经 pages[pi][li][3] 取
    _by_page = {}                    # page_idx -> [(flat 下标, 行 bbox)]
    for _j, _f in enumerate(flat):
        _by_page.setdefault(_f[0], []).append((_j, pages[_f[0]][_f[1]][3]))
    if not flat or not units:
        return {"units": {}, "questions": {}}

    # 卷尾标记: 第一处 END OF PAPER 行 (flat 索引) —— 卷尾单元的截断线
    end_j = next((j for j, f in enumerate(flat)
                  if END_RE.search(f[3])), None)

    starts = []                      # 每单元: (flat_j | None, status, y0_pdf_extended)
    cursor = 0
    for u in units:
        truncated = u.get("text_source") == "snippet_truncated"
        hint = (u.get("page") or 1) - 1
        ulines, subs = unit_lead_lines(u["text"])
        first_line = ulines[0] if ulines else ""
        if truncated:
            # 旧摘要把多行拼成一行: 用开头 45 字符做子串探测(在首行内)
            probe_lines = [(0, " ".join(u["text"].split())[:45])]
        else:
            probe_lines = subs or ([(0, first_line)] if first_line else [])

        def scan(js):
            """按实质行顺序尝试命中; 返回 (flat_j, status, unit_line_idx)。"""
            for ui, probe in probe_lines:
                m = match_lines(probe, [(flat[j][2], flat[j][3]) for j in js],
                                truncated=truncated)
                if m:
                    return js[m[0]], m[1], ui
            return None

        cand = [j for j in range(cursor, len(flat)) if abs(flat[j][0] - hint) <= 1]
        hit = scan(cand) or scan(list(range(cursor, len(flat))))
        if hit is None:
            starts.append((None, "fail", None))
            continue
        j, status, u_idx = hit
        # 回溯: 命中行上方的单元短行((i)/•/图形标注)也算本单元起点
        k, li = u_idx - 1, j - 1
        while k >= 0 and li >= 0 and flat[li][0] == flat[j][0]:
            if _norm(flat[li][3]) == _norm(ulines[k]):
                j, k, li = li, k - 1, li - 1
            else:
                break
        pi, li2 = flat[j][0], flat[j][1]
        y0 = pages[pi][li2][3][1]
        if li2 > 0:                   # 起点上扩: 裸题号行 / "N 题干" 行(左题号列)
            px, pt, ppl, pbb = pages[pi][li2 - 1]
            if (re.fullmatch(r"\d{1,2}", _norm(pt)) or
                    (u.get("part") is None and re.match(rf"^{u['q']}\s+\S", pt))) \
                    and px <= 78 and not ppl:
                y0 = pbb[1]
        starts.append((j, status, y0))
        cursor = j + 1

    def unit_seg(j, y0, j_next, y_next):
        """[j, j_next) 的逐页 seg; j_next=None 表示到卷尾标记/最后内容页。
        终点页只取 j_next 之前的行; 该页在下一单元前没有内容则不出段
        (避免只含下一题开头的残片/回退带)。整体退化时至少保留命中行行带。"""
        p0 = flat[j][0]
        if j_next is not None:
            p_end = flat[j_next][0]
        else:
            p_end = flat[end_j][0] if end_j is not None and flat[end_j][0] >= p0 \
                else max(f[0] for f in flat)
        segs = []
        pi = p0
        while True:
            rect, s = rects[pi], rects[pi].width / 595.0
            here = [bb for jn_, bb in _by_page.get(pi, [])
                    if j_next is None or pi < p_end or jn_ < j_next]
            if here:                       # 中间空白页不出段但继续翻页
                if pi == p0:
                    top = y0 - 3 * s
                else:
                    top = (min((b[1] for b in here), default=48 * s)) - 8 * s
                if j_next is not None and pi == p_end:
                    bot = y_next - 2 * s
                elif end_j is not None and pi == flat[end_j][0]:
                    bot = pages[flat[end_j][0]][flat[end_j][1]][3][1] - 4 * s
                else:
                    bot = (max((b[3] for b in here), default=782 * s)) + 5 * s
                if bot > top:
                    segs.append([pi + 1, X0_N * s, max(0.0, top), X1_N * s,
                                 min(rect.height, bot)])
            if pi >= p_end:
                break
            pi += 1
        segs = [sg for sg in segs if sg[4] - sg[2] >= 8.0]   # 滤掉容不下一行的残片
        if not segs:
            bb = pages[flat[j][0]][flat[j][1]][3]
            s2 = rects[flat[j][0]].width / 595.0
            segs = [[flat[j][0] + 1, X0_N * s2, max(0.0, bb[1] - 4 * s2),
                     X1_N * s2, bb[3] + 6 * s2]]
        return segs

    # 下一有效起点(跳过 fail)
    def next_start(k):
        for m in range(k + 1, len(units)):
            if starts[m][0] is not None:
                return starts[m][0], starts[m][2]
        return None, None

    out_units = {}
    for k, u in enumerate(units):
        j, status, y0 = starts[k]
        if j is None:
            out_units[u["id"]] = {"status": "fail", "seg": None}
            continue
        jn, yn = next_start(k)
        out_units[u["id"]] = {"status": status, "seg": unit_seg(j, y0, jn, yn)}

    # 大题级: 该大题首单元起点 → 下一大题首单元起点(fail 单元跳过)
    out_q = {}
    order, seen = [], set()
    for k, u in enumerate(units):
        if u["q"] not in seen:
            seen.add(u["q"])
            order.append(u["q"])
    first_of_q = {}
    for k, u in enumerate(units):
        first_of_q.setdefault(u["q"], k)
    for qi, q in enumerate(order):
        k0 = first_of_q[q]
        j0 = next((starts[m][0] for m in range(k0, len(units))
                   if units[m]["q"] == q and starts[m][0] is not None), None)
        if j0 is None:
            out_q[str(q)] = {"status": "fail", "seg": None}
            continue
        y0 = next(starts[m][2] for m in range(k0, len(units))
                  if units[m]["q"] == q and starts[m][0] == j0)
        jn, yn, st = None, None, "exact"
        for q2 in order[qi + 1:]:
            k2 = first_of_q[q2]
            jj = next((starts[m][0] for m in range(k2, len(units))
                       if units[m]["q"] == q2 and starts[m][0] is not None), None)
            if jj is not None:
                jn = jj
                yn = next(starts[m][2] for m in range(k2, len(units))
                          if units[m]["q"] == q2 and starts[m][0] == jj)
                break
        st = "fuzzy" if any(v["status"] == "fuzzy" for v in
                            (out_units[units[m]["id"]] for m in range(len(units))
                             if units[m]["q"] == q)) else "exact"
        out_q[str(q)] = {"status": st, "seg": unit_seg(j0, y0, jn, yn)}
    return {"units": out_units, "questions": out_q}


def main(subjects=None, papers=None):
    subjects = subjects if subjects is not None else discover_subjects()
    for subject in subjects:
        d = os.path.join(ROOT, "data", "units", subject)
        out_d = os.path.join(ROOT, "data", "loc", subject)
        os.makedirs(out_d, exist_ok=True)
        files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
        if papers:
            files = [f for f in files if f[:-5] in set(papers)]
        n_ok = n_fuzzy = n_fail = 0
        for f in files:
            rec = json.load(open(os.path.join(d, f)))
            if not rec["units"]:
                json.dump({"units": {}, "questions": {}},
                          open(os.path.join(out_d, f), "w"))
                continue
            try:
                loc = locate_paper(rec)
            except Exception as e:            # 单卷失败不拖垮全量
                print(f"  ERROR {subject}/{f}: {e!r}", file=sys.stderr)
                loc = {"units": {u["id"]: {"status": "fail", "seg": None}
                                 for u in rec["units"]},
                       "questions": {}}
            for v in loc["units"].values():
                n_ok += v["status"] == "exact"
                n_fuzzy += v["status"] == "fuzzy"
                n_fail += v["status"] == "fail"
            json.dump(loc, open(os.path.join(out_d, f), "w"), ensure_ascii=False)
        print(f"{subject}: units exact={n_ok} fuzzy={n_fuzzy} fail={n_fail} "
              f"({len(files)} papers)")


if __name__ == "__main__":
    args = sys.argv[1:]
    papers = None
    if "--papers" in args:
        i = args.index("--papers")
        papers = [p for p in args[i + 1].split(",") if p]
        args = args[:i] + args[i + 2:]
    main(args or None, papers=papers)
