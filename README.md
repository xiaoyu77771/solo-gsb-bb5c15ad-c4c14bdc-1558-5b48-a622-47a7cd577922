# 字幕断句校验台

基于 **FastAPI + SQLite + 原生前端** 的字幕断句校验工具。上传带时间码的
WebVTT/SRT 文件，设定每行字数、最小停留时间和阅读速度阈值后，系统会对跨
cue 文本做**动态规划断句**，必要时在相邻 cue 间**重分配边界时间**，并把
无法满足约束的**最小区段**标出并解释原因。

同时内置**术语审校**：维护按语言区分的术语表（规范词 / 允许变体 / 禁止
替代词），上传字幕后自动**跨 cue** 检测同一术语的大小写、全半角、标点与
近似拼写差异，逐项确认后批量生成术语修订 WebVTT 并可导出审校报告。

## 一键启动

```bash
docker compose up --build
```

启动后浏览器访问：

> http://localhost:8000

SQLite 数据库文件位于容器内 `/data/subtitles.db`，通过 Compose 命名卷
`subtitle-data` 持久化；**数据库表在应用启动时自动初始化**，无需手工建表。

`examples/` 目录提供 `demo.vtt` / `demo.srt` 示例：前两条中文 cue 时间过紧
（会产生停留/阅读速度问题），后两条英文 cue 包含 `WI-FI` / `Wi-Fi`、
`websyte`、`ＨＤＭＩ` 等不一致写法。可先在术语表卡片导入
`examples/demo-glossary.json`，再上传示例体验术语审校。

## 功能

- **上传解析**：支持 WebVTT（`.vtt`）与 SubRip（`.srt`，自动转换处理），
  自动去除样式标签、NOTE/STYLE 块。
- **三项可配阈值**
  - 每行最大字数（默认 18）
  - 最小停留时间（秒，默认 0.8）
  - 阅读速度上限（字/秒，默认 12）
- **动态规划断句**：把所有 cue 展开为不可再分单元（拉丁连续词、单个
  CJK 字符、附着标点），以「违例数量最少 → 违例程度最小 → 停留冗余最小」
  为多级目标在全部可能切点上做 DP，因此每个被标出的问题都是**无法再切分
  的最小区段**。
- **跨 cue 边界重分配**：切点落在相邻 cue 之间时，自动在两 cue 的重叠区间
  （或间隙中点）重算时间边界；统计中展示拆分、合并与边界移动数量。
- **问题标注与原因解释**
  - `超长`：含无法拆分的连续词，任何切分都保留超长片段；
  - `停留过短`：相邻 cue 可借用时间不足，无法达到最小停留；
  - `阅读过快`：按阅读速度阈值所需时长超过实际可分配时长。
- **按语言筛选问题**：基于文字系统（中/日/韩/拉丁）与常用词的启发式语言
  检测，可在结果页下拉过滤；可勾选「只看有问题的条目」。
- **导出修订 WebVTT**：修订后的 cue 导出为标准 `.vtt`，问题原因以
  `NOTE` 行内嵌，可直接用于后续校对。
- **历史任务**：每次校验存入 SQLite，可重新查看或再次导出。

### 术语审校

- **术语表（按语言维护）**：每条术语包含规范词、允许变体、禁止替代词与
  备注，可按语言筛选；支持新增 / 编辑 / 删除、JSON 导入（按「语言 +
  规范词」去重合并）与导出。语言选「任意」时对所有语言字幕生效（适合
  HDMI 这类跨语言品牌词）。
- **跨 cue 一致性检测**，在上传**原始 cue**（时间轴保持不变）上执行：
  - 大小写差异（`WI-FI` vs `Wi-Fi`）
  - 全半角差异（`ＨＤＭＩ` vs `HDMI`）
  - 标点 / 空白差异（`wi fi` / `e-mail` vs `email`）
  - 近似拼写（编辑距离阈值，拉丁词与 CJK 词分别处理，如
    `websyte`、`云瑞同步` vs `云瑞同步`）
  - 禁止替代词：任何形态命中均直接标出
  - 允许变体混用：规范词在其他 cue 出现时，提示把变体统一
