#!/usr/bin/env python3
"""构建主页搜题功能所需的客户端倒排索引 → search_data_<代码>.js

对根目录下每个 classified_<代码>.json（知识点分类产物）生成一个自包含 JS 文件：
内嵌 zlib deflate + base64 的紧凑索引（词干化词表 + 倒排表 + 题目元数据）。
主页 index.html 的搜题卡片按需解压查询（file:// 双击可用，无需服务器）。

搜索算法（快为主、省性能为辅）:
  构建期  题面 tokenize → 轻量词干化 → 倒排表; docId 增量编码(gap-1) + 词频;
          整包 JSON 一次 deflate, 4 科合计原始约 8MB → 磁盘约 1.5MB。
  查询期  查询词做同样 tokenize+词干化 → 词表二分查找精确命中 + 前缀扩展
          (按 df 取前 8, 覆盖 photosynth→photosynthesis、increase→increases/increasing
          等词形/截断) → BM25 累加(复用 Float64Array 打分缓冲, 不做全文扫描) →
          多词短语命中加成 → top 30。检索代价 O(sum(查询词 posting 长)),
          与题库总量无关; 索引静态压缩存放、按科目懒解压并缓存。

用法:
    python3 gen_search.py [根目录] [科目代码...]
默认根目录 = 本脚本所在目录; 不给科目代码则处理全部 classified_*.json。
classified 更新后(merge_tags.py 重跑)需重跑本脚本, 再重跑 gen_index.py 刷新主页。

注意: 本文件里的 STOP / stem / tokenize 必须与 gen_index.py 搜题卡片内联 JS
保持逐字一致, 改一处必须同步另一处。
"""
import base64, json, os, re, sys, zlib

if __name__ == "__main__":
    ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    CODES = sys.argv[2:]
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))
    CODES = []

STOP = set(("a an the of in to and or is are was were be been being for with that this "
            "these those on as by it its at from which has have had do does did can could "
            "will would may might must shall should than then so such but if into when where "
            "you your they their there also some any each all not no nor he she his her him "
            "we us our what who how why").split())

def stem(t):
    if len(t) <= 3: return t
    if len(t) > 4 and t.endswith("ies"): return t[:-3] + "y"
    if t.endswith("sses"): return t[:-2]
    if t.endswith(("ss", "us", "is")): return t
    if t.endswith("s") and len(t) > 3: t = t[:-1]
    if t.endswith("ing") and len(t) > 5:
        t = t[:-3]
        if len(t) >= 3 and t[-1] == t[-2] and t[-1] not in "ls": t = t[:-1]
        return t
    if t.endswith("ed") and len(t) > 4:
        t = t[:-2]
        if len(t) >= 3 and t[-1] == t[-2] and t[-1] not in "ls": t = t[:-1]
    return t

def tokenize(text):
    out = []
    for tok in re.sub(r"[^a-z0-9]+", " ", text.lower()).split():
        if len(tok) < 2 or len(tok) > 25 or tok in STOP: continue
        out.append(stem(tok))
    return out

def subject_dirs():
    return sorted(d for d in os.listdir(ROOT)
                  if os.path.isdir(os.path.join(ROOT, d)) and re.match(r"^\d{4}（.*）$", d))

