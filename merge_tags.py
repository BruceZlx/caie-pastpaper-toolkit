#!/usr/bin/env python3
"""Merge per-paper tag files into classified_<subject>.json, with normalization.

用法: python3 merge_tags.py <科目> <units目录> <tags目录>
归一化规则(9709, 依据 2026-2027 syllabus 的章节命名冲突与新旧卷编号):
- P2/P3 小节同名 (2.x 与 3.x 一一对应): 代码跟卷走 — Paper 2 的 3.x -> 2.x; Paper 3 的 2.x -> 3.x
- Paper 1 不考 2/3 章的独有内容, 同名主题降级映射: 2.1/3.1->1.1, 2.3/3.3->1.5, 2.4/3.4->1.7, 2.5/3.5->1.8
  (2.2/2.6/3.2/3.6/3.7-3.9 为 P1 不可能出现的标签, 保留原样以便人工复核)
- 2010-2019 旧卷号: 卷4=M1 卷5=M2 卷6=S1 卷7=S2, 按内容分类由子代理完成, 此处不做文本规则
"""
import json, os, re, sys

SUBJECT, UNITS_DIR, TAGS_DIR = sys.argv[1], sys.argv[2], sys.argv[3]
ROOT = sys.argv[4] if len(sys.argv) > 4 else os.path.dirname(os.path.abspath(__file__))
tax = json.load(open(f"{ROOT}/topics_{SUBJECT}.json"))
valid = {s["code"] for s in tax["sections"]} | {sub["code"] for s in tax["sections"] for sub in s["subs"]}

P1_DOWN = {"2.1": "1.1", "3.1": "1.1", "2.3": "1.5", "3.3": "1.5",
           "2.4": "1.7", "3.4": "1.7", "2.5": "1.8", "3.5": "1.8"}
_P23 = ("1", "2", "3", "4", "5", "6")

def normalize(code_paper, unit, tags):
    paper = int(code_paper.split("_")[1][0])
    text = unit["text"].lower()
    if SUBJECT == "9701":
        # 2010-2020 旧卷按题面内容直接归类到 2025-2027 大纲, 分类阶段已完成新旧映射, 无编号迁移规则
        return [t for t in dict.fromkeys(tags)]
    if SUBJECT == "9702":
        # 2010-2020 全部试卷同一大纲结构, 无编号迁移问题; 规则留空 (identity)。
        return [t for t in dict.fromkeys(tags)]
    if paper == 2:
        tags = ["2." + t.split(".")[1] if t.startswith("3.") and t.split(".")[1] in _P23 else t for t in tags]
    elif paper == 3:
        tags = ["3." + t.split(".")[1] if t.startswith("2.") and t.split(".")[1] in _P23 else t for t in tags]
        # 有理指数二项式展开官方在 P3 3.1 Algebra ("use the expansion of (1+x)^n, n rational")
        tags = ["3.1" if t == "1.6" else t for t in tags]
    elif paper == 1:
        tags = [P1_DOWN.get(t, t) for t in tags]
        # 旧 P1 卷的向量题: 大纲现只有 3.7 有向量, 部分子代理按"P1 用 1.x"误标 1.3
        if re.search(r"\bvector", text):
            tags = ["3.7" if t == "1.3" else t for t in tags]
    out, seen = [], set()
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out

papers, problems = [], []
n_out = 0
out_examples = []
for f in sorted(os.listdir(UNITS_DIR)):
    if not f.endswith("_qp.json"):
        continue
    d = json.load(open(os.path.join(UNITS_DIR, f)))
    code = d["code"]
    ms_path = os.path.join(UNITS_DIR, f.replace("_qp", "_ms"))
    ms = json.load(open(ms_path)) if os.path.exists(ms_path) else {"ms_pages": {}}
    tf = os.path.join(TAGS_DIR, code + ".json")
    tags = json.load(open(tf))["tags"] if os.path.exists(tf) else {}
    units = []
    for u in d["units"]:
        t = tags.get(u["id"])
        if not t:
            # 缺标签: 报告并跳过该单元(不崩溃, 不给 ["?"] 假标签)
            problems.append(f"{code}#{u['id']}: NO TAG")
            continue
        if t[0] == "OUT":   # 判定为大纲外内容 → 不整理, 不进入分类页
            n_out += 1
            if len(out_examples) < 15:
                out_examples.append(f"{code}#{u['id']}: {' '.join(u['text'].split())[:60]}")
            continue
        if t[0] == "PENDING":
            problems.append(f"{code}#{u['id']}: PENDING {' '.join(map(str, t[1:2]))[:60]}")
            continue
        # 先校验后过滤: 非法码逐条上报(修复旧版"过滤后再查"的漏报)
        bad = [x for x in t if x not in valid]
        if bad:
            problems.append(f"{code}#{u['id']}: invalid codes {bad}")
        t = [x for x in t if x in valid]
        if not t:
            problems.append(f"{code}#{u['id']}: all tags invalid {tags[u['id']]}")
            continue
        t = normalize(code, u, t)
        t = [x for x in t if x in valid]      # normalize 后再次校验(映射错向兜底)
        if not t:
            problems.append(f"{code}#{u['id']}: normalize 产生非法码")
            continue
        snippet = " ".join(u["text"].split())
        units.append({"id": u["id"], "q": u["q"], "part": u["part"],
                      "page": u["page"], "marks": u["marks"],
                      "topics": t, "snippet": snippet[:150]})
    papers.append({"code": code, "year": d["year"], "session": d["session"],
                   "paper": d["paper"], "variant": d["variant"],
                   "qp_pdf": d["qp_pdf"], "ms_pdf": d["ms_pdf"],
                   "ms_pages": ms.get("ms_pages", {}), "units": units})

import datetime
out = f"{ROOT}/classified_{SUBJECT}.json"
json.dump({"subject": SUBJECT,
           "generated": datetime.date.today().isoformat(), "papers": papers},
          open(out, "w"), ensure_ascii=False, indent=1)
n_units = sum(len(p["units"]) for p in papers)
print(f"papers={len(papers)} units={n_units} (大纲外剔除 {n_out}) -> {out}")
for e in out_examples:
    print("  OUT:", e)
if problems:
    print("PROBLEMS:")
    for p in problems[:40]:
        print("  ", p)
    if os.environ.get("MERGE_TAGS_STRICT"):
        sys.exit(1)
else:
    print("no problems")
