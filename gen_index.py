#!/usr/bin/env python3
"""Generate the 真卷工作台 (workbench) app for PapaCambridge past papers (QP + MS).

Scans <root>/<数据文件夹>/<year>/<session>/*.pdf, parses filenames like
9702_w24_qp_11.pdf, builds year -> session -> paper -> variant -> {qp, ms}.

输出 (每次运行全部刷新):
  - papers_data_<代码>.json    数据 (生成器内部记账用)
  - papers_data_<代码>.js      同一数据的 script 版 (SPA 按需注入, file:// 可用)
  - index.html                 工作台 SPA: 顶栏/侧栏静态内嵌 + hash 路由, 站内切换零重载
  - index_<代码>.html          旧地址 stub -> index.html#/papers/<代码>
  - syllabus.html              stub -> index.html#/syllabus
布局与逻辑依赖 assets/workbench.css|js 与 assets/app.js (均本地相对路径)。

用法:
    python3 gen_index.py [根目录] [科目代码] [数据文件夹名]
默认: 根目录 = 本脚本所在目录, 科目 9702
数据文件夹默认自动识别: 优先精确匹配科目代码; 否则取以其开头的目录
(如 "9702（物理）")。也可用第三个参数显式指定。
换科目时传新参数即可，如: python3 gen_index.py ~/Desktop/真卷 9701 9701（化学）
"""
import json, os, re, sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
SYLLABUS_DIR = "syllabus"
SUBJECT = sys.argv[2] if len(sys.argv) > 2 else "9702"
if len(sys.argv) > 3:
    SUBDIR = sys.argv[3]
else:
    SUBDIR = SUBJECT
    if not os.path.isdir(os.path.join(ROOT, SUBDIR)):
        cands = sorted(d for d in os.listdir(ROOT)
                       if d.startswith(SUBJECT) and os.path.isdir(os.path.join(ROOT, d)))
        if cands:
            SUBDIR = cands[-1]
BASE = os.path.join(ROOT, SUBDIR)
if not os.path.isdir(BASE):
    sys.exit(f"数据文件夹不存在: {BASE}")

def syllabus_files(code):
    """syllabus/ 目录下属于该科目的 PDF (文件名以科目代码开头)"""
    sd = os.path.join(ROOT, "syllabus")
    if not os.path.isdir(sd):
        return []
    return [{"file": f, "label": f[:-4].replace("_", " ")}
            for f in sorted(os.listdir(sd)) if f.startswith(code) and f.endswith(".pdf")]

def subject_dirs():
    """根目录下全部 "<数字代码>（<名称>）" 形式的数据文件夹"""
    return sorted(d for d in os.listdir(ROOT)
                  if os.path.isdir(os.path.join(ROOT, d)) and re.match(r"^\d{4}（.*）$", d))


def registry_subjects():
    """subjects.json 登记表(权威科目清单): code → 中文名。
    P0-1(2026-11-07 第四轮回归): 发布构建根目录只含当前科目的数据文件夹,
    只扫目录会把全站首页配置缩成单科; 全站科目一律以登记表为准,
    目录发现仅作未登记科目的兜底。"""
    try:
        with open(os.path.join(ROOT, "subjects.json"), encoding="utf-8") as fh:
            subs = json.load(fh)["subjects"]
        return {str(c): (s.get("zh") or str(c)) for c, s in subs.items()}
    except Exception:
        return {}


# 全站科目 = 登记表 ∪ 目录发现(兜底), 按代码排序
_by_dir = {d[:4]: re.match(r"^\d{4}（(.*)）$", d).group(1) for d in subject_dirs()}
ALL_SUBJECTS = [{"code": c, "zh": zh}
                for c, zh in sorted({**_by_dir, **registry_subjects()}.items())]
SUBJECTS_JSON = json.dumps(ALL_SUBJECTS, ensure_ascii=False)

# 各卷名称 —— 优先读 subjects.json 登记; 未登记的科目回退到此表(再退则显示 Paper N)
_PAPER_NAMES_FALLBACK = {
    "9702": {
        "1": "Multiple Choice",
        "2": "AS Level Structured Questions",
        "3": "Advanced Practical Skills",
        "4": "A Level Structured Questions",
        "5": "Planning, Analysis & Evaluation",
        "6": "",
    },
    "9709": {
        "1": "Pure Mathematics 1",
        "2": "Pure Mathematics 2",
        "3": "Pure Mathematics 3",
        "4": "Mechanics",
        "5": "Probability & Statistics 1",
        "6": "Probability & Statistics 2",
    },
    "9701": {
        "1": "Multiple Choice",
        "2": "AS Level Structured Questions",
        "3": "Advanced Practical Skills",
        "4": "A Level Structured Questions",
        "5": "Planning, Analysis & Evaluation",
        "6": "",
    },
    "9618": {
        "1": "Theory Fundamentals",
        "2": "Fundamental Problem-solving & Programming Skills",
        "3": "Advanced Theory",
        "4": "Practical",
    },
}