def build_subject(code):
    d = json.load(open(os.path.join(ROOT, f"classified_{code}.json"), encoding="utf-8"))
    tnames = {}
    tp = os.path.join(ROOT, f"topics_{code}.json")
    if os.path.isfile(tp):
        tj = json.load(open(tp, encoding="utf-8"))
        for sec in tj.get("sections", []):
            tnames[sec["code"]] = sec.get("en") or sec["code"]
            for sub in sec.get("subs", []):
                tnames[sub["code"]] = sub.get("en") or sub["code"]

    papers, docs, tlist = [], [], {}
    postings = {}  # stem -> {docId: tf}
    for pi, p in enumerate(d["papers"]):
        mp = []
        for q, pg in sorted(((int(k), v) for k, v in p["ms_pages"].items() if v)):
            mp.extend([q, pg])
        # [7]=卷代码, 供主页搜题结果链到 #/q/<科目>/<卷代码>/<单元ID> 详情视图(server.py)
        papers.append([p["qp_pdf"], p.get("ms_pdf") or "", int(p["year"]),
                       p["session"], str(p["paper"]), str(p["variant"]), mp, p["code"]])
        for u in p["units"]:
            # 防御: 正式前端搜题索引只收 IN 单元(classified 本应只含 IN)
            if u.get("status") not in (None, "IN"):
                continue
            if not u.get("topics"):
                continue
            toks = tokenize(u["snippet"])
            tIdx = []
            for c in u["topics"]:
                if c not in tlist: tlist[c] = len(tlist)
                tIdx.append(tlist[c])
            di = len(docs)
            docs.append([pi, int(u["q"]), u["part"] or "", int(u["page"] or 0),
                         int(u["marks"] or 0), tIdx, len(toks), u["snippet"]])
            for t in toks:
                occ = postings.setdefault(t, {})
                occ[di] = occ.get(di, 0) + 1

    vocab = sorted(postings)
    df, pd_arr, pt_arr = [], [], []
    prev = -1
    for t in vocab:
        occ = postings[t]
        df.append(len(occ))
        for di in sorted(occ):
            pd_arr.append(di - prev - 1)   # gap-1 增量编码
            pt_arr.append(occ[di])
            prev = di
    n = len(docs)
    avgdl = round(sum(x[6] for x in docs) / max(n, 1), 3)
    payload = {"v": 1, "n": n, "avgdl": avgdl, "vocab": vocab, "df": df,
               "pd": pd_arr, "pt": pt_arr, "docs": docs, "papers": papers,
               "tl": [c for c, _ in sorted(tlist.items(), key=lambda kv: kv[1])],
               "tn": tnames}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    b64 = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    js = ('window.SEARCH_DATA=window.SEARCH_DATA||{};'
          f'window.SEARCH_DATA["{code}"]={{"units":{n},"b64":"{b64}"}};')
    out = os.path.join(ROOT, f"search_data_{code}.js")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(js)

    # ---- 自检: 解压往返 + 抽样查询, 与 JS 端同管线 ----
    rt = json.loads(zlib.decompress(base64.b64decode(b64)).decode("utf-8"))
    assert rt == payload, f"{code} 往返不一致"
    ok = verify_query(payload)
    mb = os.path.getsize(out) / 1048576
    print(f"{code}: units={n} vocab={len(vocab)} papers={len(papers)} "
          f"raw={len(raw)/1048576:.2f}MB -> {os.path.basename(out)} {mb:.2f}MB 抽查{'✓' if ok else '✗'}")
    return ok

def verify_query(p):
    """python 侧复刻一次 BM25 检索, 确认索引可召回且 top1 题面确实包含查询词干"""
    n, avgdl = p["n"], p["avgdl"]
    if n == 0:
        return True                       # 空索引(如全部 PENDING 的新卷首发布)
    vmap = {t: i for i, t in enumerate(p["vocab"])}
    off = [0]
    for x in p["df"]: off.append(off[-1] + x)
    acc = []                              # 本 payload 专用, 一次性现场解码
    prev = -1                             # (第七轮修复: 旧 id(p) 键缓存会被
    for g in p["pd"]:                     #  GC 后的地址复用污染 → IndexError)
        prev += g + 1
        acc.append(prev)
    def query(text):
        score = {}
        for st in set(tokenize(text)):
            ti = vmap.get(st)
            if ti is None:
                lo = _lb(p["vocab"], st)
                cand = [i for i in range(lo, min(lo + 200, len(p["vocab"])))
                        if p["vocab"][i].startswith(st)]
                if not cand: continue
                ti = max(cand, key=lambda i: p["df"][i])
            idf = __import__("math").log(1 + (n - p["df"][ti] + 0.5) / (p["df"][ti] + 0.5))
            for k in range(off[ti], off[ti + 1]):
                di, tf = acc[k], p["pt"][k]
                score[di] = score.get(di, 0.0) + idf * tf * 2.2 / (tf + 1.2 * (1 - 0.75 + 0.75 * p["docs"][di][6] / avgdl))
        return sorted(score, key=lambda d: -score[d])
    hits = 0
    for q in [p["docs"][n // 3][7][:40], p["docs"][n // 2][7][:40], p["docs"][n - 1][7][:40]]:
        top = query(q)
        if top and top[0] < n: hits += 1
    return hits >= 2



def _lb(arr, s):
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] < s: lo = mid + 1
        else: hi = mid
    return lo

if __name__ == "__main__":
    codes = CODES or sorted(f[len("classified_"):-len(".json")]
                            for f in os.listdir(ROOT)
                            if f.startswith("classified_") and f.endswith(".json"))
    if not codes:
        sys.exit("未找到 classified_*.json —— 请先按 TOPICS_WORKFLOW.md 完成知识点分类")
    bad = [c for c in codes if not build_subject(c)]
    if bad:
        sys.exit(f"抽查失败: {bad}")
    print("全部通过。重跑 python3 gen_index.py 以刷新主页搜题卡片。")
