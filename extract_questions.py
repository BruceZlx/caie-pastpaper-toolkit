#!/usr/bin/env python3
"""Extract per-question units (text + page numbers) from CAIE past papers.

用法:
    python3 extract_questions.py <科目代码> <试卷根目录> <输出目录> [年份范围]
例:
    python3 extract_questions.py 9618 "<题库根>/9618（CS）" data/pipeline/work/9618/units
    python3 extract_questions.py 9709 "<题库根>/9709（数学）" data/pipeline/work/9709/units 2020-2026

年份范围(可选): "2020-2026" 或单一年份 "2021"；省略则全部年份。
只影响知识点分类管线（整卷归档/整卷索引不受影响）；后续 gen_workfiles / merge_tags /
gen_topics 只处理 units 目录里存在的卷，因此范围在这一个入口控制即可。

扫描 <试卷根目录>/<年份>/<会话>/*_qp_*.pdf 与 *_ms_*.pdf：
- QP: 按题号(独立数字行)与小问号 `(a)` 切分，每个 unit 记录 {id, q, part, page, marks, text}
- MS: 记录每个题号(如 1(a)(i))在 MS 中的起始页码 ms_pages
输出: <输出目录>/<session><yy>_<paper><variant>.json（每卷一个文件）
"""
import fitz, json, os, re, sys

ROOT = os.path.dirname(os.path.abspath(__file__))

SESS = {"m": "Feb-Mar", "s": "May-Jun", "w": "Oct-Nov"}
ROMAN = r"(?:i{1,3}|iv|v|vi{1,3}|ix|x)"
part_re = re.compile(rf"^\(([a-j])\)\s")
sub_re = re.compile(rf"^\({ROMAN}\)")

def clean(lines):
    out = []
    for ln in lines:
        ln = ln.rstrip()
        # 去掉答题点线 ......
        ln = re.sub(r"[.\u2026]{4,}(?=\s*\[\d+\])?$", " ", ln)
        ln = re.sub(r"[.\u2026]{4,}", " ", ln)
        if ln.strip():
            out.append(ln)
    return out

def page_lines(page, subject):
    """行级提取: 过滤页眉/页脚/竖排边栏/条形码/乱码, 返回 [(x, line)] 按 (y,x) 排序。
    x 为行首坐标, 用于区分左边距题号列 (x≈50) 与缩进正文/代码 (x≈95-118)"""
    d = page.get_text("dict")
    # A3 放大版式(9701 有 7 份 842×1191 卷, 坐标 ≈ A4 ×1.414): 所有坐标归一到 A4 等效空间,
    # 下游全部绝对阈值(题号列 x≈50-78/小问 x≤80/页眉 48-58/页脚 782/右栏 560)按 A4 书写
    s = page.rect.width / 595.0
    keep = []
    for blk in d["blocks"]:
        if blk.get("type") != 0:
            continue
        bx0, by0 = blk["bbox"][0], blk["bbox"][1]
        if bx0 < 45 * s or bx0 > 560 * s:   # 左右 DO-NOT-WRITE 竖排边栏
            continue
        for ln in blk["lines"]:
            x, y0 = ln["bbox"][0] / s, ln["bbox"][1] / s
            if y0 < 48 or y0 > 782:      # 页眉页脚 (9709 老年卷 Q1 题干 bbox 顶可早至 y≈53.5)
                continue
            if y0 < 58 and x > 150:     # 页码/条形码等页眉装饰可在 y≈50 出现; 左上保留正文(9701 正文可早至 y≈56)
                continue
            txt = "".join(s["text"] for s in ln["spans"])
            # \x07 在部分 PDF 里充当空格 (9701 m20_22/33 题号与题干粘连成 "1Beryllium…" 导致 0 单元) → 替换为空格而非删除
            txt = txt.replace("\x07", " ").replace("\x08", " ").replace("\xa0", " ").strip()
            if not txt:
                continue
            if x > 500 and txt in ("For", "Examiner’s", "Examiner's", "Use",
                                   "for", "examiner’s"):   # 9702 P2-P4 右侧 "For Examiner's Use" 栏
                continue
            if re.search(rf"\*\s*00008\d+\s*\*", txt):   # 条形码
                continue
            odd = sum(1 for c in txt if ord(c) > 0x250 or not c.isprintable())
            if len(txt) > 10 and odd > 0.3 * len(txt):   # 乱码字体
                continue
            if any(r.search(txt) for r in NOISE_RES):
                continue
            # 9701 真题号是粗体(Arial-BoldMT/Helvetica-Bold); 表格编号列是常规体。
            # 老年卷内嵌字体(TT45Ao00)名不透明, 判不了粗体 → 只有能明确判定整行常规体才标记
            plain = bool(ln["spans"]) and all(
                "bold" not in s["font"].lower() and re.search(r"arial|helvetica|times", s["font"], re.I)
                for s in ln["spans"])
            # 排序基准 = 首 span 基线 origin y (同一视觉行基线差 <1pt)。9709 数学的行
            # bbox 顶会被公式上标 (如 3^x) 抬高 ~13pt 而题号是普通行, 按 bbox 顶排序题号
            # 会掉到题干之后 (题干被丢/小问误切); 基线分桶 4pt 不会合并 ≥13pt 的真行距。
            y = ln["spans"][0]["origin"][1] if ln["spans"] else y0
            keep.append((y, x, txt, plain))
    # y 分桶后再按 x 排: 同一视觉行内左边距题号先于题干/小问号 (y 微差竞态)。
    keep.sort(key=lambda r: (round(r[0] / 4.0), r[1]))
    return [(x, t, pl) for _, x, t, pl in keep]

