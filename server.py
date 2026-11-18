#!/usr/bin/env python3
"""server.py —— 真卷工作台本机服务（Mac 作为 LAN 服务端）。

统一提供: 现有网页(静态资源) / PDF(支持 Range) / 题目目录 / 题目与答案裁图(按需缓存) /
照片上传 OCR(仅识别用户刚上传的照片) / 全文检索 API / 题库管理 API(后台任务)。

  默认监听 127.0.0.1 —— 只有本机可访问;
  显式 --lan 才绑定 0.0.0.0 并显示实际 IP/端口, 供同一 Wi-Fi 的手机访问。
  安全: 上传限制(15MB, 仅 jpeg/png/webp/heic 魔数), 路径穿越防护(所有文件解析后
  必须落在项目根目录内), 无目录列表; 手机拍照用 <input type=file capture>
  (LAN HTTP 上无 getUserMedia)。--lan 模式下管理写接口仅限本机(127.0.0.1)调用。

用法:
  /usr/bin/python3 server.py                 # 127.0.0.1:8930
  /usr/bin/python3 server.py --lan           # 0.0.0.0:8930, 打印局域网 IP
  /usr/bin/python3 server.py --lan --port 9000
  /usr/bin/python3 server.py --root <目录>   # 以另一仓库根启动(隔离测试夹具用)
依赖: /usr/bin/python3(自带 PyMuPDF) + bin/ocr(macOS Vision 本机 OCR, 仓库已编译)
"""
import hashlib, json, os, re, socket, subprocess, sys, tempfile, threading, time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import fitz

import mock_exam
import live_version as lv

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8930
LAN_MODE = False            # main() 里按 --lan 设置
DATA_VERSION = 0            # 发布成功后 +1, 前端据此刷新缓存数据
OCR_BIN = os.path.join(ROOT, "bin", "ocr")
UNITS_DIR = os.path.join(ROOT, "data", "units")
LOC_DIR = os.path.join(ROOT, "data", "loc")
MSLOC_DIR = os.path.join(ROOT, "data", "msloc")
CACHE_DIR = os.path.join(ROOT, "data", "cache", "crops")
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads")
MAX_PHOTO = 15 * 1024 * 1024
MAX_JSON = 64 * 1024
MAX_ADMIN_JSON = 8 * 1024 * 1024      # AI 结果导入上限 8MB
MOCK_SEMAPHORE = threading.Semaphore(2)   # 同时生成的模拟卷 PDF 上限(限制工作量)
IMG_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "heic": "image/heic"}


def apply_root(root):
    """以另一仓库根启动(--root): 切换本模块与业务层的全部路径常量。
    对外可见状态(SERVED/BOOT)一并重置 —— 测试切换根目录不得泄漏上个仓库
    的版本/索引。"""
    global ROOT, OCR_BIN, UNITS_DIR, LOC_DIR, MSLOC_DIR, CACHE_DIR, UPLOAD_DIR
    global BOOT_VID
    ROOT = root
    OCR_BIN = os.path.join(ROOT, "bin", "ocr")
    UNITS_DIR = os.path.join(ROOT, "data", "units")
    LOC_DIR = os.path.join(ROOT, "data", "loc")
    MSLOC_DIR = os.path.join(ROOT, "data", "msloc")
    CACHE_DIR = os.path.join(ROOT, "data", "cache", "crops")
    UPLOAD_DIR = os.path.join(ROOT, "data", "uploads")
    BOOT_VID = None
    SERVED.update({"vid": None, "idx": None, "mock": None})
    mock_exam.ROOT = root
    mock_exam.UNITS_DIR = os.path.join(root, "data", "units")
    mock_exam.LOC_DIR = os.path.join(root, "data", "loc")
    mock_exam.MSLOC_DIR = os.path.join(root, "data", "msloc")
    lv.apply_root(root)
    import paper_pipeline, migrate_syllabus, pipeline_service
    paper_pipeline._apply_root(root)
    migrate_syllabus._apply_root(root)
    pipeline_service.apply_root(root)


# ============================ 发布后索引同进程刷新(P0-2/P0-4) ============================
_REFRESH_LOCK = threading.Lock()

# ---- 对外单一可见版本(P0-2, 第六轮) ----
# SERVED 是对外可见状态的唯一入口: (vid, 搜题索引, 组卷索引) 一次原子换入。
# 静态受管文件(index.html/*_data_*.js/…)、题目详情/裁图、搜题与组卷 API 一律
# 从 versions/<SERVED.vid> 读取 —— live 翻转后、索引刷新完成前, 对外**整体
# 保持旧版**(全旧), 刷新成功后**整体切换到新版**(全新), 任何时刻不存在
# "新版详情 + 旧版搜题"的混合。刷新失败则一直保持完整旧版直到重试成功。
SERVED = {"vid": None, "idx": None, "mock": None}
BOOT_VID = None            # 启动时的活动版本(启动索引构建基准)


