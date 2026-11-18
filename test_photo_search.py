#!/usr/bin/env python3
"""test_photo_search.py —— 照片检索链路测试(模拟照片, 非实拍)。

方法(全部标注为"模拟"):
  1. 分层抽样若干单元(四科 × 选择题/主观题/公式/图表/实践卷);
  2. 用其 QP 定位 seg 从原 PDF 渲染裁图, 施加 手机照片式退化
     (缩至 ~1000px 宽、JPEG q=0.72、±1.2° 旋转) —— 模拟拍照, 非真实手机拍摄;
  3. POST /api/photo 由本机 Vision OCR 识别 → POST /api/search 检索;
  4. 统计期望单元是否出现在 top-1 / top-3。
用法(需 server.py 已在本机运行):
  /usr/bin/python3 test_photo_search.py [样本数, 默认 24]
"""
import io, json, os, random, sys, urllib.request

import fitz

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:8930"
random.seed(20261029)

SAMPLES = [   # (科目, 卷代码, 单元ID, 说明) —— 覆盖选择题/主观题/公式/图表/实践卷
    ("9702", "w23_11", "15", "物理 MCQ"),
    ("9702", "w23_11", "33", "物理 MCQ(核物理)"),
    ("9702", "w23_21", "1a", "物理主观 共享题干"),
    ("9702", "w23_21", "3b", "物理主观"),
    ("9702", "w23_41", "9c", "物理 A2 跨小问"),
    ("9702", "w23_33", "2c", "物理实践卷"),
    ("9701", "w23_11", "10", "化学 MCQ"),
    ("9701", "w23_21", "2b", "化学主观"),
    ("9701", "w23_41", "1a", "化学 A2"),
    ("9701", "w23_33", "1a", "化学实践"),
    ("9709", "w23_11", "1b", "数学 P1"),
    ("9709", "w23_21", "3a", "数学 P2 公式题"),
    ("9709", "w23_31", "6", "数学 P3"),
    ("9709", "w23_41", "4", "数学 M1 力学"),
    ("9709", "w23_51", "2", "数学 S1 统计"),
    ("9618", "w23_11", "5", "CS 理论"),
    ("9618", "w23_21", "1a", "CS 伪代码"),
    ("9618", "w23_31", "2a", "CS A2"),
    ("9702", "s24_34", "1a", "物理实践 2024"),
    ("9701", "s23_22", "3b", "化学 2023"),
    ("9709", "s23_32", "4", "数学 2023 P3"),
    ("9618", "s24_31", "2", "CS 2024"),
    ("9702", "w24_12", "21", "物理 MCQ 2024"),
    ("9701", "w24_41", "5b", "化学 A2 2024"),
]


def degraded_photo(pdf_rel, segs):
    """按 seg 渲染裁图并施加模拟拍照退化, 返回 JPEG bytes。"""
    doc = fitz.open(os.path.join(ROOT, pdf_rel))
    pngs = []
    for seg in segs:
        pno, x0, y0, x1, y1 = int(seg[0]) - 1, *seg[1:5]
        rect = fitz.Rect(x0, y0, x1, y1) & doc[pno].rect
        if rect.is_empty:
            continue
        zoom = 1000.0 / max(1.0, rect.width)    # 目标 ~1000px 宽
        mat = fitz.Matrix(zoom, zoom) * fitz.Matrix(
            random.uniform(-0.021, 0.021))      # ≈±1.2° 旋转
        pix = doc[pno].get_pixmap(matrix=mat, clip=rect)
        pngs.append(pix.tobytes("png"))
    doc.close()
    if not pngs:
        return None
    # 单段直接编码 JPEG; 多段垂直拼接(空 PDF 页嵌图)
    imgs = [fitz.open(stream=p, filetype="png") for p in pngs]
    W = max(im[0].rect.width for im in imgs)
    H = sum(im[0].rect.height for im in imgs) + 12 * (len(imgs) - 1)
    outdoc = fitz.open()
    page = outdoc.new_page(width=W, height=H)
    y = 0
    for im in imgs:
        r = im[0].rect
        page.insert_image(fitz.Rect(0, y, r.width, y + r.height),
                          pixmap=im[0].get_pixmap())
        y += r.height + 12
        im.close()
    pix = page.get_pixmap(dpi=110)
    data = pix.tobytes("png")
    outdoc.close()
    # PNG → JPEG 压缩(模拟手机照片); 经文档渲染确保无 alpha 通道
    pdoc = fitz.open(stream=data, filetype="png")
    pm = pdoc[0].get_pixmap(dpi=96)
    jpg = pm.tobytes("jpeg", jpg_quality=72)
    pdoc.close()
    return jpg