NOISE_RES = [
    re.compile(r"^\s*\[?Turn over", re.I),
    re.compile(r"^\s*©?\s*UCLES"),
    re.compile(r"^\s*©?\s*Cambridge University Press"),
    re.compile(r"^\s*PapaCambridge", re.I),
    re.compile(r"^\s*BLANK PAGE\s*$", re.I),
]

def extract_qp(path, subject):
    """返回 [{id, q, part, page, marks, text}]，按左边距题号行与小问号 (a)-(h) 切分"""
    doc = fitz.open(path)
    units = []
    state = {"q": None, "part": None, "page": None, "id": None, "buf": []}
    prev_q = 0
    used_ids = set()          # OPTIONS 卷/分节卷题号按 option 重新从 1 编号 → 同名单元加 #2/#3 后缀保证唯一
    noise_re = [r for r in NOISE_RES]
    noise_re.append(re.compile(rf"^\s*{subject}/\d"))

    def flush():
        if state["q"] is not None and state["buf"]:
            text = "\n".join(state["buf"]).strip()
            marks = None
            for mm in re.finditer(r"\[(\d{1,2})\]", text):
                marks = int(mm.group(1))
            uid = state["id"]
            if uid in used_ids:
                n = 2
                while f"{uid}#{n}" in used_ids:
                    n += 1
                uid = f"{uid}#{n}"
            used_ids.add(uid)
            units.append({"id": uid, "q": state["q"], "part": state["part"],
                          "page": state["page"], "marks": marks, "text": text})
        state["buf"] = []

    for pno in range(doc.page_count):
        if pno == 0:          # 封面页跳过
            continue
        raw = page_lines(doc[pno], subject)
        lines = []
        for x, ln, pl in raw:
            ln0 = re.sub(r"[.\u2026]{4,}", " ", ln).rstrip()
            if ln0.strip():
                lines.append((x, ln0, pl))
        for i, (x, ln, pl) in enumerate(lines):
            mq = re.match(r"^(\d{1,2})\s+(\S.*)$", ln)          # 题号+题干同行
            if mq and (len(re.sub(r"[^A-Za-z]", "", mq.group(2))) < 2 or x > 62):
                mq = None                                        # 题号必须位于左边距列
            if not mq:
                mq2 = re.match(r"^(\d{1,2})\s*$", ln)            # 独立题号行
                if mq2 and x <= 78 and not pl:                              # 9701 部分年份题号列在 x≈70
                    nx2, nxt = next(((x2, l2) for x2, l2, _ in lines[i+1:] if l2.strip()), (9999, ""))
                    # 下一行纯数字只在同样位于左题号列 (nx2<=78) 时才视为"连续编号栏"而拒识;
                    # 9709 图注标号 (圆半径等, x≈240-400) 会紧跟题号行, 不能因此丢真题号
                    if not (re.match(r"^\d{1,2}\s*$", nxt) and nx2 <= 78) and nx2 >= x + 12:
                        mq = mq2                                 # 题干须缩进在题号右侧 ≥12pt
            if mq and int(mq.group(1)) == prev_q + 1:
                flush()
                state["q"] = prev_q + 1
                state["part"] = None
                state["page"] = pno + 1
                state["id"] = str(state["q"])
                prev_q = state["q"]
                body = re.sub(rf"^{mq.group(1)}\s*", "", ln)
                # 9702 等科目常见 "13 (a) 题干" 同行排版: 题干以小问号开头时直接开小问单元
                mb = re.match(r"^\(([a-h])\)\s*(.*)$", body)
                if mb:
                    state["part"] = mb.group(1)
                    state["id"] = f"{state['q']}{state['part']}"
                    if mb.group(2).strip():
                        state["buf"].append(mb.group(2))
                elif body.strip():
                    state["buf"].append(body)
                continue
            mp = re.match(r"^\s*\(([a-h])\)\s*(.*)$", ln)        # 罗马小问 (i)(ii) 不切分
            if mp and state["q"] is not None and x <= 80:        # 实测全部 (a)-(h) 都在 x≈50-72
                if state["part"] == mp.group(1):                 # 同一小问重复出现(排版/分栏)→并入
                    state["buf"].append(mp.group(2))
                    continue
                flush()
                state["part"] = mp.group(1)
                state["page"] = pno + 1                   # 小问起始页, 供 #page= 定位到所在页
                state["id"] = f"{state['q']}{state['part']}"
                state["buf"] = [mp.group(2)]
                continue
            state["buf"].append(ln)
    flush()
    doc.close()
    return units

