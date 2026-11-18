# paper_pipeline 使用说明（真题库增量同步与新科目接入）

**日常操作首选网页**：启动一次 `/usr/bin/python3 server.py`，打开
`http://127.0.0.1:8930/index.html#/pipeline`（左侧导航「题库管理」）。预览、更新、
新科目接入、大纲检查/迁移、AI 输入生成与结果导入、逐单元复核、发布、任务重试、
运行报告全部在网页完成；CLI 仅作批处理/排障入口（同一业务层 `pipeline_service.py`）。

统一 CLI：`/usr/bin/python3 paper_pipeline.py <子命令> …`（在仓库根目录运行）。
默认 **dry-run**（只读预览，不写仓库任何文件）；`--apply` 才实际执行。所有子命令返回
0=成功、2=部分失败（见报告 failures）、3=用法/致命错误。

## 1. 已有科目同步最新已发布试卷

```bash
/usr/bin/python3 paper_pipeline.py sync --code 9709 --latest --dry-run
/usr/bin/python3 paper_pipeline.py sync --code 9709 --latest --apply
/usr/bin/python3 paper_pipeline.py sync --code 9709 --years 2025-2026 --apply
/usr/bin/python3 paper_pipeline.py sync --code 9702 --latest --apply --source-url <科目页URL>
/usr/bin/python3 paper_pipeline.py sync --code 9702 --latest --apply --source-list 直链清单.txt
```

- `--latest`：来源站**当前已发布**的会话与文件（不按日历推断未公开考季）。
- `--years A-B`：闭区间年份过滤（年份/考季一律取自 PDF 文件名，不信会话页标题）。
- `--source-url`：自动发现失败/歧义时手工指定 PapaCambridge 科目页。
- `--source-list`：其他站点结构不兼容时导入受校验的 PDF 直链清单（每行一个 URL）。
- 行为：发现会话页→按文件名核对科目/年/考季/Paper/variant/类型（排除 solved/topical/gt/er/ci，
  接受 `qp_et`，反转命名视为重复源）→ HEAD 估算体积 → 已有有效文件跳过、不覆盖 →
  `.part` 下载→`%PDF`+PyMuPDF 验真→原子入库；同名内容变化进隔离区并报告；200+HTML、
  零字节、缺 MS 逐项报告。apply 后自动增量切题→新卷 IN/OUT/PENDING 判定→校验通过才
  发布并重建索引。若官方大纲已更新则**停止发布**并提示先 `migrate`。
- 重复 apply 幂等：无新增/无变化时第二次运行零下载、零改写（已用 9709 实测）。
- 运行报告：`data/pipeline/runs/<时间戳>_sync_<科目>.json`。

## 2. 新科目接入

```bash
/usr/bin/python3 paper_pipeline.py onboard --code 9708 --years 2025 --dry-run \
    --zh 经济学 --name Economics \
    --url <Cambridge官方科目页> --source-url <PapaCambridge科目页>
/usr/bin/python3 paper_pipeline.py onboard --code 9708 --years 2025 --apply …
/usr/bin/python3 paper_pipeline.py syllabus --code 9708 --apply     # 官方最新 syllabus(+update)
#   ↓ 按 SYLLABUS_TOPIC_AI_SOP.md 制作 topics_9708.json（AI 从官方 PDF 提取, 带页码证据）
/usr/bin/python3 paper_pipeline.py classify-prep --code 9708        # 生成 AI 判定输入
#   ↓ AI 按 SOP 判定, 写 tags_9708/<卷>.json  {"code":…,"tags":{uid:[codes]|["OUT",理由]|["PENDING",问题]}}
/usr/bin/python3 paper_pipeline.py publish --code 9708 --apply      # 校验→快照→发布→重建索引
```

- 登记（代码/名称/数据目录/来源/卷名）只做一次，存 `subjects.json`；`gen_index`/`gen_topics`/
  `persist_units`/`verify_data`/`report_coverage`/`locate_*` 均从登记或目录动态发现科目。
