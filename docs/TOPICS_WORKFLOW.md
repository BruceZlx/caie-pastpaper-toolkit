# Syllabus 知识点分类真题库 —— 完整流程手册（第二期功能）

> **这份文档是写给 AI 看的操作手册**。用户下一次只需要对 AI 说（二选一）：
> *"请阅读 `TOPICS_WORKFLOW.md` 并严格执行其中的流程，为 `<科目代码>`（如 9709）建立知识点分类真题库。"*
> *"请阅读 `TOPICS_WORKFLOW.md` 并严格执行其中的流程，为 `<科目代码>` 建立知识点分类真题库，只整理 `<如 2020-2026>` 年份的卷子。"*
> AI 按本文档逐步执行即可。前置条件：该科目的整卷库已按 `README.md` 流程下载完毕（`<代码>（<中文名>）/年份/会话/*.pdf` 与 `index_<代码>.html` 已存在）。
> 科目代码确认方式：看 `真卷/` 下 `<代码>（<中文名>）` 文件夹；对应 syllabus 下载链接见 §1 表。
>
> **9618（CS）已于 2026-08 全量完成**（118 卷 / 2823 题单元 / 20 章节 44 小节），可作为对拍基准。

---

## 0. 最终产物与文件清单

以 9618 为例，跑完本流程后根目录新增：

```
真卷/
├── syllabus/                          ← ① 各科 syllabus 原始 PDF（文件名以科目代码开头）
│   ├── 9618_ComputerScience_2027-2029_syllabus.pdf
│   ├── 9618_ComputerScience_2027-2029_syllabus-update.pdf
│   └── 9618_ComputerScience_2027-2029_pseudocode-guide.pdf
├── topics_9618.json                   ← ② 知识点大纲（章节树 + 关键词 + 中英文标题）
├── tags_9618/                         ← ③ 子代理分类结果（**必须存仓库**: /tmp 会被系统清空,
│                                          丢了就得重派子代理; 可由 classified 反向恢复, normalize 幂等）
├── classified_9618.json               ← ④ 分类结果（每卷每题 → 知识点标签 + 页码 + 摘要）
├── index_9618_topics.html             ← ⑤ 知识点分类页面（单文件自包含, 双击即用）
├── index.html / index_9618.html       ← ⑥ 自动更新（hub 加 syllabus 区; 科目页加模式切换栏）
└── 工具脚本（复用, 勿改逻辑）
    ├── extract_questions.py           ← PDF → 每题文本+页码
    ├── gen_workfiles.py               → 生成子代理分类输入
    ├── merge_tags.py                  ← 合并子代理输出 + 归一化 + 校验
    └── gen_topics.py                  ← 生成知识点页面
中间产物（/tmp，可随时删）：/tmp/qp<代码>/{units,work}/
```

用户看到的导航路径：`index.html` → 科目卡片 → 科目页顶部
**[整卷浏览 | 知识点分类题目]** 切换（同一白卡片内，右侧为 **syllabus PDF 直链**）→ 知识点页左侧大纲树（每节带题量徽章）
→ 点章节/小节 → 右侧按年份列出每道题（题号/分值/摘要/次要标签 + 试卷/答案按钮），
按钮链接带 `#page=N` 直接定位到题目所在页；顶部可按 Paper 1-4 筛选。

**页面 UI 约定（用户逐条提出, 已固化在 gen_index.py / gen_topics.py 模板里, 改版勿丢）**：
1. 每个非主页的左上方都有 "← 返回主索引" 按钮（链接 index.html; 主索引自身不加）。
2. 两页头部同款：标题 26px、统计行、模式卡片（`#modebar.card`）样式完全一致。
3. 知识点页侧栏：宽度恒定 280px（`flex:0 0 280px`）、`overflow-x:hidden` 只纵滚；
   条目 = 编号 + **syllabus 英文标题** + 题量徽章，**不放中文**（用户明确要求）。
4. 题目列表：同一大题的小题（Q6a/Q6b…）不重复显示卷号（首题显示一次）；
   不同大题之间用 2px 粗分隔线（`tr.qsep`），小题之间保持细线。
⚠️ hub 卡片整体是 `<a>`，**内部严禁再嵌 `<a>`**（浏览器会自动截断外层 a 导致卡片裂成两块），
因此入口只放在科目页切换栏，不要加进 hub 卡片。