def extract_ms(path):
    """返回 {qnum: 起始页}。dict 级行 → /Rotate 90 归一(remove_rotation) → 行方向滤斜水印
    → y 桶(4pt)合并视觉行(裸 MS 题号 '1' 与 '(a) 答案' 是同 y 两行, 必须并排才能命中)。
    四类匹配(9701/9702/9709 各年份版式实测):
    A) '1(a)(i)' 行标 → 免闸门;  B) '3 文本…' 行首题号 → prev+1 连续闸门;
    C) MCQ 答案表 '1  A  21  B' 数字+单字母对 → 免闸门(无歧义);
    D) 裸题号行(视觉行首 x≤80, 如 P3/P5/老年卷 MS) → 免闸门。
    首次出现优先(setdefault), 页码即该题起始页。"""
    doc = fitz.open(path)
    pages = {}
    prev = 0
    for pno in range(doc.page_count):
        if pno == 0:          # MS 封面页: 'maximum raw mark 12' 等杂号会被误录成题号
            continue
        page = doc[pno]
        if page.rotation:
            page.remove_rotation()
        rows = []
        for blk in page.get_text("dict")["blocks"]:
            if blk.get("type") != 0:
                continue
            for ln in blk["lines"]:
                dv = ln["dir"]
                if abs(dv[0] - 1.0) > .01 or abs(dv[1]) > .01:   # 斜向水印/竖排边栏
                    continue
                t = "".join(s["text"] for s in ln["spans"]).strip()
                if t:
                    rows.append((ln["spans"][0]["origin"][1] if ln["spans"] else ln["bbox"][1],
                                 ln["bbox"][0], t))
        rows.sort(key=lambda r: (round(r[0] / 4.0), r[1]))
        # 题号列 x 上限按页宽自适应: 竖版 A4(595) 题号在 x≈49-82; 横版(remove_rotation 后 842)
        # 题号列右移到 x≈76-91 (2018 起 P4/P5 表格式 MS)。公式碎片 '3 × 10' 在 x≥110/227, 两档均排除。
        xq = 86 if page.rect.width < 700 else 125
        i = 0
        while i < len(rows):                       # 同桶合并为视觉行
            y0, x0, t = rows[i]
            b = round(y0 / 4.0)
            while i + 1 < len(rows) and round(rows[i+1][0] / 4.0) == b:
                i += 1
                t += " " + rows[i][2]
            i += 1
            m = re.match(r"^(\d{1,2})((?:\([a-z0-9]+\))+)(?:\s+(.*))?$", t)
            if m:                                  # A) 行标 '1(a)(i)' / '4(a)'
                pages.setdefault(int(m.group(1)), pno + 1)
                prev = max(prev, int(m.group(1)))
                continue
            toks = t.split()                       # C) MCQ 两列答案表; 同桶合并是单空格拼接,
            if len(toks) >= 2 and len(toks) % 2 == 0 and \
               all(re.fullmatch(r"\d{1,2}" if k % 2 == 0 else r"[A-D]", tk)
                   for k, tk in enumerate(toks)):   # 整行须为 数字/单字母 交替才按键行解析
                for k in range(0, len(toks), 2):
                    pages.setdefault(int(toks[k]), pno + 1)
                    prev = max(prev, int(toks[k]))
                continue
            segs = re.split(r"\s{2,}", t)          # C2) 未合并的原始 2+ 空格分隔行
            hitC = False
            for j in range(len(segs) - 1):
                if re.fullmatch(r"\d{1,2}", segs[j]) and re.fullmatch(r"[A-D]", segs[j + 1]):
                    pages.setdefault(int(segs[j]), pno + 1)
                    prev = max(prev, int(segs[j]))
                    hitC = True
            if hitC:
                continue
            m = re.match(r"^\s*(\d{1,2})\s+(\S.*)$", t)
            if m and x0 <= xq:                     # B) '3 文本…' 连续闸门 (限题号列: 公式碎片
                q = int(m.group(1))                # '3 × 10' 在正文区, 不得误录顶掉 prev)
                if prev < q <= prev + 2:
                    pages.setdefault(q, pno + 1)
                    prev = q
                continue
            m = re.match(r"^(\d{1,2})$", t)
            if m and x0 <= xq:                     # D) 裸题号行 (P3/P5/老年卷 MS)
                pages.setdefault(int(m.group(1)), pno + 1)
                prev = max(prev, int(m.group(1)))
    doc.close()
    return pages