- 新科无锚点时判定走 AI SOP（子代理逐单元给 IN 标签/OUT 理由/PENDING 问题），未核准部分
  保持 PENDING，不会冒称完成。

## 3. 大纲版本管理与四科迁移

```bash
/usr/bin/python3 paper_pipeline.py syllabus --code 9701            # 只查不写
/usr/bin/python3 paper_pipeline.py syllabus --code 9701 --apply    # 下载+登记版本/哈希
/usr/bin/python3 migrate_syllabus.py 9702                          # 迁移 dry-run(判定写审计)
/usr/bin/python3 migrate_syllabus.py 9702 --apply                  # 快照→发布→重建
/usr/bin/python3 paper_pipeline.py migrate --code 9702 --apply     # 同上(CLI 入口)
```

- 目标大纲=执行时官网已发布、适用年份最晚的完整 syllabus（含 update）；每次 `syllabus`
  解析官方科目页并记录 URL/版本/SHA-256；`sync` 检测版本变化并要求迁移。
- 迁移规则：旧标签按**内容**映射到新大纲（同编号必须标题/学习目标对核；9702 为真实重编号
  迁移，映射与证据在 `data/pipeline/migration/9702_map.json`）；映射不上的按题面重判
  （近邻+关键词），强信号→IN、旧主题确认移除→OUT（理由）、其余→PENDING；从未派发分类
  的单元一律 PENDING。旧判 OUT 经新旧大纲学习目标 diff 复核（无内容回添则维持）。
- 发布原子性：先快照到 `data/pipeline/snapshots/<时间戳>_<科目>/`（topics+classified+
  data/units 全量，可整目录拷回恢复），校验通过后原子写入。

## 4. 状态与报告

```bash
/usr/bin/python3 paper_pipeline.py status --code 9709
/usr/bin/python3 paper_pipeline.py report --code 9709
```

- IN/OUT/PENDING 对账、目标大纲版本/哈希、隔离区、最近运行。
- 审计明细：`data/pipeline/adjudication/<科目>.json`（每单元状态/标签/理由/依据）、
  `data/pipeline/migration_reconciliation.md`（四科对账）、`data/pipeline/spot_checks.md`（抽检）。

## 5. 服务刷新提示

`publish`（网页或 CLI）成功后，**同一运行中的 `server.py` 会自动后台重建并原子替换**
服务端搜题与模拟组卷索引（`DATA_VERSION+1`）；前端轮询 `/api/pipeline/version`，
自动失效已缓存数据并提示/刷新，不再需要重启服务。CLI 单独发布时若恰有服务在跑，
可在网页「任务与恢复」点一次「刷新索引」任务（或重启服务）确保加载。

## 5b. 管理接口(网页使用, 默认仅本机可写)

- `GET /api/pipeline/overview|version|tasks|tasks/<id>|adjudication|classify|classify-file|runs|run`
- `POST /api/pipeline/tasks {action,params}` —— 动作白名单:
  preview_sync/sync/preview_onboard/onboard/syllabus_check/syllabus_apply/
  extract/classify_prep/import_tags/import_topics/adjudicate_new/publish/migrate/refresh_index
- `POST /api/pipeline/tasks/<id>/retry|cancel`、`POST /api/pipeline/adjudication`（复核草稿）、
  `POST /api/pipeline/reload`
- 长任务后台串行执行并持久化(`data/pipeline/tasks/`)，服务重启后 running→interrupted 可重试；
  同参数任务执行中去重拒绝。`--lan` 模式下管理写接口仅限 127.0.0.1。

## 6. 目录约定

```
data/pipeline/
  state.json            下载/大纲登记状态（可中断续跑）
  work/<科目>/units     持久化切题工作目录（替代 /tmp/qp<科目>）
  work/<科目>/classify  AI 判定输入（TOPICS.txt + 每卷清单）
  migration/            新旧大纲映射/新版 topics 草稿/页码证据
  adjudication/         每单元 IN/OUT/PENDING 审计
  snapshots/            迁移前快照
  runs/                 每次运行机器可读报告
  quarantine/           同名内容变化隔离区
```