运行环境：python3 + **PyMuPDF**（`python3 -c "import fitz"` 报错则 `pip3 install pymupdf`）。

---

## 1. 步骤一：下载 syllabus 到本地

官方 URL 模式：`https://www.cambridgeinternational.org/Images/<ID>-<年份范围>-<类型>.pdf`
**必须带完整文件名**（年份段缺失会拿到 HTML 而非 PDF；收到后 `head -c5` 验证 `%PDF-`）。
curl 需带浏览器 UA，可并行。保存到 `syllabus/`，命名 `<代码>_<Subject>_<年份>_<类型>.pdf`（下划线分隔，代码开头）。

各科 syllabus 图源 ID 对照（2026-08 实测，若失效去 cambridgeinternational.org 该科目页找新链接）：

| 科目 | 文件 | 图源 ID |
|---|---|---|
| 9701 化学 2025-2027 | syllabus | 664563 |
| 9702 物理 2025-2027 | syllabus | 664565 |
| 9709 数学 2026-2027 | syllabus / update | 697427 / 723728 |
| 9618 计算机 2027-2029 | syllabus / update / pseudocode-guide | 721397 / 747147 / 721401 |

## 2. 步骤二：从 syllabus 提取知识点大纲 → `topics_<代码>.json`

1. 用 fitz 抽全文并按 `<<<PAGE n>>>` 标页：参考 §附A 脚本。
2. 定位 "Subject content" 页码范围，抽取章节标题：行首 `N` / `N.M`（编号后常跟 tab，标题可能在同行或下一行；`continued` 重复标题取第一次）。
3. 手工整理成 JSON（推荐用 python dict 构造后 dump，**不要手写大 JSON**）：

```json
{
  "subject": "9618", "name": "Computer Science", "zh": "计算机科学",
  "source": "<syllabus文件名> Subject content (p.14-39)",
  "note": "适用年份范围说明",
  "levels": [{"level":"AS","from":"1","to":"12","papers":[1,2]},
             {"level":"A2","from":"13","to":"20","papers":[3,4]}],
  "sections": [
    {"code":"1","en":"Information representation","zh":"信息表示","subs":[
      {"code":"1.1","en":"Data Representation","zh":"中文说明（要点括号）",
       "kw":["binary","denary","hexadecimal"]}   ← kw 必须是列表, 用作分类提示与后备校验
    ]}]
}
```

- `zh` 写成"标题（要点/要点/要点）"形式，页面上直接展示。
- 校验：章节数 = 20（9618）/ 对应大纲实际数；codes 连续无缺。

## 3. 步骤三：切题 → `extract_questions.py`

```bash
python3 extract_questions.py <代码> "<科目文件夹绝对路径>" /tmp/qp<代码>/units [年份范围]
# 全部年份:
python3 extract_questions.py 9618 "<题库根>/9618（CS）" /tmp/qp9618/units
# 只整理指定年份(用户可能指定, 见下):
python3 extract_questions.py 9709 "<题库根>/9709（数学）" /tmp/qp9709/units 2020-2026
python3 extract_questions.py 9618 "…/9618（CS）" /tmp/qp9618/units 2021          # 单个年份
```

**指定年份范围**：`2020-2026` 或单一年份 `2021`，格式不对会直接报错退出。
范围只影响知识点分类管线——下游 `gen_workfiles` / `merge_tags` / `gen_topics` 都只处理
units 目录里存在的卷，所以在这一个入口控制即可，无需改其他脚本。
注意：整卷归档与整卷索引（README 流程产物）始终是全量的；若之后想扩大范围，
删掉 `/tmp/qp<代码>/units` 重跑本脚本（新范围）→ 新增卷派子代理补 tags 写入 `tags_<代码>/`
（已有卷的 tags 在仓库里原样保留，无需重跑）→ §5、§6 重跑。

对 `<科目>/年份/会话/` 下每份 `*_qp_*.pdf` 产出 `{code}_qp.json`（题目单元），`*_ms_*.pdf` 产出 `{code}_ms.json`（题号→MS 起始页码）。QP 切分粒度 = 题号 + 小问 `(a)-(h)`（罗马 `(i)(ii)` 并入母题，不单独成单元）。

