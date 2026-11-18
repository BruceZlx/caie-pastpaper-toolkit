#!/usr/bin/env python3
"""migrate_syllabus.py —— 按执行时最新官方大纲迁移/判定/发布 (IN/OUT/PENDING)。

被 paper_pipeline.py 调用(migrate/publish/sync), 也可独立运行:
  /usr/bin/python3 migrate_syllabus.py <科目代码> [--apply]

职责(对应 PAST_PAPER_PIPELINE_PLAN.md「统一大纲口径」):
  1. build_migration_assets: 从新旧大纲 PDF 与旧 topics_<code>.json 生成
     - 新版 topics 草稿 data/pipeline/migration/<code>_topics_new.json
       (9709/9701/9618: 同码换版, 标题逐项与新 PDF 对核; 9702: 2028-2030
       改革后 25 章新编号树, kw 沿内容映射迁入)
     - 旧码→新码映射 data/pipeline/migration/<code>_map.json (带证据;
       绝不按编号相同直接沿用)
  2. adjudicate: 对 data/units 每个单元判 IN(当前合法标签)/OUT(考纲外+理由)/
     PENDING(证据不足+问题), 输出 data/pipeline/adjudication/<code>.json
     供审计与复核; 规则:
     - 旧分类标签全部有同义新码映射 → IN(迁移标签)
     - 旧标签主题被最新大纲移除 → 按题面内容重判(近邻+关键词), 强信号→IN,
       无信号→OUT(理由), 其余→PENDING
     - 旧判 OUT → 以最新大纲关键词复核, 无 IN 强信号则维持 OUT
     - 从未派发分类 → PENDING(不冒称完成)
  3. publish: 快照 → 校验 → 原子写入 topics_/data/units(status)/classified_(仅IN)
     → 重建 topics_data/search_data/papers_data + 工作台页 → 复核校验。
"""
import argparse
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
PIPE = os.path.join(ROOT, "data", "pipeline")
MIG_DIR = os.path.join(PIPE, "migration")
ADJ_DIR = os.path.join(PIPE, "adjudication")
SNAP_DIR = os.path.join(PIPE, "snapshots")
OVERRIDES_DIR = os.path.join(PIPE, "overrides")      # 人工复核裁定(带官方证据, 重跑保留)
PUBLISHED_PATH = os.path.join(PIPE, "published.json")  # 各科已成功发布的卷(待发布以此为准)
STAGING_DIR = os.path.join(PIPE, "staging")

# 发布成功后的索引刷新钩子(server.py 在进程内注册; CLI 会尝试调用本机服务)
_REFRESH_HOOKS = []


def register_refresh_hook(fn):
    if fn not in _REFRESH_HOOKS:
        _REFRESH_HOOKS.append(fn)


def _fire_refresh_hooks(code):
    """发布成功后触发索引刷新钩子(server 同进程)。返回错误列表(P0-4:
    刷新失败必须显式呈现, 不得吞掉后照报成功)。"""
    errs = []
    for fn in list(_REFRESH_HOOKS):
        try:
            fn(code)
        except Exception as e:
            errs.append(f"{getattr(fn, '__name__', repr(fn))}: {e!r}")
            print(f"  [publish] ⚠ 索引刷新钩子失败: {e!r}")
    return errs


def _apply_root(root):
    """切换根目录(测试夹具用临时仓库)。"""
    global ROOT, PIPE, MIG_DIR, ADJ_DIR, SNAP_DIR
    global OVERRIDES_DIR, PUBLISHED_PATH, STAGING_DIR
    ROOT = root
    PIPE = os.path.join(ROOT, "data", "pipeline")
    MIG_DIR = os.path.join(PIPE, "migration")
    ADJ_DIR = os.path.join(PIPE, "adjudication")
    SNAP_DIR = os.path.join(PIPE, "snapshots")
    OVERRIDES_DIR = os.path.join(PIPE, "overrides")
    PUBLISHED_PATH = os.path.join(PIPE, "published.json")
    STAGING_DIR = os.path.join(PIPE, "staging")
    import live_version
    live_version.apply_root(root)

# ---------------- 分词(与 gen_search.py 逐字一致) ----------------
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


# ======================================================================
# 9702 旧(2016-2021 结构, 27 章) → 新(2028-2030, 25 章) 内容映射表。
# 依据: 逐条阅读 9702_Physics_2028-2030_syllabus.pdf Subject content(p.15-39)
# 与旧 topics_9702.json 的学习目标; 同编号不代表同内容, 全表按标题+目标对核。
# action: map=直接映射(rematch 可按题面正则改判) / rejudge=按内容重判 /
#         removed=主题已从最新大纲移除(先重判再 OUT)
# ======================================================================
MAP_9702 = {
    # 旧 1 Physical quantities and units
    "1.1": {"to": ["1.1"]}, "1.2": {"to": ["1.2"]},
    "1.3": {"to": ["15.1"], "ev": "Avogadro 常数内容并入新 15 Ideal gases(The mole)"},
    "1.4": {"to": ["1.4"]},
    "2.1": {"action": "rejudge", "hint": ["1.3", "26.1", "26.2"],
            "ev": "测量技术从 Subject content 移入 Practical assessment; 不确定度题→1.3, 实操/仪器/读数题→26.x(官方p44-45)"},
    "2.2": {"to": ["1.3"]},
    "3.1": {"to": ["2.1"]},
    "4.1": {"to": ["3.1"]}, "4.2": {"to": ["3.2"]}, "4.3": {"to": ["3.3"]},
    "5.1": {"action": "rejudge", "hint": ["3.1", "3.2"],
            "ev": "Types of force 拆并入新 3 Dynamics(摩擦/阻力/重量)"},
    "5.2": {"to": ["4.1"]}, "5.3": {"to": ["4.2"]}, "5.4": {"to": ["4.3"]},
    "6.1": {"to": ["5.1"]}, "6.2": {"to": ["5.1"], "ev": "新 5.1 Energy conservation 含 work+efficiency"},
    "6.3": {"to": ["5.2"]}, "6.4": {"to": ["5.1"], "ev": "新 5.1 目标 5-7 为 power"},
    "7.1": {"to": ["12.1"]}, "7.2": {"to": ["12.2"]},
    "8.1": {"to": ["13.1"]}, "8.2": {"to": ["13.2"]}, "8.3": {"to": ["13.3"]},
    "8.4": {"to": ["13.4"]},
    "9.1": {"to": ["6.1"]}, "9.2": {"to": ["6.2"]},
    "10.1": {"to": ["15.2"]}, "10.2": {"to": ["15.3"]},
    "10.3": {"to": ["15.3"], "ev": "分子平均动能并入新 15.3 Kinetic theory(pV=⅓Nm<c²>推导)"},
    "11.1": {"to": ["14.1"]}, "11.2": {"to": ["14.2"]},
    "11.3": {"to": ["14.2"], "ev": "新 14.2 目标 1 即测温性质(含 thermocouple 等)"},
    "12.1": {"to": ["14.3"]},
    "12.2": {"to": ["16.1", "16.2"], "ev": "旧节合并内容拆为新 16.1+16.2"},
    "13.1": {"to": ["17.1"]}, "13.2": {"to": ["17.2"]}, "13.3": {"to": ["17.3"]},
    "14.1": {"to": ["7.1"]}, "14.2": {"to": ["7.2"]},
    "14.3": {"to": ["7.1"], "ev": "CRO 测频率/振幅在新 7.1 目标 3"},
    "14.4": {"to": ["7.3"]}, "14.5": {"to": ["7.4"]}, "14.6": {"to": ["24.1"]},
    "15.1": {"to": ["8.1"]}, "15.2": {"to": ["8.2"]}, "15.3": {"to": ["8.3"]},
    "15.4": {"to": ["8.4"]},
    "16": {"action": "removed", "ev": "Communication 章 2022 起整体移除"},
    "16.1": {"action": "removed", "ev": "Communication channels 已移除"},
    "16.2": {"action": "removed", "ev": "Modulation 已移除"},
    "16.3": {"action": "removed", "ev": "Digital communication 已移除"},
    "16.4": {"action": "removed", "ev": "Channels of communication 对比已移除"},
    "16.5": {"action": "removed", "ev": "Attenuation(通信语境)已移除"},
    "17.1": {"to": ["18.1"]}, "17.2": {"to": ["18.2"]}, "17.3": {"to": ["18.3"]},
    "17.4": {"to": ["18.4"]}, "17.5": {"to": ["18.5"]},
    "18.1": {"to": ["19.1"], "rematch": [(r"discharg|exponential decay of|time constant", ["19.3"],
                                          "题面为电容放电")],
             "ev": "新 19.3 单列 Discharging a capacitor"},
    "18.2": {"to": ["19.2"], "rematch": [(r"discharg|time constant|\bRC\b.*decay", ["19.3"],
                                          "题面为电容放电")]},
    "19.1": {"to": ["9.1"]}, "19.2": {"to": ["9.2"]}, "19.3": {"to": ["9.3"]},
    "19.4": {"to": ["9.3"], "ev": "thermistor/LDR 特性在新 9.3 I-V characteristics"},
    "20.1": {"to": ["10.1"]}, "20.2": {"to": ["10.2"]}, "20.3": {"to": ["10.3"]},
    "21": {"action": "removed", "ev": "Electronics 章 2022 起整体移除"},
    "21.1": {"action": "removed", "ev": "理想运放已移除"},
    "21.2": {"action": "removed", "ev": "运放电路已移除"},
    "21.3": {"action": "removed", "ev": "输出器件已移除"},
    "22.1": {"to": ["20.1"]}, "22.2": {"to": ["20.2"]}, "22.3": {"to": ["20.3"]},
    "22.4": {"to": ["20.4"]},
    "22.5": {"action": "removed", "ev": "NMR 成像已移除(医学物理改为超声/X 射线/PET)"},
    "23.1": {"to": ["20.5"]},
    "24.1": {"to": ["21.1"]},
    "24.2": {"action": "removed", "ev": "变压器不在 2028-2030 Subject content(检索无 transformer)"},
    "24.3": {"action": "removed", "ev": "输电已移除(检索无 transmission of electrical)"},
    "24.4": {"to": ["21.2"]},
    "25.1": {"to": ["22.1"]}, "25.2": {"to": ["22.2"]}, "25.3": {"to": ["22.3"]},
    "25.4": {"to": ["22.4"]},
    "25.5": {"action": "removed", "ev": "Band theory 已移除(检索无 band theory/energy bands)"},
    "25.6": {"to": ["24.2"]}, "25.7": {"to": ["24.3"]},
    "26.1": {"to": ["11.1"]}, "26.2": {"to": ["11.2"]}, "26.3": {"to": ["23.1"]},
    "26.4": {"to": ["23.2"]},
    "27.1": {"to": ["25.1"]}, "27.2": {"to": ["25.2"]}, "27.3": {"to": ["25.3"]},
}

# 新 9702 topics 树(标题来自 9702_Physics_2028-2030_syllabus.pdf Subject content)
NEW9702_SECTIONS = [
    ("1", "Physical quantities and units", "物理量与单位",
     [("1.1", "Physical quantities"), ("1.2", "SI units"),
      ("1.3", "Errors and uncertainties"), ("1.4", "Scalars and vectors")]),
    ("2", "Kinematics", "运动学", [("2.1", "Equations of motion")]),
    ("3", "Dynamics", "动力学",
     [("3.1", "Momentum and Newton's laws of motion"), ("3.2", "Non-uniform motion"),
      ("3.3", "Linear momentum and its conservation")]),
    ("4", "Forces, density and pressure", "力、密度与压强",
     [("4.1", "Turning effects of forces"), ("4.2", "Equilibrium of forces"),
      ("4.3", "Density and pressure")]),
    ("5", "Work, energy and power", "功、能量与功率",
     [("5.1", "Energy conservation"),
      ("5.2", "Gravitational potential energy and kinetic energy")]),
    ("6", "Deformation of solids", "固体形变",
     [("6.1", "Stress and strain"), ("6.2", "Elastic and plastic behaviour")]),
    ("7", "Waves", "波",
     [("7.1", "Progressive waves"), ("7.2", "Transverse and longitudinal waves"),
      ("7.3", "Doppler effect for sound waves"), ("7.4", "Electromagnetic spectrum"),
      ("7.5", "Polarisation")]),
    ("8", "Superposition", "叠加",
     [("8.1", "Stationary waves"), ("8.2", "Diffraction"), ("8.3", "Interference"),
      ("8.4", "The diffraction grating")]),
    ("9", "Electricity", "电学",
     [("9.1", "Electric current"), ("9.2", "Potential difference and power"),
      ("9.3", "Resistance and resistivity")]),
    ("10", "D.C. circuits", "直流电路",
     [("10.1", "Practical circuits"), ("10.2", "Kirchhoff's laws"),
      ("10.3", "Potential dividers")]),
    ("11", "Particle physics", "粒子物理",
     [("11.1", "Atoms, nuclei and radiation"), ("11.2", "Fundamental particles")]),
    ("12", "Motion in a circle", "圆周运动",
     [("12.1", "Kinematics of uniform circular motion"),
      ("12.2", "Centripetal acceleration")]),
    ("13", "Gravitational fields", "引力场",
     [("13.1", "Gravitational field"),
      ("13.2", "Gravitational force between point masses"),
      ("13.3", "Gravitational field of a point mass"),
      ("13.4", "Gravitational potential")]),
    ("14", "Temperature", "温度",
     [("14.1", "Thermal equilibrium"), ("14.2", "Temperature scales"),
      ("14.3", "Specific heat capacity and specific latent heat")]),
    ("15", "Ideal gases", "理想气体",
     [("15.1", "The mole"), ("15.2", "Equation of state"),
      ("15.3", "Kinetic theory of gases")]),
    ("16", "Thermodynamics", "热力学",
     [("16.1", "Internal energy"), ("16.2", "The first law of thermodynamics")]),
    ("17", "Oscillations", "振荡",
     [("17.1", "Simple harmonic oscillations"),
      ("17.2", "Energy in simple harmonic motion"),
      ("17.3", "Damped and forced oscillations, resonance")]),
    ("18", "Electric fields", "电场",
     [("18.1", "Electric fields and field lines"), ("18.2", "Uniform electric fields"),
      ("18.3", "Electric force between point charges"),
      ("18.4", "Electric field of a point charge"), ("18.5", "Electric potential")]),
    ("19", "Capacitance", "电容",
     [("19.1", "Capacitors and capacitance"), ("19.2", "Energy stored in a capacitor"),
      ("19.3", "Discharging a capacitor")]),
    ("20", "Magnetic fields", "磁场",
     [("20.1", "Concept of a magnetic field"),
      ("20.2", "Force on a current-carrying conductor"),
      ("20.3", "Force on a moving charge"),
      ("20.4", "Magnetic fields due to currents"),
      ("20.5", "Electromagnetic induction")]),
    ("21", "Alternating currents", "交流电",
     [("21.1", "Characteristics of alternating currents"),
      ("21.2", "Rectification and smoothing")]),
    ("22", "Quantum physics", "量子物理",
     [("22.1", "Energy and momentum of a photon"), ("22.2", "Photoelectric effect"),
      ("22.3", "Wave–particle duality"),
      ("22.4", "Energy levels in atoms and line spectra")]),
    ("23", "Nuclear physics", "核物理",
     [("23.1", "Mass defect and nuclear binding energy"),
      ("23.2", "Radioactive decay")]),
    ("24", "Medical physics", "医学物理",
     [("24.1", "Production and use of ultrasound"),
      ("24.2", "Production and use of X-rays"), ("24.3", "PET scanning")]),
    ("25", "Astronomy and cosmology", "天文与宇宙学",
     [("25.1", "Standard candles"), ("25.2", "Stellar radii"),
      ("25.3", "Hubble's law and the Big Bang theory")]),
]