def _capture_boot_vid():
    """启动/首次对外读取时记录活动版本: SERVED 安装前的对外基准。
    同时写保留标记(第七轮 P0): 任何进程(含 CLI 发布)的版本回收都不得
    删除正在对外服务的 BOOT/SERVED 版本。"""
    global BOOT_VID
    if BOOT_VID is None:
        try:
            BOOT_VID = lv.active_version()
        except Exception:
            BOOT_VID = None
    if BOOT_VID:
        lv.mark_retained(BOOT_VID)
        # 新进程只可能服务 BOOT: 其余遗留 retain 标记(上个进程的 SERVED)转为
        # 退役 —— 宽限期后可回收, 避免重启导致保留标记永久泄漏。
        try:
            for f in os.listdir(lv.VERSIONS_DIR):
                if f.endswith(".retain"):
                    old = f[:-len(".retain")]
                    if old != BOOT_VID:
                        lv.mark_retired(old)
        except OSError:
            pass
    return BOOT_VID


def served_state():
    """当前对外可见状态(单次读引用, 请求内一致): (vid, search_idx, mock_idx)。"""
    s = SERVED
    if s["idx"] is not None:
        return s
    return {"vid": _capture_boot_vid(), "idx": IDX, "mock": mock_exam._IDX}


def served_vid():
    return served_state()["vid"]


def served_base():
    """对外可见版本目录; 无版本布局时 None(回退仓库根真实路径)。"""
    vid = served_vid()
    if vid is None:
        return None
    return os.path.join(lv.VERSIONS_DIR, vid)


def _install_served(vid, search_idx, mock_idx):
    """一次换入对外可见状态(GIL 下单次 dict 赋值对读者原子)。
    版本保留协议(第七轮 P0): 新对外版本立即写保留标记; 旧对外版本仅在
    此时(切换完成后, 新请求不再到达旧版)打退役标记, 满 GC 宽限期(默认
    600s, 覆盖在途请求)后才可被任何进程的 GC 回收。"""
    prev = SERVED.get("vid")
    if vid:
        lv.mark_retained(vid)
    if prev and prev != vid:
        lv.mark_retired(prev)
    new_state = {"vid": vid, "idx": search_idx, "mock": mock_idx}
    SERVED.update(new_state)


def refresh_indexes(subject=None, timeout=600):
    """发布成功后由业务层钩子调用(同步): **针对当前活动版本目录**构建两个
    索引(units/loc/msloc/topics 同一显式版本根), 都构建并校验完成后才一次
    换入 SERVED 并递增 data_version。构建失败抛异常(发布方据此显式报告刷新
    失败, 对外保持旧版), 绝不先递增版本后慢慢构建。"""
    global IDX, DATA_VERSION
    with _REFRESH_LOCK:
        lv.ensure_layout()                   # 幂等: 确保版本布局存在(基线采纳)
        vid = lv.active_version()
        base = os.path.join(lv.VERSIONS_DIR, vid) if vid else None
        data_base = os.path.join(base, "data") if base else None
        new_search = SearchIndex()
        new_search.build(data_base)             # 失败直接抛出
        if not new_search.ready or not new_search.docs:
            raise RuntimeError("搜题索引构建异常(空索引)")
        new_mock = mock_exam.MockIndex()
        new_mock.build(base)                    # base=None → 回退根目录
        if not new_mock.ready or not new_mock.subjects:
            raise RuntimeError("组卷索引构建异常(空索引)")
        # ---- 全部就绪, 一次换入对外可见版本 ----
        _install_served(vid, new_search, new_mock)
        IDX = new_search                        # 兼容引用/测试
        mock_exam._IDX = new_mock
        DATA_VERSION += 1
        print(f"[refresh] 索引已安装(版本 {vid}): search={len(new_search.docs)} 单元, "
              f"mock={len(new_mock.subjects)} 科, data_version={DATA_VERSION}")
        return DATA_VERSION


def pinned_data_dirs():
    """请求级版本钉住(P0-2): 三棵数据树(units/loc/msloc)一律从**对外可见
    版本**(SERVED/BOOT)读取 —— 发布翻转后、索引刷新完成前, 详情/裁图与
    搜题/组卷/静态页整体同为旧版; 刷新成功后整体同为新版。无版本布局时
    回退模块目录常量(与历史行为一致, 兼容测试对常量的替换)。"""
    base = served_base()
    if base is None:
        return {"units": UNITS_DIR, "loc": LOC_DIR, "msloc": MSLOC_DIR}
    return {s: os.path.join(base, "data", s) for s in ("units", "loc", "msloc")}

# ============ 分词: 与 gen_search.py 保持逐字同步(改一处须同步所有副本) ============
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


