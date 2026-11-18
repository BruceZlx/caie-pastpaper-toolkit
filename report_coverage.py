#!/usr/bin/env python3
"""report_coverage.py —— 生成拍照搜题/答案定位的逐科覆盖报告 (data/coverage.json + markdown)。

统计口径(与验收表格一一对应; QP 与 MS 相互独立统计):
  已切单元数          data/units 里的白名单单元总数
  QP裁图成功/失败数    locate_qp 状态 exact|fuzzy 且有 seg = 成功; 其余(fail/无文件) = 失败。
                      对全部 47,218 个单元独立统计, 不受 MS 状态影响; 成功+失败 == 单元总数。
  答案精确到单元数    locate_ms 状态 part|letter 的单元 —— part 含"小问行标命中"与
                      "整题单元的大题区域"(该单元本身就是整题), letter 为 MCQ 官方字母;
                      三者都精确对应到该单元, 故不叫"小问答案"。
  仅大题答案数        locate_ms 状态 question 的单元(小问单元但 MS 只能切到大题, 界面标注"按大题匹配")
  缺 MS 数            ms_status=missing(源站缺失该卷 MS) 的单元
  无文字层 MS 数      ms_status=no_text_layer(扫描件, 按约定不识别) 的单元
另附: 未进旧索引单元数、低置信(fuzzy)题面定位数、MS not_found 数。

用法: /usr/bin/python3 report_coverage.py          # 输出 data/coverage.md
"""
import json, os
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))


def discover_subjects():
    d = os.path.join(ROOT, "data", "units")
    if not os.path.isdir(d):
        return []
    return sorted(s for s in os.listdir(d)
                  if os.path.isdir(os.path.join(d, s)) and s.isdigit())


SUBJECTS = discover_subjects()


def main():
    rows, total = [], Counter()
    for s in SUBJECTS:
        c = Counter()
        ud = os.path.join(ROOT, "data", "units", s)
        for f in sorted(os.listdir(ud)):
            rec = json.load(open(os.path.join(ud, f)))
            c["units"] += len(rec["units"])
            c["not_in_old"] += sum(1 for u in rec["units"] if not u["in_old_index"])
            lf = os.path.join(ROOT, "data", "loc", s, f)
            mf = os.path.join(ROOT, "data", "msloc", s, f)
            loc = json.load(open(lf)) if os.path.exists(lf) else {"units": {}}
            msl = json.load(open(mf)) if os.path.exists(mf) else {"units": {}}
            if not rec["units"]:
                continue
            ms_status = msl.get("ms_status", "missing")
            for u in rec["units"]:
                # ---- QP 裁图: 与 MS 状态无关, 逐单元独立统计 ----
                v = (loc.get("units") or {}).get(u["id"])
                if v and v.get("status") in ("exact", "fuzzy") and v.get("seg"):
                    c["qp_ok"] += 1
                    c["qp_fuzzy"] += v["status"] == "fuzzy"
                else:
                    c["qp_fail"] += 1
                # ---- MS 答案 ----
                if ms_status == "missing":
                    c["ms_missing"] += 1
                elif ms_status == "no_text_layer":
                    c["ms_no_text"] += 1
                elif ms_status == "no_units":
                    pass
                else:
                    m = (msl.get("units") or {}).get(u["id"]) or {}
                    st = m.get("status")
                    if st in ("part", "letter"):
                        c["ans_unit"] += 1
                        c["ans_letter"] += st == "letter"
                    elif st == "question":
                        c["ans_q"] += 1
                    elif ms_status == "ok":
                        c["ans_nf"] += 1
        assert c["qp_ok"] + c["qp_fail"] == c["units"], \
            f"{s}: QP 成功{c['qp_ok']}+失败{c['qp_fail']} != 单元{c['units']}"
        rows.append((s, c))
        total.update(c)

    lines = ["# 拍照搜题 · 题目/答案定位覆盖报告", "",
             "| 科目 | 已切单元数 | QP裁图成功 | QP裁图失败 | 答案精确到单元数 | 仅大题答案数 | 缺 MS 数 | 无文字层 MS 数 |",
             "|---|---|---|---|---|---|---|---|"]
    for s, c in rows:
        lines.append(f"| {s} | {c['units']:,} | {c['qp_ok']:,} | {c['qp_fail']:,} | "
                     f"{c['ans_unit']:,} | {c['ans_q']:,} | {c['ms_missing']:,} | "
                     f"{c['ms_no_text']:,} |")
    lines.append(f"| **合计** | **{total['units']:,}** | **{total['qp_ok']:,}** | "
                 f"**{total['qp_fail']:,}** | **{total['ans_unit']:,}** | **{total['ans_q']:,}** | "
                 f"**{total['ms_missing']:,}** | **{total['ms_no_text']:,}** |")
    lines += ["", "口径说明：",
              f"- QP 成功+失败 = 已切单元总数（{total['units']:,}），QP 与 MS 状态互相独立。",
              f"- 答案精确到单元 = 小问行标命中 + 整题单元的大题区域 + 选择题官方字母"
              f"（其中选择题字母 {total['ans_letter']:,} 个）。",
              f"- 未进入旧索引/未分类单元: {total['not_in_old']:,}（详情页明确标注, 不展示知识点）",
              f"- 低置信(fuzzy)题面定位: {total['qp_fuzzy']:,}（界面标注“低置信定位”）",
              f"- MS 有文字层但未能定位该题答案(not_found): {total['ans_nf']:,}"]
    out = os.path.join(ROOT, "data", "coverage.md")
    with open(out, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    json.dump({s: dict(c) for s, c in rows}, open(os.path.join(ROOT, "data", "coverage.json"), "w"),
              ensure_ascii=False, indent=1)
    print("\n".join(lines))
    print("\n->", out)


if __name__ == "__main__":
    main()