**切分器要点（9618 实测踩坑，换科目先复查这几点再跑）**：
- 用 `page.get_text("dict")` 的**行级**坐标，不要用块级/纯排序文本：
  - 过滤：块 x0<45（左竖排 "DO NOT WRITE IN THIS MARGIN" 栏）或 x0>560（右侧栏）、y0<58（页眉页码/条形码 `* 00008xxxxxx *`）、y0>782（页脚版权/PapaCambridge 水印）、乱码字体块（非 ASCII 占比>30%）。
  - 排序：`sort(key=(round(y/2), x))` —— **y 按 2pt 分桶**。真题号与题干/小问行有 ≤0.1pt 的 y 微差，直接按 y 排会让 `(a)` 抢在题号前出现，把新题并进上一题（w25_23 / s24_32 实测坑）。
- 题号识别（三重约束，缺一不可）：
  1. 行级 x ≤ 62（题号固定在最左列 x≈50；伪代码行号/SQL 行号在 x≈95-118，块级 x0 会伪装成 50 —— 必须用行级 x）；
  2. `^\d{1,2}\s+(\S.*)$` 且剩余部分 ≥2 个字母（或独立纯数字行且下一非空行不是纯数字）；
  3. 题号 == 上一题号+1（连续性闸门，杀掉答案栏 `1 ....`、表格行号等杂音）。首页（封面）整页跳过。
- 小问：`^\s*\(([a-h])\)\s` 且行 x≤80（**不能含 i/j**，否则罗马 `(i)` 被当部件；9618 全 118 卷实测 (a)-(h) 都在 x≈50-72，x=95 的 "(a)" 是正文杂散行）；同一 part 重复出现则并入不新切。
- 文本清洗：`.……`4 个以上点替换为空格；去 `[Turn over / © UCLES / <代码>/nn / BLANK PAGE / PapaCambridge]` 行。
- marks 取单元文本中最后一个 `[N]`。

**验收关（不过不继续）**：
```python
# 逐卷检查: 每卷题号集合 == 1..max 连续; unit id 无重复; 空文本(<15字符)单元 ≤2
# 9618 基准: 118 卷全部通过, 共 2823 单元, marks 覆盖 82%
```
MS 覆盖率对账时注意：units 里 `q` 是 int，`ms_pages` 键是 str，比较时先 `str()`（否则假报 0）。

## 4. 步骤四：子代理批量分类

```bash
python3 gen_workfiles.py <代码> /tmp/qp<代码>/units /tmp/qp<代码>/work
# 产出 work/TOPICS.txt（大纲+分类守则）和 work/<code>.txt（每卷题目清单, 单元文本截 500 字符）
```

然后**并行派发子代理**（每批 ~12 卷，9618=118 卷用了 10 个并行 general-purpose 代理，每个 35 万-140 万 token，4-16 分钟）。提示词模板（三处按需改）：

```
You are classifying Cambridge International <科目> past-paper questions by syllabus topic.
Step 1: Read /tmp/qp<代码>/work/TOPICS.txt — taxonomy codes + guidance.
Step 2: For EACH paper code in this list: <12个code>
- Read /tmp/qp<代码>/work/<code>.txt (每行 "[id|p页|m分] 题面" 是一个分类单元)
- Decide codes for EVERY unit: 1-3 codes, PRIMARY FIRST, subsection codes preferred.
- If a unit genuinely tests NOTHING in the taxonomy (content outside the syllabus, pure
  administrative/evidence instructions with no assessable topic), write ["OUT"] instead of
  forcing a topic. Never invent codes that are not in TOPICS.txt.
- Write /tmp/qp<代码>/tags/<code>.json EXACTLY: {"code":"<code>","tags":{"1":["1.1"],"1a":["1.1"]}}
  (9618 惯例路径可写 <真卷>/tags_<代码>/<code>.json, 与 merge 调用一致)
- Every unit id must appear exactly once in "tags".
Step 3: Verify each JSON parses and tags keys exactly match the work file ids; fix if wrong.
Step 4: Report "<code>: N units tagged" per paper, then "ALL DONE".
```

9618 分类口径决策记录（换科目要按其 syllabus 重新核定，**不信记忆查原文**）：
- AS 卷(P1/2) 逻辑门/真值表 → **3.2**；A2 卡诺图/布尔代数/触发器 → **15.2**（子代理普遍爱把 AS 逻辑门塞给 15.2 或 4.1，必须归一化）。
- BNF/语法图/RPN 官方在 **16.2** Translation Software（不在 5.2）。
- A2 的 stack/queue/linked list/binary tree ADT 算法官方在 **19.1**（10.4 只是 AS 引介）→ A2 卷里 "10.4" 一律归一化为 "19.1"。
- 编程题按"考察什么"标而非"属于哪张卷"：P4 写数组循环 → 10.2 + 11.1 是合法且有用的标签。

