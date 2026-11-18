#!/usr/bin/env python3
"""persist_units.py —— 把切题产物持久化到项目内 data/ 目录（服务运行不依赖 /tmp）。

数据来源（白名单：只收录 extract_questions.py 已切出的卷与单元，不新增题目单元）：
  - /tmp/qp9701/units、/tmp/qp9702/units、/tmp/qp9709/units（现有完整题面）
  - 9618：/tmp/qp9618/units 若不存在，仅对磁盘上已知的 118 份 QP（已在
    classified_9618.json 中）重跑原有 extract_questions.py 恢复完整题面；
    不扩大卷代码范围。
同时把 classified_<科目>.json 的知识点标签合并进来：
  - in_old_index=false 且 topics=null 的单元 = 已切出但未进入旧索引/未分类
    （9702 的 66 份 2002-2009 老卷；以及任何与 classified 差异的单元）。

输出:
  data/units/<科目>/<卷代码>.json   每卷一份（QP 单元全文 + MS 页码 + 分类状态）
  data/reconcile.json               对账明细（47,218 vs 43,645 差异原因逐条）

用法:
  /usr/bin/python3 persist_units.py            # 全部四科
  /usr/bin/python3 persist_units.py 9701 9702  # 指定科目
"""
import json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = "/usr/bin/python3"          # 带 PyMuPDF 的解释器（README 已注明）


def sub_dirs():
    """科目 → 数据目录。优先 subjects.json 登记, 兜底磁盘 <代码>（<名>） 发现。"""
    out = {}
    try:
        subs = json.load(open(os.path.join(ROOT, "subjects.json"),
                              encoding="utf-8"))["subjects"]
        for code, s in subs.items():
            out[code] = s["data_dir"]
    except Exception:
        pass
    for d in sorted(os.listdir(ROOT)):
        m = re.match(r"^(\d{4})（(.*)）$", d)
        if m and os.path.isdir(os.path.join(ROOT, d)):
            out.setdefault(m.group(1), d)
    return out


SUBJ_DIRS = sub_dirs()
SUBJECTS = sys.argv[1:] or sorted(SUBJ_DIRS)

FNAME_RE = {c: re.compile(rf"^{c}_([msw])(\d{{2}})_(qp|ms)_(\d)(\d?)\.pdf$") for c in SUBJ_DIRS}


def ensure_tmp_units(subject):
    """确保切题单元目录存在。优先仓库工作目录 data/pipeline/work/<code>/units
    (paper_pipeline extract 产物, 持久化), 兜底旧 /tmp/qp<code>/units;
    9618 允许重跑原提取脚本恢复白名单卷。"""
    work = os.path.join(ROOT, "data", "pipeline", "work", subject, "units")
    for cand in (work, f"/tmp/qp{subject}/units"):
        if os.path.isdir(cand) and any(f.endswith("_qp.json")
                                       for f in os.listdir(cand)):
            return cand
    if subject not in SUBJ_DIRS:
        sys.exit(f"科目 {subject} 无数据目录登记, 且无已切单元目录")
    print(f"{subject}: 工作目录与 /tmp 均无单元, 重跑 extract_questions.py 恢复 …")
    subprocess.run([PY, os.path.join(ROOT, "extract_questions.py"), subject,
                    os.path.join(ROOT, SUBJ_DIRS[subject]), work], check=True)
    return work


def _norm(s):
    return " ".join(s.split()).casefold()