# identity 科目(9709/9701/9618)的人工核准覆盖: PDF 标题因符号换行/词序
# 导致自动对核失败, 但逐条读新 PDF 确认存在且同义的小节 → 同码映射
IDENTITY_OVERRIDES = {
    "9701": {
        "24.2": ("24.2", "新 PDF p41 'Standard electrode potentials E⦵, standard cell "
                         "potentials E⦵'(⦵ 符号提取截断), 同码同义"),
        "29.4": ("29.4", "新 PDF p48 'Optical isomerism'(词序调整), 同码同义"),
    },
}


# 新 9702 无旧来源小节的补充 kw(旧 kw 经映射自动迁入其余小节)
NEW9702_EXTRA_KW = {
    "15.1": ["mole", "avogadro constant", "amount of substance",
             "number of molecules", "number of particles"],
    "19.3": ["discharge of a capacitor", "time constant", "exponential decay",
             "capacitor discharge"],
    "7.5": ["polarisation", "plane polarised", "polarising filter", "malus"],
    "20.5": ["electromagnetic induction", "faraday", "lenz", "magnetic flux",
             "flux linkage", "induced emf"],
    "24.3": ["pet scanning", "positron", "annihilation", "tracer"],
    "25.1": ["standard candle", "luminosity", "parallax", "cepheid"],
    "25.2": ["stellar radii", "black body", "wien", "stefan"],
    "25.3": ["hubble", "redshift", "big bang", "recession velocity"],
}

# ======================================================================
# Practical assessment 技能分类(官方 syllabus 的独立考查部分, 不属于
# Subject content 章节; 页码证据来自已下载的官方 PDF)。
# 依据:
#   9701 Chemistry 2028-2030 syllabus 5 Practical assessment p58-73
#     - p58  Paper 3/5 可采用 Subject content 之外的情境, 只考查实操技能
#     - p59  Paper 3 技能分值表(操作/呈现/分析评价)
#     - p60-62 成功收集数据、读数质量、读数决策、计算呈现、数据布局、
#              得出结论、误差来源与改进
#     - p63  定量分析程序(滴定/速率/重量)
#     - p64  定性分析(含安全与观察记录)
#   9702 Physics 2028-2030 syllabus 5 Practical assessment p42-55
#     - p44  操作/测量/观察(装置搭建、重复读数、常用仪器)
#     - p45-46 结果表/数据记录/图象描点与趋势线
#     - p47-48 解释图象、得出结论、局限与改进
#     - p49-52 Paper 5 规划(定义问题/数据收集/分析方法/安全)与分析评价
# ======================================================================
PRACTICAL_TAXONOMY = {
    "9701": {
        "section": ("38", "Practical skills (Paper 3 & 5)", "实验技能（Paper 3/5）",
                    [("38.1", "Manipulation, measurement and observation",
                      "操作、测量与观察",
                      ["set up apparatus", "follow instructions", "apparatus",
                       "burette", "pipette", "measuring cylinder", "thermometer",
                       "stop-clock", "stopwatch", "crucible", "weigh", "balance",
                       "collect gas", "gas syringe", "titration", "titre",
                       "concordant", "repeat readings", "wash", "rinse",
                       "transfer", "make up solution", "water bath",
                       "temperature measurements", "reaction mixture",
                       "measurements that should be taken"],
                      "p58-60"),
                     ("38.2", "Presentation of data and observations",
                      "数据与观察的呈现",
                      ["table of results", "draw a table", "record data",
                       "recording data", "column headings", "significant figures",
                       "data layout", "present data", "record all data",
                       "observations table", "appropriate units",
                       "complete table", "complete the table",
                       "records the measurements", "record values",
                       "values to the nearest"],
                      "p59,61"),
                     ("38.3", "Analysis, conclusions and evaluation",
                      "分析、结论与评价",
                      ["interpretation of data", "draw conclusions",
                       "sources of error", "source of error", "systematic error",
                       "random error", "percentage error", "uncertainty",
                       "accuracy", "improvements", "limitations of the method",
                       "anomal", "calculate the", "percentage difference",
                       "evaluate", "compare with"],
                      "p59-62"),
                     ("38.4", "Quantitative analysis procedures",
                      "定量分析程序",
                      ["rough titration", "potassium manganate",
                       "sodium thiosulfate", "iodine titration", "acid-alkali",
                       "indicator", "endpoint", "end-point", "rate of reaction",
                       "timing the reaction", "precipitate", "heat in a crucible",
                       "hydrated salt", "loss of water", "gravimetric",
                       "mix reagents"],
                      "p63"),
                     ("38.5", "Qualitative analysis", "定性分析",
                      ["qualitative analysis", "cation", "anion", "tests for gases",
                       "identify the", "add aqueous sodium hydroxide",
                       "add excess", "effervescence", "damp red litmus",
                       "observations when", "test tube", "unknown solution",
                       "precipitate dissolves", " coloured precipitate"],
                      "p64,69-71"),
                     ("38.6", "Planning, safety and evaluation (Paper 5)",
                      "规划·安全·评价（Paper 5）",
                      ["identify the steps necessary", "suitable piece of apparatus",
                       "apparatus that is suitable", "independent variable",
                       "dependent variable", "variables to be controlled",
                       "safety precaution", "precaution", "hazards",
                       "understanding of the risks", "reliable",
                       "theoretical value", "safe and efficient procedure",
                       "suggest how", "describe how the student"],
                      "p72-74")]),
        "level": {"level": "Practical", "from": "38", "to": "38", "papers": [3, 5]},
        "papers": (3, 5),
    },
    "9702": {
        "section": ("26", "Practical skills (Paper 3 & 5)", "实验技能（Paper 3/5）",
                    [("26.1", "Manipulation, measurement and observation",
                      "操作、测量与观察",
                      ["set up apparatus", "apparatus", "micrometer",
                       "vernier calipers", "calipers", "newton meter",
                       "newton-meter", "protractor", "top-pan balance",
                       "measuring cylinder", "ammeter", "voltmeter",
                       "stopwatch", "time oscillations", "period of oscillation",
                       "repeat readings", "adjust", "connect the circuit"],
                      "p44"),
                     ("26.2", "Presentation of data and observations",
                      "数据与观察的呈现",
                      ["table of results", "draw a table", "record data",
                       "column headings", "quantity and the unit",
                       "significant figures", "raw data", "present numerical data",
                       "same degree of precision", "show working"],
                      "p45,52"),
                     ("26.3", "Graphs and analysis of data", "图象与数据分析",
                      ["plot", "graph", "best fit", "trend line", "tangent",
                       "gradient", "y-intercept", "error bars",
                       "worst acceptable line", "coordinates of points",
                       "anomal", "logarithmic"],
                      "p46-47,52"),
                     ("26.4", "Conclusions, limitations and improvements",
                      "结论、局限与改进",
                      ["draw conclusions", "supports the hypothesis",
                       "percentage difference", "percentage uncertainty",
                       "limitations in an experimental procedure",
                       "sources of uncertainty", "suggest modifications",
                       "improve the accuracy", "extend the investigation",
                       "systematic error", "random errors"],
                      "p47-48"),
                     ("26.5", "Planning (Paper 5)", "实验规划（Paper 5）",
                      ["defining the problem", "independent variable",
                       "dependent variable", "kept constant", "variables",
                       "method of data collection", "describe the method",
                       "method of analysis", "safety precautions",
                       "assess the risks", "plan an experiment",
                       "design an experiment", "additional detail"],
                      "p49-51")]),
        "level": {"level": "Practical", "from": "26", "to": "26", "papers": [3, 5]},
        "papers": (3, 5),
    },
}

# 无分值共享材料 → CONTEXT 的判定范围(仅对这两科的旧 OUT 单元;
# 9708 由 overrides 逐题给出证据)
CONTEXT_REVIEW_SUBJECTS = ("9701", "9702")