def _registry_paper_names():
    try:
        import json as _json
        with open(os.path.join(ROOT, "subjects.json"), encoding="utf-8") as fh:
            subs = _json.load(fh)["subjects"]
        return {code: {str(k): v for k, v in (s.get("paper_names") or {}).items()}
                for code, s in subs.items()}
    except Exception:
        return {}


PAPER_NAMES_BY_SUBJECT = {**_PAPER_NAMES_FALLBACK, **_registry_paper_names()}
PAPER_NAMES = PAPER_NAMES_BY_SUBJECT.get(SUBJECT, {})
SESS = {"m": "Feb-Mar", "s": "May-Jun", "w": "Oct-Nov"}
SESS_ORDER = {"Feb-Mar": 0, "May-Jun": 1, "Oct-Nov": 2}
# 常规: 9709_w24_qp_11.pdf; 少数老年份颠倒: s14_9709_qp_13.pdf
fname_re = re.compile(rf"^{SUBJECT}_([msw])(\d{{2}})_([a-z]+(?:_[a-z]+)?)_(\d{{1,2}})\.pdf$")
fname_re_rev = re.compile(rf"^([msw])(\d{{2}})_{SUBJECT}_([a-z]+(?:_[a-z]+)?)_(\d{{1,2}})\.pdf$")

data = {}
for year in sorted(os.listdir(BASE), reverse=True):
    ydir = os.path.join(BASE, year)
    if not os.path.isdir(ydir):
        continue
    for sess in sorted(os.listdir(ydir)):
        sdir = os.path.join(ydir, sess)
        if not os.path.isdir(sdir):
            continue
        for f in sorted(os.listdir(sdir)):
            m = fname_re.match(f) or fname_re_rev.match(f)
            if not m:
                continue
            seas, yy, typ, variant = m.groups()
            kind = "qp" if typ.startswith("qp") else ("ms" if typ == "ms" else None)
            if kind is None:
                continue
            node = (data.setdefault(year, {})
                        .setdefault(SESS[seas], {})
                        .setdefault(variant[0], {})
                        .setdefault(variant, {}))
            rel = os.path.relpath(os.path.join(sdir, f), ROOT)
            if kind in node:  # 同一 variant 多份文件时追加 qp2/ms2…（如 w09 的 qp_et_33）
                i = 2
                while f"{kind}{i}" in node:
                    i += 1
                kind = f"{kind}{i}"
            node[kind] = rel

ordered = {y: {s: data[y][s] for s in sorted(data[y], key=lambda s: SESS_ORDER[s])}
           for y in data}

total_qp = total_ms = 0
for y in ordered.values():
    for s in y.values():
        for p in s.values():
            for v in p.values():
                total_qp += sum(1 for k in v if k.startswith("qp"))
                total_ms += sum(1 for k in v if k.startswith("ms"))

payload = {
    "papers": PAPER_NAMES,
    "data": ordered,
    "stats": {"qp": total_qp, "ms": total_ms,
              "sessions": sum(len(y) for y in ordered.values())},
}

# ---- 数据文件: json(记账) + js(SPA 按需注入, 自注册全局) ----
with open(os.path.join(ROOT, f"papers_data_{SUBJECT}.json"), "w") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=1)
with open(os.path.join(ROOT, f"papers_data_{SUBJECT}.js"), "w") as fh:
    fh.write('window.WB_PAPERS=window.WB_PAPERS||{};window.WB_PAPERS["%s"]=%s;\n'
             % (SUBJECT, json.dumps(payload, ensure_ascii=False)))

