#!/usr/bin/env python3
"""pipeline_service.py —— 题库管理业务层(网页管理 API 与 CLI 共用)。

职责(PAST_PAPER_PIPELINE_PLAN「统一服务与前端」):
  1. 后台任务管理器: 长任务(sync/onboard/migrate/publish…)立即返回任务 ID,
     单写线程串行执行(索引/正式数据不相互覆盖); 任务状态+日志持久化到
     data/pipeline/tasks/<id>.json, 服务重启后 running → interrupted(可恢复);
     同 action+参数 的活动任务去重拒绝。
  2. 动作白名单与参数校验: 每个动作显式声明参数 schema, 不在 HTTP 层拼 shell。
  3. 只读查询: 科目总览 / 任务 / 运行报告 / 判定(复核)分页 / AI 判定输入输出。
  4. 即时写: 判定草稿保存(overrides+adjudication)、AI tags 导入(校验后落盘)。

不在本层做的事: 渲染页面(server.py)、改数据判定逻辑(migrate_syllabus)。
"""
import contextlib
import io
import json
import os
import queue
import re
import threading
import time
import traceback

import paper_pipeline as pp
import migrate_syllabus as ms

ROOT = pp.ROOT
TASKS_DIR = os.path.join(pp.PIPE_DIR, "tasks")

ACTIVE_STATES = ("queued", "running")
TERMINAL_STATES = ("succeeded", "failed", "cancelled")


# ============================ 参数校验 ============================
def _v_code(v):
    return isinstance(v, str) and re.fullmatch(r"\d{4}", v) is not None


def _v_years(v):
    return v is None or (isinstance(v, str)
                         and re.fullmatch(r"\d{4}(-\d{4})?", v) is not None)


def _v_url(v):
    return v is None or (isinstance(v, str) and re.fullmatch(
        r"https?://[A-Za-z0-9.\-:_~/]+", v) is not None)


class ParamError(ValueError):
    pass


# P1-1(2026-11-07): 取消信号与业务层共用同一异常类, 保证穿透
# cmd_sync/cmd_publish/migrate 到 TaskManager(终态 cancelled 而非 failed)
TaskCancelled = pp.TaskCancelled


class CancelRejected(ParamError):
    """提交阶段等不可中断边界拒绝取消。"""


ACTIONS = {
    # action: (func, schema {field: validator}, need_apply_defaults)
    "preview_sync": ("_act_preview_sync",
                     {"code": _v_code, "years": _v_years, "latest": None,
                      "source_url": _v_url, "source_list": None}, False),
    "sync": ("_act_sync",
             {"code": _v_code, "years": _v_years, "latest": None,
              "source_url": _v_url, "source_list": None}, False),
    "preview_onboard": ("_act_preview_onboard",
                        {"code": _v_code, "years": _v_years, "name": None,
                         "zh": None, "url": _v_url, "source_url": _v_url,
                         "source_list": None, "data_dir": None}, False),
    "onboard": ("_act_onboard",
                {"code": _v_code, "years": _v_years, "name": None, "zh": None,
                 "url": _v_url, "source_url": _v_url, "source_list": None,
                 "data_dir": None}, False),
    "syllabus_check": ("_act_syllabus_check", {"code": _v_code}, False),
    "syllabus_apply": ("_act_syllabus_apply",
                       {"code": _v_code, "url": _v_url}, False),
    "extract": ("_act_extract", {"code": _v_code, "papers": None}, False),
    "classify_prep": ("_act_classify_prep", {"code": _v_code}, False),
    "import_tags": ("_act_import_tags",
                    {"code": _v_code, "papers": None, "payload": None}, False),
    "import_topics": ("_act_import_topics",
                      {"code": _v_code, "payload": None}, False),
    "adjudicate_new": ("_act_adjudicate_new", {"code": _v_code}, False),
    "confirm_candidates": ("_act_confirm_candidates",
                           {"code": _v_code, "min_conf": None, "dry_run": None}, False),
    "publish": ("_act_publish", {"code": _v_code}, False),
    "migrate": ("_act_migrate", {"code": _v_code}, False),
    "refresh_index": ("_act_refresh_index", {}, False),
}


def validate_params(action, params):
    if action not in ACTIONS:
        raise ParamError(f"未知动作 {action!r}; 可用: {', '.join(sorted(ACTIONS))}")
    if not isinstance(params, dict):
        raise ParamError("params 应为对象")
    _, schema, _ = ACTIONS[action]
    unknown = set(params) - set(schema)
    if unknown:
        raise ParamError(f"未知参数: {', '.join(sorted(unknown))}")
    for k, v in params.items():
        chk = schema.get(k)
        if callable(chk):
            if not chk(v):
                raise ParamError(f"参数 {k} 不合法: {v!r}")


# ============================ 任务管理器 ============================
def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _params_key(action, params):
    return json.dumps([action, params], sort_keys=True, ensure_ascii=False)


