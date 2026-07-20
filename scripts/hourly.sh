#!/usr/bin/env bash
# AI Radar 每小时增量推送脚本（供 crontab 调用）
# 用法: crontab 每小时执行一次
#   5 * * * * /home/lqp/projs/ai-radar/scripts/hourly.sh >> /home/lqp/projs/ai-radar/data/hourly.log 2>&1
#
# 前置条件:
#   1) 已配置微信推送 token（见 ~/.config/fish/config.fish 或下方方式 A）
#   2) ollama serve 已运行（若用本地摘要）

set -euo pipefail

PROJ_DIR="/home/lqp/projs/ai-radar"
PY="$PROJ_DIR/.venv/bin/python"
cd "$PROJ_DIR"

# crontab 的 PATH 很干净，补上常用路径让 fish/ollama 能被找到
export PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:${PATH:-}"

# ===== 微信推送 token（五选一）=====
# 方式 A: 直接在此处填入（最稳，推荐 crontab 用；取消注释即可）
# export AI_RADAR_WXPUSHER_SPT=SPT_xxxxxxxxx            # WxPusher 极简（最简单，推荐）
# export AI_RADAR_WXPUSHER_APP_TOKEN=AT_xxxxxxxxx       # WxPusher 标准
# export AI_RADAR_WXPUSHER_UID=UID_xxxxxxxxx
# export AI_RADAR_PUSHPLUS_TOKEN=xxxxxxxx
# export AI_RADAR_SERVERCHAN_KEY=SCTxxxxxxxx
# export AI_RADAR_WECOM_BOT_KEY=xxxxxxxx-xxxx-xxxx

# 方式 B: 从 fish config.fish 读取（你已在那里 set -gx 配过）
FISH_BIN=$(command -v fish 2>/dev/null || true)
if [ -z "$FISH_BIN" ]; then
    FISH_BIN="/usr/bin/fish"
fi
if [ -x "$FISH_BIN" ]; then
    for var in AI_RADAR_WXPUSHER_SPT \
               AI_RADAR_WXPUSHER_APP_TOKEN AI_RADAR_WXPUSHER_UID \
               AI_RADAR_PUSHPLUS_TOKEN AI_RADAR_SERVERCHAN_KEY \
               AI_RADAR_WECOM_BOT_KEY; do
        _fish_val=$("$FISH_BIN" -c "echo \$$var" 2>/dev/null || true)
        if [ -n "$_fish_val" ]; then
            export "$var=$_fish_val"
        fi
    done
fi

# 方式 C: 从 ~/.bashrc 读取（若有）
if [ -z "${AI_RADAR_WXPUSHER_SPT:-}" ] && [ -z "${AI_RADAR_WXPUSHER_APP_TOKEN:-}" ] \
   && [ -z "${AI_RADAR_PUSHPLUS_TOKEN:-}" ] && [ -z "${AI_RADAR_SERVERCHAN_KEY:-}" ] \
   && [ -z "${AI_RADAR_WECOM_BOT_KEY:-}" ] \
   && [ -f "$HOME/.bashrc" ]; then
    source "$HOME/.bashrc" 2>/dev/null || true
fi

# 本地 Ollama 绕过代理（若需要 LLM 摘要）
export no_proxy="localhost,127.0.0.1,${no_proxy:-}"
export NO_PROXY="localhost,127.0.0.1,${NO_PROXY:-}"

# 诊断输出（写到日志，便于排查 crontab 问题）
echo "[$(date '+%Y-%m-%d %H:%M:%S')] hourly.sh start; spt=${AI_RADAR_WXPUSHER_SPT:+set}; wxpusher_token=${AI_RADAR_WXPUSHER_APP_TOKEN:+set}; uid=${AI_RADAR_WXPUSHER_UID:+set}"

# 执行 hourly: 采集近 1 小时 -> 取未推送过的 Top10 -> LLM 摘要前 5 -> 推送 -> 标记
exec "$PY" -m ai_radar hourly --hours 1 --top 10 --summarize 5
