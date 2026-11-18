#!/usr/bin/env python3
"""live_version.py —— 正式产物「单一活动版本入口」(P0-3b / 整体版本切换)。

第四轮验收阻断项: publish 在切换循环中逐文件 os.replace, 中途暂停/崩溃时
并发读者会看到新版 classified + 旧版 search_data 的混合状态。本模块把切换
改为**一次原子翻转**:

布局(全部在仓库内, 同一文件系统):
  data/pipeline/versions/<vid>/     一个完整站点版本: index.html/syllabus.html/
                                    *_data_*.js|json/topics_*.json/classified_*.json
                                    /index_*.html + data/units|loc|msloc 全树
  data/pipeline/versions/<vid>.json 版本清单 {status: ready|active|retired,
                                    subject, created, prev, files:{rel:sha256}}
  data/pipeline/live                符号链接 -> versions/<vid>   ← 唯一活动版本入口
  仓库根的受管路径 = 稳定符号链接 -> data/pipeline/live/<同相对路径>
      index.html -> data/pipeline/live/index.html
      data/units -> pipeline/live/data/units   (loc/msloc 同理, 整树一个入口)

切换 = os.replace(live.tmp, live) 一次原子重命名。任何时刻(含切换中进程崩溃)
所有读者(静态文件/服务端 API/索引构建/file:// 直开/verify_data)经根路径读到
的都是**完整旧版或完整新版**, 不存在混合版本; 恢复 = ensure_layout() 幂等
自愈(采纳已验证版本/完成中断的符号链接化/清理候选残留)。

暂停/崩溃注入: set_pause(fn) 后每个 checkpoint(name) 调 fn(name); 环境变量
ZJ_CRASH_AT=<checkpoint> 在该点 os._exit(70) 模拟进程崩溃(子进程测试用)。
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
VERSIONS_DIR = os.path.join(ROOT, "data", "pipeline", "versions")
LIVE = os.path.join(ROOT, "data", "pipeline", "live")

DATA_SUBS = ("units", "loc", "msloc")

# 根目录受管产物(符号链接到 live): 精确名 + 模式; notes.html 等不在内
_EXACT_MANAGED = ("index.html", "syllabus.html")
_MANAGED_RES = (
    re.compile(r"^topics_\d{4}\.json$"),
    re.compile(r"^classified_\d{4}\.json$"),
    re.compile(r"^topics_data_\d{4}\.js$"),
    re.compile(r"^search_data_\d{4}\.js$"),
    re.compile(r"^papers_data_\d{4}\.js$"),
    re.compile(r"^papers_data_\d{4}\.json$"),
    re.compile(r"^index_\d{4}\.html$"),
    re.compile(r"^index_\d{4}_topics\.html$"),
)


def apply_root(root):
    global ROOT, VERSIONS_DIR, LIVE
    ROOT = root
    VERSIONS_DIR = os.path.join(root, "data", "pipeline", "versions")
    LIVE = os.path.join(root, "data", "pipeline", "live")


def is_managed_root(name):
    """根目录文件名是否属于受管正式产物。"""
    return name in _EXACT_MANAGED or any(r.match(name) for r in _MANAGED_RES)


def _rel_target(target_abs, link_abs):
    """符号链接目标(相对链接所在目录, 仓库整体移动不失效)。"""
    return os.path.relpath(target_abs, os.path.dirname(link_abs))


# ============================ 暂停/崩溃注入 ============================
_PAUSE_FN = None
_PAUSE_LOCK = threading.Lock()


def set_pause(fn):
    """注册暂停钩子 fn(name); 测试在指定切换检查点阻塞/断言并发读取。"""
    global _PAUSE_FN
    with _PAUSE_LOCK:
        _PAUSE_FN = fn


def clear_pause():
    global _PAUSE_FN
    with _PAUSE_LOCK:
        _PAUSE_FN = None


def checkpoint(name):
    """切换关键检查点。生产环境为空操作; 测试可暂停或注入崩溃。"""
    with _PAUSE_LOCK:
        fn = _PAUSE_FN
    if fn is not None:
        fn(name)
    crash = os.environ.get("ZJ_CRASH_AT")
    if crash and crash == name:
        sys_exit_crash(name)


def sys_exit_crash(name):
    """硬崩溃模拟: 不触发任何 finally/回滚, 等价进程被杀。"""
    import sys
    sys.stderr.write(f"[live_version] ZJ_CRASH_AT={name} → os._exit(70)\n")
    sys.stderr.flush()
    os._exit(70)


class SwitchError(RuntimeError):
    """翻转后复核失败等切换阶段错误(已尝试翻回旧版)。"""


# ============================ 哈希/清单 ============================
def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_tree(root, rels=None):
    """root 下指定 rel 集合(缺省=全部受管路径)的 {rel: sha256}。"""
    out = {}
    if rels is None:
        rels = managed_rels_in(root)
    for rel in rels:
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            out[rel] = sha256_file(p)
    return out


def managed_rels_in(root):
    """root(某版本目录)内受管路径全集: 根产物 + 三棵数据树的 json。"""
    rels = set()
    for name in os.listdir(root):
        if is_managed_root(name) and os.path.isfile(os.path.join(root, name)):
            rels.add(name)
    for sub in DATA_SUBS:
        d = os.path.join(root, "data", sub)
        if not os.path.isdir(d):
            continue
        for dp, _, fs in os.walk(d):
            for f in fs:
                if f.endswith(".json"):
                    p = os.path.join(dp, f)
                    rels.add(os.path.relpath(p, root))
    return rels


def _manifest_path(vid):
    return os.path.join(VERSIONS_DIR, f"{vid}.json")


def _write_manifest(vid, **kw):
    tmp = _manifest_path(vid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kw, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _manifest_path(vid))


def read_manifest(vid):
    try:
        with open(_manifest_path(vid), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ============================ 活动版本解析 ============================
def live_target():
    """live 符号链接当前指向的版本 id; 无/无效返回 None。"""
    try:
        tgt = os.readlink(LIVE)
    except OSError:
        return None
    vid = os.path.basename(os.path.normpath(tgt))
    if os.path.isdir(os.path.join(VERSIONS_DIR, vid)):
        return vid
    return None


def active_version():
    return live_target()


def active_dir():
    """活动版本目录绝对路径(一次 readlink 解析, 调用方据此钉住读取)。"""
    vid = live_target()
    return os.path.join(VERSIONS_DIR, vid) if vid else None


def pinned_dirs():
    """一次解析 live 后固定三棵数据树的实际目录(请求级一致读取)。
    尚未采纳版本布局时回退到根目录真实路径。"""
    base = active_dir()
    if base is None:
        return {s: os.path.join(ROOT, "data", s) for s in DATA_SUBS}
    return {s: os.path.join(base, "data", s) for s in DATA_SUBS}


# ============================ 布局采纳/自愈 ============================
def ensure_layout(log=print):
    """幂等: 确保版本布局就绪。任何一步崩溃后重跑可完成收尾。
    返回当前活动版本 id。"""
    os.makedirs(VERSIONS_DIR, exist_ok=True)
    _sweep_tmp(log)
    _finish_dir_adoption(log)          # 上次 dir 符号链接化中断的收尾
    vid = live_target()
    if vid is None:
        vid = _recover_or_adopt(log)
    else:
        man = read_manifest(vid)
        if man and man.get("status") == "ready":
            # 翻转后、记账前崩溃: 内容已验证, 直接转正
            _write_manifest(vid, **{**man, "status": "active"})
            log(f"  [live] 版本 {vid} 自愈转正(翻转后崩溃恢复)")
    _ensure_root_links(log)
    _gc_versions(log)
    return vid


def _sweep_tmp(log):
    """清理崩溃残留: live.tmp* 符号链接与超龄候选目录。"""
    pipe = os.path.dirname(LIVE)
    if os.path.isdir(pipe):
        for f in os.listdir(pipe):
            if f.startswith("live.tmp"):
                try:
                    os.remove(os.path.join(pipe, f))
                    log(f"  [live] 清理残留临时链接 {f}")
                except OSError:
                    pass
    now = time.time()
    if os.path.isdir(VERSIONS_DIR):
        for f in os.listdir(VERSIONS_DIR):
            if not f.startswith(".cand_"):
                continue
            p = os.path.join(VERSIONS_DIR, f)
            try:
                if now - os.path.getmtime(p) > 7200:
                    shutil.rmtree(p, ignore_errors=True)
                    log(f"  [live] 清理超龄候选 {f}")
            except OSError:
                pass


def _live_link_target(rel):
    """受管路径 rel 的 live 符号链接目标(相对链接所在目录的路径)。
    rel 是仓库根相对路径(如 index.html 或 data/units); 链接可能位于根目录
    或 data/ 下, 目标必须按**链接的父目录**计算相对路径。"""
    target = os.path.join(ROOT, "data", "pipeline", "live", *rel.split("/"))
    return os.path.relpath(target, os.path.dirname(os.path.join(ROOT, rel)))


def _finish_dir_adoption(log):
    """data/units|loc|msloc 符号链接化中断的收尾(aside 目录存在=中断)。"""
    for sub in DATA_SUBS:
        d = os.path.join(ROOT, "data", sub)
        aside = d + ".__adopt__"
        if not os.path.isdir(aside):
            continue
        vid = live_target()
        # aside 内容在基线采纳时已硬链入版本; live 有效则直接补链接删 aside
        if vid and os.path.isdir(os.path.join(VERSIONS_DIR, vid, "data", sub)):
            if not os.path.lexists(d):
                os.symlink(_live_link_target(f"data/{sub}"), d)
            shutil.rmtree(aside, ignore_errors=True)
            log(f"  [live] 完成中断的 {sub} 符号链接化(清理 aside)")
        elif not os.path.lexists(d):
            os.rename(aside, d)    # 布局无效: 还原真实目录
            log(f"  [live] 还原 {sub} 真实目录(布局未生效)")


def _recover_or_adopt(log):
    """live 缺失: 优先恢复最近的已验证版本, 否则从根目录现状采纳基线。"""
    best = None
    if os.path.isdir(VERSIONS_DIR):
        for f in sorted(os.listdir(VERSIONS_DIR)):
            if f.startswith(".") or not os.path.isdir(os.path.join(VERSIONS_DIR, f)):
                continue
            man = read_manifest(f)
            if man and man.get("status") in ("ready", "active"):
                best = f             # 名字含时间戳, 排序后取最新
    if best:
        _flip(best, log)
        man = read_manifest(best) or {}
        _write_manifest(best, **{**man, "status": "active"})
        log(f"  [live] 恢复活动版本 {best}(崩溃后自愈)")
        return best
    return _adopt_baseline(log)


def _trace_adopt(vid):
    """采纳溯源: 记录触发进程/根目录/调用栈, 便于核对谁在何时建立布局。"""
    import traceback
    try:
        with open(os.path.join(VERSIONS_DIR, "adopt_trace.log"), "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S")
                    + f" pid={os.getpid()} vid={vid} root={ROOT}\n"
                    + "".join(traceback.format_stack(limit=8)) + "\n")
    except OSError:
        pass


def _adopt_baseline(log):
    """首次采纳: 把根目录当前正式产物硬链为基线版本, 建立 live 入口。"""
    vid = _unique_vid("base")
    d = os.path.join(VERSIONS_DIR, vid)
    os.makedirs(d)
    _trace_adopt(vid)
    n = 0
    for sub in DATA_SUBS:
        src = os.path.join(ROOT, "data", sub)
        dst = os.path.join(d, "data", sub)
        os.makedirs(dst, exist_ok=True)
        if os.path.isdir(src):
            for dp, dns, fs in os.walk(src):
                dns[:] = [x for x in dns if not x.endswith(".__adopt__")]
                for x in dns:
                    os.makedirs(os.path.join(dst, os.path.relpath(
                        os.path.join(dp, x), src)), exist_ok=True)
                for f in fs:
                    if not f.endswith(".json"):
                        continue
                    s = os.path.join(dp, f)
                    t = os.path.join(dst, os.path.relpath(s, src))
                    os.makedirs(os.path.dirname(t), exist_ok=True)
                    _hardlink_or_copy(s, t)
                    n += 1
    for name in sorted(os.listdir(ROOT)):
        if is_managed_root(name) and os.path.isfile(os.path.join(ROOT, name)) \
                and not os.path.islink(os.path.join(ROOT, name)):
            _hardlink_or_copy(os.path.join(ROOT, name), os.path.join(d, name))
            n += 1
    files = hash_tree(d)
    _write_manifest(vid, id=vid, subject=None, status="ready", created=_now(),
                    prev=None, baseline=True, files=files)
    _flip(vid, log)
    _write_manifest(vid, id=vid, subject=None, status="active", created=_now(),
                    prev=None, baseline=True, files=files)
    log(f"  [live] 采纳基线版本 {vid}({n} 个硬链文件 + {len(files)} 项清单)")
    return vid


def _unique_vid(prefix):
    vid = f"{prefix}_" + time.strftime("%Y%m%dT%H%M%S")
    i = 2
    while os.path.exists(os.path.join(VERSIONS_DIR, vid)) \
            or os.path.exists(_manifest_path(vid)):
        vid = f"{prefix}_" + time.strftime("%Y%m%dT%H%M%S") + f"_{i}"
        i += 1
    return vid


_VID_SEQ = [0]
_VID_LOCK = threading.Lock()


def reserve_vid(prefix="v"):
    """预留一个唯一版本 ID, 供发布在**翻转前**把"预备提交意图"与具体版本
    绑定(P0-1): 意图记录 intended_version=vid, 只有当该 vid 真正成为活动
    版本时刷新动作才允许晋升。唯一性 = 进程内单调序号 + 磁盘对账(其他
    进程/崩溃残留的同秒版本目录也算撞名)。"""
    with _VID_LOCK:
        while True:
            _VID_SEQ[0] += 1
            vid = f"{prefix}_{time.strftime('%Y%m%dT%H%M%S')}_{_VID_SEQ[0]}"
            if not os.path.exists(os.path.join(VERSIONS_DIR, vid)) \
                    and not os.path.exists(_manifest_path(vid)):
                return vid


def _link_live(rel, link_path):
    """把 link_path(不存在)建为指向 live/<rel> 的稳定符号链接。"""
    tmp = link_path + ".lnk.tmp"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(_live_link_target(rel), tmp)
    os.replace(tmp, link_path)


def _ensure_root_links(log):
    """根目录受管路径 → live 稳定符号链接(幂等; 内容不变, 原子替换)。"""
    vid = live_target()
    if vid is None:
        return
    act = os.path.join(VERSIONS_DIR, vid)
    made = 0
    # 三棵数据树: 真实目录 → aside → 符号链接 → 删 aside(仅首次, 窗口为单次重命名)
    for sub in DATA_SUBS:
        d = os.path.join(ROOT, "data", sub)
        if os.path.islink(d):
            continue
        tgt_dir = os.path.join(act, "data", sub)
        if os.path.isdir(d):
            aside = d + ".__adopt__"
            if os.path.exists(aside):
                shutil.rmtree(aside, ignore_errors=True)
            os.rename(d, aside)
            os.symlink(_live_link_target(f"data/{sub}"), d)
            shutil.rmtree(aside, ignore_errors=True)
            made += 1
        elif os.path.isdir(tgt_dir) and not os.path.lexists(d):
            _link_live(f"data/{sub}", d)
            made += 1
    # 根产物文件: 真实文件 → 同步入活动版本后原子换成符号链接(同内容, 零窗口);
    # 版本内存在而根缺失(新产物/中断) → 补悬空符号链接(翻转前语义=文件不存在)
    for name in sorted(os.listdir(act)):
        if not is_managed_root(name):
            continue
        p = os.path.join(ROOT, name)
        if os.path.islink(p):
            continue
        if os.path.isfile(p):                      # 布局后遗留的真实文件: 同步入版本
            dst = os.path.join(act, name)
            if not os.path.exists(dst) or sha256_file(dst) != sha256_file(p):
                tmp = dst + ".sync.tmp"
                shutil.copy2(p, tmp)
                os.replace(tmp, dst)
        if not os.path.lexists(p) or os.path.isfile(p):
            _link_live(name, p)
            made += 1
    if made:
        log(f"  [live] 根目录 {made} 个受管路径已接入 live 入口")


# ---------------- 版本保留协议(第七轮 P0: 回收不得删除对外服务中的版本) ----------------
# 回收受保护的版本集合 = live(活动) ∪ SERVED/BOOT 等被 mark_retained 的版本 ∪
# 刚退役(退役时间 < GC_GRACE 秒, 覆盖在途请求)的版本 ∪ 同进程在用集合。
# 服务进程在安装对外版本/捕获启动版本时写保留标记(磁盘文件, 跨进程可见 ——
# CLI 发布进程的 GC 同样读得到); 只有当对外版本切换到新版后, 旧版才被标记
# 退役, 退役满宽限期(默认 600s, 远大于任何请求生命周期)后方可回收。
GC_GRACE = int(os.environ.get("ZJ_VERSION_GC_GRACE", "600"))
_IN_USE = set()          # 同进程内显式声明"正在使用"的版本(请求级可选)


def mark_retained(vid):
    """声明版本正被对外服务(SERVED/BOOT) —— 任何进程的 GC 都不得删除;
    同时清除退役标记(例如服务重启又回到该版本)。"""
    if not vid:
        return
    p = os.path.join(VERSIONS_DIR, vid + ".retired")
    if os.path.exists(p):
        try:
            os.remove(p)
        except OSError:
            pass
    rp = os.path.join(VERSIONS_DIR, vid + ".retain")
    if not os.path.exists(rp):
        try:
            open(rp, "w").close()
        except OSError:
            pass


def mark_retired(vid):
    """版本不再被对外服务(对外已切换到新版本): 打退役时间戳, 满宽限期后可回收。"""
    if not vid:
        return
    rp = os.path.join(VERSIONS_DIR, vid + ".retain")
    if os.path.exists(rp):
        try:
            os.remove(rp)
        except OSError:
            pass
    tp = os.path.join(VERSIONS_DIR, vid + ".retired")
    if not os.path.exists(tp):
        try:
            open(tp, "w").close()
        except OSError:
            pass


def is_version_protected(vid, now=None):
    """该版本当前是否受回收保护(retain 标记 / 退役未满宽限 / 在用集合)。"""
    if not vid:
        return False
    vd = os.path.join(VERSIONS_DIR, vid)
    if not os.path.isdir(vd):
        return False
    if os.path.exists(os.path.join(VERSIONS_DIR, vid + ".retain")):
        return True
    if vid in _IN_USE:
        return True
    tp = os.path.join(VERSIONS_DIR, vid + ".retired")
    if os.path.exists(tp):
        try:
            age = (now or time.time()) - os.path.getmtime(tp)
            return age < GC_GRACE
        except OSError:
            return True
    return False


def _gc_versions(log, keep=2):
    """回收历史版本(硬链共享数据, 删除零拷贝)。受保护集合(见版本保留协议)
    永不删除; 其余按 created 时间只留最近 keep 个。"""
    vid = live_target()
    if vid is None or not os.path.isdir(VERSIONS_DIR):
        return
    hist = []
    for f in sorted(os.listdir(VERSIONS_DIR)):
        if f.startswith(".") or not os.path.isdir(os.path.join(VERSIONS_DIR, f)):
            continue
        if f == vid:
            continue
        if is_version_protected(f):
            continue                      # SERVED/BOOT/退役宽限期内: 不回收
        man = read_manifest(f) or {}
        hist.append((man.get("created") or "", f))
    hist.sort()
    for _, old in hist[:-keep] if keep else []:
        shutil.rmtree(os.path.join(VERSIONS_DIR, old), ignore_errors=True)
        mp = _manifest_path(old)
        if os.path.exists(mp):
            os.remove(mp)
        for ext in (".retain", ".retired"):
            xp = os.path.join(VERSIONS_DIR, old + ext)
            if os.path.exists(xp):
                try:
                    os.remove(xp)
                except OSError:
                    pass
        log(f"  [live] 回收历史版本 {old}")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _hardlink_or_copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


# ============================ 候选版本与翻转 ============================
def new_candidate(subject):
    """新建候选版本目录(空; 由 publish 克隆活动版本并写入新产物)。"""
    cand = os.path.join(VERSIONS_DIR, ".cand_"
                        + time.strftime("%Y%m%dT%H%M%S") + "_" + str(subject))
    shutil.rmtree(cand, ignore_errors=True)
    os.makedirs(cand)
    return cand


def discard_candidate(cand, log=print):
    if cand and os.path.isdir(cand):
        shutil.rmtree(cand, ignore_errors=True)
        log(f"  [live] 丢弃候选版本 {os.path.basename(cand)}")


def promote(cand, subject, log=print, vid=None):
    """把已构建并校验的候选版本切换为活动版本 —— 全库唯一可见性变更点。
    步骤: 重命名入列 → 清单(ready) → 根目录补符号链接(新产物悬空, 翻转前
    无害) → **单次 os.replace 翻转 live** → 清单复核(active) → 回收。
    vid: 发布方在翻转前 reserve_vid() 预留的版本 ID(与预备提交意图绑定);
    缺省内部生成。
    返回 {vid, prev, files}。"""
    checkpoint("switch:staging")
    vid = vid or _unique_vid("v")
    verdir = os.path.join(VERSIONS_DIR, vid)
    if os.path.exists(verdir):
        raise RuntimeError(f"版本 ID 已存在: {vid}")
    os.rename(cand, verdir)
    prev = live_target()
    files = hash_tree(verdir)
    _write_manifest(vid, id=vid, subject=subject, status="ready",
                    created=_now(), prev=prev, files=files)
    checkpoint("switch:staged")
    # 根目录补新产物符号链接: 指向 live(翻转前悬空=该文件尚不存在, 语义一致)
    for name in sorted(files):
        if "/" in name:
            continue                        # 数据树已由整树符号链接覆盖
        p = os.path.join(ROOT, name)
        if not os.path.lexists(p):
            _link_live(name, p)
    checkpoint("switch:pre-flip")
    _flip(vid, log)
    checkpoint("switch:flipped")
    # 翻转后复核: live 实际内容必须与清单一致(此时读者已可见新版)
    try:
        got = hash_tree(verdir, set(files))
        if got != files:
            raise SwitchError(f"翻转后清单复核不一致: "
                              f"{len(got)}!={len(files)}")
    except Exception as e:
        log(f"  [live] ⚠ 翻转后复核失败({e}), 翻回旧版 {prev}")
        if prev:
            _flip(prev, log)
        raise SwitchError(str(e))
    _write_manifest(vid, id=vid, subject=subject, status="active",
                    created=_now(), prev=prev, files=files)
    if prev:
        man = read_manifest(prev) or {}
        _write_manifest(prev, **{**man, "status": "retired",
                                 "retired_at": _now()})
    checkpoint("switch:post-verify")
    _ensure_root_links(log)
    _gc_versions(log)
    checkpoint("switch:done")
    return {"vid": vid, "prev": prev, "files": len(files)}


def _flip(vid, log=print):
    """唯一可见性变更: 一次原子重命名翻转 live 符号链接。"""
    pipe = os.path.dirname(LIVE)
    tmp = os.path.join(pipe, f"live.tmp{os.getpid()}")
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(os.path.join("versions", vid), tmp)
    os.replace(tmp, LIVE)
    log(f"  [live] 活动版本 → {vid}(单次原子翻转)")