def norm_title(s):
    s = unicodedata.normalize("NFKC", s).replace("’", "'").replace("–", "-")
    s = re.sub(r"\((?:for paper \d+)\)", "", s, flags=re.I)
    s = re.sub(r"[^A-Za-z0-9&,;:()'/+-]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- 人工复核裁定(overrides)
def load_overrides():
    path = os.path.join(OVERRIDES_DIR, "review.json")
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_overrides(data):
    os.makedirs(OVERRIDES_DIR, exist_ok=True)
    path = os.path.join(OVERRIDES_DIR, "review.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def record_override(code, paper, uid, entry):
    """网页复核保存: 写入 overrides 并应用到现行判定文件。entry 需带 reason/证据。"""
    ov = load_overrides()
    ov.setdefault(code, {}).setdefault(paper, {})[uid] = entry
    save_overrides(ov)
    adj_path = os.path.join(ADJ_DIR, f"{code}.json")
    if os.path.exists(adj_path):
        adj = json.load(open(adj_path, encoding="utf-8"))
        adj.setdefault("units", {}).setdefault(paper, {})[uid] = dict(entry)
        _recount(adj)
        atomic_write_json(adj_path, adj)


def _recount(adj):
    ci = co = cp = cc = 0
    for pa in adj.get("units", {}).values():
        for e in pa.values():
            s = e.get("status")
            ci += s == "IN"
            co += s == "OUT"
            cp += s == "PENDING"
            cc += s == "CONTEXT"
    adj["counts"] = {"in": ci, "out": co, "pending": cp, "context": cc}


def batch_confirm_candidates(code, min_conf, dry_run=False, log=print):
    """按置信度阈值批量确认自动候选(网页「按conf批量确认IN」按钮):
    把 status=PENDING · basis=auto-candidate · 非作废 · conf≥min_conf · 候选标签
    合法的单元批量转 IN(标签=候选标签)。与网页逐条保存同一持久化机制——
    写入 overrides/review.json(重跑判定不丢)并更新现行判定文件; basis 仍为
    review-override(人工批量决定, 不越过复核门槛)。dry_run 只统计不改动。
    """
    adj_path = os.path.join(ADJ_DIR, f"{code}.json")
    if not os.path.exists(adj_path):
        return {"ok": False, "count": 0, "error": f"科目 {code} 尚无判定记录"}
    adj = json.load(open(adj_path, encoding="utf-8"))
    tax = load_taxonomy(code)
    valid = {x["code"] for x in tax["sections"]} | \
            {x["code"] for x in tax["sections"] for x in x.get("subs", [])}
    picks = []
    for pc, pa in sorted(adj.get("units", {}).items()):
        for uid, e in sorted(pa.items()):
            if e.get("status") != "PENDING" or e.get("basis") != "auto-candidate":
                continue
            if e.get("discounted"):
                continue                       # 官方作废题不批量转 IN
            if (e.get("conf") or 0) < min_conf:
                continue
            cands = [c for c in (e.get("candidate_tags") or []) if c in valid][:3]
            if not cands:
                continue
            picks.append((pc, uid, cands, e.get("conf") or 0))
    papers = sorted({pc for pc, *_ in picks})
    if dry_run:
        return {"ok": True, "count": len(picks), "papers": len(papers), "applied": 0}
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    ov = load_overrides()
    ov_code = ov.setdefault(code, {})
    for pc, uid, cands, conf in picks:
        entry = {"status": "IN", "tags": cands,
                 "reason": f"批量确认自动候选(conf={conf:.2f} ≥ {min_conf:.2f}) → IN"
                           "(网页「按conf批量确认」)",
                 "reviewed_at": ts, "basis": "review-override"}
        ov_code.setdefault(pc, {})[uid] = entry
        adj["units"][pc][uid] = dict(entry)
    save_overrides(ov)
    _recount(adj)
    atomic_write_json(adj_path, adj)
    log(f"  [batch-confirm {code}] conf≥{min_conf:.2f}: 确认 {len(picks)} 单元"
        f"({len(papers)} 卷) → IN; counts={adj['counts']}")
    return {"ok": True, "count": len(picks), "papers": len(papers),
            "applied": len(picks), "counts": adj["counts"]}


def apply_overrides(code, adj, log=print):
    """把人工复核裁定(overrides/review.json)应用到判定结果; 重跑迁移不丢失。
    adj 兼容两种形态: adjudicate() 的 units 映射 {paper:{uid:entry}} 或
    完整判定记录(含 units/counts)。"""
    units_map = adj["units"] if isinstance(adj, dict) and "units" in adj \
        and "counts" in adj else adj
    ov = load_overrides().get(code) or {}
    if not ov:
        return 0
    applied = 0
    for paper, units in ov.items():
        pa = units_map.setdefault(paper, {})
        for uid, entry in units.items():
            e = dict(entry)
            e["basis"] = "review-override"
            pa[uid] = e
            applied += 1
    if units_map is not adj:
        _recount(adj)                   # 完整记录: 同步刷新计数
    log(f"  [overrides {code}] 应用人工裁定 {applied} 条")
    return applied


def counts_of_units(units_map):
    c = Counter()
    for pa in units_map.values():
        for e in pa.values():
            c[e.get("status")] += 1
    return {"in": c["IN"], "out": c["OUT"], "pending": c["PENDING"],
            "context": c["CONTEXT"]}


# ---------------------------------------------------------------- 已发布卷登记
def load_published():
    if os.path.exists(PUBLISHED_PATH):
        try:
            return json.load(open(PUBLISHED_PATH, encoding="utf-8"))
        except Exception:
            return {"subjects": {}}
    return {"subjects": {}}


def save_published(data):
    os.makedirs(PIPE, exist_ok=True)
    tmp = PUBLISHED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PUBLISHED_PATH)


def published_papers(code):
    return set((load_published()["subjects"].get(code) or {}).get("papers", {}))


# ---------------- 两阶段发布状态(P0-1/P0-4: 预备提交意图 + 活动版本对账) ----------------
def refresh_pending_papers(code):
    """记录在 pending_refresh 中的卷(原始意图集合, 不论是否已提交)。"""
    return set((load_published()["subjects"].get(code) or {})
               .get("pending_refresh", {}))


def effective_refresh_pending(code):
    """**已实际提交**、只差服务索引刷新的卷(P0-1 第六轮口径):
    意图绑定的版本 == 当前活动版本, 且该卷的正式单元文件确实存在于活动
    版本中。翻转前失败/崩溃留下的过期意图(活动版本仍是旧版)不算 —— 那些
    卷仍是待发布, 刷新动作不得晋升。"""
    import live_version as lv
    vid = lv.active_version()
    if vid is None:
        return set()
    base = lv.active_dir()
    out = set()
    for pc, e in ((load_published()["subjects"].get(code) or {})
                  .get("pending_refresh") or {}).items():
        if e.get("intended_version") != vid:
            continue
        if base and os.path.isfile(os.path.join(base, "data", "units",
                                                code, f"{pc}.json")):
            out.add(pc)
    return out


def mark_commit_intent(code, papers, version):
    """翻转前持久化"预备提交意图"(P0-1): 只记录 papers 将随 <version> 提交,
    **不移走已发布卷**(papers 保持不变) —— 翻转前失败/崩溃不改变旧卷状态。
    本科此前未提交的过期意图一并被本次覆盖(它们从未成为活动版本)。"""
    data = load_published()
    subj = data["subjects"].setdefault(code, {"papers": {}})
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    subj["pending_refresh"] = {pc: {"at": ts, "intended_version": version}
                               for pc in papers}
    save_published(data)


# 兼容旧名(第五轮测试/调用方)
def mark_papers_refresh_pending(code, papers, version=None):
    mark_commit_intent(code, papers, version)


def promote_refresh_pending(code=None):
    """索引刷新成功后把 pending_refresh 晋升为完全发布(P0-1 严格门禁):
    只晋升 **意图版本 == 当前活动版本 且 正式单元文件存在于活动版本** 的卷。
    翻转前失败留下的过期意图保留待下次发布覆盖; 待发布口径不受影响。"""
    import live_version as lv
    vid = lv.active_version()
    base = lv.active_dir()
    data = load_published()
    changed = []
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    for c in ([code] if code else list(data["subjects"])):
        subj = data["subjects"][c]
        pr = subj.get("pending_refresh") or {}
        if not pr:
            continue
        keep = {}
        for pc, e in pr.items():
            if vid is not None and e.get("intended_version") == vid \
                    and base and os.path.isfile(os.path.join(
                        base, "data", "units", c, f"{pc}.json")):
                subj.setdefault("papers", {})[pc] = {
                    "at": ts, "promoted_from_version": vid}
                changed.append(pc)
            else:
                keep[pc] = e            # 过期意图保留(翻转前失败, 未提交)
        subj["pending_refresh"] = keep
    if changed:
        save_published(data)
    return changed


def formally_published_papers(code):
    """data/units/<code> 中带正式 status(IN/OUT/PENDING/CONTEXT)的卷 —— 即
    曾经成功发布过的卷。无 status 的卷(只落地未发布)不算。"""
    ud = os.path.join(ROOT, "data", "units", code)
    out = set()
    if not os.path.isdir(ud):
        return out
    for f in os.listdir(ud):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(ud, f), encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:
            continue
        if any(u.get("status") in ("IN", "OUT", "PENDING", "CONTEXT")
               for u in rec.get("units", [])):
            out.add(f[:-5])
    return out


def ensure_published_baseline(code, log=print):
    """安全初始化已发布基线(P0-1): 只把 data/units 中带正式 status 的卷登记为
    已发布 —— 这些卷必然经过历史 publish(否则不会有 status); 从未发布的卷
    (无 status)不登记, 不会被误标。pending_refresh 中的卷(意图未提交或索引
    未就绪)不登记。幂等, 返回新增登记数。"""
    data = load_published()
    subj = data["subjects"].setdefault(code, {"papers": {}})
    papers = subj.setdefault("papers", {})
    formal = formally_published_papers(code) - refresh_pending_papers(code)
    added = [pc for pc in sorted(formal) if pc not in papers]
    if added:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        for pc in added:
            papers[pc] = {"at": ts, "source": "baseline-init(data/units 正式 status)"}
        save_published(data)
        log(f"  [baseline {code}] 初始化已发布基线 {len(added)} 卷"
            f"(依据 data/units 正式 status; 无 status 的未发布卷不登记)")
    return len(added)


def pending_papers(code, log=print):
    """真正待发布的卷: workdir 有 QP JSON 且切题非空, 且既未登记 published,
    也未在 data/units 带正式 status(双保险)。**已实际提交到活动版本但索引
    未就绪**的卷(effective_refresh_pending)仍算待发布 —— 刷新失败不清待发布
    (P0-4); 翻转前失败的过期意图本来就没提交, 卷自然仍在待发布(P0-1)。
    切题为 0 单元的卷(扫描件/needs_ocr)没有可发布内容, 不计入待发布。"""
    ensure_published_baseline(code, log=log)
    work = os.path.join(PIPE, "work", code, "units")
    if not os.path.isdir(work):
        return []
    done = published_papers(code) | (formally_published_papers(code)
                                     - effective_refresh_pending(code))
    out = []
    for f in sorted(os.listdir(work)):
        if not f.endswith("_qp.json") or f[:-8] in done:
            continue
        try:
            with open(os.path.join(work, f), encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:
            continue
        if rec.get("units"):
            out.append(f[:-8])
    return out


def mark_papers_published(code, papers, counts=None):
    data = load_published()
    subj = data["subjects"].setdefault(code, {"papers": {}})
    papers_dict = subj.setdefault("papers", {})
    for pc in papers:
        papers_dict[pc] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    save_published(data)


# ---------------------------------------------------------------- 正式产物事务(P0-3a/b)
def _managed_root_files(code):
    """该科目在根目录的全部正式产物文件名(无论当前是否存在)。"""
    return (f"topics_{code}.json", f"classified_{code}.json",
            f"topics_data_{code}.js", f"search_data_{code}.js",
            f"papers_data_{code}.json", f"papers_data_{code}.js",
            f"index_{code}.html", f"index_{code}_topics.html",
            "index.html", "syllabus.html")


def _formal_artifacts(code):
    """该科目全部受管正式路径的集合: 三个数据目录的现有 json + 根目录产物模式
    (含尚不存在的 —— 首次发布失败时这些新增文件也必须被删除)。"""
    rels = set()
    for sub in (f"data/units/{code}", f"data/loc/{code}", f"data/msloc/{code}"):
        d = os.path.join(ROOT, sub)
        if os.path.isdir(d):
            rels |= {f"{sub}/{f}" for f in os.listdir(d) if f.endswith(".json")}
    rels |= set(_managed_root_files(code))
    return rels


def _hash_root(root, rels):
    out = {}
    for rel in rels:
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _hash_formal(rels):
    return _hash_root(ROOT, rels)


def _snapshot_formal(code, existing_rels, log=print):
    """把已存在的正式文件内容快照到 snapshot 目录(P0-3 完整回滚)。"""
    snap = snapshot(code)
    extra = os.path.join(snap, "_formal")
    for rel in sorted(existing_rels):
        src = os.path.join(ROOT, rel)
        dst = os.path.join(extra, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    log(f"  [publish {code}] 正式产物快照 {len(existing_rels)} 个文件 -> {snap}/_formal")
    return snap


def _restore_formal(code, before_map, snap, log=print):
    """失败回滚(P0-3a): 恢复发布前存在的文件为快照字节; 删除所有受管范围内
    新增的文件(含根目录 topics_/classified_/*_data_/index_* 等首次发布产物)。"""
    extra = os.path.join(snap, "_formal")
    for rel in sorted(before_map):
        src = os.path.join(extra, rel)
        dst = os.path.join(ROOT, rel)
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    removed = 0
    for sub in (f"data/units/{code}", f"data/loc/{code}", f"data/msloc/{code}"):
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            rel = f"{sub}/{f}"
            if rel not in before_map:
                os.remove(os.path.join(d, f))
                removed += 1
    for name in _managed_root_files(code):
        rel = name
        if rel not in before_map and os.path.exists(os.path.join(ROOT, rel)):
            os.remove(os.path.join(ROOT, rel))
            removed += 1
    log(f"  [publish {code}] 已回滚: 恢复 {len(before_map)} 个文件, 删除新增 {removed} 个")
    return removed


def _rollback_ok(code, before_map):
    """回滚校验: 受管路径的现存集合与哈希必须与发布前完全一致。"""
    return _hash_formal(_formal_artifacts(code)) == before_map


def _hardlink_or_copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _generator_written_names(code):
    """本次发布会被 publish/生成器改写的根产物(克隆时必须 copy, 不能硬链 ——
    gen_topics/gen_search/gen_index 以 open("w") 直写会穿透共享 inode)。"""
    return {f"topics_{code}.json", f"classified_{code}.json",
            f"topics_data_{code}.js", f"search_data_{code}.js",
            f"papers_data_{code}.json", f"papers_data_{code}.js",
            f"index_{code}.html", f"index_{code}_topics.html",
            "index.html", "syllabus.html"}


def _prepare_version_dir(code, cand, log=print):
    """构建候选版本目录(P0-3b + P0-1): 从**活动版本**克隆完整站点快照 ——
    其他科目根产物/数据树硬链(本发布绝不改写), 当前科目会被改写的文件与
    index.html/syllabus.html 用 copy; 再补 subjects.json、syllabus/ 与该科
    PDF 目录(只读硬链), 使 gen_index 能在候选目录内生成**全站**首页配置,
    而不是只剩当前科目(第四轮 P0-1 回归根因)。"""
    import live_version as lv
    active = lv.active_dir()
    if active is None:
        raise RuntimeError("live 版本布局未就绪(不应发生)")
    written = _generator_written_names(code)
    # 1. 根产物: 全站(所有科目) —— 改写集 copy, 其余硬链
    for name in sorted(os.listdir(active)):
        if not lv.is_managed_root(name):
            continue
        src = os.path.join(active, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(cand, name)
        if name in written:
            shutil.copy2(src, dst)          # 生成器 open("w") 直写目标
        else:
            _hardlink_or_copy(src, dst)     # 其他科目产物: 只读共享
    # 2. 数据树: 其他科目硬链整树; 当前科目 copy(locate_* 对 pending 卷
    #    以 open("w") 直写 loc/msloc, 硬链会穿透改写活动版本)
    for sub in ("units", "loc", "msloc"):
        asub = os.path.join(active, "data", sub)
        if not os.path.isdir(asub):
            continue
        for sd in sorted(os.listdir(asub)):
            src_d = os.path.join(asub, sd)
            if not os.path.isdir(src_d):
                continue
            dst_d = os.path.join(cand, "data", sub, sd)
            os.makedirs(dst_d, exist_ok=True)
            mode_copy = (sd == code)
            for dp, _, fs in os.walk(src_d):
                for f in sorted(fs):
                    s = os.path.join(dp, f)
                    t = os.path.join(dst_d, os.path.relpath(s, src_d))
                    os.makedirs(os.path.dirname(t), exist_ok=True)
                    if mode_copy:
                        shutil.copy2(s, t)
                    else:
                        _hardlink_or_copy(s, t)
    # 3. 登记表(生成全站配置的权威科目清单) 与大纲 PDF 目录(硬链只读)
    reg = os.path.join(ROOT, "subjects.json")
    if os.path.isfile(reg):
        shutil.copy2(reg, os.path.join(cand, "subjects.json"))
    syl_dir = os.path.join(ROOT, "syllabus")
    if os.path.isdir(syl_dir):
        for f in sorted(os.listdir(syl_dir)):
            if f.endswith(".pdf"):
                _hardlink_or_copy(os.path.join(syl_dir, f),
                                  os.path.join(cand, "syllabus", f))
    # 4. 当前科目 PDF 目录(只读硬链; gen_index 重建 papers_data 需要)
    data_dir = None
    try:
        subs = json.load(open(reg, encoding="utf-8"))["subjects"]
        data_dir = (subs.get(code) or {}).get("data_dir")
    except Exception:
        data_dir = None
    if not data_dir:                      # 未登记新科目: 按目录约定发现
        for d in sorted(os.listdir(ROOT)):
            if re.match(rf"^{code}（.*）$", d) and os.path.isdir(os.path.join(ROOT, d)):
                data_dir = d
                break
    if data_dir and os.path.isdir(os.path.join(ROOT, data_dir)):
        src_dir = os.path.join(ROOT, data_dir)
        os.makedirs(os.path.join(cand, data_dir), exist_ok=True)
        for dp, dns, fns in os.walk(src_dir):
            for d in dns:
                os.makedirs(os.path.join(
                    cand, os.path.relpath(os.path.join(dp, d), ROOT)),
                    exist_ok=True)
            for f in fns:
                if f.startswith(".") or not f.endswith(".pdf"):
                    continue
                s = os.path.join(dp, f)
                _hardlink_or_copy(s, os.path.join(          # PDF 只读
                    cand, os.path.relpath(s, ROOT)))
    log(f"  [publish {code}] 候选版本已克隆站点快照(根产物 {sum(1 for n in os.listdir(cand) if lv.is_managed_root(n))} 项)")


def _run_generators_in(code, build, pending, log=print):
    """在 buildroot 内运行定位与全部索引/页面生成(gen_topics/gen_search/
    gen_index)。任一步失败抛异常 —— 此时正式路径未被触碰。"""
    import locate_qp, locate_ms, gen_topics, gen_search
    saved = (locate_qp.ROOT, locate_ms.ROOT, gen_search.ROOT)
    try:
        if pending:
            locate_qp.ROOT = build
            locate_ms.ROOT = build
            locate_qp.main([code], papers=pending)
            locate_ms.main([code], papers=pending)
        gen_topics.run(code, root=build)
        gen_search.ROOT = build
        if not gen_search.build_subject(code):
            raise RuntimeError("search_data 自检失败")
        gen_index_py = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "gen_index.py")
        r = subprocess.run(["/usr/bin/python3", gen_index_py, build, code],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"gen_index 失败: {r.stderr[-300:]}")
    finally:
        locate_qp.ROOT, locate_ms.ROOT, gen_search.ROOT = saved


def _collect_buildroot(code, build):
    """收集版本目录中受管路径的内容哈希(= 新版本清单)。已由 live_version 的
    版本清单(manifest files)取代, 保留仅供外部对账工具调用。"""
    import live_version as lv
    return lv.hash_tree(build)


def extract_headings(pdf_path, lo, hi):
    """从大纲 PDF 提取 编码→(标题, 页码)，兼容同行 tab 与分行两种版式。"""
    import fitz
    VERBS = re.compile(
        r"^(recall|use|understand|state|describe|define|explain|sketch|apply|"
        r"deduce|show|determine|distinguish|interpret|carry|demonstrate|derive|"
        r"appreciate|predict|identify|suggest|construct|calculate|complete|"
        r"analyse|analyze|discuss|outline|comment|infer|recognise|recognize|"
        r"write|draw|read|perform|measure|record|present|plot|consider|know|"
        r"learn|be able|candidates|learning outcomes|cambridge|www|back to)\b",
        re.I)
    SAME = re.compile(r"^(\d{1,2}(?:\.\d{1,2})?)[.\t ]{1,4}(\S.*)$")
    LONE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})?)[.\t ]*$")
    doc = fitz.open(pdf_path)
    heads = {}
    for i in range(lo - 1, min(hi, doc.page_count)):
        lines = doc[i].get_text().splitlines()
        j = 0
        while j < len(lines):
            s = lines[j].replace("\x07", "").strip()
            m = SAME.match(s)
            if m and 4 <= len(m.group(2)) <= 100 and not VERBS.match(m.group(2)) \
                    and re.match(r"^[A-Z(]", m.group(2)):
                heads.setdefault(m.group(1), (" ".join(m.group(2).split()), i + 1))
                j += 1
                continue
            m2 = LONE.match(s)
            if m2 and j + 1 < len(lines):
                nxt = lines[j + 1].replace("\x07", "").strip()
                if nxt and re.match(r"^[A-Z(]", nxt) and not VERBS.match(nxt) \
                        and 3 < len(nxt) <= 100:
                    heads.setdefault(m2.group(1), (" ".join(nxt.split()), i + 1))
                    j += 2
                    continue
            j += 1
    doc.close()
    return heads