## 正式产物单一活动版本(第五轮, 2026-11-07)

正式数据/静态索引/页面不再散落在仓库根, 而是整站版本化:

- `data/pipeline/versions/<vid>/` — 一个完整站点版本(index.html、*_data_*.js、
  topics_*/classified_*、data/units|loc|msloc 全树);
- `data/pipeline/live` — 符号链接, 指向当前活动版本(**唯一可见性入口**);
- 仓库根的受管路径(index.html、search_data_*.js、data/units 等)是稳定符号链接
  → `data/pipeline/live/<同路径>`。

发布(publish)= 在候选目录完整构建并校验后, **一次 os.replace 翻转 live 符号链接**。
任何时刻(含切换中崩溃)读者只能看到完整旧版或完整新版; 崩溃后启动服务或下次
发布时 `live_version.ensure_layout()` 自动收尾(自愈/清理残留)。历史版本保留
最近 2 个用于回退, 之后自动回收(硬链共享数据, 回收零拷贝)。

两阶段发布状态: 翻转前先把本次提交的卷记入 `published.json` 的
`pending_refresh`(数据已提交、服务索引未就绪, **不算完全发布, 待发布不清零**);
服务端搜题/组卷索引刷新成功后自动晋升为已发布。刷新失败可在
`index.html#/pipeline` → 「任务与恢复」点「刷新服务索引」重试, 服务重启后状态仍在。

测试提速: `ZJ_PUBLISH_NOTIFY_SERVER=0` 跳过 CLI 发布后对本机 8930 服务的刷新
通知(测试进程内无此服务, 默认跳过不影响语义)。

## 预备提交意图与对外可见版本(第六轮, 2026-11-07)

**P0-1 两阶段状态(修订)**: 翻转前持久化的是"预备提交意图"
(`published.json → subjects.<code>.pending_refresh`, 每条含 `intended_version`):
- 只记录意图, **不移走** `papers` —— 翻转前失败/崩溃不改变旧卷已发布状态;
- 「刷新服务索引」只晋升 **意图版本==当前活动版本 且 正式单元文件存在于活动版本** 的卷;
  过期意图(翻转前失败)留档待下次发布覆盖, 不会被误晋升。

**P0-2 对外单一可见版本**: 服务端 `SERVED={vid,搜题索引,组卷索引}` 是对外唯一入口 ——
静态受管文件(index.html/*_data_*.js)、题目详情/裁图、搜题、组卷全部按 `versions/<vid>`
同一版本读取; `/api/health` 的 `served_version` 即当前对外版本。发布翻转后、双索引重建
完成前, 对外**整体保持旧版**(新卷详情 404、搜题仍旧索引); 刷新成功后整体切新并
`data_version+1`; 刷新失败持续保持旧版, 在「任务与恢复」重试成功后切新。

## 版本回收保留协议(第七轮, 2026-11-07)

`data/pipeline/versions/` 下版本带保留标记:
- `<vid>.retain` — 正被对外服务(SERVED/BOOT)。服务进程在启动捕获 BOOT、双索引
  安装新对外版本时写入; 任何进程(含 CLI 发布)的版本回收都不得删除带此标记的版本。
- `<vid>.retired` — 对外已切换到新版后给旧版打的退役时间戳; 满 GC 宽限期
  (默认 600s, 环境变量 ZJ_VERSION_GC_GRACE 可调, 覆盖在途请求)后方可回收。
- 服务重启时, 新进程只服务 BOOT(=当时活动版本): BOOT 写 retain, 其余遗留 retain
  标记转为 retired, 避免标记泄漏。
持续刷新失败+反复发布时, 正在服务的旧版本始终保留; 只有成功安装新版后旧版才退役,
宽限期后由任一发布进程的 GC 回收。