## 5. 步骤五：合并 + 归一化 + 校验 → `merge_tags.py`

```bash
mkdir -p tags_<代码>      # 子代理输出直接写这里(仓库内, 勿放 /tmp)
python3 merge_tags.py <代码> /tmp/qp<代码>/units tags_<代码>
# 输出 classified_<代码>.json; 归一化规则在脚本 normalize() 内, 换科目按 §4 口径改写
```

- **大纲外剔除**：子代理标为 `["OUT"]` 的单元（考纲没有的知识点/纯考试指引）会被直接剔除，
  不进入 classified、不出现在知识点页面；合并输出行会显示 `大纲外剔除 N` 并列出前 15 条样例。
  **人工复核这些样例**：拿不准的边界题应归入最接近的知识点而非 OUT（宁可错收不可漏收）。
- 自动校验：tags 覆盖每个 unit、代码全部合法（对照 topics json）、非法/缺失会逐条打印 PROBLEMS。
- tags 目录在仓库里持久保存：/tmp 被清理后只需重跑 `extract_questions.py`（PDF 重建 units），
  merge 可直接重放；万一 tags 丢失且无备份，可从 classified 反向恢复
  （`{u["id"]: u["topics"]}` per paper——normalize 幂等，恢复后重跑结果不变，9618 已实测）。
- **人工抽检**（必做）：从 AS 卷 1 份、A2 理论卷 1 份、A2 实操卷 1 份各抽 10-14 个单元，读 snippet 核对主标签；再统计"AS 卷用了多少 A2 章节码 / A2 卷用了多少 AS 章节码"（9618 基准：少量 18.1/17.1 出现在 AS 卷、大量 10.2/11.1 出现在 A2 卷属正常，都是真实考察内容）。

## 6. 步骤六：生成知识点页面 + 更新索引

```bash
python3 gen_topics.py <代码>        # → index_<代码>_topics.html（读 topics_ + classified_ + syllabus/ 自动内嵌）
for s in <全部科目代码>; do python3 gen_index.py . $s; done
python3 gen_search.py               # 主页搜题数据(classified 有更新的科目都要重跑), 详见 README §12
# gen_index.py 自动: 科目页加 [整卷|知识点分类] 切换卡片(文件存在才显示) + syllabus 链接行;
# hub 页加 SYLLABUS 区(按 syllabus/ 目录文件名前缀自动分组); hub 科目卡片保持原格式不加内嵌链接;
# gen_index 检测到 search_data_<代码>.js 时自动在 hub 顶部注入搜题卡片
```

## 7. 步骤七：发布前验证

1. 数据关：classified_*.json 里每条 qp_pdf/ms_pdf 相对路径存在；每单元 topics[0] 在 counts 里；抽样 MS 页码 `ms_pages[str(q)]` > 0；
   merge 输出的"大纲外剔除 N"数量合理，且已抽复核过这些 OUT 单元确实考纲外（§5）。
2. 页面关（起本地服务用内置浏览器实测，勿只看代码）：
   `cd 真卷 && python3 -m http.server 8931` → 开 `http://127.0.0.1:8931/index.html`：
   - hub：科目卡为单卡原格式（name+meta 同一 `<a>` 内、无嵌套链接）、SYLLABUS 区链接齐全；
   - 科目页/知识点页左上方都有 "← 返回主索引" 按钮且点击回到 index.html；
   - 科目页：切换栏出现且互链正确；
   - 知识点页：默认 overview；侧栏为**纯英文**条目、宽度恒定 280px 且无横向滚动
     （`side.scrollWidth <= clientWidth`、整页 `documentElement.scrollWidth` 也不超）；
     点一个小节（如 9.2）行数==徽章数；Paper 筛选后行数与 `classified_*.json` 里该(小节,Paper)计数**完全一致**；
     点章节级显示全部子小节且徽章=子小节之和；
     同一大题的小题卷号留空、不同大题之间有 `tr.qsep` 粗线；
     QP/MS 按钮 href 形如 `9618（CS）/...pdf#page=N`（**不带 `../`**，URL 编码正常）。
   - 注意 IAB `locator(sel, {hasText})` 的 hasText 不生效（多匹配时改用 `[data-t="…"]` 精确属性）；测试期间用户可能点动预览面板，状态对不上时先 `location.href` 确认还在测试页；截图通道偶发 "capture failed for guest"，重试无效时改用 `evaluate` 读计算样式/DOM 断言即可。
