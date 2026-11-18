#!/usr/bin/env python3
"""自动知识点分类（不派子代理）：给 tags_<科目>/ 缺失的卷补写 tags JSON。

用法:
  python3 classify_auto.py <科目> --eval          # hold-out 自评（不写任何文件）
  python3 classify_auto.py <科目> [--dry] [--limit N]   # 分类未分类卷（--dry 只出报告不写）

方法（对每个题单元打 1-3 个小节码，主码在前）:
  1. 近邻投票: 与已分类锚点单元(全文)做 IDF-cosine 相似, top-K 加权投票其标签
  2. 关键词:   topics_<科目>.json 各小节 kw 短语的 IDF 加权命中
  3. 结构先验: P(小节|卷号) 从锚点统计(拉普拉斯平滑), 软约束
  4. OUT 判定: 历史标 ["OUT"] 的单元作负锚点; 正信号弱且负相似高 → OUT

分词三件套 STOP/stem/tokenize 逐字拷贝自 gen_search.py(L30-56), 改一处须同步两处。
"""
import json, math, os, random, re, sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))

# ---- 逐字拷贝自 gen_search.py, 勿改 ----
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
# ---- 拷贝结束 ----

def tokens_of(text):
    """tokenize + 相邻二元组(带 USE_BIGRAMS 开关)。"""
    uni = tokenize(text)
    if not USE_BIGRAMS or len(uni) < 2:
        return uni
    return uni + [uni[i] + "_" + uni[i + 1] for i in range(len(uni) - 1)]

# 可调权重/阈值
W_NN, W_KW, W_PR = 1.0, 1.6, 0.45      # 近邻 / 关键词 / 先验(log) 三路权重
KW_MODE = "all"                         # all=kw+标题 | kwonly=仅kw | phrase=仅多词短语
USE_BIGRAMS = True                      # token 空间加入相邻二元组(下划线连接)
SIB_W = 0.6                             # 同题小组(1a/1b/1c)的组内平滑权重
MIN_SIB_W = 0.15                        # 弱信号兄弟单元的最低投票权
PRIOR_BACKOFF = 12.0                    # 变体级先验回退到卷号级的强度(大=几乎不用变体级)
W_POS = 0.25                            # 题号位置先验权重(试卷题目大体按大纲顺序)
DUP_BOOST = 2.5                         # 近重复题(余弦>DUP_COS)对锚点主码的增强投票
DUP_COS = 0.5                           # 判定近重复的余弦阈值
TOPK = 10                                # 近邻投票取前 K 个锚点
SEC_WEIGHT = 0.45                        # 锚点次标签的投票折减
SEC_RATIO = 0.55                         # 次码入选需达主码分数的比例
SIM_REF, KW_REF = 0.35, 6.0             # 原始余弦/kw 分的满信号参照值
OUT_WEAK = 0.9                           # 正信号强度低于此才可能判 OUT
NEG_COS = 0.22                           # 负锚点余弦超过此值才参与 OUT 判定
NEG_STRONG = 0.38                        # 负锚点余弦强阈值(可越过 OUT_WEAK)
NEG_DOMINATE = 1.15                      # 且需高于正最高相似的比例

# 调参定稿(hold-out 网格搜索): 位置先验仅物理有效(数学卷题目不按大纲顺序, 加了反降 6pp)
PARAMS = {
    "9709": dict(KW_MODE="kwonly", USE_BIGRAMS=True, W_KW=0.5, W_PR=0.3, W_POS=0.0,
                 SIB_W=0.6, PRIOR_BACKOFF=1000.0, TOPK=12, SEC_WEIGHT=0.2,
                 DUP_BOOST=6.0, DUP_COS=0.35),
    "9702": dict(KW_MODE="kwonly", USE_BIGRAMS=True, W_KW=0.5, W_PR=0.2, W_POS=0.15,
                 SIB_W=0.6, PRIOR_BACKOFF=12.0, TOPK=12, SEC_WEIGHT=0.2,
                 DUP_BOOST=6.0, DUP_COS=0.35),
}


def apply_params(subject):
    for k, v in PARAMS.get(subject, {}).items():
        globals()[k] = v