def attach_text_for_9618(cls_paper, new_units):
    """9618 专用：白名单 = classified 里的旧单元（ID 顺序为准），重跑产物只用来补全文。

    重跑用的 extract_questions.py 比当年切 9618 的版本新，个别卷切点漂移
    （如 '10'→'10a'，s25_22 把迹表数字误切成假单元）。按约束不创建新单元：
    旧单元逐个取全文 —— 先精确 ID，再按题面前缀匹配（ID 重命名），都失败则
    保留 150 字符摘要并标记 text_truncated；重跑多出的单元直接丢弃。"""
    new_by_id = {u["id"]: u for u in new_units}
    used = set()
    out = []
    for cu in cls_paper["units"]:
        snip = _norm(cu["snippet"])
        probe = snip[:60]
        u_new, how = new_by_id.get(cu["id"]), "id"
        # id 命中但正文对不上(新版提取器同卷切点漂移, 同 id 已是另一题) → 视为未命中
        if u_new is not None and probe and probe[:40] not in _norm(u_new["text"]):
            u_new, how = None, None
        if u_new is None:
            cands = [u for u in new_units
                     if u["id"] not in used and probe and probe in _norm(u["text"])]
            if len(cands) == 1:
                u_new, how = cands[0], "text"
        rec = {"id": cu["id"], "q": cu["q"], "part": cu["part"], "page": cu["page"],
               "marks": cu["marks"], "topics": cu.get("topics"),
               "in_old_index": True}
        if u_new is not None:
            used.add(u_new["id"])
            rec.update(text=u_new["text"], text_source=how)
        else:
            rec.update(text=cu["snippet"], text_source="snippet_truncated")
        out.append(rec)
    return out


def persist(subject):
    tmp = ensure_tmp_units(subject)
    out_dir = os.path.join(ROOT, "data", "units", subject)
    os.makedirs(out_dir, exist_ok=True)
    classified = json.load(open(os.path.join(ROOT, f"classified_{subject}.json")))
    cls_by_code = {p["code"]: p for p in classified["papers"]}
    # 迁移后的判定(status/tags)以判定文件为准, 重跑 persist 不得回退
    adj_path = os.path.join(ROOT, "data", "pipeline", "adjudication",
                            f"{subject}.json")
    adj = json.load(open(adj_path))["units"] if os.path.exists(adj_path) else None
    prev = {}                      # 现有 units 记录(保留 status/reason 等字段)
    if os.path.isdir(out_dir):
        for f in os.listdir(out_dir):
            if f.endswith(".json"):
                try:
                    r = json.load(open(os.path.join(out_dir, f)))
                    prev[r["code"]] = {u["id"]: u for u in r.get("units", [])}
                except Exception:
                    pass

    codes = sorted(f[:-len("_qp.json")] for f in os.listdir(tmp) if f.endswith("_qp.json"))
    n_units = n_old = n_new = 0
    for code in codes:
        qp = json.load(open(os.path.join(tmp, f"{code}_qp.json")))
        ms_path = os.path.join(tmp, f"{code}_ms.json")
        ms_pages = json.load(open(ms_path))["ms_pages"] if os.path.exists(ms_path) else {}
        cls = cls_by_code.get(code)
        if subject == "9618" and cls is not None and not adj:
            # 白名单 = 旧 classified 单元；重跑产物仅补全文（见 attach_text_for_9618）
            units = attach_text_for_9618(cls, qp["units"])
            n_units += len(units)
            n_old += len(units)
        else:
            cls_tags = {u["id"]: u.get("topics") for u in cls["units"]} if cls else {}
            paper_adj = (adj or {}).get(code) or {}
            prev_p = prev.get(code, {})
            units = []
            for u in qp["units"]:
                pu = prev_p.get(u["id"]) or {}
                e = paper_adj.get(u["id"])
                if e:                      # 判定文件为准
                    status, topics = e.get("status"), e.get("tags")
                    reason = e.get("reason")
                else:                      # 无判定: 保留旧 status, 否则未分类
                    status = pu.get("status")
                    topics = pu.get("topics") if status == "IN" else \
                        (cls_tags.get(u["id"]) if not status else None)
                    reason = pu.get("reason")
                in_old = topics is not None
                rec_u = {"id": u["id"], "q": u["q"], "part": u["part"],
                         "page": u["page"], "marks": u["marks"], "text": u["text"],
                         "topics": topics, "in_old_index": in_old,
                         "text_source": pu.get("text_source", "id")}
                if status:
                    rec_u["status"] = status
                if reason:
                    rec_u["reason"] = reason
                units.append(rec_u)
                n_units += 1
                n_old += in_old
                n_new += not in_old
        rec = {"subject": subject, "code": code, "year": qp["year"],
               "session": qp["session"], "paper": qp["paper"], "variant": qp["variant"],
               "qp_pdf": qp["qp_pdf"], "ms_pdf": qp.get("ms_pdf"),
               "ms_pages": ms_pages, "units": units}
        prev_meta = prev.get(code)
        with open(os.path.join(out_dir, f"{code}.json"), "w") as fh:
            json.dump(rec, fh, ensure_ascii=False)
    return len(codes), n_units, n_old, n_new


