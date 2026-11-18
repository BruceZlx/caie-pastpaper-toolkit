# CAIE 真卷工具箱（caie-pastpaper-toolkit）

剑桥 CAIE（Cambridge International）历年真题库的**全套后端核心工具**：整卷批量下载 →
切题与知识点分类 → 题库管理（增量同步 / 新科目接入 / 大纲版本迁移 / 原子发布）→
全文搜题 / 拍照搜题 API → 真题模拟组卷（精确总分 + 官方答案同步）。

本仓库只包含**核心功能脚本**（Python / Shell / Swift 源码），**不包含**：
前端资源（`assets/` 工作台 SPA）、任何试卷 PDF、题库数据与 AI 分类结果。
克隆后把本仓库当作你自己真题库的根目录即可从零建库。

> ⚠️ 版权提示：本工具仅下载/整理来源站已公开的试卷供**个人学习研究**使用，
> 请遵守来源站（PapaCambridge 等）使用条款与 CAIE 版权政策，请勿再分发试卷文件。

---

## 目录

1. [功能总览](#功能总览)
2. [环境要求](#环境要求)
3. [快速开始：从零建一座自己的题库](#快速开始从零建一座自己的题库)
4. [模块 ①：整卷批量下载（pp_download.sh）](#模块-整卷批量下载)
5. [模块 ②：题库管理管线（paper_pipeline / pipeline_service / live_version / migrate_syllabus / persist_units）](#模块-题库管理管线)
6. [模块 ③：切题与坐标定位（extract_questions / locate_qp / locate_ms）](#模块-切题与坐标定位)
7. [模块 ④：知识点分类（gen_workfiles / merge_tags / classify_auto）](#模块-知识点分类)
8. [模块 ⑤：搜题服务（server.py / gen_search.py / bin/ocr_swift.swift）](#模块-搜题服务)
9. [模块 ⑥：真题模拟组卷（mock_exam.py）](#模块-真题模拟组卷)
10. [模块 ⑦：校验与报告（verify_data / report_coverage）](#模块-校验与报告)
11. [模块 ⑧：站点索引生成器（gen_index / gen_topics，发布流程依赖）](#模块-站点索引生成器)
12. [模块 ⑨：测试（test_*.py）](#模块-测试)
13. [数据目录约定](#数据目录约定)
14. [常见问题](#常见问题)

---

## 功能总览

| 功能 | 入口脚本 | 说明 |
|---|---|---|
| 整卷下载 | `pp_download.sh` | PapaCambridge 直链下载 → 按年份/考季自动归档，带重试与 `%PDF` 魔数校验 |
| 题库管理 | `paper_pipeline.py` 等 | 增量同步新卷、新科目接入、大纲版本管理与迁移、原子发布、运行报告 |
| 切题定位 | `extract_questions.py` / `locate_qp.py` / `locate_ms.py` | PDF → 每题文本单元；QP/MS 裁图坐标定位 |
| 知识点分类 | `gen_workfiles.py` / `merge_tags.py` / `classify_auto.py` | AI 子代理分类工作流 + 纯算法自动分类兜底 |
| 搜题 | `server.py`（`/api/search` 等） | 服务端 BM25 全文搜题、题目详情、QP/MS 裁图、拍照 OCR 搜题 |
| 组卷 | `mock_exam.py` | 以完整大题为单位、总分**精确等于**目标分的抽题；同步官方答案 PDF；可 CLI 直出 |
| 校验报告 | `verify_data.py` / `report_coverage.py` | 数据一致性、坐标边界、裁图内容全量自检；覆盖率报告 |
| 站点生成 | `gen_index.py` / `gen_topics.py` / `gen_search.py` | 发布流程所需的索引数据/页面生成器 |

所有 Python 脚本互相以平级模块引用（`import mock_exam` 等），**必须在仓库根目录下运行**，
不要把脚本挪进子目录。除特别说明外，脚本一律以「本脚本所在目录」为根目录（ROOT），
因此本仓库目录就是你的题库根目录。

## 环境要求

- macOS（OCR 与 `/usr/bin/python3` 相关路径按 macOS 设计；纯管线部分其他平台亦可运行）
- Python 3（macOS 自带 `/usr/bin/python3` 即可）+ **PyMuPDF**：
  ```bash
  /usr/bin/python3 -c "import fitz"          # 应打印版本号
  # 缺失则: /usr/bin/python3 -m pip install --user pymupdf
  ```
- OCR（拍照搜题，可选）：Xcode Command Line Tools：
  ```bash
  swiftc -O -o bin/ocr bin/ocr_swift.swift   # 编译 macOS Vision 本机 OCR
  ```
- 不使用任何外部 AI / 网络 API（分类由 AI 子代理离线完成，OCR 走本机 Vision）。

## 快速开始：从零建一座自己的题库

```bash
git clone https://github.com/<你的用户名>/caie-pastpaper-toolkit.git 我的题库
cd 我的题库

# 1) 接入一个新科目（以 9709 数学为例；先 dry-run 预览，确认后 --apply）
/usr/bin/python3 paper_pipeline.py onboard --code 9709 --years 2001-2026 --dry-run \
    --zh 数学 --name Mathematics \
    --url https://www.cambridgeinternational.org/programmes-and-qualifications/cambridge-international-as-and-a-level-mathematics-9709/
/usr/bin/python3 paper_pipeline.py onboard --code 9709 --years 2001-2026 --apply …

# 2) 登记官方 syllabus（下载 + 记录版本/哈希）
/usr/bin/python3 paper_pipeline.py syllabus --code 9709 --apply

# 3) 制作该科 topics_9709.json（知识点大纲树，见 docs/TOPICS_WORKFLOW.md §2）

# 4) 切题 + 生成 AI 分类输入 → AI 按标签判定 → 合并 → 校验发布
/usr/bin/python3 paper_pipeline.py extract --code 9709 --apply
/usr/bin/python3 paper_pipeline.py classify-prep --code 9709
#    …AI 按 docs/SYLLABUS_TOPIC_AI_SOP.md 写 tags_9709/<卷>.json…
/usr/bin/python3 paper_pipeline.py publish --code 9709 --apply

# 5) 坐标定位 + 校验 + 覆盖报告
/usr/bin/python3 locate_qp.py 9709
/usr/bin/python3 locate_ms.py 9709
/usr/bin/python3 verify_data.py
/usr/bin/python3 report_coverage.py

# 6) 起服务：搜题 / 组卷 / 题库管理全在 API 里
/usr/bin/python3 server.py            # http://127.0.0.1:8930
```

更详细的操作手册见 `docs/`：

| 文档 | 内容 |
|---|---|
| `docs/PIPELINE_USAGE.md` | 题库管理管线完整手册（同步/接入/大纲迁移/发布/版本回收） |
| `docs/TOPICS_WORKFLOW.md` | 知识点分类真题库完整流程手册（切题→分类→合并→生成→验收） |
| `docs/SYLLABUS_TOPIC_AI_SOP.md` | AI 子代理逐单元知识点判定的 SOP（分类提示词规范） |

---

## 模块 ①：整卷批量下载

### `pp_download.sh` —— 通用单文件下载器

从 PapaCambridge 直链下载一份试卷/评分标准，按文件名里的会话码自动归档到
`<根目录>/<年份>/<Feb-Mar|May-Jun|Oct-Nov>/原文件名`。带浏览器 UA，最多重试 5 次，
校验非空且以 `%PDF` 魔数开头，失败自动删除并报 `FAIL`。

```bash
./pp_download.sh <PDF直链> <归档根目录>
# 例:
./pp_download.sh "https://pastpapers.papacambridge.com/directories/CAIE/CAIE-pastpapers/upload/9702_w24_qp_11.pdf" "./9702（物理）"
```

批量并行（配合 §2 手工抓取的直链清单 `/tmp/all_pdfs.txt`）：

```bash
ROOT="$PWD/9702（物理）"
sed "s|\$| $ROOT|" /tmp/all_pdfs.txt | xargs -P 6 -n 2 ./pp_download.sh > /tmp/download_log.txt 2>&1
grep -c '^OK' /tmp/download_log.txt; grep '^FAIL' /tmp/download_log.txt
```

> 新科目推荐直接用模块② 的 `paper_pipeline.py onboard/sync`（自动发现会话页、
> HEAD 估算、验真、增量、隔离区、报告一体）；`pp_download.sh` 适合手工补下零散文件。
> 注意来源站对缺失文件可能返回 `200 + HTML 软 404`，一切以 `%PDF` 魔数为准。

**直链清单怎么来**（手工路线，详细步骤见 `docs/TOPICS_WORKFLOW.md` 与原手册）：
抓科目主页 → 提取会话 slug（排除 solved/topical）→ 并行抓会话页 → 正则提取
`.../upload/<科目>_<会话>_qp|ms_<variant>.pdf` 直链去重。

---

## 模块 ②：题库管理管线

统一 CLI：`/usr/bin/python3 paper_pipeline.py <子命令> …`（**在仓库根目录运行**）。
默认 **dry-run**（只读预览，不写任何文件）；`--apply` 才实际执行。
退出码：`0` 成功、`2` 部分失败（见报告 `failures`）、`3` 用法/致命错误。

### `paper_pipeline.py` 子命令一览

```bash
# 1. 已有科目同步最新已发布试卷
/usr/bin/python3 paper_pipeline.py sync --code 9709 --latest --dry-run   # 预览
/usr/bin/python3 paper_pipeline.py sync --code 9709 --latest --apply     # 执行
/usr/bin/python3 paper_pipeline.py sync --code 9709 --years 2025-2026 --apply
/usr/bin/python3 paper_pipeline.py sync --code 9702 --latest --apply --source-url <科目页URL>
/usr/bin/python3 paper_pipeline.py sync --code 9702 --latest --apply --source-list 直链清单.txt

# 2. 新科目接入（登记进 subjects.json + 下载建库）
/usr/bin/python3 paper_pipeline.py onboard --code 9708 --years 2025 --dry-run \
    --zh 经济学 --name Economics --url <Cambridge官方科目页> --source-url <PapaCambridge科目页>
/usr/bin/python3 paper_pipeline.py onboard --code 9708 --years 2025 --apply …

# 3. 大纲版本管理
/usr/bin/python3 paper_pipeline.py syllabus --code 9701            # 只查不写
/usr/bin/python3 paper_pipeline.py syllabus --code 9701 --apply    # 下载+登记版本/哈希
/usr/bin/python3 paper_pipeline.py migrate --code 9702 --apply     # 大纲变更后迁移分类

# 4. 切题 / 分类输入 / 发布
/usr/bin/python3 paper_pipeline.py extract --code 9709 --apply [--papers s25_11,…]
/usr/bin/python3 paper_pipeline.py classify-prep --code 9709
/usr/bin/python3 paper_pipeline.py publish --code 9709 --apply     # 校验→快照→发布→重建索引

# 5. 状态与报告
/usr/bin/python3 paper_pipeline.py status --code 9709
/usr/bin/python3 paper_pipeline.py report --code 9709
```

行为要点：

- `sync`：按 PDF 文件名核对科目/年份/考季/Paper/variant/类型（排除 gt/er/ci，接受 `qp_et`），
  HEAD 估算体积，已有有效文件跳过，`.part` 下载 → `%PDF` + PyMuPDF 验真 → 原子入库；
  同名内容变化进隔离区；apply 后自动增量切题 → IN/OUT/PENDING 判定 → 校验通过才发布。
  官方大纲有更新时**停止发布**并提示先 `migrate`。重复 apply 幂等（无新增零下载零改写）。
- `publish`：先快照到 `data/pipeline/snapshots/<时间戳>_<科目>/`，校验通过后原子写入，
  一次 `os.replace` 翻转 `data/pipeline/live` 符号链接（整站版本切换，读者任何时刻只能
  看到完整旧版或完整新版）。历史版本保留最近 2 个，硬链共享数据零拷贝回收。
- 运行报告：`data/pipeline/runs/<时间戳>_<子命令>_<科目>.json`。

### `pipeline_service.py` —— 题库管理业务层

被 `paper_pipeline.py` CLI 与 `server.py` 的 `/api/pipeline/*` 网页管理接口**共用**的
业务层（任务队列、取消信号、审计、状态机）。一般不直接运行；`server.py` 启动时自动加载。
长任务后台串行执行并持久化在 `data/pipeline/tasks/`，服务重启后 `running → interrupted`
可重试；同参数任务执行中去重拒绝。

### `live_version.py` —— 单一活动版本入口

发布产物整站版本化的核心库：维护 `data/pipeline/versions/<vid>/` 与 `data/pipeline/live`
符号链接、根目录受管路径（`index.html`、`*_data_*.js`、`data/units|loc|msloc` 等）的
稳定链接、`<vid>.retain`（正被服务使用，回收保护）与 `<vid>.retired`（退役 + GC 宽限期，
默认 600s，环境变量 `ZJ_VERSION_GC_GRACE` 可调）。由各脚本 import，无 CLI。

### `migrate_syllabus.py` —— 大纲迁移

`paper_pipeline.py migrate` 的底层实现，也可独立运行：

```bash
/usr/bin/python3 migrate_syllabus.py 9702          # dry-run（判定写审计）
/usr/bin/python3 migrate_syllabus.py 9702 --apply  # 快照→发布→重建
```

旧标签按**内容**映射到新大纲（同编号标题/学习目标对核），映射不上的按题面重判
（近邻 + 关键词）：强信号 → IN、旧主题确认移除 → OUT（记理由）、其余 → PENDING；
映射与证据落盘 `data/pipeline/migration/<科目>_map.json`。

### `persist_units.py` —— 切题产物持久化

把切题工作目录（`data/pipeline/work/<科目>/units`）持久化到正式 `data/units/`，含对账：

```bash
/usr/bin/python3 persist_units.py            # 全部科目
/usr/bin/python3 persist_units.py 9701 9702  # 指定科目
```

### `subjects.json` —— 科目登记表

`paper_pipeline.py` 的唯一科目清单（代码 / 中英文名 / 数据目录 / 来源 slug / 官方科目页 /
卷名表）。`onboard` 会写入；手工编辑时保持结构一致。已内置 9618 / 9701 / 9702 / 9708 / 9709。

---

## 模块 ③：切题与坐标定位

### `extract_questions.py` —— PDF → 题目单元

对 `<科目>/<年份>/<会话>/` 下每份 `*_qp_*.pdf` 产出 `<卷代码>_qp.json`（题目单元：
题号 + 小问粒度、页码、marks、全文），`*_ms_*.pdf` 产出 `<卷代码>_ms.json`（题号 → MS
起始页码）。切分要点（行级坐标、y 2pt 分桶排序、题号三重约束、小问 x 坐标约束等）
已固化在脚本里，换科目排版异常时先读脚本头注释。

```bash
python3 extract_questions.py <科目代码> <试卷根目录> <输出目录> [年份范围]
# 例:
python3 extract_questions.py 9709 "$PWD/9709（数学）" data/pipeline/work/9709/units
python3 extract_questions.py 9709 "$PWD/9709（数学）" data/pipeline/work/9709/units 2020-2026
```

> 日常建议走 `paper_pipeline.py extract --code <科目> --apply`（自动选工作目录并接发布流程）；
> 本脚本适合独立调试/指定年份范围。单元 id 规则：`<科目>/<卷代码>#<单元ID>`。

### `locate_qp.py` —— QP 裁图坐标

为每卷每单元在原 QP PDF 上定位裁切段（`exact` / `fuzzy` / `fail` 三态），写入 `data/loc/<科目>/`。
供题目详情页与组卷渲染 QP 原卷裁图（含共享题干、跨页拼段）。

```bash
/usr/bin/python3 locate_qp.py            # 全部已切科目（动态发现）
/usr/bin/python3 locate_qp.py 9702 9709  # 指定科目
```

### `locate_ms.py` —— MS 答案坐标

在 MS PDF 上定位每小问答案区域（`part` / `question` / `letter`(MCQ 官方字母) /
`not_found` 四态），写入 `data/msloc/<科目>/`。小问区域 = 该字母首行标 → 下一**不同字母**
行标（同字母罗马子问整段涵盖，含跨页）；MS 旋转页在 `remove_rotation()` 后的坐标系定位
（渲染/校验侧必须同样先归一，否则裁到页眉）。

```bash
/usr/bin/python3 locate_ms.py                 # 默认 9618/9701/9702/9709
/usr/bin/python3 locate_ms.py 9701 9702       # 指定科目
```

---

## 模块 ④：知识点分类

### `gen_workfiles.py` —— 生成 AI 分类输入

```bash
python3 gen_workfiles.py <科目代码> <units目录> <输出目录>
# 例: python3 gen_workfiles.py 9709 data/pipeline/work/9709/units data/pipeline/work/9709/classify
# 产出 TOPICS.txt（大纲+分类守则）与每卷 <卷代码>.txt（每行 "[id|p页|m分] 题面(截500字)"）
```

然后按 `docs/SYLLABUS_TOPIC_AI_SOP.md` 派发 AI 子代理判定，输出写
`tags_<科目>/<卷代码>.json`：`{"code":"<卷>","tags":{"<单元id>":["小节码",…]|["OUT",理由]|["PENDING",问题]}}`。
tags 目录**必须存在仓库内**（`/tmp` 会被系统清空；丢了可由 classified 反向恢复）。

### `merge_tags.py` —— 合并 + 归一化 + 校验

```bash
python3 merge_tags.py <科目> <units目录> <tags目录> [根目录]
# 例: python3 merge_tags.py 9709 data/pipeline/work/9709/units tags_9709
# 输出 classified_9709.json（每卷每题 → 标签+页码+摘要）+ 大纲外剔除清单
```

自动校验：tags 覆盖每个单元、代码全部合法（对照 topics json）、非法/缺失逐条打印。
**归一化规则按科目写死在脚本 `normalize()` 内**（如 9709 的 P2/P3 小节同名跟卷走、
P1 降级映射、旧卷号 4/5/6/7 → M1/M2/S1/S2），换科目须按该科 syllabus 重写。

### `classify_auto.py` —— 纯算法自动分类（兜底/补漏）

不派子代理，给 `tags_<科目>/` 缺失的卷补写 tags：近邻投票（IDF-cosine）+ 关键词 +
结构先验 + OUT 负锚点。分词三件套 `STOP/stem/tokenize` 与 `gen_search.py` 逐字一致
（改一处必须同步另一处）。

```bash
python3 classify_auto.py <科目> --eval          # hold-out 自评（不写任何文件）
python3 classify_auto.py <科目> [--dry] [--limit N] [--since 年份]
# --dry 只出报告不写; 报告: auto_classify_report_<科目>.md/.json
```

---

## 模块 ⑤：搜题服务

### `server.py` —— 本机 / LAN 一站式 API 服务

```bash
/usr/bin/python3 server.py                     # 本机 http://127.0.0.1:8930
/usr/bin/python3 server.py --lan               # 0.0.0.0:8930, 打印局域网 IP（手机可访问）
/usr/bin/python3 server.py --lan --port 9000   # 自定义端口
```

默认只绑 `127.0.0.1`；**只有显式 `--lan` 才开放局域网**，且管理写接口仍仅限本机
（127.0.0.1）。静态文件按白名单放行（页面/数据JS/notes/syllabus/各科 QP·MS PDF），
路径解析后必须落在根目录内（防穿越，realpath 拦截符号链接越界）；PDF 支持 Range。

主要 API：

| API | 说明 |
|---|---|
| `GET /api/health` | 健康检查，含当前对外服务版本 `served_version` |
| `GET /api/papers` | 整卷目录数据 |
| `GET /api/search?q=…&subject=…` | 服务端全文搜题（过滤 OUT/PENDING 单元） |
| `GET /api/unit?id=…` | 题目详情（单元全文、标签、QP/MS 链接） |
| `GET /api/crop?…` | QP/MS 裁图 PNG（缓存按源 PDF mtime+size 自动失效） |
| `POST /api/photo` | 拍照搜题：上传图片（≤15MB，jpeg/png/webp/heic 魔数校验）→ 本机 Vision OCR → 全库检索；照片 OCR 后立即删除 |
| `GET /api/mock/*` | 模拟组卷（见模块⑥） |
| `GET/POST /api/pipeline/*` | 题库管理接口（overview/tasks/runs/adjudication/classify…；任务动作白名单 preview_sync/sync/onboard/extract/publish/migrate/refresh_index 等） |
| `GET /api/pipeline/version` | 数据版本号；发布后自动 +1，前端轮询自动刷新 |

发布后**同一运行中的服务会自动后台重建并原子替换**搜题与组卷索引，无需重启；
`publish` 翻转 live 链接后、双索引重建完成前对外整体保持旧版。

### `gen_search.py` —— 客户端倒排索引构建

为存在 `classified_<代码>.json` 的科目生成 `search_data_<代码>.js`（分词 → 轻量词干化 →
倒排表 gap 编码 + 词频，整包 zlib 压缩内嵌；查询期 BM25 + 词表二分前缀扩展 + 短语加成，
检索代价与题库总量无关）。这是**发布站点自带的离线搜题数据**；纯 API 用法不需要它。

```bash
python3 gen_search.py                  # 全部科目
python3 gen_search.py . 9702           # 指定根目录与科目
# classified 更新(merge_tags 重跑)后必须重跑本脚本, 再重跑 gen_index.py 刷新主页
```

### `bin/ocr_swift.swift` —— macOS Vision 本机 OCR

`server.py` 拍照搜题的 OCR 后端（不联网、不调 AI 服务）：

```bash
swiftc -O -o bin/ocr bin/ocr_swift.swift    # 需 Xcode Command Line Tools
bin/ocr <图片路径>                           # 输出识别文本
```

---

## 模块 ⑥：真题模拟组卷

### `mock_exam.py` —— 组卷核心（核分 / 精确总分 / 官方答案 / PDF / ZIP）

功能：选一个科目 + Paper 编号多选（试卷类型，非 variant）+ 大纲章节多选 + 年份起止 +
目标总分，从本地真题**重新核分**、以**完整大题**为单位、在**实际总分精确等于目标分值**
约束下抽题；「试卷+答案」模式逐题核验官方 MS 可用性后，可分别下载同一冻结选题的
试题 PDF 与官方答案 PDF（或一键 ZIP）。同参数同种子可复现。

**CLI 直接出一套样卷**：

```bash
/usr/bin/python3 mock_exam.py <科目> <Papers> <年起> <年止> <目标分> --topics <章节> [选项]
# 例:
/usr/bin/python3 mock_exam.py 9702 1,2 2016 2026 40 --topics 3,4 --seed demo --require-each
# 选项: --topics 逗号分隔章节/小节(如 7,8.4, 必填)  --seed 种子(默认 cli)
#       --out 输出前缀  --require-each 每种 Paper 至少一题  --exam-only 仅试题(无答案)
```

核分与求和要点：

- **不信任**单元里的 `marks`，对整道大题重新核分：`[Total: N]` 优先 → 求和给分标记 `[n]`
  （`Data[1]` 类数组下标不算）→ 无标记且题数 ≥20 的 MCQ 卷每题 1 分。
- 保守排除：无法核分 / 未分类或含 OUT 单元 / 题面定位 fail / 需器材附件上机 /
  卷内题号不连续重复 / 跨 variant 重复题面。所有排除在预览「排除统计」可见原因与数量。
- 精确总分：位掩码后缀 DP + 保可行顺序贪心，「每种 Paper 至少一题」用带约束 DP
  （与暴力枚举对账一致），**只要存在精确子集必然给出**；无解返回可读错误不生成 PDF。
- 冻结选题：预览返回 `selection_id`（16 位指纹），PDF/答案/ZIP 接口服务端重新校验，
  参数或源文件已变 → 409 要求重新预览。

**HTTP API**（需 `server.py` 在跑；分值与选题一律服务端重新核定）：

```
GET  /api/mock/meta?subject=9702          # 卷/章节/年份/上限等元数据
POST /api/mock/preview                    # 预览选题 {subject,papers,topics,year_from,year_to,target,seed,require_each_paper,mode}
POST /api/mock/pdf                        # 试题 PDF（+selection_id）
POST /api/mock/answers                    # 官方答案 PDF（仅 answers 模式）
POST /api/mock/zip                        # 两份 PDF 的 ZIP
```

答案形态：MCQ 输出官方字母对照表（与 `msloc.units` 核验一致才收）；结构题输出官方
MS 矢量裁切（整题段含全部小问与跨页；表头/页脚噪声带逐带回收；旋转页归一；保留官方
原题号行标便于追溯）。

---

## 模块 ⑦：校验与报告

### `verify_data.py` —— 发布前自检

`data/` 一致性 + 定位坐标/裁图内容**全量**自检：单元白名单对账、PDF 路径存在、
坐标边界（页码/页面 rect/宽高）、letter 单元须含题号与官方字母、part/question 须含
对应标签、多罗马子问覆盖核验（`roman_check=ok/gap/no-labels`）。
输出 `data/verify.json` + 控制台摘要；**结构性问题非零退出**。

```bash
/usr/bin/python3 verify_data.py
```

### `report_coverage.py` —— 覆盖率报告

```bash
/usr/bin/python3 report_coverage.py     # → data/coverage.md (+ coverage.json)
```

---

## 模块 ⑧：站点索引生成器

发布流程（`publish` / `migrate`）内部依赖的生成器，平时**不需要手工跑**
（发布时会自动在候选版本目录里执行）；仅在手工排障/重建静态数据时使用。
生成的页面为数据自包含，SPA 布局引用的 `assets/`（workbench.css/js、app.js、pipeline.js）
**不在本仓库**（前端未收录），如需完整网页工作台请自行提供 assets 目录。

```bash
python3 gen_index.py [根目录] [科目代码] [数据文件夹名]
#   扫描 <根>/<数据文件夹>/<年>/<会话>/*.pdf → papers_data_<代码>.json/.js + index.html(SPA)
#   默认: 根目录=脚本所在目录, 科目 9702; 数据文件夹自动识别 "9702（物理）" 这类命名
python3 gen_topics.py <科目代码>
#   读 topics_<代码>.json + classified_<代码>.json → topics_data_<代码>.js (+ 旧地址 stub)
```

`gen_search.py` 见模块⑤。注意 `gen_search.py` 与 `gen_index.py` 内联搜题 JS 的
分词三件套必须逐字一致（改一处必须同步另一处）。

---

## 模块 ⑨：测试

```bash
/usr/bin/python3 test_paper_pipeline.py     # 下载/发布/版本化/可见性 —— 自包含 HTTP 夹具(44/46 无需真题库;
                                            #   NewPaperCounterexamples 与 PracticalReview 两类对照真实 topics/迁移数据)
/usr/bin/python3 test_pipeline_service.py   # 任务队列/管理 API/发布刷新 —— 自包含夹具, 32 项全离线
/usr/bin/python3 test_mock_exam.py          # 组卷 59 项 —— 需已建成的真题库(data/units 等)
/usr/bin/python3 test_photo_search.py       # 拍照搜题 —— 需 bin/ocr 与真题库
# 可带类名过滤: /usr/bin/python3 test_mock_exam.py Rescore Answers
```

测试运行会写 `data/pipeline/…` 等运行时产物，均已 `.gitignore`。

---

## 数据目录约定

```
<仓库根>/
├── subjects.json                 科目登记（模块②）
├── <代码>（<中文名）/<年>/<会话>/*.pdf     整卷库（下载/同步产物）
├── syllabus/                     各科大纲 PDF（<代码>_<Subject>_<年份>_<类型>.pdf）
├── topics_<代码>.json            知识点大纲树（章节/小节/关键词/中英标题）
├── tags_<代码>/<卷>.json          AI/自动分类结果（仓库内持久保存）
├── classified_<代码>.json        合并后的分类结果（发布产物）
├── bin/ocr                       Vision OCR（swiftc 编译产物）
└── data/
    ├── units/<科目>/<卷>.json     每卷题目单元全文 + 分类状态
    ├── loc/<科目>/…               QP 裁切坐标（exact/fuzzy/fail）
    ├── msloc/<科目>/…             MS 答案坐标（part/question/letter/not_found）
    ├── pipeline/                  题库管理管线全部状态
    │   ├── state.json  work/<科目>/…  migration/  adjudication/
    │   ├── snapshots/  runs/  quarantine/  tasks/
    │   ├── versions/<vid>/        整站版本（live 指向其一）
    │   └── live -> versions/<vid> 当前活动版本（唯一可见性入口）
    ├── coverage.md  verify.json  reconcile.json   报告/对账
    ├── cache/crops/               裁图缓存（按源 mtime+size 自动失效）
    └── uploads/                   拍照临时目录（OCR 后即删）
```

## 常见问题

- **某个文件反复 FAIL？** 先 `curl -sI` 看 HEAD，再下载后 `xxd -l 16` 看头部：大小正确但
  内容全零 = 源站坏文件，重试无意义；HEAD `000` 才是限流抖动可补下。该站对缺失文件可能
  返回 200 + HTML 软 404，一切以 `%PDF` 魔数为准。
- **MS 裁图裁到页眉？** MS 约 698 份带 `/Rotate 90`；任何新写的渲染/校验代码必须先
  `remove_rotation()` 归一坐标系（见 `locate_ms.py` 头注释）。
- **搜题结果是旧题库？** `merge_tags.py` 重跑后必须重跑 `gen_search.py`（并重跑
  `gen_index.py` 刷新主页）；服务端搜题由 `publish` 后自动刷新，或经
  `/api/pipeline/tasks` 触发 `refresh_index`。
- **换一个科目/网站？** 见 `docs/PIPELINE_USAGE.md` §接入 与 `docs/TOPICS_WORKFLOW.md` §8
  的核对清单（syllabus 表、topics json、merge_tags 归一化口径、切分器排版特征复查）。
- **`/usr/bin/python3` vs `python3`？** 脚本按 macOS 自带 Python 设计，依赖已装在
  `/usr/bin/python3` 用户目录；其他环境保证 `import fitz` 可用即可。

---

*本仓库由个人真题整理项目抽取发布，仅含工具代码；试卷版权归 Cambridge International
Assessment Education 所有。*
