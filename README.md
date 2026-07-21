# AI Radar — AI / 硬件前沿情报聚合器（本地部署）
 
每日自动聚合国内外关于 **AI（模型/训练/推理/Agent/多模态）** 与 **硬件（GPU/加速器/微架构/芯片）** 的高质量内容（论文 + 博客 + 社区），去重、评分，输出 Markdown 日报。

## 特性

- **多源采集**：arXiv、Hugging Face Daily Papers、OpenAI/Anthropic/DeepMind/Meta/MSR 官方博客、Hacker News、Reddit、机器之心、量子位、智源社区、Chips and Cheese、ServeTheHome 等。
- **去重**：URL 归一化 + 标题 SimHash（Charikar 2002）近重复聚簇。
- **评分**：`权威×0.35 + 时效×0.25 + 热度×0.20 + 新颖×0.20`，时效按 72h 半衰期指数衰减。
- **本地存储**：SQLite 单文件，零外部依赖。
- **Markdown 日报**：按 `ai` / `hardware` 分区，按评分排序。

## 安装

```bash
cd ai-radar
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

> 需要 Python 3.10+（3.12/3.14 已测）。
> 依赖 `httpx[socks]`：若环境使用 SOCKS 代理（`all_proxy=socks5://...`）可自动走代理；无需代理也兼容。

## 使用

```bash
# 端到端：采集 -> 去重 -> 评分 -> 入库 -> 出日报
python -m ai_radar run

# 采集 + 对 Top30 做 LLM 中文摘要后出日报
python -m ai_radar run --summarize 30

# 仅采集某类源
python -m ai_radar collect --source arxiv
python -m ai_radar collect --source hf
python -m ai_radar collect --source rss

# 仅从库重新生成日报（不抓新数据）
python -m ai_radar report --days 1 --top 30

# 对库中已有 TopN 做 LLM 中文摘要写回
python -m ai_radar summarize --top 30

# 启动 Web 仪表盘
python -m ai_radar serve --port 8765

# 把当日精选推送到微信
python -m ai_radar notify --top 8

# 查询库
python -m ai_radar query --tag ai --days 1 --limit 20
```

输出：
- 日报：`reports/YYYY-MM-DD.md`
- 数据库：`data/ai_radar.db`
- Web：`http://127.0.0.1:8765`（文章列表 / 主题云 / 日报归档 / 一键摘要）
- 微信：`--notify` 或 `notify` 子命令

## 微信推送

四选一（配 token 即可，环境变量优先）：

| 渠道 | 注册地址 | 环境变量 | 免费额度 |
|---|---|---|---|
| **WxPusher** (推荐) | https://wxpusher.zjiecode.com/ 注册→创建应用拿 appToken→微信扫码订阅拿 uid | `AI_RADAR_WXPUSHER_APP_TOKEN` + `AI_RADAR_WXPUSHER_UID` | 免费、不实名 |
| **企业微信群机器人** | 下载企业微信App→个人微信登录(自动创建个人企业,无需企业账号)→建群→添加机器人拿key | `AI_RADAR_WECOM_BOT_KEY` | 无限制 |
| **Server 酱** | https://sct.ftqq.com/ 微信扫码 → SENDKEY | `AI_RADAR_SERVERCHAN_KEY` | 5 条/天 |
| **PushPlus** | http://www.pushplus.plus/ → token | `AI_RADAR_PUSHPLUS_TOKEN` | 200 条/天 |

> **每小时推送**推荐 WxPusher（免费不实名）或企业微信群机器人（无限制）。

### WxPusher 配置流程（个人推荐）

1. 打开 https://wxpusher.zjiecode.com/ 注册账号（用户名+密码，无需实名）
2. 后台 → **创建应用** → 填应用名/描述 → 拿到 **AppToken**（形如 `AT_xxxxxxxx`）
3. 后台 → 应用详情 → **扫码订阅**：用你的微信扫弹出的二维码 → 微信会关注 WxPusher 公众号
4. 后台 → **用户管理** → 复制你的 **UID**（形如 `UID_xxxxxxxx`）

配置（fish）：
```fish
set -Ux AI_RADAR_WXPUSHER_APP_TOKEN AT_xxxxxxxx
set -Ux AI_RADAR_WXPUSHER_UID UID_xxxxxxxx
set -Ux no_proxy "localhost,127.0.0.1"
```

配置（bash）写入 `~/.bashrc`：
```bash
export AI_RADAR_WXPUSHER_APP_TOKEN=AT_xxxxxxxx
export AI_RADAR_WXPUSHER_UID=UID_xxxxxxxx
```

> crontab 跑的是 bash 读不到 fish 变量，`scripts/hourly.sh` 已自动从 fish universal 变量取 token；也可以直接在脚本里取消注释填入。

