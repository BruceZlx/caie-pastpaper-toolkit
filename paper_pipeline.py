#!/usr/bin/env python3
"""paper_pipeline.py —— 真题库增量同步 / 新科目接入 / 大纲版本管理 统一 CLI。

实现 PAST_PAPER_PIPELINE_PLAN.md 的短期目标一、二：
  sync      已有科目同步来源站当前已发布的 QP/MS（增量、幂等、可恢复）
  onboard   按科目代码 + 年份接入新科目（来源自动发现 / 手工 URL 兜底）
  syllabus  检查并下载 Cambridge International 官方最新已发布 syllabus(+update)
  extract   增量切题到仓库内工作目录（替代 /tmp/qp<code>/units）
  classify-prep  生成 AI 判定输入（TOPICS.txt + 每卷题目清单）
  publish   校验判定(IN/OUT/PENDING) → 快照 → 原子发布 → 重建索引
  migrate   四科旧题库按最新大纲迁移（委托 migrate_syllabus.adjudicate + publish）
  status    查看某科目当前状态（文件/大纲/判定/发布）
  report    输出人类可读运行报告（最近一次 run 的 json 转写）

通用参数: --dry-run 只读预览（不写仓库任何文件）; --apply 实际执行。
--latest 表示来源站当前已发布的会话与文件（不按日历推断）; --years 为闭区间。

用法示例:
  /usr/bin/python3 paper_pipeline.py sync --code 9709 --latest --dry-run
  /usr/bin/python3 paper_pipeline.py sync --code 9709 --years 2025-2026 --apply
  /usr/bin/python3 paper_pipeline.py onboard --code 9708 --years 2025 --dry-run
  /usr/bin/python3 paper_pipeline.py syllabus --code 9702 --apply
  /usr/bin/python3 paper_pipeline.py migrate --code 9709 --apply

退出码: 0=成功(含"远端无新增、零改动"); 2=部分失败(见报告 failures); 3=用法/致命错误。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(ROOT, "subjects.json")
PIPE_DIR = os.path.join(ROOT, "data", "pipeline")
STATE_PATH = os.path.join(PIPE_DIR, "state.json")
RUNS_DIR = os.path.join(PIPE_DIR, "runs")
QUAR_DIR = os.path.join(PIPE_DIR, "quarantine")


def _apply_root(root):
    """切换根目录(测试夹具用临时仓库; 正常运行不需要调用)。"""
    global ROOT, REGISTRY_PATH, PIPE_DIR, STATE_PATH, RUNS_DIR, QUAR_DIR
    ROOT = root
    REGISTRY_PATH = os.path.join(ROOT, "subjects.json")
    PIPE_DIR = os.path.join(ROOT, "data", "pipeline")
    STATE_PATH = os.path.join(PIPE_DIR, "state.json")
    RUNS_DIR = os.path.join(PIPE_DIR, "runs")
    QUAR_DIR = os.path.join(PIPE_DIR, "quarantine")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SESS = {"m": "Feb-Mar", "s": "May-Jun", "w": "Oct-Nov"}
PC_BASE = "https://pastpapers.papacambridge.com"
# 直链正则: 兼容任意主机(真实站点为 https://pastpapers.papacambridge.com,
# 本地夹具为 http://127.0.0.1:<port>), 路径段固定
PC_UPLOAD_RE = re.compile(
    r"https?://[A-Za-z0-9.\-:]+/directories/CAIE/CAIE-pastpapers/upload/([A-Za-z0-9_.]+\.pdf)")
# 文件名编码: <code>_<msw><yy>_<qp|ms|qp_et>_<paper><variant>.pdf；老卷 variant 可缺省
FNAME_RE = re.compile(r"^(\d{4})_([msw])(\d{2})_(qp[a-z_]*|ms[a-z_]*)_(\d)(\d?)\.pdf$")
FNAME_REV_RE = re.compile(r"^([msw])(\d{2})_(\d{4})_(qp[a-z_]*|ms[a-z_]*)_(\d)(\d?)\.pdf$")


# ---------------------------------------------------------------- 基础工具
def die(msg, code=3):
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(code)


class TaskCancelled(Exception):
    """任务取消信号(P1-1): 由 cancel_check 在安全边界抛出, 必须穿透业务层
    (cmd_sync/cmd_publish/migrate)到达 TaskManager, 终态 cancelled 而非 failed。"""


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def http_get(url, timeout=40, retries=3, sleep=1.5):
    """带 UA/重试的 GET，返回 (status, bytes)。429/5xx/网络错误重试。"""
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code < 500 and e.code != 429:
                return e.code, b""
            try:
                body = e.read()
            except Exception:
                body = b""
            if e.code == 200:            # 不应发生，防御
                return 200, body
        except Exception as e:            # 网络抖动
            last = repr(e)
        if attempt < retries - 1:
            time.sleep(sleep * (attempt + 1))
    return None, last.encode() or b""


def http_head(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA},
                                 method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, int(r.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as e:
        return e.code, 0
    except Exception:
        return None, 0


# ---------------------------------------------------------------- 登记表
def load_registry():
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)["subjects"]


def save_registry(subjects):
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump({"_comment": "科目登记（paper_pipeline.py 的唯一科目清单）。",
                   "subjects": subjects}, f, ensure_ascii=False, indent=2)


def get_subject(code):
    subs = load_registry()
    if code not in subs:
        die(f"科目 {code} 未登记。已登记: {', '.join(sorted(subs))}。\n"
            f"新科目请先运行 onboard --code {code} …")
    return subs[code]


def registry_subject_dirs():
    """从磁盘发现 <代码>（<名称>） 数据目录（登记缺失时的兜底，不写回）。"""
    out = {}
    for d in sorted(os.listdir(ROOT)):
        m = re.match(r"^(\d{4})（(.*)）$", d)
        if m and os.path.isdir(os.path.join(ROOT, d)):
            out.setdefault(m.group(1), {"code": m.group(1), "zh": m.group(2),
                                        "data_dir": d})
    return out


# ---------------------------------------------------------------- 状态
def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"subjects": {}}


def save_state(state):
    os.makedirs(PIPE_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def subj_state(state, code):
    return state["subjects"].setdefault(
        code, {"files": {}, "syllabus": None, "quarantine": [],
               "last_adjudication": None})


# ---------------------------------------------------------------- 来源发现
class RemoteFile:
    __slots__ = ("name", "url", "kind", "paper", "variant", "year", "session",
                 "size", "status")

    def __init__(self, name, url, kind, paper, variant, year, session):
        self.name, self.url, self.kind = name, url, kind
        self.paper, self.variant = paper, variant
        self.year, self.session = year, session
        self.size = None
        self.status = "found"

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def parse_pdf_name(name, code):
    """解析 PDF 文件名 → (kind, paper, variant, year, session, reversed?)。"""
    m = FNAME_RE.match(name)
    rev = False
    if not m:
        m = FNAME_REV_RE.match(name)
        rev = True
    if not m:
        return None
    if rev:
        seas, yy, c, typ, paper, var = m.groups()
    else:
        c, seas, yy, typ, paper, var = m.groups()
    if c != code:
        return None
    kind = "qp" if typ.startswith("qp") else "ms"
    if paper == "0":
        return None
    return kind, int(paper), int(var or 1), 2000 + int(yy), SESS[seas], rev


def papacambridge_discover(code, slug=None, source_url=None, log=print):
    """从 PapaCambridge 科目页发现全部已发布 QP/MS。
    只信 PDF 文件名（科目/年份/考季/Paper/variant/类型），不信会话页标题。
    返回 (files_by_name, session_pages)。"""
    url = source_url or f"{PC_BASE}/papers/caie/{slug or code}"
    status, html = http_get(url)
    if status != 200 or b"pastpapers" not in html[:4000] + html[-4000:] \
            and b"9999" not in html:
        die(f"科目页抓取失败: {url} (HTTP {status})", 3)
    html = html.decode("utf-8", "replace")
    base_slug = (source_url or f"papers/caie/{slug or code}").rstrip("/").split("/")[-1]
    # 会话页基址: 手工 URL(或本地夹具)时跟随其 host, 默认 PapaCambridge
    if source_url:
        sess_base = source_url.rstrip("/").rsplit("/", 1)[0]
    else:
        sess_base = f"{PC_BASE}/papers/caie"
    sessions = set()
    for m in re.finditer(rf'/?["\']?([a-z0-9-]*{re.escape(base_slug)}[a-z0-9-]*)["\']?',
                         html):
        s = m.group(1)
        if s in (base_slug,) or "solved" in s or "topical" in s:
            continue
        sessions.add(s)
    files, session_pages = {}, []
    for i, s in enumerate(sorted(sessions)):
        time.sleep(0.25)                       # 礼貌限速
        surl = f"{sess_base}/{s}"
        st, body = http_get(surl, timeout=30)
        session_pages.append({"slug": s, "url": surl,
                              "http": st, "bytes": len(body)})
        if st != 200:
            continue
        text = body.decode("utf-8", "replace")
        for m in PC_UPLOAD_RE.finditer(text):
            name = m.group(1)
            parsed = parse_pdf_name(name, code)
            if not parsed:
                continue                        # solved/topical/其他资料在此自然排除
            kind, paper, variant, year, session, rev = parsed
            if rev:                             # 反转命名(s14_9709_qp_13)视为重复源
                session_pages[-1]["reversed_names"] = \
                    session_pages[-1].get("reversed_names", 0) + 1
                continue
            if name in files and files[name].url != m.group(0):
                files[name].status = "dup-link"  # 同名多链：保留第一条并记录
                continue
            files[name] = RemoteFile(name, m.group(0), kind, paper, variant,
                                     year, session)
        log(f"  [discover {code}] {i+1}/{len(sessions)} {s}: {st}")
    return files, session_pages


def source_list_discover(code, list_path):
    """受校验的 PDF 直链清单导入（其他站点结构不兼容时的兜底）。"""
    files = {}
    with open(list_path, encoding="utf-8") as f:
        for ln in f:
            url = ln.strip()
            if not url or url.startswith("#"):
                continue
            m = PC_UPLOAD_RE.match(url) or re.match(r"(https?://\S+/([A-Za-z0-9_.]+\.pdf))", url)
            if not m:
                continue
            name = m.group(m.lastindex)
            parsed = parse_pdf_name(name, code)
            if not parsed:
                continue
            kind, paper, variant, year, session, rev = parsed
            if rev:
                continue
            files[name] = RemoteFile(name, m.group(0), kind, paper, variant,
                                     year, session)
    return files


# ---------------------------------------------------------------- 大纲
# 同时匹配范围版(697423-2026-2028-syllabus.pdf)与单年版(764392-2029-syllabus.pdf)
SYLLABUS_LINK_RE = re.compile(
    r'href="(/Images/(\d+)-(\d{4})(?:-(\d{4}))?-(syllabus(?:-update)?)\.pdf)"[^>]*>([^<]*)',
    re.I)


def _version_key(version):
    """'2026-2028'/'2029' → (末考年, 首考年): 末考年最晚者为最新已发布版。"""
    parts = version.split("-")
    y1 = int(parts[0])
    y2 = int(parts[1]) if len(parts) > 1 else y1
    return (y2, y1)


def resolve_latest_syllabus(caie_page, log=print):
    """解析官方科目页 → 最新已发布完整 syllabus + 对应 update。
    支持单年版(如 9708 的 2029)与范围版; 官方页不可达/无链接 → None(调用方停发)。"""
    status, html = http_get(caie_page)
    if status != 200:
        log(f"  [syllabus] 官方页抓取失败 HTTP {status}: {caie_page}")
        return None
    text = html.decode("utf-8", "replace")
    cands = []
    for m in SYLLABUS_LINK_RE.finditer(text):
        rel, img_id, y1, y2, doctype, label = m.groups()
        if "syllabus" not in doctype:
            continue
        version = y1 if not y2 else f"{y1}-{y2}"
        cands.append({"url": "https://www.cambridgeinternational.org" + rel,
                      "version": version, "label": " ".join(label.split()),
                      "is_update": "update" in doctype})
    mains = [c for c in cands if not c["is_update"]]
    if not mains:
        return None
    latest = max(mains, key=lambda c: _version_key(c["version"]))
    upd = [c for c in cands if c["is_update"] and c["version"] == latest["version"]]
    out = {"version": latest["version"], "url": latest["url"],
           "label": latest["label"]}
    out["update"] = upd[0] if upd else None
    return out


def cmd_syllabus(args):
    sub = get_subject(args.code)
    target = resolve_latest_syllabus(args.url or sub["caie_page"])
    if not target:
        die("官方页未找到 syllabus 链接（页面结构可能变化，需人工检查）")
    state = load_state()
    st = subj_state(state, args.code)
    have = (st.get("syllabus") or {}).get("version")
    lines = [f"科目 {args.code} {sub['name']}",
             f"  官方最新已发布: {target['version']}  {target['url']}",
             f"  update: {target['update']['url'] if target['update'] else '无'}",
             f"  本地已记录版本: {have or '无'}"]
    changed = have is not None and have != target["version"]
    action = "需迁移" if changed else ("已一致" if have else "首次登记")
    lines.append(f"  状态: {action}")
    if not args.apply:
        print("\n".join(lines) + "\n  (dry-run: 不下载不写状态)")
        return 0
    os.makedirs(os.path.join(ROOT, "syllabus"), exist_ok=True)
    # 下载 syllabus(含 update)，验 %PDF + fitz，哈希入库；不覆盖已有同哈希文件
    rec = {"version": target["version"], "resolved_at": now_iso(),
           "page": args.url or sub["caie_page"]}
    import fitz  # noqa: 延迟导入
    for key, url, suffix in (("pdf", target["url"], "syllabus"),
                             ("update_pdf", (target["update"] or {}).get("url"),
                              "syllabus-update")):
        if not url:
            rec[key] = None
            continue
        st_code, data = http_get(url, timeout=120)
        if st_code != 200 or not data.startswith(b"%PDF"):
            rec[key] = {"url": url, "error": f"HTTP {st_code}/非PDF"}
            continue
        try:
            with __import__("tempfile").NamedTemporaryFile(suffix=".pdf") as tf:
                tf.write(data)
                tf.flush()
                d = fitz.open(tf.name)
                pages = d.page_count
                d.close()
        except Exception as e:
            rec[key] = {"url": url, "error": f"fitz: {e!r}"}
            continue
        sha = hashlib.sha256(data).hexdigest()
        name = f"{args.code}_{sub['name'].replace(' ', '')}_{target['version']}_{suffix}.pdf"
        path = os.path.join(ROOT, "syllabus", name)
        if not os.path.exists(path):
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        rec[key] = {"url": url, "file": f"syllabus/{name}", "sha256": sha,
                    "bytes": len(data), "pages": pages}
    st["syllabus"] = rec
    save_state(state)
    print("\n".join(lines))
    print("  已保存:", json.dumps(rec, ensure_ascii=False)[:300])
    return 0


def try_server_reload(timeout=90):
    """CLI 发布成功后尝试通知本机运行中的 server 刷新索引(同步完成才返回)。
    返回 (reloaded, data_version|None, error|None)。"""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8930/api/pipeline/reload", method="POST",
            data=b"{}", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read())
            return bool(j.get("ok")), j.get("data_version"), j.get("error")
    except urllib.error.URLError as e:
        return False, None, str(e)
    except Exception as e:
        return False, None, repr(e)


# ---------------------------------------------------------------- 下载
def local_valid(path):
    try:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def validate_pdf_bytes(path):
    """%PDF + PyMuPDF 可打开 + 页数>0；返回 (ok, reason, pages)。"""
    try:
        with open(path, "rb") as f:
            if f.read(5) != b"%PDF-":
                return False, "not-pdf-magic", 0
        import fitz
        d = fitz.open(path)
        pages = d.page_count
        d.close()
        if pages <= 0:
            return False, "zero-pages", 0
        return True, None, pages
    except Exception as e:
        return False, f"fitz-open-failed: {e!r}", 0


def download_one(rf, dest, rate_sleep=0.3):
    """下载单个文件: .part → 验真 → 原子入库。返回 (status, detail)。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    st, data = http_get(rf.url, timeout=180, retries=3)
    time.sleep(rate_sleep)
    if st != 200:
        return f"http-{st}", {"url": rf.url}
    if not data:
        return "zero-bytes", {"url": rf.url}
    if not data.startswith(b"%PDF-"):
        return "200-html", {"url": rf.url, "head": data[:60].decode("utf-8", "replace")}
    with open(part, "wb") as f:
        f.write(data)
    ok, reason, pages = validate_pdf_bytes(part)
    if not ok:
        os.unlink(part)
        return f"invalid:{reason}", {"url": rf.url}
    os.replace(part, dest)
    return "downloaded", {"sha256": sha256_file(dest), "bytes": len(data),
                          "pages": pages, "url": rf.url}


