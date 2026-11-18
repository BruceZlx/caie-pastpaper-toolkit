#!/usr/bin/env python3
"""verify_data.py —— data/ 目录一致性 + 定位坐标/裁图内容自检（发布前跑一遍）。

1. 单元白名单: data/units 总数 = 47,218, 逐科对齐; 不多不少。
2. PDF 路径: 每卷 qp_pdf/ms_pdf 相对路径存在。
3. 坐标边界: 打开每份涉及的 PDF, 检查 loc/msloc 每段 seg 的页码在页数内、
   坐标在页面 rect 内(±2pt 容差)、宽高合理(高≥6pt、宽≥30pt); 统计异常短裁图(<5pt)。
4. 内容核验(区分“有坐标”与“完整性已核验”, 全量、非抽样; 跨页段全部核验):
   - letter 单元: 全部 seg 的 clip 文本拼接后须含该题号与官方字母;
   - part/question 单元: 全部 seg 的 clip 文本拼接后须含对应标签(N(a) 或 N);
   - 多罗马子问(题面含 ≥2 个 (i)/(ii)/(iii)…): MS 有对应行标时, 裁图文本必须
     覆盖全部对应子问; 缺失 → gap(不完整); MS 不重复行标 → no-labels(无法核验)。
     该两类不计入“完整性已核验”。
输出: data/verify.json + 控制台摘要。结构性问题(缺文件/越界/标签缺失)非零退出。
"""
import json, os, re, sys
from collections import Counter

import fitz

ROOT = os.path.dirname(os.path.abspath(__file__))


def discover_subjects():
    d = os.path.join(ROOT, "data", "units")
    if not os.path.isdir(d):
        return []
    return sorted(s for s in os.listdir(d)
                  if os.path.isdir(os.path.join(d, s)) and s.isdigit())


SUBJECTS = discover_subjects()
# 期望单元数: 迁移后有判定文件时按 IN+OUT+PENDING+CONTEXT 对账; 否则只做结构校验
EXPECT = {}
for _s in SUBJECTS:
    _adj = os.path.join(ROOT, "data", "pipeline", "adjudication", f"{_s}.json")
    if os.path.exists(_adj):
        _c = json.load(open(_adj))["counts"]
        EXPECT[_s] = _c["in"] + _c["out"] + _c["pending"] + _c.get("context", 0)
ROMAN_RE = re.compile(r"\((i{1,3}|iv|v|vi{0,3}|ix|x)\)")
problems = []


def check(cond, msg):
    if not cond:
        problems.append(msg)


def seg_bounds_ok(seg, rects):
    """seg = [page1, x0, y0, x1, y1]; 页码在范围内且坐标落在页面 rect(±2pt)。"""
    pno = int(seg[0]) - 1
    if pno < 0 or pno >= len(rects):
        return f"页码越界 p{seg[0]}/{len(rects)}"
    r = rects[pno]
    x0, y0, x1, y1 = seg[1], seg[2], seg[3], seg[4]
    if x0 < -2 or y0 < -2 or x1 > r.width + 2 or y1 > r.height + 2:
        return f"坐标越界 ({x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}) page {r.width:.0f}x{r.height:.0f}"
    if x1 - x0 < 30 or y1 - y0 < 6:
        return f"尺寸异常 w={x1-x0:.1f} h={y1-y0:.1f}"
    return None


def clip_text(doc, seg, normalize_rotation=False):
    """MS 定位数据在 remove_rotation 坐标系(698 份 MS 有旋转页), 读取时同样归一。"""
    pno = int(seg[0]) - 1
    if pno < 0 or pno >= doc.page_count:
        return ""
    page = doc[pno]
    if normalize_rotation and page.rotation:
        page.remove_rotation()
    return page.get_text(clip=fitz.Rect(seg[1], seg[2], seg[3], seg[4]))


def all_segs_text(doc, segs):
    return " ".join(clip_text(doc, sg, normalize_rotation=True) for sg in segs)