# ============================ 全文检索索引(启动时构建) ============================
class SearchIndex:
    """BM25 over 47,218 单元全文(含未进旧索引的单元)。"""

    def __init__(self):
        self.docs = []            # [{subject, code, uid, ...unit fields}]
        self.postings = {}        # stem -> {doc_id: tf}
        self.ready = False
        self.avgdl = 1.0

    def build(self, data_base=None):
        t0 = time.time()
        n_skip = 0
        # 版本钉住(P0-2): 显式 data_base(versions/<vid>/data)或对外可见版本
        units_base = (os.path.join(data_base, "units") if data_base
                      else pinned_data_dirs()["units"])
        for subject in sorted(os.listdir(units_base)):
            sd = os.path.join(units_base, subject)
            if not os.path.isdir(sd):
                continue
            for f in sorted(os.listdir(sd)):
                if not f.endswith(".json"):
                    continue
                rec = json.load(open(os.path.join(sd, f)))
                for u in rec["units"]:
                    # 正式搜题索引只收 IN 单元; OUT(考纲外)/PENDING(待复核)
                    # 保留在 data/units 审计记录但不进入任何搜题渠道
                    if u.get("status") != "IN":
                        n_skip += 1
                        continue
                    toks = tokenize(u["text"])
                    di = len(self.docs)
                    self.docs.append({
                        "subject": subject, "code": rec["code"], "uid": u["id"],
                        "q": u["q"], "part": u["part"], "page": u["page"],
                        "marks": u["marks"], "topics": u["topics"],
                        "in_old_index": u.get("in_old_index"),
                        "status": u.get("status"),
                        "year": rec["year"], "session": rec["session"],
                        "paper": rec["paper"], "variant": rec["variant"],
                        "text": u["text"], "ntok": len(toks)})
                    for t in toks:
                        occ = self.postings.setdefault(t, {})
                        occ[di] = occ.get(di, 0) + 1
        self.avgdl = (sum(d["ntok"] for d in self.docs) / max(1, len(self.docs))) or 1.0
        self.ready = True
        print(f"[index] {len(self.docs)} IN units (skipped {n_skip} OUT/PENDING), "
              f"vocab={len(self.postings)}, {time.time()-t0:.1f}s")

    def search(self, query, subject=None, topn=20):
        if not self.ready:
            return []
        qt = tokenize(query)
        if not qt:
            return []
        n = len(self.docs)
        K1, B = 1.2, 0.75
        score = {}
        for st in set(qt):
            occ = self.postings.get(st)
            if not occ:
                continue
            dfi = len(occ)
            idf = __import__("math").log(1 + (n - dfi + 0.5) / (dfi + 0.5))
            for di, tf in occ.items():
                d = self.docs[di]
                if subject and d["subject"] != subject:
                    continue
                score[di] = score.get(di, 0.0) + idf * tf * (K1 + 1) / \
                    (tf + K1 * (1 - B + B * d["ntok"] / self.avgdl))
        # 短语加成: 查询前 3 个词干按序出现在文本中
        phrase = " ".join(qt[:3])
        for di in score:
            low = self.docs[di]["text"].lower()
            if phrase and phrase in low:
                score[di] *= 1.4
        order = sorted(score, key=lambda d: -score[d])[:topn]
        out = []
        for di in order:
            d = self.docs[di]
            snip = self._snippet(d["text"], qt)
            out.append({k: d[k] for k in ("subject", "code", "uid", "q", "part",
                                          "marks", "topics", "in_old_index",
                                          "status", "year",
                                          "session", "paper", "variant")}
                       | {"score": round(score[di], 4), "snippet": snip})
        return out

    @staticmethod
    def _snippet(text, qt, width=140):
        low = text.lower()
        best = -1
        for st in qt[:6]:
            i = low.find(st)
            if i >= 0 and (best < 0 or i < best):
                best = i
        s = max(0, best - 40)
        return ("…" if s else "") + " ".join(text[s:s + width].split()) + \
               ("…" if s + width < len(text) else "")


IDX = SearchIndex()

# ============================ 裁图渲染与缓存 ============================
DPI_BY_MAXW = re.compile(r"^(\d{3,4})$")


def safe_under_root(rel):
    """解析相对路径并确保落在 ROOT 内; 拒绝穿越。返回绝对路径或 None。"""
    p = os.path.normpath(os.path.join(ROOT, rel))
    if p == ROOT or p.startswith(ROOT + os.sep):
        if any(part.startswith(".") for part in p.split(os.sep)[len(ROOT.split(os.sep)):]):
            return None
        return p
    return None