# ---- 旧地址 stub: 保留 index_<代码>.html 可直接打开, 跳入工作台对应路由 ----
def stub(title, route):
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} · 真卷工作台</title>
<meta http-equiv="refresh" content="0; url=index.html{route}">
</head>
<body>
<p style="font-family:system-ui,sans-serif;padding:24px;color:#4A5A74">
正在进入 真卷工作台 · <a href="index.html{route}">{title}</a></p>
<script>try{{location.replace("index.html{route}")}}catch(e){{}}</script>
</body>
</html>
"""

SUBJECT_ZH = next((s["zh"] for s in ALL_SUBJECTS if s["code"] == SUBJECT), SUBJECT)
with open(os.path.join(ROOT, f"index_{SUBJECT}.html"), "w") as fh:
    fh.write(stub(f"整卷浏览 · {SUBJECT_ZH} {SUBJECT}", f"#/papers/{SUBJECT}"))

# ============================ 工作台 SPA (index.html) ============================
# 顶栏/侧栏静态内嵌(首屏即有, 不由脚本重建); 视图与数据逻辑在 assets/app.js

def nav_icon(id_):
    return {
        "search":   '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.5-4.5"/>',
        "papers":   '<path d="M4 6h10v12H4z"/><path d="M8 6V4h10v12h-4"/>',
        "topics":   '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
        "mock":     '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
                    '<path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>',
        "notes":    '<path d="M5 4h11l3 3v13H5z"/><path d="M9 9h6M9 13h6"/>',
        "syllabus": '<path d="M7 3h9l3 3v15H7z"/><path d="M10 9h6M10 13h6"/>',
        "pipeline": '<path d="M4 4h7v7H4z"/><path d="M13 4h7v4h-7z"/>'
                    '<path d="M13 10h7v10h-7z"/><path d="M4 13h7v7H4z"/>',
    }[id_]

func_items = "".join(
    f'    <a class="nav-item" data-func="{fid}" href="{href}">'
    f'<svg class="nav-ic" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    f'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
    f'aria-hidden="true">{nav_icon(fid)}</svg>'
    f'<span class="nav-label">{label}</span>{code}</a>'
    for fid, label, href, code in [
        ("search", "搜题", "#/search", ""),
        ("papers", "整卷浏览", "#/papers/" + ALL_SUBJECTS[0]["code"],
         f'<span class="nav-code">{ALL_SUBJECTS[0]["code"]}</span>'),
        ("topics", "知识点分类", "#/topics/" + ALL_SUBJECTS[0]["code"],
         f'<span class="nav-code">{ALL_SUBJECTS[0]["code"]}</span>'),
        ("mock", "模拟组卷", "#/mock/" + ALL_SUBJECTS[0]["code"],
         f'<span class="nav-code">{ALL_SUBJECTS[0]["code"]}</span>'),
        ("notes", "复习资料", "#/notes", ""),
        ("syllabus", "考试大纲", "#/syllabus", ""),
        ("pipeline", "题库管理", "#/pipeline", ""),
    ])

# 搜索学科下拉的题数标注 (从 search_data_<代码>.js 头部读取)
counts = {}
for s in ALL_SUBJECTS:                      # P0-1: 全站科目(登记表), 不止构建根内目录
    scode = s["code"]
    sjs = os.path.join(ROOT, f"search_data_{scode}.js")
    if os.path.isfile(sjs):
        with open(sjs, encoding="utf-8") as fh:
            mu = re.search(r'"units":(\d+)', fh.read(200))
        if mu:
            counts[scode] = int(mu.group(1))
select_options = '<option value="all">全部科目</option>' + "".join(
    f'<option value="{s["code"]}">{s["zh"]} {s["code"]}'
    + (f' · {counts[s["code"]]:,} 题' if s["code"] in counts else '') + '</option>'
    for s in ALL_SUBJECTS)

syllabus_cfg = [{"code": s["code"], "zh": s["zh"], "files": syllabus_files(s["code"])}
                for s in ALL_SUBJECTS]
syllabus_cfg = [x for x in syllabus_cfg if x["files"]]
config_js = ("window.WB_CONFIG = " + json.dumps(
    {"subjects": ALL_SUBJECTS, "counts": counts, "syllabus": syllabus_cfg},
    ensure_ascii=False) + ";")

SPA_HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>搜题 · 真卷工作台</title>
<link rel="stylesheet" href="assets/workbench.css">
</head>
<body>
<a class="skip" href="#main">跳到主内容</a>
<header class="topbar">
  <div class="topbar-in">
    <button type="button" class="nav-btn" id="wbNavBtn" aria-expanded="false"
            aria-controls="wbNav" aria-label="展开或收起导航">
      <svg class="ic-burger" width="18" height="18" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
        <path d="M3 6h18M3 12h18M3 18h18"/></svg>
      <svg class="ic-panel" width="18" height="18" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></svg>
    </button>
    <a class="brand" href="#/search">
      <span class="brand-mark" aria-hidden="true">卷</span>
      <span class="brand-name">真卷工作台</span>
      <span class="brand-sub">CAIE Past Papers &amp; MS</span></a>
    <form class="searchform" id="wbSearchForm" role="search">
      <select id="wbSubject" aria-label="搜索学科">{select_options}</select>
      <input id="wbQuery" type="text" autocomplete="off" spellcheck="false"
             placeholder="输入题面关键词，如 projectile / benzene / binary search" aria-label="搜索关键词">
      <button type="submit" class="search-go">搜索</button>
    </form>
  </div>
</header>
<div class="shell">
  <aside class="sidenav" id="wbNav" aria-label="全站导航">
    <div class="nav-cap">功能</div>
{func_items}
  </aside>
  <main class="main" id="main" tabindex="-1"></main>
</div>
<noscript><p style="padding:20px;font-family:system-ui">真卷工作台需要启用 JavaScript。
各科目试卷也可直接通过 index_9618.html / index_9701.html / index_9702.html / index_9709.html 等地址访问。</p></noscript>
<script>{config_js}</script>
<script src="assets/workbench.js"></script>
<script src="assets/app.js"></script>
</body>
</html>
"""

with open(os.path.join(ROOT, "index.html"), "w") as fh:
    fh.write(SPA_HTML)
with open(os.path.join(ROOT, "syllabus.html"), "w") as fh:
    fh.write(stub("考试大纲", "#/syllabus"))

print(f"{SUBJECT} ({SUBDIR}): years={len(ordered)} sessions={payload['stats']['sessions']} "
      f"qp={total_qp} ms={total_ms} -> papers_data_{SUBJECT}.js + index_{SUBJECT}.html(stub)")
print(f"spa -> index.html · syllabus -> syllabus.html(stub) · 搜索索引题数: "
      f"{', '.join(k + '=' + format(v, ',') for k, v in counts.items())}")