class _Tee(io.TextIOBase):
    """把 print 输出按行接入任务日志。"""

    def __init__(self, tm, tid):
        self.tm = tm
        self.tid = tid
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.tm.append_log(self.tid, line)
        return len(s)

    def flush(self):
        if self.buf:
            self.tm.append_log(self.tid, self.buf)
            self.buf = ""


class TaskManager:
    """持久化后台任务: 单工作线程串行执行全部动作(写任务全局串行)。
    tasks_dir 在构造时固化 —— 测试切换根目录不会改变已构造实例的落盘位置。"""

    def __init__(self, tasks_dir=None):
        self.lock = threading.RLock()
        self.tasks = {}            # id -> task dict
        self.q = queue.Queue()     # id
        self.worker = None
        self.tasks_dir = tasks_dir or TASKS_DIR
        os.makedirs(self.tasks_dir, exist_ok=True)
        self._load_and_recover()

    # ---------- 持久化 ----------
    def _load_and_recover(self):
        if not os.path.isdir(self.tasks_dir):
            return
        for f in sorted(os.listdir(self.tasks_dir)):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.tasks_dir, f), encoding="utf-8") as fh:
                    t = json.load(fh)
            except Exception:
                continue
            if t.get("status") == "running":
                t["status"] = "interrupted"
                t["error"] = "服务重启时任务中断; 可从页面重试"
                t["finished"] = _now()
                self._save(t)
            self.tasks[t["id"]] = t
            if t["status"] == "queued":
                self.q.put(t["id"])

    def _save(self, t):
        with self.lock:
            os.makedirs(self.tasks_dir, exist_ok=True)
            path = os.path.join(self.tasks_dir, f"{t['id']}.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(t, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)

    def append_log(self, tid, line):
        with self.lock:
            t = self.tasks.get(tid)
            if t is None:
                return
            t.setdefault("log", []).append(line)
            if len(t["log"]) > 4000:
                t["log"] = t["log"][-4000:]
            self._save(t)

    # ---------- 对外 ----------
    def start_worker(self):
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self._run_loop, daemon=True,
                                       name="pipeline-worker")
        self.worker.start()

    def create(self, action, params, max_log=200):
        validate_params(action, params)
        key = _params_key(action, params)
        with self.lock:
            for t in self.tasks.values():
                if t["status"] in ACTIVE_STATES and t["params_key"] == key:
                    raise ParamError(f"同参数任务已在执行(任务 {t['id']}), 未重复提交",
                                     )
            tid = time.strftime("%Y%m%dT%H%M%S") + f"_{action}_{len(self.tasks)+1:04d}"
            t = {"id": tid, "action": action, "params": params,
                 "params_key": key, "status": "queued", "created": _now(),
                 "log": [], "stage": "queued"}
            self.tasks[tid] = t
            self._save(t)
        self.q.put(tid)
        return t

    def get(self, tid):
        with self.lock:
            t = self.tasks.get(tid)
            return dict(t) if t else None

    def list(self, limit=100):
        with self.lock:
            out = sorted(self.tasks.values(),
                         key=lambda t: t.get("created", ""), reverse=True)
            return [{k: t.get(k) for k in
                     ("id", "action", "params", "status", "created",
                      "started", "finished", "stage", "error", "exit_code",
                      "summary")} for t in out[:limit]]

    def retry(self, tid):
        with self.lock:
            t = self.tasks.get(tid)
            if t is None:
                raise ParamError(f"任务不存在: {tid}")
            if t["status"] in ACTIVE_STATES:
                raise ParamError("任务仍在执行, 无需重试")
        return self.create(t["action"], t["params"])

    def cancel(self, tid):
        with self.lock:
            t = self.tasks.get(tid)
            if t is None:
                raise ParamError(f"任务不存在: {tid}")
            if t["status"] == "queued":
                t["status"] = "cancelled"
                t["finished"] = _now()
                self._save(t)
                return dict(t)
            if t["status"] == "running":
                if t.get("stage") == "commit":
                    raise CancelRejected(
                        "发布提交阶段不可中断(数据切换进行中); 结束后可重新操作")
                t["cancel_requested"] = True
                t["stage"] = "cancelling"
                self._save(t)
                return dict(t)
            raise ParamError(f"任务已结束({t['status']}), 无需取消")

    def raise_if_cancelled(self, tid):
        """动作在安全边界调用: 若用户已请求取消 → 抛 TaskCancelled。"""
        t = self.tasks.get(tid)
        if t and t.get("status") == "running" and t.get("cancel_requested"):
            raise TaskCancelled(f"任务 {tid} 在阶段边界被取消")

    def _cc_factory(self, t):
        """生成传给业务函数的 cancel_check(stage=...) 回调(P1-1)。"""
        def _cc(stage=None):
            self.raise_if_cancelled(t["id"])
            if stage:
                with self.lock:
                    t["stage"] = stage
                    self._save(t)
                self.append_log(t["id"], f"-- 进入阶段: {stage}")
        return _cc

    # ---------- 执行 ----------
    def _run_loop(self):
        while True:
            tid = self.q.get()
            with self.lock:
                t = self.tasks.get(tid)
                if t is None or t["status"] != "queued":
                    continue
                t["status"] = "running"
                t["started"] = _now()
                self._save(t)
            try:
                tee = _Tee(self, tid)
                with contextlib.redirect_stdout(tee), \
                        contextlib.redirect_stderr(tee):
                    fn = getattr(self, ACTIONS[t["action"]][0])
                    result = fn(t["params"], t)
                tee.flush()
                with self.lock:
                    t["status"] = "succeeded" if result.get("ok", True) \
                        else "failed"
                    t["result"] = result
                    t["summary"] = result.get("summary")
                    t["exit_code"] = result.get("exit_code", 0)
                    if t["status"] == "failed":
                        t["error"] = result.get("summary") or "failed"
                    t["finished"] = _now()
                    self._save(t)
            except TaskCancelled as e:               # P1-1: 边界取消 → cancelled
                with self.lock:
                    t["status"] = "cancelled"
                    t["error"] = str(e)
                    t["summary"] = f"已取消: {e}"
                    t["exit_code"] = 4
                    t["finished"] = _now()
                    self._save(t)
                self.append_log(tid, f"CANCELLED: {e}")
            except SystemExit as e:                 # pp.die() → 任务失败而非杀线程
                with self.lock:
                    t["status"] = "failed"
                    t["error"] = f"exited: {e.code}"
                    t["summary"] = t.get("log", [""])[-1] or f"exited: {e.code}"
                    t["exit_code"] = e.code if isinstance(e.code, int) else 3
                    t["finished"] = _now()
                    self._save(t)
                self.append_log(tid, f"EXIT: {e.code}")
            except Exception as e:
                with self.lock:
                    t["status"] = "failed"
                    t["error"] = f"{e!r}"
                    t["summary"] = str(e)
                    t["exit_code"] = 3
                    t["traceback"] = traceback.format_exc()[-4000:]
                    t["finished"] = _now()
                    self._save(t)
                self.append_log(tid, f"EXCEPTION: {e!r}")

    def _check_cancel(self, t):
        cur = self.get(t["id"])
        if cur and cur.get("cancel_requested"):
            raise RuntimeError("任务被用户取消")

    # ============================ 动作实现 ============================
    def _act_preview_sync(self, p, t):
        """只读预览: 发现+计划, 零仓库写入。"""
        if p["code"] not in pp.load_registry():
            raise ParamError(f"科目 {p['code']} 未登记; 新科目请用 onboard")
        args = argparse_ns(p, latest=True, apply=False,
                           source_url=p.get("source_url"),
                           source_list=p.get("source_list"))
        sub = pp.get_subject(p["code"])
        report = {"targets": [], "failures": []}
        files = pp.discover_flow(p["code"], sub, args, report, log=print)
        state = pp.load_state()
        pp.plan_and_download(p["code"], sub, files, _years_tuple(p.get("years")),
                             False, state, report, log=print)
        target = pp.resolve_latest_syllabus(sub.get("caie_page") or "",
                                            log=print) \
            if sub.get("caie_page") else None
        d = report.get("discovery") or {}
        summary = (f"预览 {p['code']}: 会话页 {d.get('session_pages', 0)} · "
                   f"文件 {len(files)} · 待下载 {report['plan']['to_download']} · "
                   f"估算 {report['plan']['bytes_estimate']/1048576:.1f}MB · "
                   f"大纲 {target['version'] if target else '不可确认'}")
        if not files:
            summary += " ⚠来源为空(非'没有新卷')"
        return {"ok": True, "summary": summary, "preview": {
            "discovery": d, "plan": report["plan"],
            "targets": report["targets"][:400],
            "missing_ms": report.get("missing_ms", []),
            "syllabus": {"version": target["version"], "url": target["url"]}
            if target else None, "zero_write": True}, "exit_code": 0}

    def _act_sync(self, p, t):
        cc = self._cc_factory(t)
        rc = pp.cmd_sync(argparse_ns(
            p, latest=p.get("latest", True), apply=True,
            source_url=p.get("source_url"), source_list=p.get("source_list")),
            cancel_check=cc)
        rc = rc or 0
        return {"ok": rc == 0, "exit_code": rc,
                "summary": f"sync {p['code']} 退出码 {rc}"
                           + ("" if rc == 0 else "(见任务日志/运行报告)")}

    def _act_preview_onboard(self, p, t):
        args = argparse_ns(p, apply=False, source_url=p.get("source_url"),
                           source_list=p.get("source_list"))
        sub = _onboard_sub(p, preview=True)
        report = {"targets": [], "failures": []}
        files = pp.discover_flow(p["code"], sub, args, report, log=print)
        state = pp.load_state()
        pp.plan_and_download(p["code"], sub, files, _years_tuple(p.get("years")),
                             False, state, report, log=print)
        summary = (f"预览新科目 {p['code']}: 发现 {len(files)} 文件 · "
                   f"待下载 {report['plan']['to_download']}")
        return {"ok": True, "summary": summary, "preview": {
            "discovery": report.get("discovery"), "plan": report["plan"],
            "targets": report["targets"][:400]}, "exit_code": 0}

    def _act_onboard(self, p, t):
        cc = self._cc_factory(t)
        cc(stage="download")
        rc = pp.cmd_onboard(argparse_ns(
            p, apply=True, source_url=p.get("source_url"),
            source_list=p.get("source_list")))
        rc = rc or 0
        cc(stage="extract")
        return {"ok": rc == 0, "exit_code": rc,
                "summary": f"onboard {p['code']} 退出码 {rc}"}

    def _act_syllabus_check(self, p, t):
        sub = pp.get_subject(p["code"])
        target = pp.resolve_latest_syllabus(p.get("url") or sub["caie_page"],
                                            log=print)
        if not target:
            return {"ok": False, "exit_code": 2,
                    "summary": "官方页无法确认最新 syllabus(不发布)"}
        recorded = (pp.subj_state(pp.load_state(), p["code"])
                    .get("syllabus") or {}).get("version")
        return {"ok": True, "exit_code": 0,
                "summary": (f"官方最新 {target['version']} · 本地 {recorded or '无'}"
                            f"{' · 需迁移' if recorded and recorded != target['version'] else ''}"),
                "target": target}

    def _act_syllabus_apply(self, p, t):
        rc = pp.cmd_syllabus(argparse_ns(p, apply=True, url=p.get("url")))
        rc = rc or 0
        return {"ok": rc == 0, "exit_code": rc,
                "summary": f"syllabus {p['code']} 退出码 {rc}"}

    def _act_extract(self, p, t):
        r = pp.cmd_extract(argparse_ns(p, apply=True, papers=p.get("papers")))
        return {"ok": True, "exit_code": 0,
                "summary": f"切题: 新切/重切 {r['extracted']} 卷",
                "extracted": r["extracted"], "forced": r["forced"]}

    def _act_classify_prep(self, p, t):
        pp.get_subject(p["code"])
        import gen_workfiles as gw
        work = os.path.join(pp.PIPE_DIR, "work", p["code"], "units")
        out = os.path.join(pp.PIPE_DIR, "work", p["code"], "classify")
        gw.run(p["code"], work, out, root=pp.ROOT)
        files = sorted(os.listdir(out)) if os.path.isdir(out) else []
        return {"ok": True, "exit_code": 0,
                "summary": f"AI 判定输入已生成({len(files)} 个文件)",
                "files": files, "out_dir": os.path.relpath(out, pp.ROOT)}

    def _act_import_tags(self, p, t):
        """AI 判定结果导入: payload = [{code(paper), tags:{uid:[...]}}] 或
        {paper: {uid: [...]}}; 校验单元存在后写 tags_<code>/<paper>.json。"""
        code = p["code"]
        pp.get_subject(code)
        payload = p.get("payload")
        if not payload:
            return {"ok": False, "exit_code": 2, "summary": "缺少 payload"}
        papers = p.get("papers")
        norm = []
        if isinstance(payload, dict):
            norm = [{"code": k, "tags": v} for k, v in payload.items()]
        elif isinstance(payload, list):
            for it in payload:
                if not isinstance(it, dict) or "code" not in it \
                        or not isinstance(it.get("tags"), dict):
                    return {"ok": False, "exit_code": 2,
                            "summary": "payload 条目需为 {code, tags}"}
                norm.append(it)
        else:
            return {"ok": False, "exit_code": 2, "summary": "payload 格式错误"}
        if papers:
            keep = set(papers if isinstance(papers, list) else [papers])
            norm = [x for x in norm if x["code"] in keep]
        units_dir = os.path.join(pp.ROOT, "data", "units", code)
        work_dir = os.path.join(pp.PIPE_DIR, "work", code, "units")
        known = {f[:-5] for f in os.listdir(units_dir)
                 if f.endswith(".json")} if os.path.isdir(units_dir) else set()
        if os.path.isdir(work_dir):
            known |= {f[:-8] for f in os.listdir(work_dir)
                      if f.endswith("_qp.json")}
        saved, skipped = [], []
        for it in norm:
            pc = it["code"]
            if not re.fullmatch(r"[a-z0-9_]+", pc):
                skipped.append(f"{pc}: 卷代码不合法")
                continue
            if known and pc not in known:
                skipped.append(f"{pc}: 未知卷")
                continue
            path = os.path.join(pp.ROOT, f"tags_{code}", f"{pc}.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"code": pc, "tags": it["tags"]}, f,
                          ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            saved.append(pc)
        return {"ok": not skipped or bool(saved), "exit_code": 0 if saved else 2,
                "summary": f"AI 结果导入: 保存 {len(saved)} 卷"
                           + (f" · 跳过 {len(skipped)}: {skipped[:3]}" if skipped else ""),
                "saved": saved, "skipped": skipped}

    def _act_import_topics(self, p, t):
        """topics JSON 导入(P1-2): 一律先存**草稿**并做结构/版本/来源校验;
        不直接改动正式 topics_<code>.json —— 新科目由 publish 把草稿转正,
        已有科目须经「按新大纲迁移+判定+发布」流程。"""
        code = p["code"]
        payload = p.get("payload")
        if not isinstance(payload, dict):
            return {"ok": False, "exit_code": 2,
                    "summary": "payload 应为 topics JSON 对象"}
        problems = []
        sections = payload.get("sections")
        if not isinstance(sections, list) or not sections:
            problems.append("sections 需为非空数组")
        seen = set()
        if isinstance(sections, list):
            for s in sections:
                if not isinstance(s, dict):
                    problems.append(f"章节需为对象: {str(s)[:50]}")
                    continue
                c = s.get("code")
                if not isinstance(c, str) or not c:
                    problems.append(f"章节缺 code: {str(s)[:60]}")
                    continue
                if c in seen:
                    problems.append(f"章节 code 重复: {c}")
                seen.add(c)
                subs = s.get("subs")
                if not isinstance(subs, list) or not subs:
                    problems.append(f"章 {c} 需要至少 1 个小节(subs)")
                for x in subs or []:
                    sc = x.get("code") if isinstance(x, dict) else None
                    if not sc:
                        problems.append(f"{c} 小节缺 code")
                    elif sc in seen:
                        problems.append(f"小节 code 重复: {sc}")
                    seen.add(sc)
        ver = payload.get("syllabus_version")
        if not isinstance(ver, str) or not re.fullmatch(
                r"\d{4}(-\d{4})?", ver or ""):
            problems.append(f"syllabus_version 应为年份或年份区间(如 2028-2030), "
                            f"收到 {ver!r}")
        if not (payload.get("source") or "").strip():
            problems.append("source 必填(官方 PDF 文件名/页码来源, 供追溯)")
        if payload.get("subject") not in (None, code):
            problems.append(f"subject 应为 {code}")
        if problems:
            return {"ok": False, "exit_code": 2,
                    "summary": "topics 草稿校验失败: " + "; ".join(problems[:6])}
        tax = {"subject": code,
               "name": payload.get("name") or f"Subject {code}",
               "zh": payload.get("zh") or code,
               "source": payload.get("source"),
               "syllabus_version": ver,
               "note": (payload.get("note") or "") + " [导入草稿, 待发布转正]",
               "levels": payload.get("levels") or [],
               "sections": sections,
               "imported_at": _now()}
        draft = ms.imported_topics_path(code)
        os.makedirs(os.path.dirname(draft), exist_ok=True)
        tmp = draft + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(tax, f, ensure_ascii=False, indent=1)
        os.replace(tmp, draft)
        n_sub = sum(len(s.get("subs") or []) for s in sections)
        formal = os.path.join(pp.ROOT, f"topics_{code}.json")
        if os.path.exists(formal):
            return {"ok": True, "exit_code": 0,
                    "summary": (f"已有科目 {code}: 导入草稿已保存"
                                f"({len(sections)} 章 {n_sub} 小节, 版本 {ver}), "
                                f"正式 topics 未改动; 须经「按新大纲迁移+判定+发布」"
                                f"生效"),
                    "draft": os.path.relpath(draft, pp.ROOT)}
        return {"ok": True, "exit_code": 0,
                "summary": (f"新科目 {code}: 导入草稿已保存({len(sections)} 章 "
                            f"{n_sub} 小节, 版本 {ver}); 由发布(publish)把草稿转正"),
                "draft": os.path.relpath(draft, pp.ROOT)}

    def _act_adjudicate_new(self, p, t):
        cc = self._cc_factory(t)
        cc(stage="adjudicate")
        res = ms.adjudicate_new_papers(p["code"], log=print)
        return {"ok": bool(res.get("ok")), "exit_code": 0 if res.get("ok") else 2,
                "summary": f"判定: {res.get('summary')} · 新卷 "
                           f"{len(res.get('new_papers') or [])}",
                "new_papers": res.get("new_papers"),
                "problems": (res.get("problems") or [])[:20]}

    def _act_publish(self, p, t):
        code = p["code"]
        # 版本核准门禁: 官方登记版本与判定目标版本不一致 → 禁止发布
        state = pp.load_state()
        registered = (pp.subj_state(state, code).get("syllabus") or {}).get("version")
        topics_path = os.path.join(pp.ROOT, f"topics_{code}.json")
        target = json.load(open(topics_path, encoding="utf-8")).get(
            "syllabus_version") if os.path.exists(topics_path) else None
        if target is None:
            draft = ms.imported_topics_path(code)
            if os.path.exists(draft):
                with open(draft, encoding="utf-8") as f:
                    target = json.load(f).get("syllabus_version")
        if registered and target and registered != target:
            return {"ok": False, "exit_code": 2,
                    "summary": (f"版本未核准: 官方登记 {registered} != 判定目标 "
                                f"{target}; 请先在「大纲与迁移」完成迁移")}

        def cc_commit():                     # 最后取消边界; 之后为不可中断提交
            self.raise_if_cancelled(t["id"])
            with self.lock:
                t["stage"] = "commit"
                self._save(t)
            self.append_log(t["id"], "-- 进入提交阶段(不可中断)")
        res = ms.publish(code, log=print, cancel_check=cc_commit)
        return {"ok": bool(res.get("ok")), "exit_code": 0 if res.get("ok") else 2,
                "summary": res.get("summary"),
                "problems": (res.get("problems") or [])[:20],
                "counts": res.get("counts"),
                "published": res.get("published"),
                "pending_published": res.get("pending_published"),
                "pending_refresh": res.get("pending_refresh") or [],
                "stage": res.get("stage"),
                "version": res.get("version"),
                "index_refresh_errors": res.get("index_refresh_errors") or []}

    def _act_confirm_candidates(self, p, t):
        """按 conf 阈值批量确认自动候选为 IN(网页「按conf批量确认IN」):
        只处理 PENDING · auto-candidate · 非作废 · 候选标签合法的单元;
        写 overrides(重跑判定不丢)。dry_run=True 只返回数量预览。"""
        try:
            min_conf = float(p.get("min_conf", 0.67))
        except (TypeError, ValueError):
            raise ParamError("min_conf 应为 0~1 之间的小数")
        if not (0 < min_conf <= 1):
            raise ParamError("min_conf 应为 0~1 之间的小数")
        dry = bool(p.get("dry_run"))
        res = ms.batch_confirm_candidates(p["code"], min_conf, dry_run=dry,
                                          log=print)
        if not res.get("ok"):
            return {"ok": False, "exit_code": 2, "summary": res.get("error"),
                    "count": 0}
        if dry:
            return {"ok": True, "exit_code": 0,
                    "summary": (f"预览: {res['count']} 个 PENDING 单元符合 "
                                f"conf≥{min_conf:.2f} 且带候选标签"
                                f"({res['papers']} 卷); 未改动"),
                    "count": res["count"], "papers": res["papers"], "applied": 0}
        c = res.get("counts") or {}
        return {"ok": True, "exit_code": 0,
                "summary": (f"已批量确认 {res['applied']} 个单元为 IN"
                            f"(conf≥{min_conf:.2f}, {res['papers']} 卷, "
                            f"overrides 已记录); "
                            f"IN {c.get('in')} · PENDING {c.get('pending')}"),
                "count": res["count"], "papers": res["papers"],
                "applied": res["applied"], "counts": c}

    def _act_migrate(self, p, t):
        cc = self._cc_factory(t)
        res = ms.migrate(p["code"], apply=True, log=print, cancel_check=cc)
        return {"ok": bool(res.get("ok")), "exit_code": 0 if res.get("ok") else 2,
                "summary": res.get("summary"),
                "problems": (res.get("problems") or [])[:20]}

    def _act_refresh_index(self, p, t):
        """刷新服务端索引(P0-1/P0-4): 同步等待两类索引构建+安装完成, 按结果
        返回; 成功后只晋升**已实际提交到当前活动版本**的 pending_refresh 卷
        (意图版本==活动版本且正式文件存在 —— 翻转前失败的过期意图不晋升,
        新卷保持待发布)。"""
        errs = ms._fire_refresh_hooks(None)
        if errs:
            return {"ok": False, "exit_code": 2,
                    "summary": "索引刷新失败: " + "; ".join(errs)[:400]
                               + "(待发布未清零, 可重试)",
                    "problems": errs[:10]}
        promoted = ms.promote_refresh_pending(None)
        return {"ok": True, "exit_code": 0,
                "summary": "服务索引已刷新并安装完成(data_version 已就绪)"
                           + (f"; 晋升 {len(promoted)} 卷为完全发布"
                              if promoted else "")}


# ============================ 动作辅助 ============================
def argparse_ns(p, **kw):
    import argparse
    ns = argparse.Namespace(**{k: v for k, v in p.items() if k != "payload"})
    for k, v in kw.items():
        setattr(ns, k, v)
    for k, v in (("years", None), ("latest", False), ("apply", False),
                 ("source_url", None), ("source_list", None),
                 ("papers", None), ("data_dir", None), ("name", None),
                 ("zh", None), ("url", None), ("code", None)):
        if not hasattr(ns, k):
            setattr(ns, k, v)
    return ns


def _years_tuple(years):
    if not years:
        return None
    m = re.match(r"^(\d{4})(?:-(\d{4}))?$", years)
    if not m:
        raise ParamError(f"years 格式应为 2025-2026 或 2025, 收到 {years!r}")
    return (int(m.group(1)), int(m.group(2) or m.group(1)))


def _onboard_sub(p, preview=False):
    """onboard 预览用登记(不写 registry)。"""
    subs = pp.load_registry()
    if p["code"] in subs:
        return subs[p["code"]]
    return {"code": p["code"], "name": p.get("name") or f"Subject {p['code']}",
            "zh": p.get("zh") or p["code"], "level": "AS & A Level",
            "data_dir": p.get("data_dir") or f"{p['code']}（{p.get('zh') or p['code']}）",
            "source": {"kind": "papacambridge", "slug": None},
            "caie_page": p.get("url") or "", "paper_names": {}}


# ============================ 只读查询 ============================
def overview():
    """科目总览: 大纲版本/判定计数/待发布/最近任务。读取时安全校准已发布
    基线(P0-1: 只登记 data/units 带正式 status 的卷)。"""
    out = []
    state = pp.load_state()
    for code, sub in sorted(pp.load_registry().items()):
        ms.ensure_published_baseline(code, log=lambda *a: None)
    published = ms.load_published()["subjects"]
    for code, sub in sorted(pp.load_registry().items()):
        st = state["subjects"].get(code, {})
        syl = st.get("syllabus") or {}
        adj_path = os.path.join(pp.PIPE_DIR, "adjudication", f"{code}.json")
        counts = None
        target = None
        if os.path.exists(adj_path):
            adj = json.load(open(adj_path, encoding="utf-8"))
            counts = adj.get("counts")
            target = (adj.get("target") or {}).get("version")
        work = os.path.join(pp.PIPE_DIR, "work", code, "units")
        pending = ms.pending_papers(code, log=lambda *a: None)   # 统一口径:
        # 未登记 published 且无正式 status 且切题非空(扫描空卷不计)
        units_dir = os.path.join(pp.ROOT, "data", "units", code)
        n_papers = len([f for f in os.listdir(units_dir)
                        if f.endswith(".json")]) if os.path.isdir(units_dir) else 0
        out.append({"code": code, "name": sub.get("name"), "zh": sub.get("zh"),
                    "data_dir": sub.get("data_dir"),
                    "papers": n_papers, "counts": counts,
                    "syllabus": {"version": syl.get("version"),
                                 "target": target,
                                 "pdf": (syl.get("pdf") or {}).get("file"),
                                 "sha256": (syl.get("pdf") or {}).get("sha256"),
                                 "url": (syl.get("pdf") or {}).get("url")},
                    "pending_papers": pending,
                    "pending_refresh": sorted(ms.effective_refresh_pending(code)),
                    "quarantine": len(st.get("quarantine", []))})
    return out


def adjudication_page(subject, paper=None, status=None, page=1, page_size=50,
                      search=None):
    """复核页数据: 判定条目 + 题面/分值/标签候选。"""
    adj_path = os.path.join(pp.PIPE_DIR, "adjudication", f"{subject}.json")
    if not os.path.exists(adj_path):
        raise ParamError(f"科目 {subject} 尚无判定记录(先运行切题/判定)")
    adj = json.load(open(adj_path, encoding="utf-8"))
    tax = json.load(open(os.path.join(pp.ROOT, f"topics_{subject}.json"),
                         encoding="utf-8"))
    tag_options = [{"code": s["code"], "en": s.get("en"),
                    "zh": s.get("zh")} for s in tax["sections"]]
    for s in tax["sections"]:
        for x in s.get("subs", []):
            tag_options.append({"code": x["code"], "en": x.get("en"),
                                "zh": x.get("zh"), "sub": True})
    rows = []
    units_dir = os.path.join(pp.ROOT, "data", "units", subject)
    for pc, pa in sorted(adj["units"].items()):
        if paper and pc != paper:
            continue
        rec_path = os.path.join(units_dir, f"{pc}.json")
        rec = json.load(open(rec_path, encoding="utf-8")) \
            if os.path.exists(rec_path) else {}
        umap = {u["id"]: u for u in rec.get("units", [])}
        for uid, e in sorted(pa.items()):
            if status and e.get("status") != status:
                continue
            u = umap.get(uid, {})
            text = " ".join((u.get("text") or "").split())
            if search and search.lower() not in text.lower() \
                    and search.lower() not in uid.lower():
                continue
            rows.append({
                "paper": pc, "uid": uid, "q": u.get("q"), "part": u.get("part"),
                "marks": u.get("marks"), "page": u.get("page"),
                "status": e.get("status"), "tags": e.get("tags"),
                "reason": e.get("reason"), "basis": e.get("basis"),
                "candidate_tags": e.get("candidate_tags") or [],
                "conf": e.get("conf"),
                "discounted": bool(e.get("discounted")),
                "text": text[:400],
                "year": rec.get("year"), "session": rec.get("session"),
                "paper_no": rec.get("paper"),
                "qp_pdf": rec.get("qp_pdf"), "ms_pdf": rec.get("ms_pdf"),
                "detail": f"#/q/{subject}/{pc}/{uid}"})
    total = len(rows)
    page = max(1, min(page, (total + page_size - 1) // max(1, page_size) or 1))
    return {"subject": subject, "total": total, "page": page,
            "page_size": page_size,
            "target": adj.get("target"),
            "counts": adj.get("counts"),
            "rows": rows[(page - 1) * page_size: page * page_size],
            "tag_options": tag_options}


def adjudication_update(subject, updates):
    """保存复核草稿: 写 overrides(重跑保留)并同步现行判定文件。"""
    if not isinstance(updates, list) or not updates:
        raise ParamError("updates 应为非空数组")
    pp_codes = set()
    adj_path = os.path.join(pp.PIPE_DIR, "adjudication", f"{subject}.json")
    if not os.path.exists(adj_path):
        raise ParamError(f"科目 {subject} 尚无判定记录")
    adj = json.load(open(adj_path, encoding="utf-8"))
    tax = json.load(open(os.path.join(pp.ROOT, f"topics_{subject}.json"),
                         encoding="utf-8"))
    valid = {s["code"] for s in tax["sections"]} | \
            {x["code"] for s in tax["sections"] for x in s.get("subs", [])}
    applied = []
    for it in updates:
        pc, uid = it.get("paper"), it.get("uid")
        st = it.get("status")
        if not re.fullmatch(r"[a-z0-9_]+", str(pc or "")) or \
                not re.fullmatch(r"[0-9a-zA-Z#]+", str(uid or "")):
            raise ParamError(f"paper/uid 不合法: {pc}/{uid}")
        if st not in ("IN", "OUT", "PENDING", "CONTEXT"):
            raise ParamError(f"status 应为 IN/OUT/PENDING/CONTEXT, 收到 {st!r}")
        tags = it.get("tags") or []
        reason = (it.get("reason") or "").strip()
        if st == "IN":
            if not (1 <= len(tags) <= 3) or not set(tags) <= valid:
                raise ParamError(f"{pc}#{uid}: IN 需要 1-3 个合法标签")
        elif not reason:
            raise ParamError(f"{pc}#{uid}: {st} 需要理由/说明")
        entry = {"status": st, "reason": reason or None,
                 "reviewed_at": _now(), "basis": "review-override"}
        if st == "IN":
            entry["tags"] = tags
            entry["reason"] = reason or "人工复核: IN"
        if it.get("discounted"):
            entry["discounted"] = True
        ms.record_override(subject, pc, uid, entry)
        adj = json.load(open(adj_path, encoding="utf-8"))   # record 已更新
        applied.append(f"{pc}#{uid}->{st}")
    problems, n = ms.validate_adjudication(adj, tax)
    return {"ok": not problems, "applied": applied,
            "counts": adj.get("counts"), "problems": problems[:20]}


def classify_inputs(subject):
    """AI SOP 输入文件列表与内容(可复制/下载)。"""
    out = os.path.join(pp.PIPE_DIR, "work", subject, "classify")
    if not os.path.isdir(out):
        return {"subject": subject, "files": [],
                "note": "尚未生成: 先运行 classify_prep"}
    files = []
    for f in sorted(os.listdir(out)):
        p = os.path.join(out, f)
        if os.path.isfile(p):
            files.append({"name": f, "bytes": os.path.getsize(p)})
    return {"subject": subject, "files": files,
            "out_dir": os.path.relpath(out, pp.ROOT)}


def classify_input_content(subject, name):
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+\.txt", name or ""):
        raise ParamError("文件名不合法")
    p = os.path.join(pp.PIPE_DIR, "work", subject, "classify", name)
    if not os.path.isfile(p):
        raise ParamError("文件不存在")
    return open(p, encoding="utf-8").read()


def runs_list(subject=None, limit=50):
    runs = []
    if os.path.isdir(pp.RUNS_DIR):
        for f in sorted(os.listdir(pp.RUNS_DIR), reverse=True):
            if not f.endswith(".json"):
                continue
            if subject and not f.endswith(f"_{subject}.json"):
                continue
            p = os.path.join(pp.RUNS_DIR, f)
            try:
                rep = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            runs.append({"file": f, "cmd": rep.get("cmd"), "code": rep.get("code"),
                         "mode": rep.get("mode"), "started": rep.get("started"),
                         "finished": rep.get("finished"),
                         "stage": rep.get("stage"),
                         "failures": len(rep.get("failures") or [])})
            if len(runs) >= limit:
                break
    return runs


def run_report(file):
    if not re.fullmatch(r"[0-9T_a-z]+\.json", file or ""):
        raise ParamError("报告文件名不合法")
    p = os.path.join(pp.RUNS_DIR, file)
    if not os.path.isfile(p):
        raise ParamError("报告不存在")
    return json.load(open(p, encoding="utf-8"))


def apply_root(root):
    """服务以 --root 启动时切换业务层根目录(整个进程专用该根)。"""
    global ROOT, TASKS_DIR
    pp._apply_root(root)
    ms._apply_root(root)
    ROOT = root
    TASKS_DIR = os.path.join(pp.PIPE_DIR, "tasks")


TM = None


def get_manager():
    global TM
    if TM is None:
        TM = TaskManager()
        TM.start_worker()
    return TM