class Corpus:
    """锚点语料: 正锚点(带标签) + 负锚点(OUT), IDF 统计与倒排索引。"""
    def __init__(self, subject):
        self.subject = subject
        self.units_dir = f"/tmp/qp{subject}/units"
        self.tags_dir = os.path.join(ROOT, f"tags_{subject}")
        tax = json.load(open(os.path.join(ROOT, f"topics_{subject}.json"), encoding="utf-8"))
        self.subs = []                     # 小节码列表
        self.kw = {}                       # code -> [词组(token 列表)]
        for sec in tax["sections"]:
            self.subs.append(sec["code"])
            for sub in sec.get("subs", []):
                self.subs.append(sub["code"])
                self.kw[sub["code"]] = [tokenize(k) for k in sub.get("kw", [])]
            self.kw.setdefault(sec["code"], [tokenize(sec.get("en", ""))])
        self.papers = {}                   # pcode -> {"paper":N,"units":[(id,text,tags)]}
        self._load()
        # 小节英文标题 token 并入 kw(标题本身即强提示), 去重
        for sec in tax["sections"]:
            for sub in sec.get("subs", []):
                kws = self.kw[sub["code"]]
                if KW_MODE == "phrase":
                    kws = [p for p in kws if len(p) >= 2]
                elif KW_MODE == "kwonly":
                    pass
                else:
                    title = tuple(tokenize(sub.get("en", "")))
                    seen = {tuple(p) for p in kws}
                    if title and title not in seen:
                        kws = kws + [list(title)]
                self.kw[sub["code"]] = kws

    def _load(self):
        for f in sorted(os.listdir(self.units_dir)):
            if not f.endswith("_qp.json"):
                continue
            pcode = f[:-8]
            tf = os.path.join(self.tags_dir, pcode + ".json")
            tags = json.load(open(tf))["tags"] if os.path.exists(tf) else None
            d = json.load(open(os.path.join(self.units_dir, f), encoding="utf-8"))
            self.papers[pcode] = {
                "paper": d["paper"], "variant": d.get("variant"), "year": d["year"],
                "units": [(u["id"], u["text"], tags.get(u["id"]) if tags else None)
                          for u in d["units"]],
            }

    def anchor_lists(self, exclude=None):
        """返回 (正锚点, 负锚点, 锚点单元id): [(tokens, topics, paper, variant)] / [Counter] / [uid]"""
        pos, neg, uids = [], [], []
        for pcode, p in self.papers.items():
            if tags_missing(p):               # 未分类卷不做锚点
                continue
            if exclude and pcode in exclude:
                continue
            for uid, text, t in p["units"]:
                if t is None:
                    continue
                toks = Counter(tokens_of(text))
                if not toks:
                    continue
                if t and t[0] == "OUT":
                    neg.append(toks)
                else:
                    pos.append((toks, t, p["paper"], p["variant"]))
                    uids.append(uid)
        return pos, neg, uids


def tags_missing(p):
    """paper dict 里是否所有单元都没标签(即未分类卷)。"""
    return all(t is None for _, _, t in p["units"]) and p["units"]