def plan_and_download(code, sub, files, years, apply, state, report, log=print):
    """对照本地 → 生成计划 → (apply 时)下载。逐文件记录状态。"""
    data_dir = os.path.join(ROOT, sub["data_dir"])
    st = subj_state(state, code)
    targets, plan_bytes = [], 0
    for name in sorted(files):
        rf = files[name]
        if years and not (years[0] <= rf.year <= years[1]):
            continue
        dest = os.path.join(data_dir, str(rf.year), rf.session, name)
        targets.append((rf, dest))
    # 体积估算（HEAD，并发限 8 —— 顺序太慢）
    log(f"  [plan] 目标文件 {len(targets)} 个，HEAD 估算 …")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        sizes = list(ex.map(lambda t: http_head(t[0].url), targets))
    for (rf, dest), (code_, size) in zip(targets, sizes):
        rf.size = size or None
        plan_bytes += size or 0
    have = miss = 0
    for rf, dest in targets:
        entry = {"name": rf.name, "url": rf.url, "target":
                 os.path.relpath(dest, ROOT), "kind": rf.kind,
                 "year": rf.year, "session": rf.session,
                 "paper": rf.paper, "variant": rf.variant,
                 "size": rf.size}
        if local_valid(dest):
            entry["status"] = "have"
            have += 1
            # 远端大小与本地不一致 → 内容可能变化，需要核对（冲突处理）
            if rf.size and os.path.getsize(dest) != rf.size:
                entry["status"] = "size-mismatch"
        else:
            entry["status"] = "missing-locally"
            miss += 1
        report["targets"].append(entry)
    report["plan"] = {"files": len(targets), "have": have, "to_download": miss,
                      "bytes_estimate": plan_bytes}
    log(f"  [plan] 已有 {have} · 待下载 {miss} · 估算 "
        f"{plan_bytes/1048576:.1f} MB")
    if not apply:
        return
    downloaded = failed = 0
    for i, (rf, dest) in enumerate(targets):
        entry = report["targets"][i]
        if entry["status"] == "have":
            continue
        if entry["status"] == "size-mismatch":
            # 下载到隔离区比对，绝不静默覆盖
            qdir = os.path.join(QUAR_DIR, code)
            os.makedirs(qdir, exist_ok=True)
            qpath = os.path.join(qdir, rf.name)
            status, detail = download_one(rf, qpath)
            if status == "downloaded":
                old_sha = sha256_file(dest)
                new_sha = detail["sha256"]
                if old_sha == new_sha:
                    entry.update(status="have", note="同哈希，保留本地")
                    os.unlink(qpath)
                else:
                    entry.update(status="conflict-quarantined",
                                 quarantined=os.path.relpath(qpath, ROOT),
                                 local_sha256=old_sha, remote_sha256=new_sha,
                                 note="同名内容变化，已隔离待人工确认")
                    st["quarantine"].append({"name": rf.name, "path":
                                             os.path.relpath(qpath, ROOT),
                                             "local_sha256": old_sha,
                                             "remote_sha256": new_sha,
                                             "at": now_iso()})
            else:
                entry["status"] = f"failed:{status}"
                failed += 1
            continue
        status, detail = download_one(rf, dest)
        if status == "downloaded":
            downloaded += 1
            entry.update(status="downloaded", sha256=detail["sha256"],
                         bytes=detail["bytes"], pages=detail["pages"])
            st["files"][rf.name] = {"url": rf.url, "sha256": detail["sha256"],
                                    "bytes": detail["bytes"],
                                    "pages": detail["pages"],
                                    "downloaded_at": now_iso(),
                                    "path": os.path.relpath(dest, ROOT)}
        else:
            failed += 1
            entry["status"] = f"failed:{status}"
            entry["reason"] = detail
        save_state(state)                    # 逐文件持久化 → 中断可续
        log(f"  [download {i+1}/{len(targets)}] {rf.name}: {entry['status']}")
    report["download"] = {"downloaded": downloaded, "failed": failed,
                          "conflicts": sum(1 for e in report["targets"]
                                           if e["status"] == "conflict-quarantined")}
    # 缺 MS 对账（远端与本地都没有 MS 的 QP）
    qp_names = {e["name"]: e for e in report["targets"] if e["kind"] == "qp"}
    report["missing_ms"] = []
    for name, e in qp_names.items():
        ms_name = name.replace("_qp", "_ms", 1)
        if ms_name not in qp_names and not any(
                t["name"] == ms_name for t in report["targets"]):
            local_ms = os.path.join(ROOT, sub["data_dir"], str(e["year"]),
                                    e["session"], ms_name)
            if not local_valid(local_ms):
                report["missing_ms"].append({"qp": name, "note": "远端与本地均无 MS"})
    if failed:
        report["failures"].extend(
            {"name": e["name"], "status": e["status"]}
            for e in report["targets"] if e["status"].startswith("failed"))