def load_json_under(rel):
    p = safe_under_root(rel)
    if not p or not os.path.isfile(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def _load_json_abs(p):
    if not os.path.isfile(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def unit_record(subject, code, dirs=None):
    if dirs is None:
        dirs = pinned_data_dirs()
    p = os.path.join(dirs["units"], subject, code + ".json")
    if not os.path.isfile(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def crop_key(subject, code, uid, kind, pdf_rel, loc_path):
    st = os.stat(os.path.join(ROOT, pdf_rel))
    lt = os.stat(loc_path).st_mtime if os.path.exists(loc_path) else 0
    raw = f"{subject}|{code}|{uid}|{kind}|{st.st_mtime_ns}|{st.st_size}|{lt}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def render_crop(pdf_rel, segs, maxw=900, normalize_rotation=False):
    """按 seg 逐段渲染 PNG; 多段(跨页)用 _stack_with_fitz 垂直拼接。
    normalize_rotation: MS 定位数据(locate_ms)在 remove_rotation 后的坐标系里,
    MS 裁图必须同样归一; QP 定位未归一(且 QP 无旋转页)保持原样。"""
    doc = fitz.open(os.path.join(ROOT, pdf_rel))
    imgs = []
    try:
        for seg in segs:
            pno, x0, y0, x1, y1 = int(seg[0]) - 1, *seg[1:5]
            if pno < 0 or pno >= doc.page_count:
                continue
            page = doc[pno]
            if normalize_rotation and page.rotation:
                page.remove_rotation()
            rect = fitz.Rect(x0, y0, x1, y1) & page.rect
            if rect.is_empty:
                continue
            dpi = int(max(72, min(220, maxw / max(1e-6, rect.width) * 72)))
            pix = page.get_pixmap(dpi=dpi, clip=rect)
            imgs.append(pix.tobytes("png"))
    finally:
        doc.close()
    if not imgs:
        return None
    if len(imgs) == 1:
        return imgs[0]
    return _stack_with_fitz(imgs)


def _stack_with_fitz(pngs):
    """用 fitz 新建空白 PDF 页嵌图再整体转 PNG —— 拼接多页裁切。"""
    doc = fitz.open()
    imgs = [fitz.open(stream=b, filetype="png") for b in pngs]
    W = max(d[0].rect.width for d in imgs)
    H = sum(d[0].rect.height for d in imgs) + 12 * (len(imgs) - 1)
    page = doc.new_page(width=W, height=H)
    y = 0
    for d in imgs:
        r = d[0].rect
        page.insert_image(fitz.Rect(0, y, r.width, y + r.height),
                          pixmap=d[0].get_pixmap())
        y += r.height + 12
        d.close()
    pix = page.get_pixmap(dpi=150)
    out = pix.tobytes("png")
    doc.close()
    return out


# ============================ 图片魔数校验 ============================
def sniff_image(head):
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    for sig in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        if len(head) >= 12 and head[4:12] == sig:
            return "heic"
    return None


# ============================ HTTP Handler ============================
class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "zhenjuan/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- 静态访问白名单 ----------
    # 只开放: 页面 html / assets / 数据 JS / notes / syllabus / 各科 QP·MS PDF。
    # server.py、*.py、data/(units/loc/msloc/缓存/上传)、tags_*、classified_* 等一律拒绝。
    ALLOWED_PAGES = {"index.html", "notes.html", "syllabus.html"}
    PAGE_RE = re.compile(r"^index_\d{4}(_topics)?\.html$")
    DATA_JS_RE = re.compile(r"^(search|papers|topics)_data_\d{4}\.js$")
    SUBJECT_DIR_RE = re.compile(r"^\d{4}（.*）$")

    @classmethod
    def static_allowed(cls, rel):
        rel = rel.lstrip("/")
        if not rel:
            return False
        parts = rel.split("/")
        if any(p in ("", ".", "..") for p in parts):
            return False
        top = parts[0]
        if top in ("assets", "notes", "syllabus"):
            return len(parts) >= 2                      # 只允许子文件, 不列目录
        if top in cls.ALLOWED_PAGES or cls.PAGE_RE.match(top) \
                or cls.DATA_JS_RE.match(top):
            return len(parts) == 1
        if cls.SUBJECT_DIR_RE.match(top):
            return rel.lower().endswith(".pdf")         # 各科目录只开放 QP/MS PDF
        return False

    def list_directory(self, path):
        self.send_error(404, "No directory listing")
        return None

    # ---------- GET ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if route.startswith("/api/pipeline/"):
            return self._api_pipeline_get(parsed)
        if route == "/api/health":
            idx = served_state()["idx"]
            return self._json({"ok": True, "units": len(idx.docs),
                               "ready": idx.ready, "root": ROOT,
                               "data_version": DATA_VERSION,
                               "served_version": served_vid()})
        if route == "/api/search":
            qs = urllib.parse.parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0].strip()
            subject = (qs.get("subject") or [None])[0]
            if not q:
                return self._json({"results": []})
            return self._json({"query": q,
                               "results": served_state()["idx"].search(q, subject)})
        if route == "/api/papers":
            return self._api_papers(urllib.parse.parse_qs(parsed.query))
        if route == "/api/unit":
            return self._api_unit(urllib.parse.parse_qs(parsed.query))
        if route == "/api/crop":
            return self._api_crop(urllib.parse.parse_qs(parsed.query))
        if route == "/api/mock/meta":
            return self._api_mock_meta(urllib.parse.parse_qs(parsed.query))
        if route.startswith("/api/"):
            return self.send_error(404, "unknown api")
        if route == "/":
            route = "/index.html"
        # 静态文件 / PDF(Range)
        return self._serve_static(route)

    def _serve_static(self, full_path):
        rel = urllib.parse.unquote(
            urllib.parse.urlparse(full_path).path).lstrip("/")
        if not self.static_allowed(rel):
            self.send_error(403, "Forbidden")
            return
        # P0-2: 受管正式产物(首页/数据 JS/stub)从**对外可见版本**读取 ——
        # live 翻转后、索引刷新前对外整体保持旧版; 非受管(assets/PDF/notes)
        # 跨版本不变, 仍从仓库根读取。
        base = ROOT
        if lv.is_managed_root(rel.split("/")[0]):
            sb = served_base()
            if sb:
                base = sb
        path = os.path.realpath(os.path.join(base, rel))
        root_real = os.path.realpath(ROOT)
        if not path.startswith(root_real + os.sep):     # 符号链接越界
            self.send_error(403, "Forbidden")
            return
        if os.path.isdir(path) or not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_error(404, "File not found")
            return
        ctype = self.guess_type(path)
        if ctype == "application/pdf":
            self._serve_file_range(path, size, ctype)
            return
        with open(path, "rb") as f:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(f.read())

    def _serve_file_range(self, path, size, ctype):
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:                       # suffix range: bytes=-N
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                partial = True
        with open(path, "rb") as f:
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            if self.command != "HEAD":
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    # ---------- API 实现 ----------
    def _api_papers(self, qs):
        subject = (qs.get("subject") or [None])[0]
        out = []
        units_base = pinned_data_dirs()["units"]        # 请求级版本钉住
        subjects = [subject] if subject else sorted(os.listdir(units_base))
        for s in subjects:
            sd = os.path.join(units_base, s)
            if not os.path.isdir(sd):
                continue
            for f in sorted(os.listdir(sd)):
                if not f.endswith(".json"):
                    continue
                rec = json.load(open(os.path.join(sd, f)))
                out.append({
                    "subject": s, "code": rec["code"], "year": rec["year"],
                    "session": rec["session"], "paper": rec["paper"],
                    "variant": rec["variant"],
                    "units": [{"uid": u["id"], "q": u["q"], "part": u["part"],
                               "marks": u["marks"]} for u in rec["units"]]})
        return self._json({"papers": out})

    def _api_unit(self, qs):
        subject = (qs.get("subject") or [""])[0]
        code = (qs.get("code") or [""])[0]
        uid = (qs.get("uid") or [""])[0]
        if not re.fullmatch(r"\d{4}", subject) or not re.fullmatch(r"[a-z0-9_#]+", code) \
                or not re.fullmatch(r"[0-9a-h#]+", uid):
            return self.send_error(400, "bad params")
        dirs = pinned_data_dirs()                     # 请求级版本钉住
        rec = unit_record(subject, code, dirs)
        if not rec:
            return self.send_error(404, "paper not found")
        u = next((x for x in rec["units"] if x["id"] == uid), None)
        if u is None:
            # 旧索引结果只用 q+part 寻址; OPTIONS 重编号单元 id 带 #2 后缀 → 唯一匹配回退
            cands = [x for x in rec["units"] if str(x["q"]) + (x["part"] or "") == uid]
            if len(cands) == 1:
                u = cands[0]
        if not u:
            return self.send_error(404, "unit not found")
        uid = u["id"]                    # 回退匹配后一律用真实单元 id 取定位/裁图
        loc = _load_json_abs(os.path.join(dirs["loc"], subject, code + ".json")) or {}
        msloc = _load_json_abs(os.path.join(dirs["msloc"], subject, code + ".json")) or {}
        uloc = (loc.get("units") or {}).get(uid)
        qloc = (loc.get("questions") or {}).get(str(u["q"]))
        ums = (msloc.get("units") or {}).get(uid)

        def key_of(kind, pdf_rel, loc_path):
            return crop_key(subject, code, uid, kind, pdf_rel, loc_path)

        kp = os.path.join(dirs["loc"], subject, code + ".json")
        km = os.path.join(dirs["msloc"], subject, code + ".json")
        qp_pdf = rec["qp_pdf"]
        qp = {"status": (uloc or {}).get("status", "fail"),
              "seg": (uloc or {}).get("seg"),
              "img": None}
        qpq = {"status": (qloc or {}).get("status", "fail"),
               "seg": (qloc or {}).get("seg"), "img": None}
        if qp["seg"]:
            qp["img"] = f"/api/crop?subject={subject}&code={code}&uid={urllib.parse.quote(uid)}&kind=qp&v={key_of('qp', qp_pdf, kp)}"
        if qpq["seg"]:
            qpq["img"] = f"/api/crop?subject={subject}&code={code}&uid={urllib.parse.quote(uid)}&kind=qpq&v={key_of('qpq', qp_pdf, kp)}"
        ms_status_paper = msloc.get("ms_status", "missing")
        ms = {"paper_status": ms_status_paper,
              "status": (ums or {}).get("status", "not_found"),
              "seg": (ums or {}).get("seg"),
              "letter": (ums or {}).get("letter"),
              "roman_check": (ums or {}).get("roman_check"),
              "img": None}
        if ms["seg"]:
            ms["img"] = f"/api/crop?subject={subject}&code={code}&uid={urllib.parse.quote(uid)}&kind=ms&v={key_of('ms', rec['ms_pdf'], km)}"
        return self._json({
            "subject": subject, "code": code, "uid": uid,
            "meta": {k: rec[k] for k in ("year", "session", "paper", "variant",
                                         "qp_pdf", "ms_pdf")},
            "unit": {"status": u.get("status"), **{k: u.get(k) for k in (
                "id", "q", "part", "page", "marks", "text", "topics",
                "in_old_index")}, "reason": u.get("reason")},
            "qp": qp, "qp_question": qpq, "ms": ms})

    def _api_crop(self, qs):
        subject = (qs.get("subject") or [""])[0]
        code = (qs.get("code") or [""])[0]
        uid = (qs.get("uid") or [""])[0]
        kind = (qs.get("kind") or ["qp"])[0]
        v = (qs.get("v") or [""])[0]
        if kind not in ("qp", "qpq", "ms", "msq"):
            return self.send_error(400, "bad kind")
        dirs = pinned_data_dirs()                     # 请求级版本钉住
        rec = unit_record(subject, code, dirs)
        if not rec:
            return self.send_error(404, "paper not found")
        u = next((x for x in rec["units"] if x["id"] == uid), None)
        if u is None:
            cands = [x for x in rec["units"] if str(x["q"]) + (x["part"] or "") == uid]
            if len(cands) == 1:
                u = cands[0]
        if not u:
            return self.send_error(404, "unit not found")
        uid = u["id"]
        if kind in ("qp", "qpq"):
            loc = _load_json_abs(os.path.join(dirs["loc"], subject, code + ".json")) or {}
            entry = (loc.get("units") or {}).get(uid) if kind == "qp" \
                else (loc.get("questions") or {}).get(str(u["q"]))
            pdf_rel = rec["qp_pdf"]
            loc_path = os.path.join(dirs["loc"], subject, code + ".json")
        else:
            loc = _load_json_abs(os.path.join(dirs["msloc"], subject, code + ".json")) or {}
            entry = (loc.get("units") or {}).get(uid) if kind == "ms" \
                else (loc.get("questions") or {}).get(str(u["q"]))
            pdf_rel = rec.get("ms_pdf")
            loc_path = os.path.join(dirs["msloc"], subject, code + ".json")
        if not entry or not entry.get("seg") or not pdf_rel:
            return self.send_error(404, "no crop")
        key = crop_key(subject, code, uid, kind, pdf_rel, loc_path)
        if v and v != key:                       # 缓存失效: 源 PDF/定位数据已变
            return self.send_error(410, "stale key, refresh unit data")
        cache = os.path.join(CACHE_DIR, f"{key}.png")
        if not os.path.exists(cache):
            png = render_crop(pdf_rel, entry["seg"],
                              normalize_rotation=kind in ("ms", "msq"))
            if not png:
                return self.send_error(404, "render failed")
            tmp = cache + ".tmp"
            with open(tmp, "wb") as f:
                f.write(png)
            os.replace(tmp, cache)
        with open(cache, "rb") as f:
            data = f.read()
        etag = f'"{key}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    # ---------- POST ----------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/pipeline/"):
            return self._api_pipeline_post(parsed)
        if parsed.path == "/api/photo":
            return self._api_photo()
        if parsed.path == "/api/mock/preview":
            return self._api_mock("preview")
        if parsed.path == "/api/mock/pdf":
            return self._api_mock("pdf")
        if parsed.path == "/api/mock/answers":
            return self._api_mock("answers")
        if parsed.path == "/api/mock/zip":
            return self._api_mock("zip")
        if parsed.path == "/api/search":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_JSON:
                return self.send_error(413, "payload too large")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                q = str(body.get("q", ""))[:2000]
                subject = body.get("subject")
                if subject and not re.fullmatch(r"\d{4}", str(subject)):
                    subject = None
            except Exception:
                return self.send_error(400, "bad json")
            return self._json({"query": q,
                               "results": served_state()["idx"].search(q, subject)})
        return self.send_error(404, "unknown api")

    # ---------- 模拟组卷 API ----------
    def _api_mock_meta(self, qs):
        """GET /api/mock/meta?subject=9702 → 该科可用 Paper/年份范围/大纲树/适配说明。"""
        subject = (qs.get("subject") or [""])[0]
        idx = served_state()["mock"] or mock_exam.get_index()
        if idx is None:
            return self._json({"ok": False, "error": "索引未初始化"}, status=503)
        try:
            if not re.fullmatch(r"\d{4}", subject):
                raise mock_exam.MockError("subject 应为 4 位科目代码，如 9702")
            return self._json({"ok": True} | idx.meta(subject))
        except mock_exam.MockError as e:
            return self._json({"ok": False, "error": e.message, "code": e.code},
                              status=400)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_JSON:
            raise mock_exam.MockError("请求体缺失或超过 64KB 上限", code="too-large")
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            raise mock_exam.MockError("请求体不是有效 JSON", code="bad-json")
        if not isinstance(body, dict):
            raise mock_exam.MockError("请求体应为 JSON 对象", code="bad-json")
        return body

    def _api_mock(self, mode):
        """POST /api/mock/preview | pdf | answers | zip
        body: {subject, papers:[1,2], topics:["3","4.1"], year_from, year_to,
               target, seed, require_each_paper, mode, selection_id}
        → 预览 JSON / 试题 PDF / 同一冻结选题的官方答案 PDF / 双 PDF ZIP。
        参数严格校验; 分值与选题一律由服务端重新核定, 不接受浏览器传来的
        分值/题目/路径; 下载需携带预览返回的 selection_id, 过期/源文件变化
        返回 409 要求重新预览; PDF 生成用信号量限并发(2)。"""
        idx = served_state()["mock"] or mock_exam.get_index()
        if idx is None:
            return self._json({"ok": False, "error": "索引未初始化"}, status=503)
        try:
            body = self._read_json_body()
            if mode == "preview":
                return self._json(mock_exam.run_preview(idx, body))
            with MOCK_SEMAPHORE:               # 限制并发生成的工作量
                if mode == "pdf":
                    filename, data = mock_exam.run_qp_pdf(idx, body)
                    ctype = "application/pdf"
                elif mode == "answers":
                    filename, data = mock_exam.run_answers_pdf(idx, body)
                    ctype = "application/pdf"
                elif mode == "zip":
                    filename, data = mock_exam.run_zip(idx, body)
                    ctype = "application/zip"
                else:                          # pragma: no cover(路由已挡)
                    return self.send_error(404, "unknown api")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except mock_exam.MockError as e:
            status = {"not-ready": 503, "index-error": 503,
                      "stale": 409, "render-failed": 500,
                      "bad-selection-id": 400, "too-large": 413}.get(e.code, 400)
            return self._json({"ok": False, "error": e.message, "code": e.code},
                              status=status)
        except Exception as e:                  # 兜底: 不泄漏堆栈给客户端
            sys.stderr.write(f"[mock] {mode} error: {e!r}\n")
            return self._json({"ok": False, "error": "生成失败，请稍后重试"},
                              status=500)

    def _api_photo(self):
        raw_ct = self.headers.get("Content-Type") or ""
        ctype = raw_ct.lower()
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_PHOTO:
            return self.send_error(413, "photo too large (max 15MB)")
        body = self.rfile.read(length)
        if ctype.startswith("multipart/form-data"):
            # boundary 取原始大小写(分隔符区分大小写, 不能用 lower 后的头部)
            m = re.search(r'boundary="?([^";]+)"?', raw_ct)
            if not m:
                return self.send_error(400, "bad multipart")
            boundary = b"--" + m.group(1).encode()
            extracted = b""
            for part in body.split(boundary):
                if b"\r\n\r\n" not in part:
                    continue
                head, _, payload = part.partition(b"\r\n\r\n")
                if b"filename=" in head:
                    extracted = payload[:-2] if payload.endswith(b"\r\n") else payload
                    break
            if not extracted:
                return self.send_error(400, "multipart has no file part")
            body = extracted
        kind = sniff_image(body[:16])
        if not kind:
            return self.send_error(415, "only jpeg/png/webp/heic images")
        # 临时文件: OCR 完立即删除(不留存用户照片)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix="." + kind, dir=UPLOAD_DIR)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            r = subprocess.run([OCR_BIN, tmp], capture_output=True, timeout=40)
            if r.returncode != 0:
                return self._json({"ok": False, "error": "OCR failed"},
                                  status=500)
            text = r.stdout.decode("utf-8", "replace").strip()
            return self._json({"ok": True, "kind": kind, "chars": len(text),
                               "text": text})
        except subprocess.TimeoutExpired:
            return self._json({"ok": False, "error": "OCR timeout"}, status=504)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ---------- 题库管理 API(仅本机写; --lan 下写接口限 127.0.0.1) ----------
    def _client_loopback(self):
        ip = self.client_address[0]
        return ip in ("127.0.0.1", "::1")

    def _admin_write_allowed(self):
        """管理写接口默认仅本机; --lan 绑定 0.0.0.0 时拒绝局域网写。"""
        if self._client_loopback():
            return True
        return not LAN_MODE      # 非 lan 模式必然只绑 127.0.0.1(防御性)

    def _api_pipeline_get(self, parsed):
        import pipeline_service as ps
        route = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        def q1(k, default=None):
            return (qs.get(k) or [default])[0]

        try:
            if route == "/api/pipeline/overview":
                idx = served_state()["idx"]
                return self._json({"ok": True, "subjects": ps.overview(),
                                   "data_version": DATA_VERSION,
                                   "served_version": served_vid(),
                                   "index": {"units": len(idx.docs),
                                             "ready": idx.ready}})
            if route == "/api/pipeline/version":
                return self._json({"ok": True, "data_version": DATA_VERSION,
                                   "units": len(IDX.docs)})
            if route == "/api/pipeline/tasks":
                return self._json({"ok": True,
                                   "tasks": ps.get_manager().list(
                                       limit=int(q1("limit", "100") or 100))})
            m = re.fullmatch(r"/api/pipeline/tasks/([A-Za-z0-9_]+)", route)
            if m:
                t = ps.get_manager().get(m.group(1))
                if not t:
                    return self._json({"ok": False, "error": "任务不存在"},
                                      status=404)
                return self._json({"ok": True, "task": t})
            if route == "/api/pipeline/adjudication":
                return self._json({"ok": True, "data": ps.adjudication_page(
                    q1("subject", ""),
                    paper=q1("paper"), status=q1("status"),
                    page=int(q1("page", "1") or 1),
                    page_size=int(q1("page_size", "50") or 50),
                    search=q1("search"))})
            if route == "/api/pipeline/classify":
                return self._json({"ok": True,
                                   **ps.classify_inputs(q1("subject", ""))})
            if route == "/api/pipeline/classify-file":
                return self._json({"ok": True, "name": q1("name", ""),
                                   "content": ps.classify_input_content(
                                       q1("subject", ""), q1("name", ""))})
            if route == "/api/pipeline/runs":
                return self._json({"ok": True,
                                   "runs": ps.runs_list(q1("subject"),
                                                        limit=int(q1("limit", "50") or 50))})
            if route == "/api/pipeline/run":
                return self._json({"ok": True,
                                   "report": ps.run_report(q1("file", ""))})
            return self.send_error(404, "unknown pipeline api")
        except ps.ParamError as e:
            return self._json({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            sys.stderr.write(f"[pipeline-api] GET {route} error: {e!r}\n")
            return self._json({"ok": False, "error": str(e)}, status=500)

    def _api_pipeline_post(self, parsed):
        import pipeline_service as ps
        route = parsed.path
        # 写接口防护: 非本机(且 --lan 模式)一律拒绝
        if not self._admin_write_allowed():
            return self._json({"ok": False, "code": "forbidden",
                               "error": "管理写接口仅限本机(127.0.0.1)调用; "
                                        "--lan 模式下局域网客户端只读"},
                              status=403)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_ADMIN_JSON:
            return self._json({"ok": False, "error": "请求体缺失或超过 8MB 上限"},
                              status=413)
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("body 应为 JSON 对象")
        except Exception:
            return self._json({"ok": False, "error": "请求体不是有效 JSON"},
                              status=400)
        try:
            tm = ps.get_manager()
            if route == "/api/pipeline/tasks":
                action, params = body.get("action"), body.get("params") or {}
                t = tm.create(action, params)
                return self._json({"ok": True, "task": {
                    k: t.get(k) for k in ("id", "action", "params", "status",
                                          "created")}}, status=201)
            m = re.fullmatch(r"/api/pipeline/tasks/([A-Za-z0-9_]+)/(retry|cancel)",
                             route)
            if m:
                t = tm.retry(m.group(1)) if m.group(2) == "retry" \
                    else tm.cancel(m.group(1))
                return self._json({"ok": True, "task": {
                    k: t.get(k) for k in ("id", "action", "params", "status",
                                          "created")}})
            if route == "/api/pipeline/adjudication":
                res = ps.adjudication_update(body.get("subject", ""),
                                             body.get("updates"))
                return self._json(res, status=200 if res["ok"] else 400)
            if route == "/api/pipeline/reload":
                try:
                    ver = refresh_indexes()   # 同步: 安装完成才返回新版本号
                    return self._json({"ok": True, "data_version": ver})
                except Exception as e:
                    return self._json({"ok": False, "error": f"索引刷新失败: {e}"},
                                      status=500)
            return self.send_error(404, "unknown pipeline api")
        except ps.ParamError as e:
            return self._json({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            sys.stderr.write(f"[pipeline-api] POST {route} error: {e!r}\n")
            return self._json({"ok": False, "error": str(e)}, status=500)

    # ---------- helpers ----------
    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))          # 不实际发包
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _startup_served_coordinator():
    """等待启动期两个索引构建完成, 把对外可见状态安装到启动版本(P0-2)。
    在此之前对外走 BOOT 回退(同一版本根), 不递增 data_version。"""
    boot = _capture_boot_vid()
    deadline = time.time() + 300
    while time.time() < deadline:
        if IDX.ready and mock_exam._IDX is not None \
                and getattr(mock_exam._IDX, "ready", False):
            _install_served(boot, IDX, mock_exam._IDX)
            print(f"[startup] 对外可见版本就绪: {boot} "
                  f"(search={len(IDX.docs)} 单元)")
            return
        if getattr(IDX, "error", None) or getattr(
                getattr(mock_exam, "_IDX", None), "error", None):
            print("[startup] ⚠ 启动索引构建失败, 对外暂用 BOOT 版本回退; "
                  "修复后请在网页点「刷新服务索引」")
            return
        time.sleep(0.2)


def main():
    import argparse
    global LAN_MODE
    ap = argparse.ArgumentParser()
    ap.add_argument("--lan", action="store_true", help="监听 0.0.0.0 (手机可访问); 默认仅 127.0.0.1")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--root", help="以另一仓库根目录启动(隔离测试夹具用)")
    args = ap.parse_args()
    if args.root:
        apply_root(os.path.abspath(os.path.expanduser(args.root)))
    LAN_MODE = args.lan
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    lv.ensure_layout()   # 版本布局自愈(崩溃恢复/首次采纳), 索引从活动版本构建
    _capture_boot_vid()  # 启动版本 = 当前活动版本(启动索引的构建基准)
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    threading.Thread(target=IDX.build, daemon=True).start()
    mock_exam.init_index(background=True)      # 模拟组卷索引(核分/定位/标签)
    threading.Thread(target=_startup_served_coordinator, daemon=True).start()
    # 发布成功 → 同一进程刷新搜题/组卷索引(不重启服务)
    import pipeline_service
    pipeline_service.ms.register_refresh_hook(refresh_indexes)
    pipeline_service.get_manager()             # 后台任务管理器(启动时恢复中断任务)
    httpd = ThreadingHTTPServer((host, args.port), Handler)
    url = f"http://{lan_ip()}:{args.port}" if args.lan else f"http://127.0.0.1:{args.port}"
    print(f"真卷工作台服务已启动: {url}  (绑定 {host}:{args.port}, root={ROOT})")
    if args.lan:
        print(f"手机访问: {url}/index.html#/photo  (需与本机同一局域网; "
              f"管理写接口仅限本机)")
    print("Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
