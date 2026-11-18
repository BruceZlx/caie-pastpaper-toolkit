#!/usr/bin/env python3
"""Generate the per-topic data and address stub for the workbench.

用法: python3 gen_topics.py <科目代码>   (根目录默认为脚本所在目录)
读取 topics_<code>.json (知识点大纲) 与 classified_<code>.json (每题标签),
输出:
  - topics_data_<代码>.js        数据 (script 自注册全局, SPA 按需注入, file:// 可用)
  - index_<代码>_topics.html     旧地址 stub -> index.html#/topics/<代码>
题目链接指向本地 PDF 并带 #page=N 定位 (Chrome/Edge/Firefox 支持)。

正式分题库只含 IN 单元(migrate_syllabus.publish 保证 classified 仅 IN);
OUT/PENDING 不会出现在本数据中。
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.abspath(__file__))

PAPER_NAMES = {
    "9618": {"1": "Theory Fundamentals",
             "2": "Fundamental Problem-solving & Programming Skills",
             "3": "Advanced Theory", "4": "Practical"},
    "9701": {"1": "Multiple Choice", "2": "AS Structured Questions",
             "3": "Advanced Practical Skills", "4": "A Level Structured Questions",
             "5": "Planning, Analysis & Evaluation"},
    "9702": {"1": "Multiple Choice", "2": "AS Structured Questions",
             "3": "Advanced Practical Skills",
             "4": "A Level Structured Questions",
             "5": "Planning, Analysis & Evaluation"},
    "9709": {"1": "Pure Mathematics 1", "2": "Pure Mathematics 2",
             "3": "Pure Mathematics 3", "4": "Mechanics",
             "5": "Probability & Statistics 1", "6": "Probability & Statistics 2"},
}.get


def run(SUBJECT, root=None):
    root = root or ROOT
    try:
        subjects = json.load(open(os.path.join(root, "subjects.json"),
                                  encoding="utf-8"))["subjects"]
        paper_names = subjects.get(SUBJECT, {}).get("paper_names") or {}
        zh_name = subjects.get(SUBJECT, {}).get("zh")
    except Exception:
        paper_names, zh_name = {}, None
    paper_names = paper_names or (PAPER_NAMES(SUBJECT) or {})

    tax = json.load(open(os.path.join(root, f"topics_{SUBJECT}.json")))
    cls = json.load(open(os.path.join(root, f"classified_{SUBJECT}.json")))

    # 每个知识点的题量统计 (供目录徽章); 章节级 = 该章全部小节之和
    counts = {}
    total_units = 0
    for p in cls["papers"]:
        for u in p["units"]:
            if not u.get("topics"):        # OUT/PENDING 单元不进知识点页
                continue
            total_units += 1
            for t in u["topics"]:
                counts[t] = counts.get(t, 0) + 1
                if "." in t:
                    sec = t.split(".")[0]
                    counts[sec] = counts.get(sec, 0) + 1

    payload = {"tax": tax, "papers": cls["papers"], "counts": counts,
               "paperNames": paper_names}

    with open(os.path.join(root, f"topics_data_{SUBJECT}.js"), "w") as fh:
        fh.write('window.WB_TOPICS=window.WB_TOPICS||{};window.WB_TOPICS["%s"]=%s;\n'
                 % (SUBJECT, json.dumps(payload, ensure_ascii=False)))

    # 旧地址 stub: 保留 index_<代码>_topics.html 可直接打开, 跳入工作台对应路由
    if not zh_name:
        dirs = {d[:4]: re.match(r"^\d{4}（(.*)）$", d).group(1)
                for d in sorted(os.listdir(root))
                if os.path.isdir(os.path.join(root, d))
                and re.match(r"^\d{4}（.*）$", d)}
        zh_name = dirs.get(SUBJECT, SUBJECT)
    stub = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>知识点分类 · {zh_name} {SUBJECT} · 真卷工作台</title>
<meta http-equiv="refresh" content="0; url=index.html#/topics/{SUBJECT}">
</head>
<body>
<p style="font-family:system-ui,sans-serif;padding:24px;color:#4A5A74">
正在进入 真卷工作台 · <a href="index.html#/topics/{SUBJECT}">知识点分类 · {zh_name} {SUBJECT}</a></p>
<script>try{{location.replace("index.html#/topics/{SUBJECT}")}}catch(e){{}}</script>
</body>
</html>
"""
    path = os.path.join(root, f"index_{SUBJECT}_topics.html")
    with open(path, "w") as fh:
        fh.write(stub)
    print(f"{SUBJECT}: papers={len(cls['papers'])} units={total_units} -> "
          f"topics_data_{SUBJECT}.js + index_{SUBJECT}_topics.html(stub)")
    return total_units


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "9618")
