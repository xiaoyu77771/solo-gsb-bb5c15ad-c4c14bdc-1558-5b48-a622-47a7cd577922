# 字幕断句校验台

基于 **FastAPI + SQLite + 原生前端** 的字幕断句校验工具。上传带时间码的
WebVTT/SRT 文件，设定每行字数、最小停留时间和阅读速度阈值后，系统会对跨
cue 文本做**动态规划断句**，必要时在相邻 cue 间**重分配边界时间**，并把
无法满足约束的**最小区段**标出并解释原因。

## 一键启动

```bash
docker compose up --build
```

启动后浏览器访问：

> http://localhost:8000

SQLite 数据库文件位于容器内 `/data/subtitles.db`，通过 Compose 命名卷
`subtitle-data` 持久化；**数据库表在应用启动时自动初始化**，无需手工建表。

`examples/` 目录提供 `demo.vtt` / `demo.srt` 示例：前两条中文 cue 时间过紧
（会产生停留/阅读速度问题），第三条英文 cue 正常，可用于体验按语言筛选。

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

## API 摘要

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/analyze` | multipart 上传 `file`、`max_chars`、`min_dwell`、`max_cps` |
| GET  | `/api/submissions` | 历史任务列表 |
| GET  | `/api/submissions/{id}` | 单次任务完整结果（支持 `?language=en` 过滤） |
| GET  | `/api/submissions/{id}/export` | 下载修订后的 WebVTT |
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
├── main.py          # FastAPI 路由与静态文件挂载
├── segmenter.py     # 单元切分 + DP 断句 + 边界重分配 + 问题生成
├── subtitles.py     # VTT/SRT 解析、VTT 导出、语言检测
├── database.py      # SQLite 初始化与持久化
└── static/          # 原生 HTML/CSS/JS（无构建步骤）
tests/smoke_test.py  # FastAPI TestClient 端到端冒烟测试
examples/            # demo.vtt / demo.srt 示例字幕
Dockerfile
docker-compose.yml
requirements.txt
```

## 测试

```bash
pip install -r requirements.txt
DB_PATH=/tmp/test.db python3 tests/smoke_test.py
```