# ======================================================================
# 资产构建: 新版 topics 草稿 + 旧→新映射
# ======================================================================
def _practical_section(pspec):
    """PRACTICAL_TAXONOMY 条目 → topics sections 节点(带官方页码 evidence)。"""
    code, en, zh, subs = pspec["section"]
    return {"code": code, "en": en, "zh": zh,
            "kw": [s[1].lower() for s in subs][:8],
            "evidence": f"官方 syllabus Practical assessment {subs[0][4]} 起",
            "subs": [{"code": sc, "en": se, "zh": sz, "kw": kw,
                      "evidence": f"官方 syllabus Practical assessment {ev}"}
                     for sc, se, sz, kw, ev in subs]}


def build_migration_assets(code, log=print):
    """生成/刷新 migration 资产。返回 (new_topics, mapping)。"""
    os.makedirs(MIG_DIR, exist_ok=True)
    old = json.load(open(os.path.join(ROOT, f"topics_{code}.json"), encoding="utf-8"))
    old_codes = {}
    for s in old["sections"]:
        old_codes[s["code"]] = s
        for x in s.get("subs", []):
            old_codes[x["code"]] = x
    mapping_path = os.path.join(MIG_DIR, f"{code}_map.json")
    topics_path = os.path.join(MIG_DIR, f"{code}_topics_new.json")

    if code == "9702":
        already_new = old.get("syllabus_version") == "2028-2030"
        syl = os.path.join(ROOT, "syllabus", "9702_Physics_2028-2029_syllabus.pdf")
        syl = syl if os.path.exists(syl) else os.path.join(
            ROOT, "syllabus", "9702_Physics_2028-2030_syllabus.pdf")
        heads = extract_headings(syl, 15, 40) if os.path.exists(syl) else {}
        # 标题对核(新树每个 code 必须能在新 PDF 找到一致标题; 找不到则用树内标题并标注)
        checks = []
        for sec, en, zh, subs in NEW9702_SECTIONS:
            t = heads.get(sec, (None, None))[0]
            checks.append((sec, en, t, norm_title(en or "") == norm_title(t or ""),
                           heads.get(sec, (None, None))[1]))
            for sc, se in subs:
                t2 = heads.get(sc, (None, None))[0]
                checks.append((sc, se, t2, norm_title(se) == norm_title(t2 or ""),
                               heads.get(sc, (None, None))[1]))
        bad = [c for c in checks if not c[3]]
        if bad:
            log(f"  [assets 9702] ⚠ {len(bad)} 个新树标题与新 PDF 不一致(需人工看): "
                f"{[(c[0], c[1], c[2]) for c in bad][:6]}")
        # kw 迁移: 旧小节 kw → 经映射并入新小节
        new_kw = defaultdict(list)
        for oldc, spec in MAP_9702.items():
            dest = spec.get("to") or spec.get("hint") or []
            src = old_codes.get(oldc)
            kws = (src.get("kw") if isinstance(src, dict) else None) or []
            for d in dest:
                for k in kws:
                    if k not in new_kw[d]:
                        new_kw[d].append(k)
        for k, v in NEW9702_EXTRA_KW.items():
            for w in v:
                if w not in new_kw[k]:
                    new_kw[k].append(w)
        sections = []
        for sec, en, zh, subs in NEW9702_SECTIONS:
            sections.append({
                "code": sec, "en": en, "zh": zh,
                "kw": new_kw[sec][:24],
                "subs": [{"code": sc, "en": se, "zh": "", "kw": new_kw[sc][:24]}
                         for sc, se in subs]})
        levels = [
            {"level": "AS", "from": "1", "to": "11", "papers": [1, 2, 3]},
            {"level": "A2", "from": "12", "to": "25", "papers": [4, 5]}]
        psec = PRACTICAL_TAXONOMY["9702"]
        sections.append(_practical_section(psec))
        levels.append(psec["level"])
        new_topics = {
            "subject": "9702", "name": "Physics", "zh": "物理",
            "source": ("9702_Physics_2028-2030_syllabus.pdf Subject content (p.15-39) "
                       "+ Practical assessment (p.42-55)"),
            "syllabus_version": "2028-2030",
            "note": ("知识点按 2028-2030 最新已发布大纲(官方 2025-09 发布, 自述"
                     "\"version 1, no significant changes which affect teaching\", "
                     "延续 2022 改革结构)。旧 27 章标签经内容映射迁移(见 "
                     "data/pipeline/migration/9702_map.json): 通信/电子学/变压器/"
                     "输电/能带/NMR 已移除; 编号大面积改变, 一律按标题+学习目标对核, "
                     "不按编号沿用。第 26 章为官方 Practical assessment 技能分类"
                     "(p42-55, Paper 3/5 可考 Subject content 之外的情境)。"),
            "levels": levels,
            "sections": sections}
        mapping = {c: dict(spec) for c, spec in MAP_9702.items()}
        # 旧 taxonomy 中未出现在 MAP 的 code(如章节级 1..27) → rejudge 兜底
        for c in old_codes:
            if c not in mapping:
                mapping[c] = {"action": "rejudge",
                              "ev": "映射表未覆盖, 按内容重判"}
        if already_new:
            # 本地 topics/classified 已是 2028-2030 新树(第一轮迁移已发布):
            # 重跑必须恒等映射, 不得把 旧→新 映射表再套一次(双重迁移)
            new_valid = {s[0] for s in NEW9702_SECTIONS} | \
                {sc for sec, en, zh, subs in NEW9702_SECTIONS for sc, se in subs} | \
                {"26", "26.1", "26.2", "26.3", "26.4", "26.5"}
            mapping = {c: ({"to": [c], "ev": "已是 2028-2030 新树编码, 恒等映射"}
                           if c in new_valid else
                           {"action": "rejudge", "ev": "新树未覆盖, 按内容重判"})
                       for c in old_codes}
    else:
        # 9709/9701/9618/9708: 新版与本地旧版结构同码 → 逐项标题对核后同码迁移
        # (9708: 官方 2029 单年版, 2026-2028→2029 学习目标逐页 diff 一致, 见
        #  data/pipeline/acceptance/*/data-review/)
        syl_file = {
            "9709": "9709_Mathematics_2028-2030_syllabus.pdf",
            "9701": "9701_Chemistry_2028-2030_syllabus.pdf",
            "9618": "9618_ComputerScience_2027-2029_syllabus.pdf",
            "9708": "9708_Economics_2029_syllabus.pdf"}[code]
        syl = os.path.join(ROOT, "syllabus", syl_file)
        page_rng = {"9709": (18, 42), "9701": (15, 60), "9618": (14, 44),
                    "9708": (15, 36)}[code]
        heads = extract_headings(syl, *page_rng) if os.path.exists(syl) else {}
        mapping, mismatch = {}, []
        overrides = IDENTITY_OVERRIDES.get(code, {})
        for c, node in old_codes.items():
            if c in overrides:
                dest, ev = overrides[c]
                mapping[c] = {"to": [dest], "ev": f"人工核准: {ev}"}
                continue
            t = heads.get(c, (None, None))[0]
            if t and norm_title(node["en"]) == norm_title(t):
                mapping[c] = {"to": [c], "ev": f"标题与新 PDF p{heads[c][1]} 一致"}
            elif t:
                ratio = difflib.SequenceMatcher(
                    None, norm_title(node["en"]), norm_title(t)).ratio()
                if ratio >= 0.85:
                    mapping[c] = {"to": [c],
                                  "ev": f"标题微调(相似度{ratio:.2f}), p{heads[c][1]}: {t}"}
                else:
                    mismatch.append((c, node["en"], t))
            else:
                mismatch.append((c, node["en"], None))
        if mismatch:
            log(f"  [assets {code}] ⚠ {len(mismatch)} 个标题无法对核: {mismatch[:8]}")
        if code in PRACTICAL_TAXONOMY:
            # 实操技能小节是本引擎依据官方 Practical assessment 增设的分类,
            # 不在 Subject content 标题里 → 显式同码映射(带官方页码证据)
            psec = PRACTICAL_TAXONOMY[code]
            for sc, se, sz, kw, ev in psec["section"][3]:
                if sc in old_codes or sc == psec["section"][0]:
                    mapping[sc] = {"to": [sc],
                                   "ev": f"Practical assessment 技能分类(官方 {ev})"}
            mapping[psec["section"][0]] = {
                "to": [psec["section"][0]],
                "ev": f"Practical assessment 技能分类(官方 p{psec['section'][3][0][4].split(',')[0]} 起)"}
        ver = {"9709": "2028-2030", "9701": "2028-2030", "9618": "2027-2029",
               "9708": "2029"}[code]
        new_topics = dict(old)
        new_topics["syllabus_version"] = ver
        new_topics["source"] = old["source"]
        for key in ("source",):
            base = os.path.basename(old.get(key, ""))
            if "2028-2030" not in base and "2027-2029" not in base:
                new_topics["source"] = f"{syl_file} Subject content"
        new_topics["note"] = (f"知识点按 {ver} 最新已发布大纲。与前一版结构逐项对核"
                              f"(标题+学习目标归一化 diff 无实质变化), 编号不变; "
                              f"标签经迁移引擎核对后发布。") + " " + old.get("note", "")
        if code in PRACTICAL_TAXONOMY:
            psec = PRACTICAL_TAXONOMY[code]
            have = {s["code"] for s in new_topics["sections"]}
            if psec["section"][0] not in have:
                pages = psec["section"][3][0][4] if psec["section"][3] else ""
                new_topics["sections"] = list(new_topics["sections"]) + \
                    [_practical_section(psec)]
                new_topics["levels"] = list(new_topics.get("levels") or []) + \
                    [psec["level"]]
                new_topics["note"] = new_topics.get("note", "") + (
                    f" 第 {psec['section'][0]} 章为官方 Practical assessment 技能分类"
                    f"(官方 {pages} 起, Paper 3/5 可考 Subject content 之外的情境)。")
            else:
                # 章节已存在: 以 PRACTICAL_TAXONOMY(官方页码证据的最新精化)为准
                # 刷新 kw/evidence, 不沿用上次发布的旧短语表
                fresh = _practical_section(psec)
                for s in new_topics["sections"]:
                    if s["code"] == fresh["code"]:
                        s["subs"] = fresh["subs"]
                        s["evidence"] = fresh.get("evidence")
    json.dump(mapping, open(mapping_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(new_topics, open(topics_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    log(f"  [assets {code}] 映射 {len(mapping)} 条 -> {mapping_path}")
    log(f"  [assets {code}] 新版 topics 草稿 -> {topics_path}")
    return new_topics, mapping


# ======================================================================
# 重判分类器(近邻投票 + 关键词)
# ======================================================================
class Judge:
    def __init__(self, anchors, kw_by_code, kw_weight=1.0):
        """anchors: [(text, tags)]; kw_by_code: code -> [短语]
        kw_weight: 关键词通路权重(新科目锚点稀少时调大, 避免 NN 掩盖精确短语命中)"""
        self.kw = {c: [tuple(tokenize(p)) for p in ps] for c, ps in kw_by_code.items()}
        self.kw_weight = kw_weight
        self.df = Counter()
        self.docs = []
        self.post = defaultdict(list)
        for text, tags in anchors:
            toks = Counter(tokenize(text) + self._bi(text))
            if not toks:
                continue
            self.docs.append((toks, tags))
            for t in toks:
                self.df[t] += 1
        n = max(len(self.docs), 1)
        self.idf = defaultdict(lambda: math.log(3 * n))
        for t, df in self.df.items():
            self.idf[t] = math.log(1 + n / df)
        self.norms = []
        for i, (toks, _) in enumerate(self.docs):
            n2 = 0.0
            for t, tf in toks.items():
                w = self.idf[t] * tf
                n2 += w * w
                self.post[t].append((i, tf))
            self.norms.append(math.sqrt(n2) or 1.0)

    @staticmethod
    def _bi(text):
        uni = tokenize(text)
        return [uni[i] + "_" + uni[i + 1] for i in range(len(uni) - 1)]

    def score(self, text):
        """→ {code: score}, best_sim, kw_score_max"""
        toks = Counter(tokenize(text) + self._bi(text))
        if not toks:
            return {}, 0.0, 0.0
        myn2 = 0.0
        acc = defaultdict(float)
        for t, tf in toks.items():
            w = self.idf[t] * tf
            myn2 += w * w
            for i, atf in self.post.get(t, ()):
                acc[i] += w * self.idf[t] * atf
        mynorm = math.sqrt(myn2) or 1.0
        sims = sorted(((s / (mynorm * self.norms[i]), self.docs[i][1])
                       for i, s in acc.items()), key=lambda x: -x[0])[:10]
        best = sims[0][0] if sims else 0.0
        vote = defaultdict(float)
        for sim, tags in sims:
            if sim <= 0.05:
                continue
            for j, c in enumerate(tags):
                vote[c] += sim * (1.0 if j == 0 else 0.25)
        uni = {t for t in toks if "_" not in t}
        kw = defaultdict(float)
        for c, phrases in self.kw.items():
            for ph in phrases:
                if len(ph) == 1:
                    if ph[0] in uni:
                        kw[c] += 3.0
                elif len(ph) >= 2 and all(t in uni for t in ph):
                    kw[c] += 5.0 * len(ph)
        vm = max(vote.values(), default=0.0)
        km = max(kw.values(), default=0.0)
        out = {c: (vote[c] / vm if vm else 0.0)
               + self.kw_weight * (kw[c] / km if km and km > 0 else 0.0)
               for c in set(vote) | set(kw)}
        return out, best, km

    def judge(self, text, hint_codes=()):
        """→ (codes, conf, detail)。conf∈[0,1]: 综合 NN 相似与 kw 命中。"""
        score, best, km = self.score(text)
        if not score:
            return None, 0.0, {"best": 0.0, "kw": 0.0}
        ranked = sorted(score.items(), key=lambda x: -x[1])
        top_c, top_s = ranked[0]
        codes = [top_c]
        for c, s in ranked[1:3]:
            if s >= 0.55 * top_s:
                codes.append(c)
        conf = min(1.0, 0.5 * min(1.0, best / 0.30)
                   + 0.5 * min(1.0, km / 12.0))
        return codes, conf, {"best": round(best, 3), "kw": round(km, 1),
                             "ranked": ranked[:4]}


# ======================================================================
# 判定
# ======================================================================
def load_paper_labels(code):
    """旧标签来源: classified(IN) 与 tags(含 OUT)。"""
    cls = json.load(open(os.path.join(ROOT, f"classified_{code}.json"),
                         encoding="utf-8"))
    cls_by = {p["code"]: {u["id"]: u.get("topics") for u in p["units"]}
              for p in cls["papers"]}
    tags_by = {}
    tdir = os.path.join(ROOT, f"tags_{code}")
    if os.path.isdir(tdir):
        for f in os.listdir(tdir):
            if f.endswith(".json"):
                try:
                    d = json.load(open(os.path.join(tdir, f), encoding="utf-8"))
                    tags_by[d.get("code") or f[:-5]] = d.get("tags") or {}
                except Exception:
                    pass
    return cls_by, tags_by


def parse_tag_value(v):
    """tags 值: [codes] / ["OUT", reason] / ["PENDING", question] /
    ["CONTEXT", note] / ["DISCOUNTED", evidence] → (kind, payload)"""
    if isinstance(v, dict):
        st = v.get("status", "IN")
        return st, v.get("tags") or v.get("t") or [], v.get("note") or v.get("reason")
    if not v:
        return "missing", [], None
    if v[0] == "OUT":
        return "OUT", [], (v[1] if len(v) > 1 else None)
    if v[0] == "PENDING":
        return "PENDING", [], (v[1] if len(v) > 1 else None)
    if v[0] == "CONTEXT":
        return "CONTEXT", [], (v[1] if len(v) > 1 else None)
    if v[0] == "DISCOUNTED":
        return "DISCOUNTED", [], (v[1] if len(v) > 1 else None)
    return "IN", list(v), None


def adjudicate(code, new_topics=None, mapping=None, log=print, papers_filter=None):
    """对 data/units/<code> 全部单元产出 IN/OUT/PENDING 判定。"""
    if new_topics is None or mapping is None:
        nt_path = os.path.join(MIG_DIR, f"{code}_topics_new.json")
        mp_path = os.path.join(MIG_DIR, f"{code}_map.json")
        if os.path.exists(nt_path) and os.path.exists(mp_path):
            new_topics = json.load(open(nt_path, encoding="utf-8"))
            mapping = json.load(open(mp_path, encoding="utf-8"))
        else:
            new_topics, mapping = build_migration_assets(code, log=log)
    valid = {s["code"] for s in new_topics["sections"]} | \
            {x["code"] for s in new_topics["sections"] for x in s.get("subs", [])}
    kw_by_code = {}
    for s in new_topics["sections"]:
        kw_by_code.setdefault(s["code"], [s["en"]] + s.get("kw", []))
        for x in s.get("subs", []):
            kw_by_code[x["code"]] = [x["en"]] + x.get("kw", [])
    cls_by, tags_by = load_paper_labels(code)
    units_dir = os.path.join(ROOT, "data", "units", code)

    # 第一遍: 干净映射 → 收集锚点
    anchors, pending_judge = [], []
    adjud = {}       # paper -> uid -> entry
    stats = Counter()
    files = sorted(f for f in os.listdir(units_dir) if f.endswith(".json")) \
        if os.path.isdir(units_dir) else []
    if papers_filter:
        files = [f for f in files if f[:-5] in papers_filter]
    for f in files:
        rec = json.load(open(os.path.join(units_dir, f), encoding="utf-8"))
        pc = rec["code"]
        cls_p = cls_by.get(pc, {})
        tags_p = tags_by.get(pc)
        paper_adj = {}
        for u in rec["units"]:
            uid = u["id"]
            old = cls_p.get(uid)
            if old:
                mapped, unmapped = [], []
                for t in old:
                    spec = mapping.get(t)
                    if spec and spec.get("to"):
                        mapped.extend(spec["to"])
                    else:
                        unmapped.append(t)
                if not unmapped:
                    tags = list(dict.fromkeys(c for c in mapped if c in valid))
                    if len(tags) > 3:
                        # 一对多映射叠加原多标签 → 把多目标折叠为章级码, 再截断
                        multi_targets = set()
                        for old_t in old:
                            spec = mapping.get(old_t) or {}
                            if len(spec.get("to") or []) > 1:
                                multi_targets.update(spec["to"])
                        collapsed = []
                        for t in tags:
                            if len(collapsed) >= 3:
                                break
                            if t in multi_targets and "." in t:
                                sec = t.split(".")[0]
                                if sec not in collapsed and sec in valid:
                                    collapsed.append(sec)
                            elif t not in collapsed:
                                collapsed.append(t)
                        tags = collapsed[:3]
                    if tags:
                        paper_adj[uid] = {"status": "IN", "tags": tags,
                                          "basis": "mapped", "old": old}
                        anchors.append((u["text"], tags))
                        stats["in_mapped"] += 1
                        continue
                pending_judge.append((pc, uid, u, old, unmapped))
            elif tags_p is not None and uid in tags_p:
                kind, payload, note = parse_tag_value(tags_p[uid])
                if kind == "OUT":
                    paper_adj[uid] = {"status": "OUT",
                                      "reason": note or
                                      "旧分类阶段判定考纲外; 已对照最新大纲"
                                      "学习目标 diff 复核(无内容回添)维持",
                                      "basis": "old-out", "old": ["OUT"]}
                    stats["out_old"] += 1
                elif kind == "PENDING":
                    paper_adj[uid] = {"status": "PENDING",
                                      "reason": note or "AI 判定证据不足",
                                      "basis": "ai-pending"}
                    stats["pending_ai"] += 1
                elif kind == "CONTEXT":
                    paper_adj[uid] = {"status": "CONTEXT",
                                      "reason": note or "无分值共享材料(题干上下文)",
                                      "basis": "ai-context"}
                    stats["context_material"] += 1
                elif kind == "DISCOUNTED":
                    paper_adj[uid] = {"status": "PENDING", "discounted": True,
                                      "reason": note or "官方 MS 标注作废(0 分)",
                                      "basis": "ai-discounted"}
                    stats["pending_ai"] += 1
                else:
                    prev = u.get("status")
                    if prev in ("OUT", "PENDING", "CONTEXT"):
                        # tags 里是旧树时代的陈旧 IN 草稿, 而该单元上次发布已有
                        # 裁定(含 removed 证据/待复核问题) → 以上次发布为准,
                        # 不回退到自动重判
                        paper_adj[uid] = {"status": prev,
                                          "reason": u.get("reason") or "",
                                          "basis": "prev-publish", "old": [prev]}
                        stats[f"prev_{prev.lower()}"] += 1
                        continue
                    pending_judge.append((pc, uid, u, payload, []))
            else:
                prev = u.get("status")
                if prev in ("OUT", "PENDING", "CONTEXT"):
                    # 该单元无分类标签, 但上次发布已带状态与理由(如 removed 证据、
                    # 待复核问题) → 继承, 重跑不丢失上一轮裁定
                    paper_adj[uid] = {"status": prev,
                                      "reason": u.get("reason") or "",
                                      "basis": "prev-publish", "old": [prev]}
                    stats[f"prev_{prev.lower()}"] += 1
                    continue
                paper_adj[uid] = {"status": "PENDING",
                                  "reason": ("该卷从未派发分类"
                                             f"({'无 tags 文件' if tags_p is None else 'tags 缺此单元'})"
                                             ", 未按最新大纲核准"),
                                  "basis": "never-classified"}
                stats["pending_never"] += 1
        adjud[pc] = paper_adj
    log(f"  [adjudicate {code}] 干净映射 IN {stats['in_mapped']} · 旧OUT "
        f"{stats['out_old']} · 待重判 {len(pending_judge)} · "
        f"未分类 PENDING {stats['pending_never']}")

    # 第二遍: 重判(近邻+关键词)
    judge = Judge(anchors, kw_by_code)
    for pc, uid, u, old, unmapped in pending_judge:
        # hints = 已映射部分的映射结果 + 未映射部分的 hint(测量/力类型等)
        mapped_here = []
        for t in old:
            if t not in unmapped:
                mapped_here.extend((mapping.get(t) or {}).get("to") or [])
        hints = list(mapped_here)
        for t in unmapped:
            sp = mapping.get(t) or {}
            hints.extend(sp.get("to") or sp.get("hint") or [])
        rematch = None
        for t in unmapped:
            sp = mapping.get(t) or {}
            for rx, dest, ev in sp.get("rematch") or []:
                if re.search(rx, u["text"], re.I):
                    rematch = (dest, ev)
                    break
            if rematch:
                break
        if rematch:
            dest, ev = rematch
            adjud[pc][uid] = {"status": "IN", "tags": dest,
                              "basis": "rematch", "old": old, "ev": ev}
            stats["in_rematch"] += 1
            continue
        codes, conf, det = judge.judge(u["text"], hints)
        all_removed = bool(unmapped) and not mapped_here and all(
            (mapping.get(t) or {}).get("action") == "removed" for t in unmapped)
        strong_nn = (det or {}).get("best", 0) >= 0.30   # 存在足够近的已映射锚点
        if codes and conf >= 0.62 and (codes[0] in hints or not hints or strong_nn):
            adjud[pc][uid] = {"status": "IN", "tags": codes[:3],
                              "basis": "rejudged", "old": old,
                              "conf": round(conf, 2), "detail": det}
            stats["in_rejudged"] += 1
        elif all_removed and (not codes or conf < 0.62):
            # 旧考点整体移除且无足够在纲信号 → OUT(低于 IN 阈值即判考纲外)
            reason = "旧考点 " + ",".join(unmapped) + " 已从最新大纲移除(" + \
                     ";".join((mapping.get(t) or {}).get("ev", "") for t in unmapped) + \
                     f"), 题面无其他在纲考点信号(conf={conf:.2f})"
            adjud[pc][uid] = {"status": "OUT", "reason": reason,
                              "basis": "removed", "old": old}
            stats["out_removed"] += 1
        else:
            adjud[pc][uid] = {"status": "PENDING",
                              "reason": f"旧标签 {old} 无最新大纲直接对应"
                              f"({'未映射' if unmapped else ''}), 自动信号不足"
                              f"(conf={conf:.2f})",
                              "basis": "rejudge-low", "old": old,
                              "conf": round(conf, 2)}
            stats["pending_rejudge"] += 1

    # 第三遍: 旧 OUT 复核 —— 近邻相似不翻案(相邻主题天然相似, 如旧 M2 抛体 vs
    # 在纲直线运动); 以「新旧大纲学习目标 diff 是否回添内容」为复核依据。
    # identity 映射(标题+目标逐项对核一致)→ 无回添 → 维持 OUT 并记录证据;
    # 内容确有变化(mapping 含 removed/rejudge 且非 identity)→ 强 kw 信号者转
    # PENDING 待人工重判。全部旧 OUT 带题面摘要进复核清单供抽检。
    identity_map = all(
        (not spec) or (spec.get("to") == [c])
        for c, spec in mapping.items()) if mapping else True
    old_out_list = []
    paper_of = {}                 # (pc, uid) -> paper 号(实操卷复核用)
    for f in files:
        rec = json.load(open(os.path.join(units_dir, f), encoding="utf-8"))
        pa = adjud.get(rec["code"], {})
        for u in rec["units"]:
            e = pa.get(u["id"])
            paper_of[(rec["code"], u["id"])] = rec.get("paper")
            if e and e.get("basis") in ("old-out", "practical-pending",
                                        "context-material", "practical-in",
                                        "review-in"):
                # 实操复核过的单元也进入清单: 重跑可随 kw 精化重新判定
                old_out_list.append((rec["code"], u["id"], u["text"], u))
    flipped = 0
    for pc, uid, text, u in old_out_list:
        if identity_map:
            continue                     # 大纲未回添内容: 维持 OUT(diff 已证)
        cur = adjud[pc].get(uid) or {}
        if cur.get("basis") != "old-out" or cur.get("status") != "OUT":
            continue                     # 只翻当前仍为 OUT 的旧判条目
        codes, conf, det = judge.judge(text)
        if codes and conf >= 0.80 and det.get("kw", 0) >= 15:
            adjud[pc][uid] = {
                "status": "PENDING",
                "reason": (f"旧判 OUT, 但新旧大纲内容确有变化且题面强命中 "
                           f"{codes[:2]} 关键词(conf={conf:.2f}), 需人工按最新"
                           f"大纲学习目标复核"),
                "basis": "old-out-review", "old": ["OUT"],
                "conf": round(conf, 2), "detail": det}
            stats["out_old"] -= 1
            stats["pending_review"] += 1
            flipped += 1
    if old_out_list:
        log(f"  [adjudicate {code}] 旧OUT复核 {len(old_out_list)} 个 "
            f"(identity_map={identity_map}, {flipped} 个转 PENDING)")

    # 第四遍(9701/9702): 官方 Practical assessment 技能复核 + 无分值材料转 CONTEXT
    practical_review = []
    if code in PRACTICAL_TAXONOMY:
        practical_review = _revalidate_practical_and_context(
            code, adjud, new_topics, old_out_list, paper_of, judge, stats, log)

    # 人工复核裁定(overrides)最后应用 —— 官方作废题/共享材料/逐题裁定不因重跑丢失
    apply_overrides(code, adjud, log=log)
    review = []
    for pc, uid, u, old, unmapped in pending_judge:
        e = adjud[pc].get(uid, {})
        if e.get("status") != "IN":
            review.append(f"{pc}#{uid}: {e.get('reason', '')[:80]}")
    for pc, uid, text, u in old_out_list[:2000]:
        e = adjud[pc].get(uid, {})
        st = e.get("status", "OUT")
        if st == "OUT":
            review.append(f"{pc}#{uid}: [OUT·复核] "
                          f"{' '.join(text.split())[:90]}")
    # 计数以最终映射为准(overrides 可能在 stats 累计之后改状态)
    counts = counts_of_units(adjud)
    total = sum(counts.values())
    out = {"subject": code, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "target": {"version": new_topics.get("syllabus_version"),
                      "source": new_topics.get("source")},
           "counts": counts, "stats": dict(stats),
           "papers": {pc: {"in": sum(1 for e in pa.values() if e["status"] == "IN"),
                           "out": sum(1 for e in pa.values() if e["status"] == "OUT"),
                           "pending": sum(1 for e in pa.values()
                                          if e["status"] == "PENDING"),
                           "context": sum(1 for e in pa.values()
                                          if e["status"] == "CONTEXT")}
                      for pc, pa in adjud.items()},
           "units": adjud, "review_sample": review[:400]}
    if practical_review is not None:
        out["practical_review"] = practical_review
    return out


def _kw_hits(text, kw_by_code):
    """直接短语命中分(与 Judge.kw 同规则): 返回 {code: score}。可预测可审计。"""
    toks = set(tokenize(text))
    hits = {}
    for c, phrases in kw_by_code.items():
        s = 0.0
        for ph in phrases:
            pt = tokenize(ph)
            if len(pt) == 1 and pt[0] in toks:
                s += 3.0
            elif len(pt) >= 2 and all(t in toks for t in pt):
                s += 5.0 * len(pt)
        if s:
            hits[c] = s
    return hits


def _revalidate_practical_and_context(code, adjud, new_topics, old_out_list,
                                      paper_of, judge, stats, log):
    """9701/9702 旧 OUT 单元按官方 Practical assessment 复核。

    规则(对应 CODEX_ACCEPTANCE P0-3):
      - 有分值小问在 Paper 3/5: 实操技能短语命中(≥10 分, 即一个双词官方短语)
        → IN 38.x/26.x(官方页码证据); 或最新大纲内容强命中 → IN;
        无信号 → PENDING(不得仅因低相似度维持自动 OUT)。
      - 有分值小问在其他卷: 最新大纲关键词强命中(≥15) → IN; 否则 → PENDING 待人工。
      - 无分值单元(共享材料/说明/表格): → CONTEXT(题干上下文, 不阻断大题)。
    返回逐单元复核记录(供审计/交接包 data-review)。
    """
    pspec = PRACTICAL_TAXONOMY[code]
    prac_codes = {sc: ev for sc, se, sz, kw, ev in pspec["section"][3]}
    kw_by_code = {}
    for s in new_topics["sections"]:
        kw_by_code.setdefault(s["code"], [s["en"]] + s.get("kw", []))
        for x in s.get("subs", []):
            kw_by_code[x["code"]] = [x["en"]] + x.get("kw", [])
    papers = set(pspec["papers"])
    review = []
    n_in = n_pend = n_ctx = 0
    for pc, uid, text, u in old_out_list:
        e = adjud[pc].get(uid)
        if not e or e.get("basis") not in ("old-out", "practical-pending"):
            continue          # 已翻案/其余 basis 不动; practical-pending 重跑可精化
        paper = paper_of.get((pc, uid))
        marks = u.get("marks")
        old_reason = e.get("reason") or ""
        if marks:
            hits = _kw_hits(text, kw_by_code)
            prac_hits = sorted(((s, c) for c, s in hits.items()
                                if c in prac_codes), reverse=True)
            if paper in papers and prac_hits and prac_hits[0][0] >= 10:
                tags = [c for _, c in prac_hits[:3]]
                ev = "; ".join(sorted({prac_codes.get(t, "Subject content")
                                       for t in tags}))
                adjud[pc][uid] = {
                    "status": "IN", "tags": tags,
                    "reason": (f"实操技能复核: 题面命中官方 Practical assessment "
                               f"{', '.join(tags)} 短语(kw={prac_hits[0][0]:.0f}; "
                               f"官方 {ev}); Paper {paper} 可考 Subject content "
                               f"之外的情境"),
                    "basis": "practical-in", "old": ["OUT"], "marks": marks,
                    "kw": round(prac_hits[0][0], 1)}
                stats["in_practical"] += 1
                n_in += 1
                review.append({"paper": pc, "uid": uid, "marks": marks,
                               "old": "OUT", "new": "IN", "tags": tags,
                               "evidence": f"官方 Practical assessment {ev}",
                               "kw": round(prac_hits[0][0], 1),
                               "reason": "实操技能短语命中(官方技能页码)"})
                continue
            # P0-2: 内容关键词命中不足以把旧 OUT 升为 IN —— 词汇重合不能证明
            # 符合最新大纲学习目标; 一律 PENDING 待人工逐题核对官方依据。
            content_hits = sorted(((s, c) for c, s in hits.items()
                                   if c not in prac_codes), reverse=True)
            kw_note = (f"; 内容词命中 {content_hits[0][1]}"
                       f"(kw={content_hits[0][0]:.0f}, 词汇重合≠官方依据)"
                       if content_hits else "")
            adjud[pc][uid] = {
                "status": "PENDING",
                "reason": (f"旧判 OUT({old_reason[:60]})经复核无逐题官方依据"
                           f"{kw_note}; 依规则'无证据只能 PENDING'转待人工; "
                           f"Paper {paper}, {marks} 分"),
                "basis": "practical-pending", "old": ["OUT"],
                "marks": marks}
            stats["pending_practical"] += 1
            n_pend += 1
            review.append({"paper": pc, "uid": uid, "marks": marks,
                           "old": "OUT", "new": "PENDING",
                           "evidence": None,
                           "kw": round(content_hits[0][0], 1) if content_hits else 0,
                           "reason": "无逐题官方依据(关键词命中不作 IN 依据), 待人工复核"})
        else:
            adjud[pc][uid] = {
                "status": "CONTEXT",
                "reason": ("无分值共享材料/说明性文本(旧判 OUT 依据不足); 作为"
                           "完整大题的题干上下文保留, 不单独判考纲外、不阻断组卷"),
                "basis": "context-material", "old": ["OUT"]}
            stats["context_material"] += 1
            n_ctx += 1
            review.append({"paper": pc, "uid": uid, "marks": None,
                           "old": "OUT", "new": "CONTEXT", "evidence": None,
                           "reason": "无分值材料 → 题干上下文"})
    log(f"  [practical {code}] 旧OUT复核: IN {n_in} · PENDING {n_pend} · "
        f"CONTEXT {n_ctx}")
    return review


def validate_adjudication(adj, new_topics):
    valid = {s["code"] for s in new_topics["sections"]} | \
            {x["code"] for s in new_topics["sections"] for x in s.get("subs", [])}
    problems = []
    n = Counter()
    for pc, pa in adj["units"].items():
        for uid, e in pa.items():
            st = e.get("status")
            if st not in ("IN", "OUT", "PENDING", "CONTEXT"):
                problems.append(f"{pc}#{uid}: 非法状态 {st!r}")
                continue
            n[st] += 1
            if st == "IN":
                tags = e.get("tags") or []
                if not (1 <= len(tags) <= 3):
                    problems.append(f"{pc}#{uid}: IN 标签数 {len(tags)}")
                for t in tags:
                    if t not in valid:
                        problems.append(f"{pc}#{uid}: 非法标签 {t}")
            elif st == "OUT" and not e.get("reason"):
                problems.append(f"{pc}#{uid}: OUT 缺理由")
            elif st == "PENDING" and not e.get("reason"):
                problems.append(f"{pc}#{uid}: PENDING 缺待核问题")
            elif st == "CONTEXT" and not e.get("reason"):
                problems.append(f"{pc}#{uid}: CONTEXT 缺说明")
    if n["IN"] + n["OUT"] + n["PENDING"] + n["CONTEXT"] != sum(adj["counts"].values()):
        problems.append(f"计数不符: {dict(n)} vs {adj['counts']}")
    return problems, dict(n)


# ======================================================================
# 发布
# ======================================================================
def snapshot(code):
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(SNAP_DIR, f"{ts}_{code}")
    os.makedirs(dest, exist_ok=True)
    for f in (f"topics_{code}.json", f"classified_{code}.json"):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            shutil.copy2(p, dest)
    ud = os.path.join(ROOT, "data", "units", code)
    if os.path.isdir(ud):
        shutil.copytree(ud, os.path.join(dest, "units"),
                        dirs_exist_ok=True)
    with open(os.path.join(dest, "README.txt"), "w") as f:
        f.write(f"迁移前快照 {code} @ {ts}\n恢复: 把 units/ 拷回 data/units/{code}/, "
                f"topics_/classified_ 拷回根目录。\n")
    return dest


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_taxonomy(code):
    """现行 taxonomy: 正式 topics 优先, 新科目用导入草稿(P1-2: 草稿不直改正式)。"""
    p = os.path.join(ROOT, f"topics_{code}.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    draft = imported_topics_path(code)
    if os.path.exists(draft):
        with open(draft, encoding="utf-8") as f:
            return json.load(f)
    raise FileNotFoundError(f"topics_{code}.json 与导入草稿均不存在")


def imported_topics_path(code):
    return os.path.join(MIG_DIR, f"{code}_topics_imported.json")


def _pending_unit_record(code, pc):
    """从 workdir 构建待发布卷的 unit 记录(不写正式 data/units —— P0-1)。"""
    work = os.path.join(PIPE, "work", code, "units")
    with open(os.path.join(work, f"{pc}_qp.json"), encoding="utf-8") as f:
        src = json.load(f)
    ms_path = os.path.join(work, f"{pc}_ms.json")
    ms_pages = {}
    if os.path.exists(ms_path):
        with open(ms_path, encoding="utf-8") as f:
            ms_pages = json.load(f).get("ms_pages", {})
    return {"subject": code, "code": pc, "year": src["year"],
            "session": src["session"], "paper": src["paper"],
            "variant": src["variant"], "qp_pdf": src["qp_pdf"],
            "ms_pdf": src.get("ms_pdf"), "ms_pages": ms_pages,
            "units": [{"id": u["id"], "q": u["q"], "part": u["part"],
                       "page": u["page"], "marks": u["marks"],
                       "text": u["text"], "topics": None,
                       "in_old_index": False, "text_source": "id"}
                      for u in src["units"]]}


def publish(code, adj=None, new_topics=None, log=print, dry=False,
            cancel_check=None):
    """发布事务(P0-1/P0-3): 校验 → 临时区构建并复核 → 快照全部正式产物 →
    切换(数据+定位+全部静态索引/页面) → 复核 → 登记已发布 → 刷新服务端索引。

    - 待发布卷在成功切换前**不写** data/units(判定/定位/生成均在本函数内);
    - 任一提交步骤失败: 正式数据、静态索引、页面、published.json 全部恢复
      发布前字节(含删除新增文件);
    - 刷新钩子错误显式返回(index_refresh_errors), 由调用方决定失败语义。
    - cancel_check: 在进入提交阶段前调用一次(安全取消边界; 提交阶段不可中断)。
    """
    ensure_published_baseline(code, log=log)
    if adj is None:
        p = os.path.join(ADJ_DIR, f"{code}.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                adj = json.load(fh)
        else:
            adj = None
    pending = pending_papers(code, log=log)
    if pending and (adj is None or any(pc not in adj.get("units", {})
                                       for pc in pending)):
        log(f"  [publish {code}] 待发布卷缺判定, 先运行判定")
        adjudicate_new_papers(code, log=log)
        with open(os.path.join(ADJ_DIR, f"{code}.json"), encoding="utf-8") as fh:
            adj = json.load(fh)
    if adj is None:
        # 无判定文件且无待发布卷: 以现行正式单元状态合成(等价重发布当前状态)
        units_dir0 = os.path.join(ROOT, "data", "units", code)
        synth = {}
        if os.path.isdir(units_dir0):
            for f in sorted(os.listdir(units_dir0)):
                if not f.endswith(".json"):
                    continue
                with open(os.path.join(units_dir0, f), encoding="utf-8") as fh:
                    rec = json.load(fh)
                pa = {}
                for u in rec["units"]:
                    st = u.get("status")
                    if st not in ("IN", "OUT", "PENDING", "CONTEXT"):
                        continue
                    e = {"status": st, "reason": u.get("reason"),
                         "basis": "current-formal"}
                    if st == "IN":
                        e["tags"] = u.get("topics") or []
                    if u.get("discounted"):
                        e["discounted"] = True
                    pa[u["id"]] = e
                if pa:
                    synth[rec["code"]] = pa
        if not synth:
            return {"ok": False, "summary": "无判定文件且无已发布单元, 拒绝空发布",
                    "stage": "validate"}
        adj = {"subject": code,
               "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "target": {"version": (new_topics or {}).get("syllabus_version")},
               "counts": {}, "papers": {}, "units": synth}
        _recount(adj)
    if new_topics is None:
        nt_path = os.path.join(MIG_DIR, f"{code}_topics_new.json")
        if os.path.exists(nt_path):
            new_topics = json.load(open(nt_path, encoding="utf-8"))
        else:
            new_topics = load_taxonomy(code)   # 正式 topics, 新科目则导入草稿
    problems, n = validate_adjudication(adj, new_topics)
    if problems:
        log(f"  [publish {code}] ⚠ 判定校验 {len(problems)} 项问题, 前 10: {problems[:10]}")
        return {"ok": False, "summary": f"校验失败: {len(problems)} 项",
                "problems": problems, "stage": "validate"}
    units_dir = os.path.join(ROOT, "data", "units", code)
    os.makedirs(units_dir, exist_ok=True)
    formal_files = sorted(f for f in os.listdir(units_dir) if f.endswith(".json"))
    # ---- 临时区构建(不触碰正式文件): 既有正式卷 + 待发布卷 ----
    stage = os.path.join(STAGING_DIR, code)
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(os.path.join(stage, "units"), exist_ok=True)
    cls_papers = []
    staged_units = {}
    changed_units = 0

    def build_paper(pc, rec):
        nonlocal changed_units
        pa = adj["units"].get(pc, {})
        new_units, cls_units = [], []
        for u in rec["units"]:
            e = pa.get(u["id"])
            if not e:
                e = {"status": "PENDING", "reason": "判定缺失(不应发生)",
                     "basis": "missing"}
            nu = dict(u)
            if e["status"] == "IN":
                nu["status"] = "IN"
                nu["topics"] = e["tags"]
                nu.pop("reason", None)
                nu.pop("discounted", None)
                cls_units.append({"id": u["id"], "q": u["q"], "part": u["part"],
                                  "page": u["page"], "marks": u["marks"],
                                  "topics": e["tags"],
                                  "snippet": " ".join((u.get("text") or "").split())[:150]})
            else:
                nu["status"] = e["status"]
                nu["topics"] = None
                nu["reason"] = e.get("reason") or ""
                if e.get("discounted"):
                    nu["discounted"] = True     # 官方作废题: 保留原卷, 不计分
                else:
                    nu.pop("discounted", None)
            if nu != u:
                changed_units += 1
            new_units.append(nu)
        rec["units"] = new_units
        rec["syllabus_version"] = new_topics.get("syllabus_version")
        staged_units[pc] = rec
        cls_papers.append({"code": pc, "year": rec["year"], "session": rec["session"],
                           "paper": rec["paper"], "variant": rec["variant"],
                           "qp_pdf": rec["qp_pdf"], "ms_pdf": rec.get("ms_pdf"),
                           "ms_pages": rec.get("ms_pages") or {},
                           "units": cls_units})

    for f in formal_files:
        pc = f[:-5]
        with open(os.path.join(units_dir, f), encoding="utf-8") as fh:
            build_paper(pc, json.load(fh))
    for pc in pending:                      # 待发布卷来自 workdir, 不经过正式目录
        if pc in {f[:-5] for f in formal_files}:
            continue   # 已提交过(如翻转后崩溃重试/pending_refresh): 从正式文件重建
        build_paper(pc, _pending_unit_record(code, pc))
    cls = {"subject": code,
           "syllabus_version": new_topics.get("syllabus_version"),
           "generated": time.strftime("%Y-%m-%d"),
           "papers": cls_papers}
    n_cls = sum(len(p["units"]) for p in cls_papers)
    if n_cls != n.get("IN", 0):
        shutil.rmtree(stage, ignore_errors=True)
        return {"ok": False, "summary": f"classified IN 数 {n_cls} != 判定 IN {n['IN']}",
                "stage": "build"}
    vproblems = _post_verify_staged(code, staged_units, cls, new_topics)
    if vproblems:
        shutil.rmtree(stage, ignore_errors=True)
        return {"ok": False, "summary": f"临时区复核失败 {len(vproblems)} 项",
                "problems": vproblems, "stage": "stage-verify"}
    if dry:
        shutil.rmtree(stage, ignore_errors=True)
        summary = (f"{code}: IN {n.get('IN', 0)} · OUT {n.get('OUT', 0)} · "
                   f"PENDING {n.get('PENDING', 0)} · CONTEXT {n.get('CONTEXT', 0)} "
                   f"(总 {sum(n.values())}) [dry-run]")
        return {"ok": not vproblems, "summary": summary, "counts": n,
                "problems": vproblems, "stage": "dry-run", "pending": pending}
    if cancel_check:
        cancel_check()                      # 最后的安全取消边界; 之后为提交阶段
    # ---- 候选版本构建 + 单一活动版本切换(P0-3b 第五轮) ----
    # 完整站点快照在 versions/.cand_* 内生成并校验(生成期间正式路径=活动版本
    # 保持旧版); 校验通过后由 live_version.promote 以**一次原子翻转** live
    # 符号链接完成切换 —— 不再逐文件 os.replace, 任何暂停点/崩溃点并发读者
    # 只能看到完整旧版或完整新版(见 live_version.py)。
    import live_version as lv
    lv.ensure_layout(log=log)
    managed_rels = _formal_artifacts(code)
    before_map = _hash_formal(managed_rels)
    first_topics_publish = not os.path.exists(
        os.path.join(ROOT, f"topics_{code}.json"))   # 新科目首次发布(草稿转正)
    cand = None
    try:
        cand = lv.new_candidate(code)
        _prepare_version_dir(code, cand, log=log)
        os.makedirs(os.path.join(cand, "data", "units", code), exist_ok=True)
        atomic_write_json(os.path.join(cand, f"topics_{code}.json"), new_topics)
        for pc, rec in staged_units.items():
            atomic_write_json(os.path.join(cand, "data", "units", code,
                                           f"{pc}.json"), rec)
        atomic_write_json(os.path.join(cand, f"classified_{code}.json"), cls)
        _run_generators_in(code, cand, pending, log=log)
        vproblems = _post_verify_root(code, cand, new_topics, extra_root=ROOT)
        if vproblems:
            raise RuntimeError(f"发布后复核失败: {vproblems[:5]}")
        # 两阶段状态(P0-1/P0-4, 第六轮): 候选校验通过后、翻转前, 先**预留版本
        # ID** 并持久化"预备提交意图"(pending_refresh, 绑定 intended_version):
        # - 不移走 papers —— 翻转前失败/崩溃不改变任何旧卷的已发布状态;
        # - 翻转后、晋升前崩溃: 意图版本==活动版本 → 刷新动作可安全晋升,
        #   待发布也不清零(baseline 吸收不到);
        # - 翻转前失败/崩溃: 意图版本≠活动版本(过期意图) → 刷新动作**不得**
        #   晋升(promote_refresh_pending 严格门禁), 新卷仍在待发布。
        published_now = [pc for pc in staged_units
                         if pc in {f[:-5] for f in formal_files}
                         or pc in pending]
        intent_vid = lv.reserve_vid()
        mark_commit_intent(code, published_now, intent_vid)
        lv.checkpoint("switch:build-done")
        info = lv.promote(cand, code, log=log, vid=intent_vid)  # 单次原子翻转
        cand = None
    except lv.SwitchError as e:
        # 翻转后复核失败已翻回旧版: 正式=完整旧版, 无逐文件残留;
        # 过期意图不晋升, 旧卷 papers 不变
        log(f"  [publish {code}] ⚠ 切换失败({e}); 已恢复完整旧版")
        return {"ok": False,
                "summary": f"发布失败(切换阶段, 已恢复完整旧版): {e}",
                "problems": [str(e)], "stage": "switch",
                "rollback_verified": _rollback_ok(code, before_map)}
    except Exception as e:
        # 生成/校验/翻转前失败: 正式路径从未被触碰, 无需回滚; 清理候选即可。
        # 过期意图保留但不生效(papers 不变, 新卷仍待发布), 下次发布覆盖。
        log(f"  [publish {code}] ⚠ 版本构建失败({e!r}); 正式产物未受影响")
        import traceback as _tb
        _detail = _tb.format_exc()[-800:]
        log(f"  [publish {code}] 构建失败栈: {_detail}")
        if cand:
            lv.discard_candidate(cand, log=log)
        return {"ok": False,
                "summary": f"发布失败(构建阶段, 正式产物未受影响): {e}",
                "problems": [repr(e), _detail], "stage": "build-version",
                "pending_refresh": sorted(effective_refresh_pending(code)),
                "rollback_verified": _rollback_ok(code, before_map)}
    # ---- 切换完成: 登记判定; 索引就绪后才晋升 pending_refresh ----
    os.makedirs(ADJ_DIR, exist_ok=True)
    atomic_write_json(os.path.join(ADJ_DIR, f"{code}.json"), adj)
    # 新科目首次发布成功: 导入草稿已转正, 删除之(已有科目的迁移草稿保留)
    imp = imported_topics_path(code)
    if first_topics_publish and os.path.exists(imp):
        try:
            os.remove(imp)
        except OSError:
            pass
    # ---- 服务端索引刷新(P0-2/P0-4): 翻转后、刷新前对外仍整体呈现旧版
    # (server.SERVED 单一可见版本); 失败显式呈现"正式数据已提交、服务索引
    # 未就绪", 不再假成功; data_version 只在两类索引都安装后才递增 ----
    lv.checkpoint("publish:pre-refresh")
    refresh_errors = _fire_refresh_hooks(code)
    if not refresh_errors and not _REFRESH_HOOKS:
        refresh_errors, server_note = _notify_running_server(log=log)
    else:
        server_note = None
    if refresh_errors:
        lv.checkpoint("publish:refresh-failed")
        return {"ok": False, "stage": "refresh-failed",
                "summary": (f"{code}: 正式数据已提交、服务索引未就绪: "
                            + "; ".join(refresh_errors)[:400]
                            + " — 请在「任务与恢复」点「刷新服务索引」重试"
                              "(待发布不清零, 可安全重试)"),
                "problems": refresh_errors[:10], "counts": n,
                "published": [], "pending_published": pending,
                "pending_refresh": sorted(effective_refresh_pending(code)),
                "changed_units": changed_units,
                "version": info["vid"],
                "index_refresh_errors": refresh_errors}
    promote_refresh_pending(code)           # 索引就绪 → 晋升(意图==活动版本)
    lv.checkpoint("publish:post-refresh")
    summary = (f"{code}: IN {n.get('IN', 0)} · OUT {n.get('OUT', 0)} · "
               f"PENDING {n.get('PENDING', 0)} · CONTEXT {n.get('CONTEXT', 0)} "
               f"(总 {sum(n.values())}) -> topics/classified/units"
               + (f" · 新发布 {len(pending)} 卷" if pending else "")
               + (f" · 版本 {info['vid']}" )
               + ((f" · {server_note}") if server_note else ""))
    return {"ok": True, "summary": summary,
            "problems": [], "counts": n, "published": published_now,
            "pending_published": pending,
            "pending_refresh": [],
            "changed_units": changed_units, "stage": "done",
            "version": info["vid"], "files": info["files"]}


def _notify_running_server(timeout=30, log=print):
    """CLI 进程(无进程内刷新钩子)发布成功后通知本机运行中的服务同步刷新索引。
    返回 (errors, note):
      - 服务刷新成功 → ([], None); 服务明确报错/超时 → ([err], None)
        (按"索引未就绪"处理, 待发布保留 pending_refresh)
      - 本机根本没有服务在监听(连接拒绝) → ([], note): 索引随服务启动重建,
        不存在"正在显示旧数量的服务", 视为就绪。
    环境变量 ZJ_PUBLISH_NOTIFY_SERVER=0 可跳过(测试提速/离线)。"""
    if os.environ.get("ZJ_PUBLISH_NOTIFY_SERVER") == "0":
        return [], None
    import paper_pipeline as pp
    reloaded, ver, err = pp.try_server_reload(timeout=timeout)
    if reloaded:
        log(f"  [publish] 本机服务索引已刷新(data_version={ver})")
        return [], None
    if err is not None and "refused" not in str(err).lower() \
            and "refused" not in str(getattr(err, "reason", "")).lower():
        return [f"server-reload: {err}"], None
    return [], "本机服务未运行; 索引将随服务启动自动构建"



def _post_verify_staged(code, staged_units, cls, new_topics):
    """临时区复核: 状态齐全、IN 标签合法、classified 与 IN 一致、链接存在。"""
    problems = []
    valid = {s["code"] for s in new_topics["sections"]} | \
            {x["code"] for s in new_topics["sections"] for x in s.get("subs", [])}
    in_ids = set()
    cnt = Counter()
    for pc, rec in staged_units.items():
        if not os.path.exists(os.path.join(ROOT, rec["qp_pdf"])):
            problems.append(f"{pc}: qp_pdf 不存在 {rec['qp_pdf']}")
        for u in rec["units"]:
            st = u.get("status")
            if st not in ("IN", "OUT", "PENDING", "CONTEXT"):
                problems.append(f"{pc}#{u['id']}: 缺状态")
                continue
            cnt[st] += 1
            if st == "IN":
                if not u.get("topics") or not set(u["topics"]) <= valid:
                    problems.append(f"{pc}#{u['id']}: IN 标签非法 {u.get('topics')}")
                in_ids.add((pc, u["id"]))
    cls_ids = {(p["code"], u["id"]) for p in cls["papers"] for u in p["units"]}
    if cls_ids != in_ids:
        problems.append(f"classified({len(cls_ids)}) 与 units IN({len(in_ids)}) 不一致")
    return problems


def _post_verify_root(code, root, new_topics=None, extra_root=None):
    """发布后复核(可在指定根目录/版本目录内执行): 状态齐全、IN 标签合法、
    classified 与 IN 一致、链接存在。extra_root: qp_pdf/ms_pdf 允许落在另一
    根(版本目录只携带生成所需文件, PDF 等大文件留在正式根)。"""
    problems = []
    tax = new_topics or json.load(open(os.path.join(root, f"topics_{code}.json"),
                                       encoding="utf-8"))
    valid = {s["code"] for s in tax["sections"]} | \
            {x["code"] for s in tax["sections"] for x in s.get("subs", [])}
    units_dir = os.path.join(root, "data", "units", code)
    in_ids = set()
    cnt = Counter()

    def _asset_ok(rel):
        if os.path.exists(os.path.join(root, rel)):
            return True
        return bool(extra_root and os.path.exists(
            os.path.join(extra_root, rel)))

    for f in sorted(os.listdir(units_dir)):
        if not f.endswith(".json"):
            continue
        rec = json.load(open(os.path.join(units_dir, f), encoding="utf-8"))
        if not _asset_ok(rec["qp_pdf"]):
            problems.append(f"{rec['code']}: qp_pdf 不存在 {rec['qp_pdf']}")
        for u in rec["units"]:
            st = u.get("status")
            if st not in ("IN", "OUT", "PENDING", "CONTEXT"):
                problems.append(f"{rec['code']}#{u['id']}: 缺状态")
                continue
            cnt[st] += 1
            if st == "IN":
                if not u.get("topics") or not set(u["topics"]) <= valid:
                    problems.append(f"{rec['code']}#{u['id']}: IN 标签非法 {u.get('topics')}")
                in_ids.add((rec["code"], u["id"]))
    cls = json.load(open(os.path.join(root, f"classified_{code}.json"),
                         encoding="utf-8"))
    cls_ids = {(p["code"], u["id"]) for p in cls["papers"] for u in p["units"]}
    if cls_ids != in_ids:
        problems.append(f"classified({len(cls_ids)}) 与 units IN({len(in_ids)}) 不一致: "
                        f"missing={list(in_ids - cls_ids)[:5]} extra={list(cls_ids - in_ids)[:5]}")
    return problems


def post_verify(code, new_topics=None):
    """发布后复核(正式根目录)。"""
    return _post_verify_root(code, ROOT, new_topics)


# ======================================================================
# 对外: migrate / adjudicate_new_papers
# ======================================================================
def migrate(code, apply=False, log=print, cancel_check=None):
    if cancel_check:
        cancel_check(stage="adjudicate")
    new_topics, mapping = build_migration_assets(code, log=log)
    adj = adjudicate(code, new_topics, mapping, log=log)
    problems, n = validate_adjudication(adj, new_topics)
    log(f"  [migrate {code}] 判定: {dict(n)} 校验问题 {len(problems)}")
    os.makedirs(ADJ_DIR, exist_ok=True)
    if problems:
        atomic_write_json(os.path.join(ADJ_DIR, f"{code}.json"), adj)
        return {"ok": False, "summary": f"校验失败 {len(problems)} 项(判定已存审记)",
                "problems": problems}
    if not apply:
        atomic_write_json(os.path.join(ADJ_DIR, f"{code}.json"), adj)
        return {"ok": True, "summary": f"[dry-run] {n}"}
    if cancel_check:
        cancel_check(stage="publish")
    return publish(code, adj, new_topics, log=log, cancel_check=cancel_check)


def adjudicate_new_papers(code, log=print):
    """sync 路径: 对真正**待发布**的卷做判定(只写判定审计, 不触碰正式数据)。

    待发布 = workdir 有 QP JSON 且 (未登记 published) 且 (data/units 无该卷或
    该卷无正式 status) —— 双保险防止既有正式卷(如 9709 的 862 卷)被当新卷
    重判/改写(P0-1)。新卷的 unit 记录与定位都延迟到 publish 事务内。
    """
    work = os.path.join(PIPE, "work", code, "units")
    units_dir = os.path.join(ROOT, "data", "units", code)
    new_pcs = pending_papers(code, log=log)
    if not new_pcs:
        return {"ok": True, "summary": {"in": 0, "out": 0, "pending": 0,
                                        "context": 0},
                "new_papers": []}
    tax = load_taxonomy(code)
    kw_by_code = {}
    for s in tax["sections"]:
        kw_by_code.setdefault(s["code"], [s["en"]] + s.get("kw", []))
        for x in s.get("subs", []):
            kw_by_code[x["code"]] = [x["en"]] + x.get("kw", [])
    # 锚点 = 当前已发布 IN 单元(新科目首发布时 data/units 尚不存在 → 空锚点,
    # 判定完全依据 tags_<code>/ 的 AI 草稿; 无草稿的单元走自动低置信 → PENDING)
    anchors = []
    if not os.path.isdir(units_dir):
        os.makedirs(units_dir, exist_ok=True)     # 目录可建, 但不写卷文件
    for f in sorted(os.listdir(units_dir)):
        if not f.endswith(".json"):
            continue
        with open(os.path.join(units_dir, f), encoding="utf-8") as fh:
            rec = json.load(fh)
        for u in rec["units"]:
            if u.get("status") == "IN" and u.get("topics"):
                anchors.append((u["text"], u["topics"]))
    judge = Judge(anchors, kw_by_code, kw_weight=2.0)
    # AI 草稿优先: tags_<code>/<paper>.json 存在则用之, 否则自动判定(候选)
    tags_dir = os.path.join(ROOT, f"tags_{code}")
    adj = json.load(open(os.path.join(ADJ_DIR, f"{code}.json"), encoding="utf-8")) \
        if os.path.exists(os.path.join(ADJ_DIR, f"{code}.json")) else \
        {"units": {}, "counts": {}, "papers": {}}
    for pc in new_pcs:
        with open(os.path.join(work, f"{pc}_qp.json"), encoding="utf-8") as fh:
            rec = json.load(fh)
        draft_path = os.path.join(tags_dir, pc + ".json")
        draft = None
        if os.path.exists(draft_path):
            try:
                with open(draft_path, encoding="utf-8") as fh:
                    draft = json.load(fh).get("tags")
            except Exception:
                draft = None
        paper_adj = {}
        for u in rec["units"]:
            if draft is not None and u["id"] in draft:
                kind, payload, note = parse_tag_value(draft[u["id"]])
                if kind == "IN":
                    paper_adj[u["id"]] = {"status": "IN", "tags": payload,
                                          "basis": "ai-draft",
                                          "reason": note or "AI 草稿判定 IN(SOP 复核)"}
                elif kind == "OUT":
                    paper_adj[u["id"]] = {"status": "OUT",
                                          "reason": note or "AI 判定考纲外",
                                          "basis": "ai-out"}
                elif kind == "CONTEXT":
                    paper_adj[u["id"]] = {"status": "CONTEXT",
                                          "reason": note or "无分值共享材料",
                                          "basis": "ai-context"}
                elif kind == "DISCOUNTED":
                    paper_adj[u["id"]] = {"status": "PENDING", "discounted": True,
                                          "reason": note or "官方 MS 标注作废",
                                          "basis": "ai-discounted"}
                else:
                    paper_adj[u["id"]] = {"status": "PENDING",
                                          "reason": note or "AI 判定证据不足",
                                          "basis": "ai-pending"}
                continue
            codes, conf, det = judge.judge(u["text"])
            kw_strong = (det or {}).get("kw", 0) >= 15 or (
                (det or {}).get("kw", 0) >= 8 and
                (det or {}).get("best", 0) >= 0.08)
            # P0-2(2026-11-07): 新卷自动关键词/近邻结果**只作候选**, 不得直接 IN。
            # 与旧 OUT 复核同口径: 词汇命中不能证明符合最新大纲。强命中记为
            # 候选标签, 状态一律 PENDING 待复核(网页/AI 草稿可复核转 IN);
            # 无信号同样 PENDING。低相似度也不自动 OUT。
            cand = codes[:3] if (codes and (conf >= 0.66 or kw_strong)) else []
            paper_adj[u["id"]] = {
                "status": "PENDING",
                "reason": (f"新卷自动判定仅候选(conf={conf:.2f}"
                           + (f", 候选标签 {cand}" if cand else "")
                           + "); 无官方学习目标/实操技能依据, 待人工或 AI 草稿复核"),
                "basis": "auto-candidate" if cand else "auto-low",
                "conf": round(conf, 2)}
            if cand:
                paper_adj[u["id"]]["candidate_tags"] = cand
        adj["units"][pc] = paper_adj
        c = Counter(e["status"] for e in paper_adj.values())
        adj["papers"][pc] = {"in": c["IN"], "out": c["OUT"],
                             "pending": c["PENDING"], "context": c["CONTEXT"]}
        log(f"  [new {pc}] IN {c['IN']} OUT {c['OUT']} PENDING {c['PENDING']}")
    apply_overrides(code, adj, log=log)
    _recount(adj)
    adj["generated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    problems, _ = validate_adjudication(adj, tax)
    os.makedirs(ADJ_DIR, exist_ok=True)
    atomic_write_json(os.path.join(ADJ_DIR, f"{code}.json"), adj)
    return {"ok": not problems, "summary": adj["counts"],
            "new_papers": new_pcs, "problems": problems}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    res = migrate(a.code, apply=a.apply)
    print(res["summary"])
    if res.get("problems"):
        for p in res["problems"][:20]:
            print("  -", p)
    sys.exit(0 if res["ok"] else 2)