### 每日推送

```bash
python -m ai_radar run --days 1 --top 10 --summarize 10 --notify
# 或单独推送
python -m ai_radar notify --days 1 --top 8
```

### 每小时增量推送（推荐 crontab）

```bash
# 手动跑一次：采集近 1h -> 取未推送过的 Top10 -> LLM 摘要前 5 -> 推送 -> 标记
python -m ai_radar hourly --hours 1 --top 10 --summarize 5
```

**去重机制**：每条推送过的文章会在 SQLite 中标记 `notified=true`，下次只推未推送过的高分新文章，避免重复打扰。

**crontab 配置**（每小时第 5 分钟执行）：
```bash
crontab -e
# 加入：
5 * * * * /home/lqp/projs/ai-radar/scripts/hourly.sh >> /home/lqp/projs/ai-radar/data/hourly.log 2>&1
```

`scripts/hourly.sh` 已自带：source `~/.bashrc` 取 token、设 `no_proxy` 让 Ollama 直连、调用 `hourly` 子命令。日志写到 `data/hourly.log`。

> 若环境使用代理，确保 `~/.bashrc` 里也设置 `no_proxy="localhost,127.0.0.1"`，否则 Ollama 调用会被代理拦截。

## LLM 摘要后端

自动探测，按优先级降级：

1. **Ollama 本地**（默认）：`http://localhost:11434`，模型 `qwen2.5:7b`
   - 环境变量：`OLLAMA_HOST`、`AI_RADAR_OLLAMA_MODEL`
2. **OpenAI 兼容接口**（GLM-4.6 / DeepSeek / Qwen-Max 等）：
   - 环境变量：`AI_RADAR_LLM_BASE`、`AI_RADAR_LLM_KEY`、`AI_RADAR_LLM_MODEL`
   - 示例：`export AI_RADAR_LLM_BASE=https://open.bigmodel.cn/api/paas/v4 AI_RADAR_LLM_KEY=xxx AI_RADAR_LLM_MODEL=glm-4.6`
3. **抽取式兜底**：无 LLM 时自动用关键词 + 首句，仍能生成可读摘要与主题词。

输出结构：`summary_zh`（中文摘要）/ `key_points`（关键贡献）/ `topics`（主题标签）。

## 配置

编辑 `config/sources.yaml`：
- `schedule.lookback_hours`：回溯窗口（默认 24h）
- `ranking.*`：评分权重
- `arxiv.categories`：arXiv 类别（默认 cs.AI/LG/CL/AR/PF）
- `rss_sources`：RSS 源列表，每项含 `name / url / authority / tags / lang / enabled`
  - 默认禁用项（`enabled: false`）：Anthropic / Meta AI / 机器之心 / 智源社区 / PaperWeekly —— 这些源无可用原生 RSS，建议自建 [RSSHub](https://docs.rsshub.app/) 实例后启用（配置已预留 `rsshub.app` 占位 URL）。

## 项目结构

```
ai-radar/
├── config/sources.yaml
├── scripts/hourly.sh         # crontab 定时推送脚本
├── requirements.txt
├── src/ai_radar/
│   ├── cli.py              # 命令行
│   ├── config.py           # 配置加载
│   ├── models.py           # Article 数据模型 + URL 归一化 + SimHash
│   ├── dedup.py            # 去重聚簇
│   ├── ranker.py           # 评分排序
│   ├── storage.py          # SQLite 持久化
│   ├── summarizer.py       # LLM 摘要（Ollama/OpenAI 兼容/抽取式）
│   ├── publisher.py        # Markdown 日报
│   ├── notifier.py         # 微信推送（Server酱/PushPlus/企业微信）
│   ├── web.py              # FastAPI Web 仪表盘
│   └── collectors/
│       ├── base.py         # HTTP fetch + 基类
│       ├── arxiv.py        # arXiv Atom API
│       ├── hf_papers.py    # HF Daily Papers JSON
│       └── rss.py          # feedparser 通用
├── data/                   # SQLite
└── reports/                # Markdown 日报
```

## 合规

- 抓取遵守 robots.txt 与各源 ToS；限速 + 重试退避。
- 仅采集元数据与摘要，正文遵循各自版权；日报只提供链接与摘要。

## 路线图

- [x] LLM 摘要（Ollama/GLM/Qwen + 抽取式降级）
- [x] Web 仪表盘（FastAPI + 内嵌前端）
- [ ] 向量聚类（BGE-M3 + sqlite-vec）按主题自动分组
- [ ] Semantic Scholar 引用增速接入
- [ ] Telegram / 飞书推送
- [ ] 反馈回路（点击/收藏回流排序）