- 检测采用三层扫描（严格 NFKC+大小写 → 标点空白折叠 → 编辑距离模糊），
  对语言检测落到「其他/未知」桶的短句会按文字系统回退匹配，避免漏检。
- **上下文与来源 cue**：每个疑似项展示前后文（疑似写法高亮）、来源 cue
  编号与时间码、跨 cue 出现的全部表面形式及计数、建议替换词与原因。
- **逐项确认**：可采纳（可编辑替换词）、忽略、重置；支持按语言 / 类型 /
  状态筛选后批量采纳或批量忽略。
- **导出术语修订 WebVTT**：只应用已采纳替换，原始时间轴与 cue 边界不变，
  每条替换以 `NOTE [术语审校] 原词 → 新词` 标注。
- **导出 Markdown 审校报告**：含任务信息、按类型统计与逐项明细（决策、
  语言、来源 cue、原表面形式、建议替换、上下文、跨 cue 形式）。

> 术语审校与断句校验相互独立：断句修订可能合并/拆分 cue，而术语修订作用
> 于上传原文，两份 WebVTT 分别导出，互不影响。

## API 摘要

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/analyze` | multipart 上传 `file`、`max_chars`、`min_dwell`、`max_cps`；返回中附带 `term_review` |
| GET  | `/api/submissions` | 历史任务列表 |
| GET  | `/api/submissions/{id}` | 单次任务完整结果（支持 `?language=en` 过滤），含 `term_review` |
| GET  | `/api/submissions/{id}/export` | 下载断句修订 WebVTT |
| GET  | `/api/glossary` | 术语列表（可 `?language=en`） |
| POST | `/api/glossary` | 新增术语 |
| PUT  | `/api/glossary/{id}` | 更新术语 |
| DELETE | `/api/glossary/{id}` | 删除术语 |
| POST | `/api/glossary/import` | 批量导入 `{terms:[...], replace_all}` |
| GET  | `/api/glossary/export` | 导出全部术语 JSON |
| GET  | `/api/submissions/{id}/terms` | 术语疑似项（可按 `decision`/`language` 过滤） |
| PUT  | `/api/submissions/{id}/terms/{fid}` | 逐项决策 `{decision, replacement?}` |
| POST | `/api/submissions/{id}/terms/decisions` | 批量决策 `{decision, finding_ids?}` |
| GET  | `/api/submissions/{id}/terms/export` | 下载术语修订 WebVTT（仅已采纳项） |
| GET  | `/api/submissions/{id}/terms/report` | 下载 Markdown 审校报告 |
| GET  | `/api/health` | 健康检查 |
| GET  | `/docs` | 自动生成的接口文档 |

## 本地开发（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./data/subtitles.db uvicorn app.main:app --reload --port 8000
```

## 目录结构

```
app/
├── main.py          # FastAPI 路由（断句分析 + 术语表 + 术语审校）与静态文件
├── segmenter.py     # 单元切分 + DP 断句 + 边界重分配 + 问题生成
├── subtitles.py     # VTT/SRT 解析、VTT 导出、语言检测
├── terminology.py   # 术语归一化/三层匹配/跨 cue 审校/替换应用
├── database.py      # SQLite 初始化与持久化（任务/术语表/疑似项/决策）
└── static/          # 原生 HTML/CSS/JS（无构建步骤）
tests/smoke_test.py  # FastAPI TestClient 端到端冒烟测试
examples/            # demo.vtt / demo.srt / demo-glossary.json
Dockerfile
docker-compose.yml
requirements.txt
```

## 测试

```bash
pip install -r requirements.txt
DB_PATH=/tmp/test.db python3 tests/smoke_test.py
```
