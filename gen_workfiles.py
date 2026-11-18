#!/usr/bin/env python3
"""为知识点分类准备子代理输入: 每卷一份题目文本 + 全科知识点参考表。

用法: python3 gen_workfiles.py <科目代码> <units目录> <输出目录>
例:   python3 gen_workfiles.py 9618 /tmp/qp9618/units /tmp/qp9618/work
- <输出目录>/TOPICS.txt        知识点大纲+分类守则 (分类代理必读)
- <输出目录>/<code>.txt        每卷题目清单, 每行 "[id|p页|m分] 题面…"
"""
import json, os, sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def run(SUBJECT, UNITS, OUT, root=None):
    root = root or ROOT
    os.makedirs(OUT, exist_ok=True)
    tax = json.load(open(os.path.join(root, f"topics_{SUBJECT}.json")))

    lines = [
        f"{SUBJECT} syllabus topic taxonomy (classify each question to subsection codes like '1.1';",
        "use a section-level code like '9' ONLY if a question genuinely spans its subsections.",
    ]
    for lv in tax.get("levels", []):
        rng = (f"sections {','.join(lv['secs'])}" if lv.get("secs")
               else f"sections {lv['from']}-{lv['to']}")
        lines.append(f"{lv['level']} {rng} are examined in Papers "
                     f"{' & '.join(map(str, lv['papers']))}.")
    lines.append("")
    for s in tax["sections"]:
        lines.append(f"SECTION {s['code']} {s['en']} | {s['zh']}")
        for sub in s["subs"]:
            lines.append(f"  {sub['code']} {sub['en']} | {sub['zh']}")
            lines.append(f"      keywords: {', '.join(sub['kw'][:18])}")
    lines.append("")
    lines.append("Classification guidance:")
    lines.append("- Pick 1-3 codes per question, PRIMARY FIRST. 1 code is usually right for parts.")
    lines.append("- Whole-task programming questions (Paper 2/4): tag each part by what it actually tests.")
    lines.append("- If a unit tests NOTHING in the taxonomy (content outside the syllabus, pure administrative/evidence instructions), tag it [\"OUT\"] instead of forcing a topic.")
    lines.append("- When the official syllabus moves a subtopic between AS and A2 sections, follow the syllabus, not the paper.")
    open(os.path.join(OUT, "TOPICS.txt"), "w").write("\n".join(lines))

    n = 0
    for f in sorted(os.listdir(UNITS)):
        if not f.endswith("_qp.json"):
            continue
        d = json.load(open(os.path.join(UNITS, f)))
        out = [f"PAPER {d['code']} | {d['year']} {d['session']} | Paper {d['paper']} variant {d['variant']}"]
        for u in d["units"]:
            t = " ".join(u["text"].split())[:500]
            m = f" m{u['marks']}" if u["marks"] else ""
            out.append(f"[{u['id']}|p{u['page']}{m}] {t}")
        open(os.path.join(OUT, d["code"] + ".txt"), "w").write("\n".join(out))
        n += 1
    print(f"work files: {n} -> {OUT} (+TOPICS.txt)")
    return n


if __name__ == "__main__":
    SUBJECT, UNITS, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
    run(SUBJECT, UNITS, OUT)