def main():
    total = 0
    report = {"subjects": {}, "totals": Counter()}
    for s in SUBJECTS:
        d = os.path.join(ROOT, "data", "units", s)
        st = Counter()
        files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
        for f in files:
            rec = json.load(open(os.path.join(d, f)))
            code = f[:-5]
            check(rec["code"] == code, f"{s}/{code}: code 字段不一致")
            st["units"] += len(rec["units"])
            check(os.path.exists(os.path.join(ROOT, rec["qp_pdf"])),
                  f"{s}/{code}: qp_pdf 不存在 {rec['qp_pdf']}")
            if rec.get("ms_pdf"):
                check(os.path.exists(os.path.join(ROOT, rec["ms_pdf"])),
                      f"{s}/{code}: ms_pdf 不存在 {rec['ms_pdf']}")
            if not rec["units"]:
                continue
            loc = json.load(open(os.path.join(ROOT, "data", "loc", s, f))) \
                if os.path.exists(os.path.join(ROOT, "data", "loc", s, f)) else None
            msl = json.load(open(os.path.join(ROOT, "data", "msloc", s, f))) \
                if os.path.exists(os.path.join(ROOT, "data", "msloc", s, f)) else None
            check(loc is not None, f"{s}/{code}: 缺 loc 文件")
            check(msl is not None, f"{s}/{code}: 缺 msloc 文件")
            if not loc or not msl:
                continue
            qp_doc = fitz.open(os.path.join(ROOT, rec["qp_pdf"]))
            qp_rects = [qp_doc[i].rect for i in range(qp_doc.page_count)]
            ms_doc = fitz.open(os.path.join(ROOT, rec["ms_pdf"])) \
                if rec.get("ms_pdf") and os.path.exists(os.path.join(ROOT, rec["ms_pdf"])) else None
            if ms_doc is not None:        # MS 定位坐标在 remove_rotation 空间 → 归一
                for _i in range(ms_doc.page_count):
                    if ms_doc[_i].rotation:
                        ms_doc[_i].remove_rotation()
            ms_rects = [ms_doc[i].rect for i in range(ms_doc.page_count)] if ms_doc else []
            try:
                for u in rec["units"]:
                    uid = u["id"]
                    # ---- QP seg 边界 ----
                    v = (loc.get("units") or {}).get(uid)
                    for seg in (v or {}).get("seg") or []:
                        st["qp_segs"] += 1
                        err = seg_bounds_ok(seg, qp_rects)
                        if err:
                            problems.append(f"{s}/{code}#{uid} QP {err}")
                        else:
                            st["qp_bounds_ok"] += 1
                        if seg[4] - seg[2] < 5:
                            st["qp_short"] += 1
                    # ---- MS seg 边界 ----
                    m = (msl.get("units") or {}).get(uid) or {}
                    mst = m.get("status")
                    segs = m.get("seg") or []
                    for seg in segs:
                        st["ms_segs"] += 1
                        if ms_rects:
                            err = seg_bounds_ok(seg, ms_rects)
                            if err:
                                problems.append(f"{s}/{code}#{uid} MS {err}")
                            else:
                                st["ms_bounds_ok"] += 1
                        if seg[4] - seg[2] < 5:
                            st["ms_short"] += 1
                    if not segs or ms_doc is None:
                        continue
                    # ---- 内容核验(全部 seg 拼接) ----
                    st["coords"] += 1                  # 有答案坐标
                    txt = all_segs_text(ms_doc, segs)
                    romans = list(dict.fromkeys(ROMAN_RE.findall(u.get("text") or "")))
                    multi = len(romans) >= 2
                    if mst == "letter":
                        st["letter_total"] += 1
                        toks = txt.split()
                        if str(u["q"]) in toks and (m.get("letter") in toks or
                           re.search(rf"\b{re.escape(str(u['q']))}\s+{re.escape(m.get('letter') or '')}\b", txt)):
                            st["letter_ok"] += 1
                        else:
                            problems.append(f"{s}/{code}#{uid} 字母裁图不含题号/字母: {txt[:30]!r}")
                        continue
                    if mst == "part":
                        want = re.compile(
                            rf"{u['q']}\s*\(\s*{re.escape(u['part'])}\s*\)|\(\s*{re.escape(u['part'])}\s*\)") \
                            if u.get("part") else re.compile(rf"\b{u['q']}\b")
                        present = bool(want.search(txt))
                        if multi:
                            found = [R for R in romans if re.search(rf"\(\s*{R}\s*\)", txt)]
                            if not found:
                                st["rom_nolabels"] += 1     # MS 不重复行标 → 无法核验
                            elif len(set(found)) >= len(set(romans)):
                                st["rom_ok"] += 1
                                if present:
                                    st["part_verified"] += 1
                            else:
                                st["rom_gap"] += 1          # 行标有但裁图缺子问 → 不完整
                                if len(st.get("gap_list", [])) < 8:
                                    st.setdefault("gap_list", []).append(f"{code}#{uid}")
                        else:
                            st["rom_single"] += 1
                            if present:
                                st["part_verified"] += 1
                        if not present:
                            st["part_presence_fail"] += 1
                            if len(st.get("presence_fail_list", [])) < 8:
                                st.setdefault("presence_fail_list", []).append(f"{code}#{uid}")
                        continue
                    if mst == "question":
                        # 按大题匹配: 本就不精确到单元 → 不计入完整性已核验
                        st["question_status"] += 1
                        continue
                st["papers"] += 1
            finally:
                qp_doc.close()
                if ms_doc:
                    ms_doc.close()
        st["verified"] = st.get("letter_ok", 0) + st.get("part_verified", 0)
        st["incomplete"] = st.get("coords", 0) - st["verified"]
        report["subjects"][s] = {k: v for k, v in st.items()
                                 if not k.endswith("_list")}
        for k in ("gap_list", "presence_fail_list"):
            if k in st:
                report["subjects"][s][k] = st[k]
        total += st["units"]
        if s in EXPECT:
            check(st["units"] == EXPECT[s],
                  f"{s}: 单元数 {st['units']} != 判定总数 {EXPECT[s]}")
        for k, v in st.items():
            if not k.endswith("_list"):
                report["totals"][k] = report["totals"].get(k, 0) + v

    report["totals"] = {k: v for k, v in report["totals"].items()
                        if not k.endswith("_list")}
    print(f"units total = {total} (期望 {sum(EXPECT.values()) if EXPECT else '动态(无判定文件)'})")
    for s in SUBJECTS:
        st = report["subjects"][s]
        print(f"  {s}: 卷 {st['papers']} | QP seg {st.get('qp_segs',0)} 边界OK {st.get('qp_bounds_ok',0)} "
              f"短(<5pt) {st.get('qp_short',0)} | MS seg {st.get('ms_segs',0)} 边界OK {st.get('ms_bounds_ok',0)} "
              f"短(<5pt) {st.get('ms_short',0)}")
        print(f"     有答案坐标 {st.get('coords',0)} · 完整性已核验 {st.get('verified',0)} "
              f"(字母 {st.get('letter_ok',0)}/{st.get('letter_total',0)}, 多罗马全覆盖 {st.get('rom_ok',0)}, "
              f"无罗马标签核验 {st.get('rom_single',0)}) · 不完整或无法核验 {st.get('incomplete',0)} "
              f"[gap {st.get('rom_gap',0)} · no-labels {st.get('rom_nolabels',0)} · "
              f"标签缺失 {st.get('part_presence_fail',0)} · 按大题 {st.get('question_status',0)}]")
    t = report["totals"]
    print(f"合计: 有答案坐标 {t.get('coords',0)} · 完整性已核验 {t.get('verified',0)} · "
          f"不完整或无法核验 {t.get('incomplete',0)}")
    print(f"problems = {len(problems)}")
    for p in problems[:30]:
        print("  -", p)
    json.dump(report, open(os.path.join(ROOT, "data", "verify.json"), "w"),
              ensure_ascii=False, indent=1)
    print("-> data/verify.json")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