def post(url, data, ctype):
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def main():
    n_top1 = n_top3 = n_total = 0
    rows = []
    for subject, code, uid, desc in SAMPLES:
        rec = json.load(open(os.path.join(ROOT, "data", "units", subject, code + ".json")))
        loc = json.load(open(os.path.join(ROOT, "data", "loc", subject, code + ".json")))
        u = next((x for x in rec["units"] if x["id"] == uid), None)
        if u is None:
            continue
        ent = (loc.get("units") or {}).get(uid) or (loc.get("questions") or {}).get(str(u["q"]))
        if not ent or not ent.get("seg"):
            rows.append((subject, code, uid, desc, "-", "-", "无定位"))
            continue
        jpg = degraded_photo(rec["qp_pdf"], ent["seg"])
        if not jpg:
            rows.append((subject, code, uid, desc, "-", "-", "渲染失败"))
            continue
        try:
            ocr = post(f"{BASE}/api/photo", jpg, "image/jpeg")
        except Exception as e:
            rows.append((subject, code, uid, desc, "-", "-", f"OCR失败:{e}"))
            continue
        text = (ocr.get("text") or "").strip()
        if len(text) < 10:
            rows.append((subject, code, uid, desc, "-", "no", f"OCR文字不足({len(text)}字符)"))
            continue
        try:
            res = post(f"{BASE}/api/search", json.dumps({"q": text[:1500]}).encode(),
                       "application/json")
        except Exception as e:
            rows.append((subject, code, uid, desc, "-", "-", f"搜索失败:{e}"))
            continue
        results = res.get("results") or []
        ids = [f"{r['subject']}/{r['code']}#{r['uid']}" for r in results[:3]]
        expect = f"{subject}/{code}#{uid}"
        hit1 = "top1" if ids and ids[0] == expect else "-"
        hit3 = "top3" if expect in ids else "-"
        n_total += 1
        n_top1 += hit1 == "top1"
        n_top3 += hit3 == "top3"
        rows.append((subject, code, uid, desc, hit1, hit3,
                     f"{len(text)}字符 | top3: {'; '.join(ids) if ids else '无'}"))
    print(f"{'科目':<6}{'卷':<10}{'题':<5}{'类型':<14}{'top1':<6}{'top3':<6}备注")
    for r in rows:
        print(f"{r[0]:<6}{r[1]:<10}{r[2]:<5}{r[3]:<14}{r[4]:<6}{r[5]:<6}{r[6]}")
    print(f"\n模拟照片(非实拍): 样本 {n_total} · top-1 命中 {n_top1} · top-3 命中 {n_top3}")
    with open(os.path.join(ROOT, "data", "photo_test.md"), "w") as fh:
        fh.write("# 照片检索测试（模拟照片，非实拍手机测试）\n\n")
        fh.write("| 科目 | 卷 | 题 | 类型 | top-1 | top-3 | 备注 |\n|---|---|---|---|---|---|---|\n")
        for r in rows:
            fh.write(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} |\n")
        fh.write(f"\n样本 {n_total} · top-1 命中 {n_top1} · top-3 命中 {n_top3}\n")
    print("-> data/photo_test.md")


if __name__ == "__main__":
    main()