class Classifier:
    def __init__(self, corp, exclude=None):
        self.corp = corp
        self.pos, self.neg, self.pos_uids = corp.anchor_lists(exclude)
        # IDF 与倒排索引
        self.df = Counter()
        for toks, _, _, _ in self.pos:
            for t in toks:
                self.df[t] += 1
        N = max(len(self.pos), 1)
        self.idf = defaultdict(lambda: math.log(N * 3) )  # 未见词给高 idf 但受 cosine 抑制
        for t, df in self.df.items():
            self.idf[t] = math.log(1 + N / df)
        self.post = defaultdict(list)                  # token -> [(idx, tf)]
        self.norm = []
        for i, (toks, _, _, _) in enumerate(self.pos):
            n2 = 0.0
            for t, tf in toks.items():
                w = self.idf[t] * tf
                n2 += w * w
                self.post[t].append((i, tf))
            self.norm.append(math.sqrt(n2) or 1.0)
        self.negpost = defaultdict(list)
        self.negnorm = []
        for i, toks in enumerate(self.neg):
            n2 = 0.0
            for t, tf in toks.items():
                n2 += (self.idf[t] * tf) ** 2
                self.negpost[t].append((i, tf))
            self.negnorm.append(math.sqrt(n2) or 1.0)
        # 结构先验 P(sub|paper) 与 P(sub|paper,variant) 分层混合
        # (9702 的 P3 变体1=A2选择题、变体3+=实操卷, 卷号相同但考点分布完全不同)
        cnt_pv, cnt_p = defaultdict(Counter), defaultdict(Counter)
        for _, topics, paper, variant in self.pos:
            for c in topics:
                cnt_p[paper][c] += 1
                cnt_pv[(paper, variant)][c] += 1
        V = len(corp.subs)

        def smooth(m):
            return {c: (cc + 0.5) / (sum(m.values()) + 0.5 * V) for c, cc in m.items()}

        self.prior_paper = {p: smooth(m) for p, m in cnt_p.items()}
        self.prior = {}
        for (paper, variant), m in cnt_pv.items():
            sp, pp = smooth(m), self.prior_paper[paper]
            n = sum(m.values())
            self.prior[(paper, variant)] = {
                c: (n * sp.get(c, 0.0) + PRIOR_BACKOFF * pp.get(c, 0.0)) / (n + PRIOR_BACKOFF)
                for c in set(sp) | set(pp)}
        # 题号位置先验 P(sub|paper, 位置分桶): 试卷题目大体按大纲顺序排列
        self.pos_prior = {}
        if W_POS > 0:
            cnt_q = defaultdict(Counter)
            paper_maxq = defaultdict(int)
            for (toks, topics, paper, variant), uid in zip(self.pos, self.pos_uids):
                m = re.match(r"\d+", uid)
                if m:
                    paper_maxq[paper] = max(paper_maxq[paper], int(m.group()))
            for (toks, topics, paper, variant), uid in zip(self.pos, self.pos_uids):
                m = re.match(r"\d+", uid)
                if not m or paper_maxq[paper] < 3:
                    continue
                frac = min(0.999, (int(m.group()) - 1) / paper_maxq[paper])
                b = int(frac * 8)                     # 8 个位置桶
                for c in topics:
                    cnt_q[(paper, b)][c] += 1
            self.pos_prior = {k: smooth(m) for k, m in cnt_q.items()}
        # kw 短语 idf: 词组 idf = min(词 idf); 出现 df 次数另行统计(词组粒度太细, 用词 idf 代替)
        self.kw_cache = {c: [tuple(p) for p in ps] for c, ps in corp.kw.items()}

    def cos_top(self, toks):
        """对正锚点的 top-K 相似: [(sim, topics)] 按相似降序。"""
        acc = defaultdict(float)
        mynorm2 = 0.0
        for t, tf in toks.items():
            w = self.idf[t] * tf
            mynorm2 += w * w
            for i, atf in self.post.get(t, ()):
                acc[i] += w * self.idf[t] * atf
        mynorm = math.sqrt(mynorm2) or 1.0
        sims = sorted(((s / (mynorm * self.norm[i]), self.pos[i][1])
                       for i, s in acc.items()), key=lambda x: -x[0])
        return sims[:TOPK], mynorm

    def neg_cos(self, toks):
        acc = defaultdict(float)
        mynorm2 = 0.0
        for t, tf in toks.items():
            w = self.idf[t] * tf
            mynorm2 += w * w
            for i, atf in self.negpost.get(t, ()):
                acc[i] += w * self.idf[t] * atf
        mynorm = math.sqrt(mynorm2) or 1.0
        return max((s / (mynorm * self.negnorm[i]) for i, s in acc.items()), default=0.0)

    def _prior_for(self, paper, variant):
        if variant is not None and (paper, variant) in self.prior:
            return self.prior[(paper, variant)]
        return self.prior_paper.get(paper, {})

    def _score(self, text, paper, variant=None, uid=None, paper_maxq=None):
        """单单元打分: (score 字典或 None, best_pos, kwm, neg, strength)。"""
        toks = Counter(tokens_of(text))
        if not toks:
            return None, 0.0, 0.0, 0.0, 0.0
        tset = set(toks)
        # 1) 近邻投票
        sims, _ = self.cos_top(toks)
        nn = defaultdict(float)
        best_pos = sims[0][0] if sims else 0.0
        for sim, topics in sims:
            if sim <= 0:
                continue
            for j, c in enumerate(topics):
                nn[c] += sim * (1.0 if j == 0 else SEC_WEIGHT)
            if sim > DUP_COS and DUP_BOOST > 0:
                nn[topics[0]] += (sim - DUP_COS) * DUP_BOOST
        # 2) 关键词(短语按 unigram 集合命中)
        uni = {t for t in tset if "_" not in t}
        kw = defaultdict(float)
        for c, phrases in self.kw_cache.items():
            for ph in phrases:
                if len(ph) == 1:
                    if ph[0] in uni:
                        kw[c] += self.idf.get(ph[0], 5.0) * 0.9
                elif len(ph) >= 2 and all(t in uni for t in ph):
                    kw[c] += min(self.idf.get(t, 5.0) for t in ph) * (0.8 + 0.25 * len(ph))
        # 3) 综合: 各路按单元内最大值归一后与先验(对数)线性融合
        nnm = max(nn.values(), default=0.0)
        kwm = max(kw.values(), default=0.0)
        pr = self._prior_for(paper, variant)
        pospr = {}
        if uid and W_POS > 0 and paper_maxq and paper_maxq >= 3:
            m = re.match(r"\d+", uid)
            if m:
                frac = min(0.999, (int(m.group()) - 1) / paper_maxq)
                pospr = self.pos_prior.get((paper, int(frac * 8)), {})
        score = {c: W_NN * (nn[c] / nnm if nnm else 0.0)
                 + W_KW * (kw[c] / kwm if kwm else 0.0)
                 + W_PR * math.log(1 + 400 * pr.get(c, 0.0))
                 + W_POS * math.log(1 + 400 * pospr.get(c, 0.0))
                 for c in set(nn) | set(kw) | set(pospr)}
        score = {c: s for c, s in score.items() if s > 0}
        strength = (W_NN * min(1.0, best_pos / SIM_REF) if best_pos else 0.0) \
            + (W_KW * min(1.0, kwm / KW_REF) if kwm else 0.0)
        return score, best_pos, kwm, self.neg_cos(toks), strength

    def _decide(self, score, best_pos, kwm, neg):
        ranked = sorted(score.items(), key=lambda x: -x[1])
        top_code, top_s = ranked[0]
        out = [top_code]
        for c, s in ranked[1:3]:
            if s >= top_s * SEC_RATIO:
                out.append(c)
        # OUT / 低置信: 正信号强度(不含先验)弱 且 负锚点相似占优 → OUT
        sim_score = min(1.0, best_pos / SIM_REF) if best_pos else 0.0
        kw_score = min(1.0, kwm / KW_REF) if kwm else 0.0
        pos_strength = W_NN * sim_score + W_KW * kw_score
        out_cond = (pos_strength < OUT_WEAK and neg > NEG_COS
                    and neg > best_pos * NEG_DOMINATE) or \
                   (neg > NEG_STRONG and neg > best_pos * NEG_DOMINATE * 1.5)
        if out_cond:
            return ["OUT"], neg, "out", neg
        conf = min(1.0, (sim_score + kw_score) / 2.0)
        status = "ok" if pos_strength >= OUT_WEAK else "low"
        return out, conf, status, neg

    def classify(self, text, paper, variant=None):
        score, best_pos, kwm, neg, _ = self._score(text, paper, variant)
        if score is None:
            c = self._prior_argmax(paper)
            return ([c] if c else ["OUT"]), 0.0, "empty", None
        if not score:
            c = self._prior_argmax(paper)
            return ([c] if c else ["OUT"]), 0.0, "nosignal", None
        return self._decide(score, best_pos, kwm, neg)

    def classify_paper(self, units, paper, variant=None):
        """units: [(id, text)] → {id: (codes, conf, status, neg)}。
        同题小组("1a"/"1b" 属题 1)组内平滑: 兄弟票按其信号强度加权
        (实操卷过程性小问文本近乎零信号, 应让题干描述单元主导)。"""
        raw = {}
        qs = [int(m.group()) for m in (re.match(r"\d+", uid) for uid, _ in units) if m]
        maxq = max(qs) if qs else 0
        for uid, text in units:
            raw[uid] = self._score(text, paper, variant, uid, maxq)
        groups = defaultdict(list)
        for uid in raw:
            m = re.match(r"\d+", uid)
            groups[m.group() if m else uid].append(uid)
        final = {}
        for uid, (score, best_pos, kwm, neg, _) in raw.items():
            gk = re.match(r"\d+", uid)
            sib = [u for u in groups[gk.group() if gk else uid] if u != uid]
            sm = dict(score) if score else {}
            if sib:
                agg = defaultdict(float)
                wsum = 0.0
                for u in sib:
                    s2, _, _, _, st2 = raw[u]
                    w = max(MIN_SIB_W, st2 / (W_NN + W_KW))
                    if s2:
                        for c, v in s2.items():
                            agg[c] += v * w
                        wsum += w
                if wsum > 0:
                    k = SIB_W / wsum
                    for c in set(sm) | set(agg):
                        sm[c] = sm.get(c, 0.0) + k * agg[c]
            if not sm:
                c = self._prior_argmax(paper)
                final[uid] = (([c] if c else ["OUT"]), 0.0, "empty", None)
                continue
            final[uid] = self._decide(sm, best_pos, kwm, neg)
        return final

    def _prior_argmax(self, paper):
        pr = self.prior.get(paper)
        if not pr:
            return None
        return max(pr.items(), key=lambda x: x[1])[0]