3. PDF 关：任取页面上一条 href（保留 #page）curl 得 200 且 `%PDF-` 魔数。
   ⚠️ **页面位于根目录，所有 PDF/syllabus 链接一律相对根目录、严禁带 `../` 前缀**：
   在 HTTP 根路径下 `../x` 会被解析回 `/x` 而照常工作（测不出问题），但双击 file:// 打开时
   `../` 会跳出 `真卷/` 导致全部链接 ERR_FILE_NOT_FOUND。最终验收必须包含 file:// 场景：
   取渲染后的 href 做 `unquote` 后拼在 `真卷/` 下，逐一确认文件存在。
4. `#page=` 定位在 Chrome/Edge/Firefox 有效，Safari 会忽略（页脚已有说明，无需处理）。

## 8. 换科目的核对清单（连同 §10 全部步骤）

0. **问清年份范围**：用户可能只要部分年份（如 "只整理 2020 年之后"）。要则在 §3 切题时传
   年份范围参数；不问默认全量。整卷库/整卷索引不受影响。
1. syllabus 下载 → 识别科目改 §1 表；
2. `topics_<代码>.json`：大纲树 + levels(papers 映射) + 关键词；
3. `extract_questions.py`：先在 3-5 份不同年份卷上试切，核对新版试卷的边栏/页眉特征（9701/9702/9709 与 9618 同为 Cambridge 排版，预期只需调噪声正则；老年份数学卷无竖排边栏）；
4. 分类口径：读该科 syllabus Subject content 原文，重写 merge_tags 的 normalize()；
5. `gen_topics.py` 无需改（PAPER_NAMES 可选补卷名）；`gen_index.py` 若该科无 topics 页会自动不显示切换栏。
   §0 的 UI 约定（返回按钮、英文侧栏、题组省卷号+粗线、头部同款）都固化在两个生成器模板里，新科目自动继承，不要在改版时删掉。

## 9. 增量更新（次年新卷发布后）

1. 按 README §11 下载新卷；
2. `extract_questions.py`（脚本自带跳过已存在文件）→ `gen_workfiles.py` → 只对新增卷派子代理 → `merge_tags.py` 全量重跑（快，纯内存）；
3. `gen_topics.py` + `gen_index.py` + **`gen_search.py`**（主页搜题索引随 classified 更新）重跑即可。

---

## 附A：syllabus 全文抽取参考脚本

```python
import fitz
doc = fitz.open("syllabus/9618_ComputerScience_2027-2029_syllabus.pdf")
open("/tmp/syll.txt","w").write("".join(
    f"\n<<<PAGE {i+1}>>>\n" + p.get_text() for i, p in enumerate(doc)))
# 章节标题行特征: r"^(\d+(\.\d+)?)\s*\t?\s*(标题)$"，标题也可能在下一行（先 grep 再人工确认）
```

## 附B：9618 实测基准（2026-08）

| 项目 | 数值 |
|---|---|
| syllabus 文件 | 7 份（4 科 syllabus + 2 update + 伪代码指南） |
| 切分 | 118 卷 → 2823 单元（中位 25/卷），0 异常卷；marks 覆盖 82% |
| 分类 | 10 并行子代理，全部 2823 单元有 1-3 个合法标签；归一化 4 条规则 |
| 大纲外剔除 | 0 题（OUT 机制已就绪：9618 无考纲外题；其他科目预期会出现） |
| tags 存储 | `tags_9618/` 仓库内 118 个文件（/tmp 清理后重跑 merge 实测无损） |
| 页面 | index_9618_topics.html 0.7MB 自包含；44 小节全部有题（最少 24、最多 295） |
| 已知源站缺失 | s22 整个会话、w21 的 33/43 卷（整卷库阶段已记录，分类阶段无影响） |
| 排版坑 | 边栏竖排字(w24+)、条形码页眉、y 微差竞态、Courier 伪代码行号、块级 x0 假象 —— 均已写进 extract_questions.py |