# ---------------------------------------------------------------- 切题(增量)
EXTRACTOR_VER = 2      # extract_questions.py 结构版本（改动切分逻辑时 +1）


def cmd_extract(args):
    """增量切题。--dry-run(默认): 只读计算将重切的卷, 零写入(不建目录/不调提取器)。"""
    import extract_questions as eq
    sub = get_subject(args.code)
    work = os.path.join(PIPE_DIR, "work", args.code, "units")
    meta_path = os.path.join(PIPE_DIR, "work", args.code, "meta.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    base = os.path.join(ROOT, sub["data_dir"])
    # 找出源 PDF 新增/哈希变化的卷 → 强制重切
    force = set()
    for ydir in sorted(os.listdir(base)):
        if not ydir.isdigit():
            continue
        for sess in sorted(os.listdir(os.path.join(base, ydir))):
            sp = os.path.join(base, ydir, sess)
            if not os.path.isdir(sp):
                continue
            for f in sorted(os.listdir(sp)):
                m = FNAME_RE.match(f)
                if not m or m.group(4) != "qp" or m.group(5) == "0":
                    continue
                code = f"{m.group(2)}{m.group(3)}_{m.group(5)}{m.group(6) or '1'}"
                sha = sha256_file(os.path.join(sp, f))
                prev = meta.get(code, {})
                if prev.get("sha256") != sha or \
                        prev.get("extractor_ver") != EXTRACTOR_VER or \
                        not os.path.exists(os.path.join(work, f"{code}_qp.json")):
                    force.add(code)
                meta[code] = {"sha256": sha, "extractor_ver": EXTRACTOR_VER,
                              "src": f"{ydir}/{sess}/{f}"}
    if args.papers:
        force &= set(args.papers.split(","))
    if not args.apply:
        # dry-run: 零仓库写入 —— 不建工作目录、不写 meta、不运行提取器
        print(f"extract(dry-run): 将重切 {len(force)} 卷 "
              f"(force={sorted(force)[:8]}{'…' if len(force) > 8 else ''}) "
              f"-> {work}(未写入)")
        return {"extracted": 0, "would_extract": sorted(force), "forced": sorted(force),
                "workdir": work, "dry_run": True}
    os.makedirs(work, exist_ok=True)
    n = eq.run(args.code, base, work, force=force, root=ROOT)
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    tmp = meta_path + ".tmp"
    json.dump(meta, open(tmp, "w"))
    os.replace(tmp, meta_path)
    print(f"extract: 新切/重切 {n} 卷 -> {work} (force={sorted(force)[:8]}"
          f"{'…' if len(force) > 8 else ''})")
    return {"extracted": n, "forced": sorted(force), "workdir": work}


# ---------------------------------------------------------------- 报告
def write_run_report(report):
    os.makedirs(RUNS_DIR, exist_ok=True)
    ts = report["started"].replace(":", "").replace("-", "")
    path = os.path.join(RUNS_DIR, f"{ts}_{report['cmd']}_{report['code']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def human_report(report):
    L = [f"== {report['cmd']} {report['code']} ({report['mode']}) "
         f"{report['started']} → {report['finished']}"]
    if "discovery" in report:
        d = report["discovery"]
        L.append(f"发现: 会话页 {d['session_pages']} · 远端 QP {d['remote_qp']} · "
                 f"MS {d['remote_ms']} · 去重后 {d['unique_files']}")
    if "plan" in report:
        p = report["plan"]
        L.append(f"计划: 目标 {p['files']} · 已有 {p['have']} · 待下载 "
                 f"{p['to_download']} · 估算 {p['bytes_estimate']/1048576:.1f}MB")
    if "download" in report:
        L.append(f"下载: {report['download']}")
    if report.get("missing_ms"):
        L.append(f"缺 MS: {len(report['missing_ms'])} 项 "
                 f"{[x['qp'] for x in report['missing_ms']][:8]}")
    if "extraction" in report:
        L.append(f"切题: {report['extraction']}")
    if "syllabus" in report and report["syllabus"]:
        s = report["syllabus"]
        L.append(f"大纲: 目标版本 {s.get('version')} "
                 f"(已登记 {s.get('recorded') or '无'}) "
                 f"{'→ 需迁移!' if s.get('migration_required') else ''}")
    if "adjudication" in report and report["adjudication"]:
        a = report["adjudication"]
        L.append(f"判定: IN {a.get('in')} · OUT {a.get('out')} · "
                 f"PENDING {a.get('pending')} · CONTEXT {a.get('context', 0)}")
    if "publish" in report and report["publish"]:
        L.append(f"发布: {report['publish']}")
    if report["failures"]:
        L.append(f"失败 {len(report['failures'])} 项:")
        for x in report["failures"][:20]:
            L.append(f"  - {x}")
    else:
        L.append("失败: 0")
    return "\n".join(L)


# ---------------------------------------------------------------- sync / onboard
def discover_flow(code, sub, args, report, log):
    if args.source_list:
        files = source_list_discover(code, args.source_list)
        report["discovery"] = {"session_pages": 0, "remote_qp": 0,
                               "remote_ms": 0, "unique_files": len(files),
                               "mode": "direct-list"}
    else:
        files, spages = papacambridge_discover(
            code, slug=(sub.get("source") or {}).get("slug"),
            source_url=args.source_url, log=log)
        report["discovery"] = {
            "session_pages": len(spages), "remote_qp":
            sum(1 for f in files.values() if f.kind == "qp"),
            "remote_ms": sum(1 for f in files.values() if f.kind == "ms"),
            "unique_files": len(files),
            "pages": spages[:200]}
    return files


def cmd_sync(args, cancel_check=None):
    sub = get_subject(args.code)
    if not args.latest and not args.years:
        die("sync 需要 --latest 或 --years A-B 之一")
    years = None
    if args.years:
        m = re.match(r"^(\d{4})(?:-(\d{4}))?$", args.years)
        if not m:
            die(f"--years 格式应为 2025-2026 或 2025，收到 {args.years}")
        years = (int(m.group(1)), int(m.group(2) or m.group(1)))
    report = {"cmd": "sync", "code": args.code, "mode":
              "apply" if args.apply else "dry-run",
              "started": now_iso(), "targets": [], "failures": [], "stage": "start"}
    print(f"[sync {args.code}] {'APPLY' if args.apply else 'DRY-RUN'} "
          f"years={years or 'latest'}")
    files = discover_flow(args.code, sub, args, report, log=print)
    # 空发现 ≠ 成功: 区分"来源为空/解析失败"与"确实没有新卷"
    d = report.get("discovery") or {}
    if not files:
        report["failures"].append({
            "name": "discovery", "status": "empty-source",
            "detail": (f"会话页 {d.get('session_pages', 0)} · 解析出文件 0"
                       f"(来源为空或页面结构变化, 不是'没有新卷')")})
    state = load_state()
    plan_and_download(args.code, sub, files, years, args.apply, state, report)
    report["stage"] = "download"
    # 大纲版本检查: 官方又更新 → 标记需迁移，并停止新分类发布;
    # 官方页不可确认 → 同样停发(不能沿用旧版继续发布)
    target = resolve_latest_syllabus(sub["caie_page"])
    if target:
        recorded = (subj_state(state, args.code).get("syllabus") or {}).get("version")
        report["syllabus"] = {"version": target["version"], "url": target["url"],
                              "recorded": recorded,
                              "resolved": True,
                              "migration_required": bool(
                                  recorded and recorded != target["version"])}
    else:
        report["syllabus"] = {"resolved": False}
        if args.apply:
            report["failures"].append({
                "name": "syllabus", "status": "unresolvable",
                "detail": "官方科目页不可达或无法唯一确定最新 syllabus, "
                          "已停止分类/发布(不沿用旧版)"})
    rc = 0
    if args.apply:
        try:
            if report["failures"]:
                pass                     # 下载/发现/大纲已失败: 不进入发布链
            elif report.get("syllabus", {}).get("migration_required"):
                print("  ⚠ 官方大纲已更新: 需先运行 migrate --code "
                      f"{args.code} --apply 再发布新分类（本次已跳过分类/发布）")
            else:
                if cancel_check:
                    cancel_check(stage="extract")
                ext = cmd_extract(argparse.Namespace(code=args.code, apply=True,
                                                     papers=None))
                report["extraction"] = {"extracted": ext["extracted"],
                                        "forced": ext["forced"]}
                report["stage"] = "extract"
                import migrate_syllabus as ms
                if cancel_check:
                    cancel_check(stage="adjudicate")
                # 待发布以 published.json + data/units 正式 status 为准:
                # 本次切题数为 0 仍处理(真正的)待发布卷
                adj = ms.adjudicate_new_papers(args.code, log=print)
                if adj.get("new_papers"):
                    report["adjudication"] = adj["summary"]
                    report["stage"] = "adjudicate"
                    if adj["ok"]:
                        if cancel_check:
                            cancel_check(stage="publish")
                        pub = ms.publish(args.code, log=print,
                                         cancel_check=(lambda:
                                                       cancel_check(stage="commit"))
                                         if cancel_check else None)
                        report["publish"] = pub["summary"]
                        report["stage"] = "publish"
                        if pub.get("index_refresh_errors"):
                            report["failures"].append({
                                "name": "index-refresh", "status": "failed",
                                "detail": "; ".join(pub["index_refresh_errors"])})
                        elif not pub["ok"]:
                            report["failures"].append({
                                "name": "publish", "status": "failed",
                                "detail": pub.get("summary")})
                        else:
                            # 服务通知已由 ms.publish 内部完成(无进程内钩子时
                            # CLI 发布会尝试 try_server_reload 并决定
                            # pending_refresh 晋升), 此处不重复刷新
                            pass
                    else:
                        report["failures"].append({"name": "adjudication",
                                                   "status": "validation-failed"})
                else:
                    report["adjudication"] = adj["summary"]
                    report["stage"] = "adjudicate"
        except TaskCancelled as e:            # P1-1: 取消必须穿透, 终态 cancelled
            report["cancelled_at_stage"] = report.get("stage", "pipeline")
            report["finished"] = now_iso()
            print(f"  已取消(阶段 {report.get('stage')}): {e}")
            raise
        except Exception as e:
            report["failures"].append({"name": report.get("stage", "pipeline"),
                                       "status": f"exception: {e!r}"})
    report["finished"] = now_iso()
    if args.apply:
        path = write_run_report(report)     # 报告在全部阶段结束后落盘
        print(f"  运行报告: {path}")
    print(human_report(report))
    if report["failures"]:
        rc = 2
    return rc


def cmd_onboard(args):
    if not re.fullmatch(r"\d{4}", args.code):
        die("科目代码应为 4 位数字")
    m = re.match(r"^(\d{4})(?:-(\d{4}))?$", args.years)
    if not m:
        die(f"--years 格式应为 2024-2025 或 2024，收到 {args.years}")
    years = (int(m.group(1)), int(m.group(2) or m.group(1)))
    subs = load_registry()
    existing_dirs = registry_subject_dirs()
    if args.code in subs:
        print(f"科目 {args.code} 已登记({subs[args.code]['name']})，按增量 sync 处理")
        sub = subs[args.code]
    else:
        if args.code in existing_dirs and not args.data_dir:
            die(f"磁盘已有 {existing_dirs[args.code]['data_dir']} 但未登记；"
                f"请用 --data-dir 指定或换目录")
        sub = {"code": args.code, "name": args.name or f"Subject {args.code}",
               "zh": args.zh or args.code, "level": "AS & A Level",
               "data_dir": args.data_dir or f"{args.code}（{args.zh or args.code}）",
               "source": {"kind": "papacambridge", "slug": None},
               "caie_page": args.url or "",
               "paper_names": {}}
        if not args.source_url and not args.source_list:
            # 自动发现: 用 PapaCambridge 目录搜索科目代码
            found = papacambridge_search_code(args.code)
            if len(found) == 1:
                sub["source"]["slug"] = found[0]
                print(f"  自动发现来源: {found[0]}")
            else:
                die(f"来源自动发现得到 {len(found)} 个候选 "
                    f"{found[:5]}；请用 --source-url 指定科目页")
    report = {"cmd": "onboard", "code": args.code, "mode":
              "apply" if args.apply else "dry-run", "started": now_iso(),
              "targets": [], "failures": [], "stage": "start"}
    print(f"[onboard {args.code}] {'APPLY' if args.apply else 'DRY-RUN'} "
          f"years={years}")
    if args.source_url:
        sub.setdefault("source", {})["slug"] = None
    files = discover_flow(args.code, sub, args, report, log=print)
    if not files:
        report["failures"].append({
            "name": "discovery", "status": "empty-source",
            "detail": "来源页解析出 0 个 QP/MS(来源为空或结构变化)"})
    state = load_state()
    plan_and_download(args.code, sub, files, years, args.apply, state, report)
    report["stage"] = "download"
    # 官方大纲
    if sub["caie_page"]:
        target = resolve_latest_syllabus(sub["caie_page"])
        if target:
            report["syllabus"] = {"version": target["version"],
                                  "url": target["url"], "resolved": True}
        else:
            report["syllabus"] = {"resolved": False}
    if args.apply:
        if args.code not in subs:
            # 持久化生效来源: 手工 URL 时解析出 slug, 自动发现时保留 slug
            eff_slug = (sub.get("source") or {}).get("slug")
            if not eff_slug and args.source_url:
                cand = args.source_url.rstrip("/").split("/")[-1]
                if cand.isdigit() is False and len(cand) > len(args.code):
                    eff_slug = cand
            sub["source"] = {"kind": "papacambridge", "slug": eff_slug}
            subs[args.code] = sub
            save_registry(subs)
            print(f"  已登记科目 {args.code} -> {sub['data_dir']} (source slug: {eff_slug})")
        try:
            ext = cmd_extract(argparse.Namespace(code=args.code, apply=True,
                                                 papers=None))
            report["extraction"] = {"extracted": ext["extracted"]}
            report["stage"] = "extract"
        except Exception as e:
            report["failures"].append({"name": "extract",
                                       "status": f"exception: {e!r}"})
        has_topics = os.path.exists(os.path.join(ROOT, f"topics_{args.code}.json"))
        print("  下一步:" + ("" if has_topics else
              f" 1) syllabus --code {args.code} --apply 下载官方最新大纲;"
              f" 2) 按 SYLLABUS_TOPIC_AI_SOP 制作 topics_{args.code}.json;") +
              f" 3) classify-prep --code {args.code} 生成 AI 判定输入;"
              f" 4) AI 判定写入 tags_{args.code}/; 5) publish --code {args.code} --apply")
    report["finished"] = now_iso()
    if args.apply:
        path = write_run_report(report)     # 报告在全部阶段结束后落盘
        print(f"  运行报告: {path}")
    print(human_report(report))
    return 2 if report["failures"] else 0


def papacambridge_search_code(code):
    """在 PapaCambridge CAIE A Level 目录页按科目代码搜索 slug。"""
    for hub in ("https://pastpapers.papacambridge.com/?dir=CAIE/CAIE-pastpapers",
                f"{PC_BASE}/caie"):
        st, html = http_get(hub, timeout=30)
        if st != 200:
            continue
        text = html.decode("utf-8", "replace")
        slugs = set(re.findall(rf'([a-z0-9-]*-{code})["/\']?', text))
        return sorted(s for s in slugs if "solved" not in s and "topical" not in s)
    return []


# ---------------------------------------------------------------- classify / publish
def cmd_classify_prep(args):
    get_subject(args.code)
    import gen_workfiles as gw
    work = os.path.join(PIPE_DIR, "work", args.code, "units")
    out = os.path.join(PIPE_DIR, "work", args.code, "classify")
    gw.run(args.code, work, out, root=ROOT)
    print(f"AI 判定输入已生成: {out}/TOPICS.txt + <卷代码>.txt")
    print(f"按 SYLLABUS_TOPIC_AI_SOP 派发; 结果写 tags_{args.code}/<卷>.json，"
          f"格式 {{\"code\":…,\"tags\":{{uid:[codes]|[\\\"OUT\\\",理由]|[\\\"PENDING\\\",问题]}}}}")


def cmd_publish(args, cancel_check=None):
    import migrate_syllabus as ms
    res = ms.publish(args.code, log=print,
                     cancel_check=(lambda: cancel_check(stage="commit"))
                     if cancel_check else None)
    print(res["summary"])
    if res.get("index_refresh_errors"):
        for e in res["index_refresh_errors"]:
            print(f"  ⚠ 索引刷新失败: {e}")
        print("  待发布未清零(pending_refresh), 可在网页点「刷新服务索引」重试")
        return 2
    # 服务通知与 pending_refresh 晋升已在 ms.publish 内完成, 不重复刷新
    return 0 if res["ok"] else 2


def cmd_migrate(args):
    import migrate_syllabus as ms
    print(f"[migrate {args.code}] {'APPLY' if args.apply else 'DRY-RUN'}")
    res = ms.migrate(args.code, apply=args.apply, log=print)
    print(res["summary"])
    return 0 if res["ok"] else 2


def cmd_status(args):
    sub = get_subject(args.code)
    state = load_state()
    st = state["subjects"].get(args.code, {})
    print(f"科目 {args.code} {sub['name']} ({sub['zh']})  数据目录 {sub['data_dir']}")
    syl = st.get("syllabus")
    if syl:
        print(f"  大纲: {syl['version']}  {syl.get('pdf', {}).get('url')}")
        print(f"        sha256={syl.get('pdf', {}).get('sha256', '?')[:16]}… "
              f"文件 {syl.get('pdf', {}).get('file')}")
    else:
        print("  大纲: 未登记（运行 syllabus --code … --apply）")
    adj_path = os.path.join(PIPE_DIR, "adjudication", f"{args.code}.json")
    if os.path.exists(adj_path):
        adj = json.load(open(adj_path))
        c = adj.get("counts", {})
        print(f"  判定: IN {c.get('in')} · OUT {c.get('out')} · "
              f"PENDING {c.get('pending')} · CONTEXT {c.get('context', 0)} · "
              f"目标大纲 {adj.get('target', {}).get('version')}")
    else:
        print("  判定: 未运行")
    import migrate_syllabus as ms
    pending = sorted(set(os.listdir(os.path.join(PIPE_DIR, "work", args.code,
                                                 "units")))
                     - {p + "_qp.json" for p in ms.published_papers(args.code)}) \
        if os.path.isdir(os.path.join(PIPE_DIR, "work", args.code, "units")) else []
    pending = [p[:-8] for p in pending if p.endswith("_qp.json")]
    print(f"  待发布卷(workdir 未成功发布): {pending or '无'}")
    print(f"  已下载文件记录: {len(st.get('files', {}))} · 隔离区 "
          f"{len(st.get('quarantine', []))}")
    runs = sorted(f for f in os.listdir(RUNS_DIR)
                  if f.endswith(f"_{args.code}.json")) if os.path.isdir(RUNS_DIR) else []
    if runs:
        print(f"  最近运行: {runs[-5:]}")
    return 0


def cmd_report(args):
    get_subject(args.code)
    runs = sorted(f for f in os.listdir(RUNS_DIR)
                  if f.endswith(f"_{args.code}.json")) if os.path.isdir(RUNS_DIR) else []
    if not runs:
        print(f"{args.code}: 暂无运行报告")
        return 0
    path = os.path.join(RUNS_DIR, runs[-1])
    rep = json.load(open(path))
    print(f"# 最近运行: {path}")
    print(human_report(rep))
    return 0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--code", required=True, help="4 位科目代码")
        p.add_argument("--apply", action="store_true", help="实际执行(默认 dry-run)")
        p.add_argument("--dry-run", action="store_true",
                       help="只读预览(默认行为; 不写仓库任何文件)")
        p.add_argument("--source-url", help="手工指定来源科目页 URL")
        p.add_argument("--source-list", help="受校验的 PDF 直链清单文件")

    p = sub.add_parser("sync", help="已有科目同步最新已发布试卷")
    common(p)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--latest", action="store_true", help="来源站当前已发布(不猜未公开考季)")
    g.add_argument("--years", help="闭区间年份 2025-2026 或 2025")

    p = sub.add_parser("onboard", help="新科目按代码和年份接入")
    common(p)
    p.add_argument("--years", required=True, help="闭区间年份 2024-2025 或 2024")
    p.add_argument("--name", help="英文名(登记用)")
    p.add_argument("--zh", help="中文名(登记用)")
    p.add_argument("--data-dir", help="数据目录名(默认 <code>（<zh>）)")
    p.add_argument("--url", help="Cambridge International 官方科目页 URL")

    p = sub.add_parser("syllabus", help="检查/下载官方最新 syllabus")
    p.add_argument("--code", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--url", help="覆盖官方科目页 URL")

    p = sub.add_parser("extract", help="增量切题到仓库工作目录")
    p.add_argument("--code", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--papers", help="逗号分隔卷代码(仅重切这些)")

    p = sub.add_parser("classify-prep", help="生成 AI 判定输入")
    p.add_argument("--code", required=True)

    p = sub.add_parser("publish", help="校验判定并原子发布+重建索引")
    p.add_argument("--code", required=True)
    p.add_argument("--apply", action="store_true")

    p = sub.add_parser("migrate", help="四科按最新大纲迁移")
    p.add_argument("--code", required=True)
    p.add_argument("--apply", action="store_true")

    p = sub.add_parser("status", help="科目状态")
    p.add_argument("--code", required=True)

    p = sub.add_parser("report", help="最近一次运行报告")
    p.add_argument("--code", required=True)

    args = ap.parse_args()
    if getattr(args, "dry_run", False) and getattr(args, "apply", False):
        ap.error("--dry-run 与 --apply 互斥")
    if not getattr(args, "apply", False) and args.cmd in ("sync", "onboard"):
        args.apply = False
    rc = {"sync": cmd_sync, "onboard": cmd_onboard, "syllabus": cmd_syllabus,
          "extract": cmd_extract, "classify-prep": cmd_classify_prep,
          "publish": cmd_publish, "migrate": cmd_migrate, "status": cmd_status,
          "report": cmd_report}[args.cmd](args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