def reconcile(subjects):
    """磁盘 QP → 已切单元 → classified 的逐卷对账，写入 data/reconcile.json。"""
    detail = {}
    for subject in subjects:
        rows, counts = [], {"disk_qp": 0, "no_text_layer": 0, "rule_excluded": 0,
                            "reversed_duplicate": 0, "extracted_papers": 0, "units": 0,
                            "classified_units": 0, "not_in_classified_units": 0}
        base = os.path.join(ROOT, SUBJ_DIRS[subject])
        for dp, _, fs in os.walk(base):
            for f in sorted(fs):
                if not f.endswith(".pdf") or "_qp_" not in f:
                    continue
                counts["disk_qp"] += 1
                m = FNAME_RE[subject].match(f)
                reason = None
                if m:
                    if m.group(4) == "0":
                        reason = "rule_excluded"          # 9709 s08 qp_01/06 (paper=0)
                elif "_qp_et_" in f:
                    reason = "rule_excluded"              # 9702 w09 qp_et_33/34 (ET)
                elif re.match(rf"^[msw]\d{{2}}_{subject}_", f):
                    reason = "reversed_duplicate"         # s14_9709_qp_13 与正名卷同卷不同扫描版
                if reason:
                    counts[reason] += 1
                    rows.append({"file": f, "status": reason})
                    continue
                if not m:
                    rows.append({"file": f, "status": "unmatched"})
                    continue
                seas, yy, typ, paper, variant = m.groups()
                code = f"{seas}{yy}_{paper}{variant or '1'}"
                upath = os.path.join(ROOT, "data", "units", subject, f"{code}.json")
                if not os.path.exists(upath):
                    rows.append({"file": f, "status": "no_unit_file"})
                    continue
                rec = json.load(open(upath))
                n = len(rec["units"])
                counts["extracted_papers"] += 1
                counts["units"] += n
                if n == 0:
                    # 是否真无文字层：data/units 里的 0 单元卷 = 提取产物为空
                    rows.append({"file": f, "status": "zero_units", "code": code})
                    continue
                cls = json.load(open(os.path.join(ROOT, f"classified_{subject}.json"))) \
                    if False else None
                rows.append({"file": f, "status": "ok", "code": code, "units": n})
        # classified 侧计数
        cls = json.load(open(os.path.join(ROOT, f"classified_{subject}.json")))
        counts["classified_units"] = sum(len(p["units"]) for p in cls["papers"])
        counts["not_in_classified_units"] = counts["units"] - counts["classified_units"]
        detail[subject] = {"counts": counts, "files": rows}
    with open(os.path.join(ROOT, "data", "reconcile.json"), "w") as fh:
        json.dump(detail, fh, ensure_ascii=False, indent=1)
    return detail


if __name__ == "__main__":
    total = {"papers": 0, "units": 0}
    for s in SUBJECTS:
        papers, units, old, new = persist(s)
        total["papers"] += papers
        total["units"] += units
        print(f"{s}: papers={papers} units={units} (旧索引 {old} · 未进旧索引 {new})")
    print(f"合计: papers={total['papers']} units={total['units']}")
    rec = reconcile(SUBJECTS)
    print("对账:")
    for s in SUBJECTS:
        c = rec[s]["counts"]
        print(f"  {s}: 磁盘QP={c['disk_qp']} 已切卷={c['extracted_papers']} 单元={c['units']} "
              f"零单元(无文字层)={sum(1 for r in rec[s]['files'] if r['status']=='zero_units')} "
              f"规则排除={c['rule_excluded']} 反转重复={c['reversed_duplicate']} "
              f"classified={c['classified_units']} 差={c['not_in_classified_units']}")