def run(subject, base, out, year_lo=None, year_hi=None, force=None, root=None):
    """扫描 base 下 QP/MS → out/<code>_{qp,ms}.json。
    year_lo/year_hi: 闭区间年份过滤(可选); force: 允许重切的卷代码集合
    (默认已存在的输出跳过 —— 增量); root: qp_pdf 相对路径的基准目录。
    返回本次新切/重切的卷数。"""
    root = root or ROOT
    os.makedirs(out, exist_ok=True)
    force = force or set()
    fname_re = re.compile(rf"^{subject}_([msw])(\d{{2}})_(qp|ms)_(\d)(\d?)\.pdf$")
    count = 0
    for ydir in sorted(os.listdir(base)):
        ypath = os.path.join(base, ydir)
        if not os.path.isdir(ypath):
            continue
        if year_lo and (not ydir.isdigit() or not (year_lo <= int(ydir) <= year_hi)):
            continue
        for sess in sorted(os.listdir(ypath)):
            spath = os.path.join(ypath, sess)
            if not os.path.isdir(spath):
                continue
            for f in sorted(os.listdir(spath)):
                m = fname_re.match(f)
                if not m:
                    continue
                seas, yy, typ, paper, variant = m.groups()
                if paper == "0":
                    continue
                variant = variant or "1"
                code = f"{seas}{yy}_{paper}{variant}"
                out_path = os.path.join(out, f"{code}_{typ}.json")
                if os.path.exists(out_path) and code not in force:
                    continue
                rec = {"code": code, "year": 2000 + int(yy), "session": SESS[seas],
                       "paper": int(paper), "variant": int(variant),
                       "qp_pdf": os.path.relpath(os.path.join(spath, f), root)}
                ms_name = f.replace("_qp_", "_ms_")
                ms_path = os.path.join(spath, ms_name)
                rec["ms_pdf"] = os.path.relpath(ms_path, root) if os.path.exists(ms_path) else None
                if typ == "qp":
                    rec["units"] = extract_qp(os.path.join(spath, f), subject)
                else:
                    rec["ms_pages"] = extract_ms(os.path.join(spath, f))
                tmp = out_path + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(rec, fh, ensure_ascii=False)
                os.replace(tmp, out_path)
                count += 1
    print("extracted:", count, "->", out)
    return count


if __name__ == "__main__":
    SUBJECT = sys.argv[1]
    BASE = sys.argv[2]
    OUT = sys.argv[3]
    YEAR_LO = YEAR_HI = None
    if len(sys.argv) > 4:
        m = re.match(r"^(\d{4})(?:-(\d{4}))?$", sys.argv[4])
        if not m:
            sys.exit(f"年份范围格式应为 2020-2026 或 2021, 收到: {sys.argv[4]}")
        YEAR_LO = int(m.group(1))
        YEAR_HI = int(m.group(2) or m.group(1))
    run(SUBJECT, BASE, OUT, YEAR_LO, YEAR_HI)
