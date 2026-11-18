#!/usr/bin/env python3
"""locate_ms.py —— 为每道题在对应 Mark Scheme 文字层定位官方答案区域。

输入: data/units/<科目>/<卷代码>.json（含 QP 单元与其 ms_pdf 路径）
输出: data/msloc/<科目>/<卷代码>.json
  {"ms_status": "ok|no_text_layer|missing",
   "letters":   {"1": "C", ...},                        # 仅 MCQ 卷: 文字层解析的官方答案字母
   "units":     {<unit_id>: {"status": "part|question|letter|not_found",
                             "seg": [[page,x0,y0,x1,y1],...], "letter": "C"}},
   "questions": {"<q>": {"seg": [...]}}}

定位方法（纯文字层/版面/标签解析, 不调用任何 LLM/视觉模型, 不生成答案）:
  行提取同 extract_questions.extract_ms（斜水印滤除 + remove_rotation + 4pt y 桶合并视觉行）,
  卷首 Generic/Science Marking Principles、Mark Scheme Notes、Abbreviations 等说明页会被跳过
  —— 从首个含 行标 N(a) / Question-Answer 表头 / MCQ 答案行 的页开始解析
  （旧管线 ms_pages 被这些说明页的编号列表污染, 此处不再依赖旧 ms_pages）。
  大题区域 = 该题首个行标 → 下一大题首个行标; 小问区域 = N(a) 行标 → 下一行标;
  MCQ 卷解析 答案表 得每题官方字母 + 该题所在行带。卷尾截到最后一行内容。
状态语义:
  part     = 精确到该单元(小问行标命中, 或整题单元的大题区域)
  question = 小问单元但 MS 只能切到大题 → 界面标注"按大题匹配"
  letter   = MCQ 官方字母
  not_found= 无法可靠定位(不展示答案, 只给状态)

用法: /usr/bin/python3 locate_ms.py [科目...]     # 默认全部已切科目(动态发现)
      /usr/bin/python3 locate_ms.py 9709 --papers w25_12   # 只算指定卷(增量)
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

ROOT = os.path.dirname(os.path.abspath(__file__))
SUBJECTS = sys.argv[1:] or ["9618", "9701", "9702", "9709"]

FOOT_RE = re.compile(r"^(©\s*UCLES.*|Page \d+\s+of\s+\d+|PUBLISHED)$")
HDR_RE = re.compile(r"^(Question\s+Answer\s+Marks?|Question\s+Answer)$", re.I)
LABEL_RE = re.compile(r"^(\d{1,2})\s*((?:\([a-z0-9]{1,4}\))+)(?:\s+(.*))?$")
PART_OF_LABEL = re.compile(r"\(([a-h])\)")
MCQ_ROW3_RE = re.compile(r"^(\d{1,2})\s+([A-D])\s+\d{1,2}$")
MCQ_PAIRS_SPLIT = re.compile(r"\s{2,}")


def page_rows(page):
    """视觉行 [(y0_pdf, y1_pdf, x0, x1, text)] —— 行提取规则与 extract_questions.extract_ms 一致。"""
    rows = []
    h = page.rect.height
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        for ln in blk["lines"]:
            dv = ln["dir"]
            if abs(dv[0] - 1.0) > .01 or abs(dv[1]) > .01:
                continue
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if t:
                y = ln["spans"][0]["origin"][1] if ln["spans"] else ln["bbox"][1]
                rows.append((round(y / 4.0), ln["bbox"][0], ln["bbox"][2],
                             ln["bbox"][1], ln["bbox"][3], t))
    rows.sort()
    merged = []
    for b, x0, x1, y0, y1, t in rows:
        if y0 < 50 or y0 > h - 42:            # 页眉页脚
            continue
        tt = t.strip()
        if not tt or FOOT_RE.match(tt) or HDR_RE.match(tt):
            continue
        if merged and merged[-1][0] == b:
            mb = merged[-1]
            merged[-1] = (mb[0], min(mb[1], x0), max(mb[2], x1), min(mb[3], y0),
                          max(mb[4], y1), mb[5] + " " + tt)
        else:
            merged.append((b, x0, x1, y0, y1, tt))
    return [(m[3], m[4], m[1], m[2], m[5]) for m in merged]


def mcq_pairs(text):
    """'1 A 21 B' 型答案表行 → {q: letter}; 无匹配返回 None。"""
    toks = text.split()
    if len(toks) >= 4 and len(toks) % 2 == 0 and \
       all(re.fullmatch(r"\d{1,2}" if k % 2 == 0 else r"[A-D]", tk)
           for k, tk in enumerate(toks)):
        return {int(toks[k]): toks[k + 1] for k in range(0, len(toks), 2)}
    segs = MCQ_PAIRS_SPLIT.split(text)
    hits = {}
    for j in range(len(segs) - 1):
        if re.fullmatch(r"\d{1,2}", segs[j]) and re.fullmatch(r"[A-D]", segs[j + 1]):
            hits[int(segs[j])] = segs[j + 1]
    return hits or None


def locate_ms_for_paper(rec):
    """返回 msloc dict; 需要 ms_pdf 存在且有文字层。"""
    if not rec.get("ms_pdf"):
        return {"ms_status": "missing", "letters": {}, "units": {}, "questions": {}}
    path = os.path.join(ROOT, rec["ms_pdf"])
    if not os.path.exists(path):
        return {"ms_status": "missing", "letters": {}, "units": {}, "questions": {}}
    doc = fitz.open(path)
    pages = []
    for pno in range(doc.page_count):
        page = doc[pno]
        if page.rotation:
            page.remove_rotation()
        pages.append((page.rect, page_rows(page), pno))
    doc.close()
    if all(not rows for _, rows, _ in pages):
        return {"ms_status": "no_text_layer", "letters": {}, "units": {}, "questions": {}}

    # ---- 卷首说明页跳过: 从首个含 行标/表头/MCQ行 的页开始 ----
    def row_is_content(text):
        if LABEL_RE.match(text):
            return True
        if MCQ_ROW3_RE.match(text):
            return True
        if re.match(r"^Question\b", text) and re.search(r"\bAnswer\b", text):
            return True
        return False

    first_content = None
    for rect, rows, pno in pages:
        if any(row_is_content(r[4]) for r in rows):
            first_content = pno
            break
    if first_content is None:
        first_content = 1                 # 找不到特征(极老版式): 跳过封面, 用裸题号规则

    flat = []                             # (page_idx, y0, y1, x0, x1, text)
    for rect, rows, pno in pages:
        if pno < first_content or pno == 0:
            continue
        for y0, y1, x0, x1, text in rows:
            flat.append((pno, y0, y1, x0, x1, text))
    page_rows_idx = {}                    # page_idx -> [flat 下标](升序)
    for _k, _r in enumerate(flat):
        page_rows_idx.setdefault(_r[0], []).append(_k)

    # ---- 行标解析: markers = [(flat_idx, q, part|None)], letters ----
    xq_wide = False
    if flat:
        xq_wide = pages[first_content][0].width > 700   # 横版表格式 MS 题号列右移
    markers, letters = [], {}
    mcq_seg = {}                         # q -> [[page,x0,y0,x1,y1]] 含题号+字母的紧凑裁切(单段)
    prev_q = 0
    cur_q = None
    last_part = {}                       # q -> 已见最大小问字母(递增闸门)

    def pair_band(pno, rows_in_group, q, letter):
        """在答案表一行的各单元格行(每格一行: y0,y1,x0,x1,text)中, 找到 q 的题号格
        与其右侧最近的字母格, 返回只含这两个格的紧凑 seg;
        找不到唯一对应时返回 None(由调用方回退整行带)。"""
        nums = [r for r in rows_in_group if r[4].strip() == str(q)]
        if not nums:
            return None
        best = None
        for num in nums:
            lets = [r for r in rows_in_group
                    if r[4].strip() == letter and num[2] < r[2] <= num[2] + 130]
            if lets:
                let = min(lets, key=lambda r: r[2])
                if best is None or let[2] - num[2] < best[1]:
                    best = (num, let[2] - num[2], let)
        if best is None:
            return None
        num, _, let = best
        return [pno + 1, num[2] - 10, min(num[0], let[0]) - 4,
                let[3] + 10, max(num[1], let[1]) + 4]

    for j, (pno, y0, y1, x0, x1, text) in enumerate(flat):
        xq = 130 if xq_wide else 86
        m = LABEL_RE.match(text)
        if m and int(m.group(1)) <= 40:
            q = int(m.group(1))
            letters_part = PART_OF_LABEL.findall(m.group(2))
            part = letters_part[0] if letters_part else None
            markers.append((j, q, part))
            prev_q = max(prev_q, q)
            cur_q = q
            if part:
                last_part[q] = max(last_part.get(q, "`"), part)
            continue
        # 老版式独立小问行: "(b) density = ..." (题号列右侧, 字母须递增)
        mp = re.match(r"^\(([a-h])\)\s+(\S.*)?$", text)
        if mp and cur_q is not None and x0 <= xq + 30:
            p = mp.group(1)
            if p > last_part.get(cur_q, "`"):
                markers.append((j, cur_q, p))
                last_part[cur_q] = p
                continue
        m3 = MCQ_ROW3_RE.match(text)
        if m3:
            q, letter = int(m3.group(1)), m3.group(2)
            letters[q] = letter
            mcq_seg[q] = [[pno + 1, x0 - 10, y0 - 4, x1 + 10, y1 + 4]]
            markers.append((j, q, None))
            prev_q = max(prev_q, q)
            cur_q = q
            continue
        pairs = mcq_pairs(text)
        if pairs:
            for q, letter in sorted(pairs.items()):
                letters.setdefault(q, letter)
                band = pair_band(pno, [(y0, y1, x0, x1, text)], q, letter)
                if band is None:            # 单行已合并成多格: 整行带(含题号+字母)
                    band = [pno + 1, x0 - 10, y0 - 4, x1 + 10, y1 + 4]
                mcq_seg.setdefault(q, [band])
            qmin = min(pairs)
            markers.append((j, qmin, None))
            prev_q = max(prev_q, qmin)
            cur_q = qmin
            continue
        # 裸题号行 / 题号+正文行(左题号列 + 连续闸门, 同 extract_ms B/D 规则)
        mb = re.match(r"^(\d{1,2})\s+(\S.*)$", text)
        if mb and x0 <= xq:
            q = int(mb.group(1))
            if prev_q < q <= prev_q + 2:
                markers.append((j, q, None))
                prev_q = q
                cur_q = q
            continue
        md = re.match(r"^(\d{1,2})$", text)
        if md and x0 <= xq:
            q = int(md.group(1))
            if prev_q < q <= prev_q + 2 or prev_q == 0:
                markers.append((j, q, None))
                prev_q = max(prev_q, q)
                cur_q = q

    # ---- 老 MCQ 四列答案表("1 D 21 A"): 数字/字母基线差 ~2pt, 4pt 桶可能不并;
    #      粗桶(行距≤7pt)二次合并后按 数字/字母交替 解析官方字母,
    #      并为每对 (q, 字母) 用 pair_band 生成只含该题号格+字母格的紧凑裁切 ----
    by_page = {}
    for j, row in enumerate(flat):      # row = (pno, y0, y1, x0, x1, text)
        by_page.setdefault(row[0], []).append((j,) + row[1:])
    for pno, rows in by_page.items():
        rows = sorted(rows, key=lambda r: r[1])
        groups = []
        for r in rows:                  # r = (j, y0, y1, x0, x1, text)
            if groups and r[1] - groups[-1]["y0"] <= 7.0:
                g = groups[-1]
                g["rows"].append(r)
                g["y0"], g["y1"] = min(g["y0"], r[1]), max(g["y1"], r[2])
                g["x0"], g["x1"] = min(g["x0"], r[3]), max(g["x1"], r[4])
                g["text"] += " " + r[5]
            else:
                groups.append({"rows": [r], "y0": r[1], "y1": r[2],
                               "x0": r[3], "x1": r[4], "text": r[5]})
        for g in groups:
            pairs = mcq_pairs(g["text"])
            if not pairs:
                continue
            grows = [(r[1], r[2], r[3], r[4], r[5]) for r in g["rows"]]
            for q, letter in sorted(pairs.items()):
                letters.setdefault(q, letter)
                band = pair_band(pno, grows, q, letter)
                if band is None:        # 无法唯一定位格: 回退整组行带(仍含题号+字母)
                    band = [pno + 1, g["x0"] - 10, g["y0"] - 4,
                            g["x1"] + 10, g["y1"] + 4]
                mcq_seg.setdefault(q, [band])
                if not any(mm[1] == q for mm in markers):
                    markers.append((g["rows"][0][0], q, None))
        markers.sort(key=lambda mm: mm[0])

    # ---- 大题/小问区域 ----
    q_marker = {}                          # q -> 第一个 marker idx
    for idx, (j, q, part) in enumerate(markers):
        q_marker.setdefault(q, idx)

    def next_q_marker_idx(mIdx):
        """mIdx 之后第一个 不同大题 的 marker 下标(无则 None)。"""
        q0 = markers[mIdx][1]
        for k in range(mIdx + 1, len(markers)):
            if markers[k][1] != q0:
                return k
        return None

    def seg_between(j_from, j_to):
        """flat[j_from] 行顶 → flat[j_to] 行顶(或卷尾) 的逐页 seg。
        终点页只统计 j_to 之前的行; 该页在下一行标之前没有内容则不出段(避免
        只含下一题标签的残片/回退带)。整体退化时至少保留行标行本身的行带。"""
        p0 = flat[j_from][0]
        p_end = flat[j_to][0] if j_to is not None else None
        segs = []
        pi = p0
        while True:
            rect = pages[pi][0]
            idxs = page_rows_idx.get(pi, [])
            if j_to is not None and pi == p_end:
                idxs = [k for k in idxs if k < j_to]
            if not idxs:
                break                       # 该页(终点页)在下一行标前无内容
            rows_here = [flat[k] for k in idxs]
            if pi == p0:
                top = flat[j_from][1] - 4
            else:
                top = min(r[1] for r in rows_here)
            if j_to is not None and pi == p_end:
                bot = flat[j_to][1] - 2
            else:
                bot = max(r[2] for r in rows_here) + 6
            if bot > top:
                segs.append([pi + 1, 28.0, max(0.0, top), rect.width - 28.0,
                             min(rect.height, bot)])
            if j_to is not None and pi >= p_end:
                break
            pi += 1
            if pi > pages[-1][2]:
                break
        segs = [s for s in segs if s[4] - s[2] >= 8.0]   # 滤掉容不下一行的残片
        if not segs:                       # 区域整体退化: 至少给行标行自己的行带
            r0 = flat[j_from]
            segs = [[r0[0] + 1, 28.0, max(0.0, r0[1] - 4),
                     pages[r0[0]][0].width - 28.0, r0[2] + 6]]
        return segs

    questions = {}
    questions_marker_j = {}                # q -> (行标 j, 终点 j|None)
    for q, mIdx in sorted(q_marker.items()):
        j_from = markers[mIdx][0]
        nk = next_q_marker_idx(mIdx)
        j_to = markers[nk][0] if nk is not None else None
        questions[str(q)] = {"seg": seg_between(j_from, j_to)}
        questions_marker_j[str(q)] = (j_from, j_to)

    # 小问: 同一字母小问从其首个行标延续到 下一个不同字母小问 或 大题终点。
    # 同字母的罗马行标 N(a)(i)/N(a)(ii)/N(a)(iii) 属于同一小问, 必须整段涵盖
    # (旧实现把下一个罗马行标当终点, 导致 (ii)(iii) 的答案被裁掉)。
    parts_of_q = {}                        # q -> {part: (j_from, j_to)}
    for q, mIdx in q_marker.items():
        pmap = {}
        ms = [m for m in markers if m[1] == q and m[2] is not None]
        nk = next_q_marker_idx(mIdx)
        q_end_j = markers[nk][0] if nk is not None else None
        for k, (j, qq, part) in enumerate(ms):
            j_to = q_end_j                 # 默认到大题终点
            for k2 in range(k + 1, len(ms)):
                if ms[k2][2] != part:      # 下一个"不同字母"的行标才是终点
                    j_to = ms[k2][0]
                    break
            pmap.setdefault(part, (j, j_to))
        if pmap:
            parts_of_q[q] = pmap

    # ---- 单元级状态 ----
    ROMAN_RE = re.compile(r"\((i{1,3}|iv|v|vi{0,3}|ix|x)\)")

    def roman_check(q, part, j_from, j_to, qp_text):
        """题面含 ≥2 个罗马子问时核验裁图区域覆盖全部对应子问。
        返回 ok=全覆盖 / gap=MS 有行标但区域缺部分子问 / no-labels=MS 不重复
        罗马行标(无法核验) / single=题面无多罗马。"""
        romans = list(dict.fromkeys(ROMAN_RE.findall(qp_text or "")))
        if len(romans) < 2:
            return "single"
        end = j_to if j_to is not None else len(flat)
        region = " ".join(flat[k][5] for k in range(j_from, min(end, len(flat))))
        found = [R for R in romans if re.search(rf"\(\s*{R}\s*\)", region)]
        if not found:
            return "no-labels"
        return "ok" if len(set(found)) >= len(set(romans)) else "gap"

    units_out = {}
    mcq_check = {"ok": 0, "fallback": 0, "fail": 0}   # 字母裁切自动核验
    rom_stat = {"ok": 0, "gap": 0, "no-labels": 0, "single": 0}
    for u in rec["units"]:
        q, part, uid = u["q"], u["part"], u["id"]
        letter = letters.get(q)
        if letter and part is None and str(q) in questions:
            seg = mcq_seg.get(q) or questions[str(q)]["seg"]
            # 自动核验: 裁切矩形内的行文本必须含该题号与该字母(老式多列答案表逐对检查)
            s0 = seg[0]
            texts = {r[5].strip() for r in flat
                     if r[0] == int(s0[0]) - 1
                     and r[1] >= s0[2] - 1 and r[2] <= s0[4] + 1
                     and r[3] >= s0[1] - 1 and r[4] <= s0[3] + 1}
            if (str(q) in texts and letter in texts) or \
               any(str(q) in t.split() and letter in t.split() for t in texts):
                mcq_check["ok"] += 1
            elif not texts:            # 空矩形: 降级 not_found, 不给可疑裁图
                units_out[uid] = {"status": "not_found", "seg": None}
                mcq_check["fail"] += 1
                continue
            elif seg is questions[str(q)]["seg"]:
                mcq_check["fail"] += 1
            else:
                mcq_check["fallback"] += 1
            units_out[uid] = {"status": "letter", "letter": letter, "seg": seg}
            continue
        pmap = parts_of_q.get(q)
        if part and pmap and part in pmap:
            j, j_to = pmap[part]
            rc = roman_check(q, part, j, j_to, u.get("text"))
            rom_stat[rc] += 1
            units_out[uid] = {"status": "part", "seg": seg_between(j, j_to),
                              "roman_check": rc}
            continue
        if str(q) in questions:
            st = "part" if part is None else "question"
            rc = roman_check(q, part, *questions_marker_j[str(q)], u.get("text")) \
                if part else "single"
            rom_stat[rc] += 1
            entry = {"status": st, "seg": questions[str(q)]["seg"]}
            if part:
                entry["roman_check"] = rc
            units_out[uid] = entry
            continue
        units_out[uid] = {"status": "not_found", "seg": None}
    return {"ms_status": "ok", "letters": letters, "units": units_out,
            "questions": questions, "mcq_check": mcq_check,
            "roman_stat": rom_stat}


def main(subjects=None, papers=None):
    subjects = subjects if subjects is not None else discover_subjects()
    for subject in subjects:
        d = os.path.join(ROOT, "data", "units", subject)
        out_d = os.path.join(ROOT, "data", "msloc", subject)
        os.makedirs(out_d, exist_ok=True)
        files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
        if papers:
            files = [f for f in files if f[:-5] in set(papers)]
        stat = {"part": 0, "question": 0, "letter": 0, "not_found": 0}
        chk = {"ok": 0, "fallback": 0, "fail": 0}
        rom = {"ok": 0, "gap": 0, "no-labels": 0, "single": 0}
        paper_stat = {"ok": 0, "no_text_layer": 0, "missing": 0}
        for f in files:
            rec = json.load(open(os.path.join(d, f)))
            if not rec["units"]:
                json.dump({"ms_status": "no_units", "letters": {}, "units": {},
                           "questions": {}, "mcq_check": dict(chk),
                           "roman_stat": dict(rom)},
                          open(os.path.join(out_d, f), "w"))
                continue
            try:
                loc = locate_ms_for_paper(rec)
            except Exception as e:
                print(f"  ERROR {subject}/{f}: {e!r}", file=sys.stderr)
                loc = {"ms_status": "error", "letters": {},
                       "units": {u["id"]: {"status": "not_found", "seg": None}
                                 for u in rec["units"]}, "questions": {},
                       "mcq_check": dict(chk), "roman_stat": dict(rom)}
            paper_stat[loc["ms_status"]] = paper_stat.get(loc["ms_status"], 0) + 1
            for v in loc["units"].values():
                stat[v["status"]] = stat.get(v["status"], 0) + 1
            for k in chk:
                chk[k] += (loc.get("mcq_check") or {}).get(k, 0)
            for k in rom:
                rom[k] += (loc.get("roman_stat") or {}).get(k, 0)
            json.dump(loc, open(os.path.join(out_d, f), "w"), ensure_ascii=False)
        print(f"{subject}: units part={stat['part']} question(按大题)={stat['question']} "
              f"letter={stat['letter']} not_found={stat['not_found']} | "
              f"papers {paper_stat} | 字母裁切核验 ok={chk['ok']} "
              f"fallback整行带={chk['fallback']} fail降级={chk['fail']} | "
              f"罗马子问核验 ok={rom['ok']} gap={rom['gap']} "
              f"no-labels={rom['no-labels']} single={rom['single']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    papers = None
    if "--papers" in args:
        i = args.index("--papers")
        papers = [p for p in args[i + 1].split(",") if p]
        args = args[:i] + args[i + 2:]
    main(args or None, papers=papers)