def evaluate(corp, seed=42, quiet=False, min_year=None):
    rnd = random.Random(seed)
    by_paper = defaultdict(list)
    for pcode, p in corp.papers.items():
        if tags_missing(p):
            continue
        if min_year and p["year"] < min_year:
            continue
        by_paper[p["paper"]].append(pcode)
    hold = []
    for paper, codes in sorted(by_paper.items()):
        codes = sorted(codes)                          # 年代序
        k = min(5, max(2, len(codes) // 2))
        if len(codes) >= 2:
            k = min(k, len(codes) - 1)                 # 至少留 1 卷做锚点
        k = max(k, 1)
        hold += rnd.sample(codes, k) if len(codes) <= 8 else \
            [codes[int(i * (len(codes) - 1) / (k - 1))] for i in range(k)]
    clf = Classifier(corp, exclude=set(hold))
    stats = defaultdict(lambda: [0, 0, 0, 0, 0, 0])    # n, primary, top3, out_tp, out_fp, out_fn
    for pcode in sorted(hold):
        p = corp.papers[pcode]
        preds = clf.classify_paper([(uid, text) for uid, text, _ in p["units"]],
                                   p["paper"], p["variant"])
        for uid, text, truth in p["units"]:
            if truth is None:
                continue
            pred, _, _, _ = preds[uid]
            s = stats[p["paper"]]
            s[0] += 1
            t_out = truth[0] == "OUT"
            p_out = pred[0] == "OUT"
            if t_out and p_out:
                s[3] += 1
            elif t_out:
                s[5] += 1
            elif p_out:
                s[4] += 1
            if pred[0] == truth[0]:
                s[1] += 1
            if not t_out and truth[0] in pred:
                s[2] += 1
    tot = [0] * 6
    if not quiet:
        print(f"{'paper':>5} {'units':>6} {'主码一致':>8} {'top含真值':>9} {'OUT准':>6} {'OUT漏':>6}")
        for paper in sorted(stats):
            s = stats[paper]
            print(f"{paper:>5} {s[0]:>6} {s[1]/s[0]:>8.1%} {s[2]/max(1,s[0]-s[3]-s[5]):>9.1%} "
                  f"{s[3]:>4}/{s[3]+s[4]:<4} 漏{s[5]}")
            for i in range(6):
                tot[i] += s[i]
    else:
        for s in stats.values():
            for i in range(6):
                tot[i] += s[i]
    n = tot[0]
    if not quiet:
        print(f"{'ALL':>5} {n:>6} {tot[1]/n:>8.1%} {tot[2]/max(1,n-tot[3]-tot[5]):>9.1%} "
              f"{tot[3]:>4}/{tot[3]+tot[4]:<4} 漏{tot[5]}")
    return {"n": n, "primary": tot[1] / n, "top3": tot[2] / max(1, n - tot[3] - tot[5]),
            "out_tp": tot[3], "out_fp": tot[4], "out_fn": tot[5], "hold": len(hold)}


def tags_missing_paper(corp, pcode):
    p = corp.papers[pcode]
    return bool(p["units"]) and tags_missing(p)


def run(subject, dry=False, limit=None, since=None):
    corp = Corpus(subject)
    clf = Classifier(corp)
    todo = [pc for pc in sorted(corp.papers)
            if tags_missing_paper(corp, pc)
            and (since is None or corp.papers[pc]["year"] >= since)]
    scanned = [pc for pc in sorted(corp.papers) if not corp.papers[pc]["units"]]
    if limit:
        todo = todo[:limit]
    rep = {"subject": subject, "papers": {}, "skipped_scanned": scanned}
    n_out = n_low = n_units = 0
    for pcode in todo:
        p = corp.papers[pcode]
        preds = clf.classify_paper([(uid, text) for uid, text, _ in p["units"]],
                                   p["paper"], p["variant"])
        tags, det = {}, []
        for uid, text, _ in p["units"]:
            pred, conf, status, neg = preds[uid]
            tags[uid] = pred
            n_units += 1
            if pred[0] == "OUT":
                n_out += 1
            if status == "low":
                n_low += 1
            det.append({"id": uid, "tags": pred, "conf": round(conf, 3),
                        "status": status, "neg": round(neg or 0, 3),
                        "snippet": " ".join(text.split())[:120]})
        rep["papers"][pcode] = {"paper": p["paper"], "year": p["year"],
                                "units": len(tags),
                                "out": sum(1 for d in det if d["tags"][0] == "OUT"),
                                "low": sum(1 for d in det if d["status"] == "low"),
                                "units_detail": det}
        if not dry:
            out = os.path.join(ROOT, f"tags_{subject}", pcode + ".json")
            assert not os.path.exists(out), f"refuse overwrite {out}"
            with open(out, "w", encoding="utf-8") as fh:
                json.dump({"code": pcode, "tags": tags}, fh, ensure_ascii=False)
    # markdown 报告
    md = [f"# 自动分类报告 {subject}（classify_auto.py）", "",
          f"- 分类卷数: {len(todo)}（单元 {n_units}，OUT {n_out}，低置信 {n_low}）",
          f"- 跳过的扫描件卷(0 单元): {', '.join(scanned) or '无'}", ""]
    lows = [(pc, d) for pc, pr in rep["papers"].items() for d in pr["units_detail"]
            if d["status"] == "low"]
    md.append(f"## 低置信单元 {len(lows)} 个（已给最优猜测，建议人工抽查）")
    for pc, d in lows[:200]:
        md.append(f"- `{pc}#{d['id']}` → {d['tags']} conf={d['conf']} | {d['snippet']}")
    outs = [(pc, d) for pc, pr in rep["papers"].items() for d in pr["units_detail"]
            if d["tags"][0] == "OUT"]
    md.append(f"\n## 判定 OUT 的单元 {len(outs)} 个")
    for pc, d in outs[:200]:
        md.append(f"- `{pc}#{d['id']}` neg={d['neg']} | {d['snippet']}")
    open(os.path.join(ROOT, f"auto_classify_report_{subject}.md"), "w",
         encoding="utf-8").write("\n".join(md))
    if not dry:
        json.dump(rep, open(os.path.join(ROOT, f"auto_classify_report_{subject}.json"), "w",
                            encoding="utf-8"), ensure_ascii=False)
    print(f"{subject}: papers={len(todo)} units={n_units} OUT={n_out} low={n_low} "
          f"dry={dry} -> auto_classify_report_{subject}.md")


if __name__ == "__main__":
    subject = sys.argv[1]
    apply_params(subject)
    since = int(sys.argv[sys.argv.index("--since") + 1]) if "--since" in sys.argv else None
    if "--eval" in sys.argv:
        evaluate(Corpus(subject), min_year=since)
    else:
        run(subject, dry="--dry" in sys.argv, since=since,
            limit=int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None)
