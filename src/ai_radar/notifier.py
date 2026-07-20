"""微信推送通知。支持多渠道，配一个 token 即可启用。

渠道:
  1) wxpusher    - WxPusher，免费、不实名、微信扫码订阅，无明确条数限制
     注册: https://wxpusher.zjiecode.com/ → 注册 → 创建应用拿 appToken
     订阅: 用应用二维码让微信扫码订阅 → 拿到 uid
     配置: AI_RADAR_WXPUSHER_APP_TOKEN=AT_xxx
           AI_RADAR_WXPUSHER_UID=UID_xxx
  2) serverchan  - Server 酱 Turbo，微信扫码绑定即可，免费 5 条/天
     注册: https://sct.ftqq.com/  → 登录拿 SENDKEY
     配置: AI_RADAR_SERVERCHAN_KEY=<SENDKEY>
  3) pushplus    - PushPlus，微信扫码绑定，免费 200 条/天
     注册: http://www.pushplus.plus/ → 登录拿 token
     配置: AI_RADAR_PUSHPLUS_TOKEN=<token>
  4) wecom_bot   - 企业微信群机器人（个人版也行），免费无限制
     建一个企业微信群 → 群设置 → 添加机器人 → 拿到 webhook key
     配置: AI_RADAR_WECOM_BOT_KEY=<key>

环境变量优先；也可在 config/notifier.yaml 配置。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

from .models import Article

log = logging.getLogger("ai_radar.notifier")


def _split_utf8(text: str, max_bytes: int = 3900) -> list[str]:
    """按 UTF-8 字节长度分片，保证不在多字节字符中间切断。"""
    if not text:
        return []
    enc = text.encode("utf-8")
    if len(enc) <= max_bytes:
        return [text]
    chunks: list[str] = []
    i = 0
    while i < len(text):
        # 贪心：尽可能多塞字符，直到字节数超限
        j = i
        cur_bytes = 0
        while j < len(text):
            c_bytes = len(text[j].encode("utf-8"))
            if cur_bytes + c_bytes > max_bytes:
                break
            cur_bytes += c_bytes
            j += 1
        chunks.append(text[i:j])
        i = j
    return chunks


@dataclass
class NotifyResult:
    ok: bool
    channel: str
    error: str = ""


class Notifier:
    """按已配置的渠道推送，多渠道可同时启用。"""

    def __init__(self,
                 wxpusher_app_token: str | None = None,
                 wxpusher_uid: str | None = None,
                 serverchan_key: str | None = None,
                 pushplus_token: str | None = None,
                 wecom_bot_key: str | None = None,
                 timeout: float = 30.0):
        self.wxpusher_app_token = (wxpusher_app_token
                                   or os.getenv("AI_RADAR_WXPUSHER_APP_TOKEN") or "")
        self.wxpusher_uid = (wxpusher_uid
                             or os.getenv("AI_RADAR_WXPUSHER_UID") or "")
        self.serverchan_key = (serverchan_key
                               or os.getenv("AI_RADAR_SERVERCHAN_KEY") or "")
        self.pushplus_token = (pushplus_token
                               or os.getenv("AI_RADAR_PUSHPLUS_TOKEN") or "")
        self.wecom_bot_key = (wecom_bot_key
                              or os.getenv("AI_RADAR_WECOM_BOT_KEY") or "")
        self.timeout = timeout

    @property
    def available_channels(self) -> list[str]:
        chs = []
        if self.wxpusher_app_token and self.wxpusher_uid:
            chs.append("wxpusher")
        if self.serverchan_key:
            chs.append("serverchan")
        if self.pushplus_token:
            chs.append("pushplus")
        if self.wecom_bot_key:
            chs.append("wecom_bot")
        return chs

    def push(self, title: str, markdown: str,
             compact: bool = False) -> list[NotifyResult]:
        """同时推送到所有已配置渠道。
        compact=True: 用紧凑 digest（适合 hourly 多条推送，避免超长）。"""
        results: list[NotifyResult] = []
        if not self.available_channels:
            log.warning("notifier: 无可用渠道，请配置环境变量 "
                        "AI_RADAR_WXPUSHER_APP_TOKEN+AI_RADAR_WXPUSHER_UID / "
                        "AI_RADAR_SERVERCHAN_KEY / AI_RADAR_PUSHPLUS_TOKEN / "
                        "AI_RADAR_WECOM_BOT_KEY")
            return [NotifyResult(ok=False, channel="none",
                                 error="no channel configured")]
        body = markdown
        if "wxpusher" in self.available_channels:
            results.append(self._push_wxpusher(title, body))
        if "serverchan" in self.available_channels:
            results.append(self._push_serverchan(title, body))
        if "pushplus" in self.available_channels:
            results.append(self._push_pushplus(title, body))
        if "wecom_bot" in self.available_channels:
            results.append(self._push_wecom(title, body))
        ok_n = sum(1 for r in results if r.ok)
        log.info("notifier: pushed via %d/%d channels", ok_n, len(results))
        return results

    # ---------- WxPusher ----------
    def _push_wxpusher(self, title: str, markdown: str) -> NotifyResult:
        # 文档: https://wxpusher.zjiecode.com/api/?wapi=/api/send/message
        # contentType: 1=文本 2=html 3=markdown
        url = "https://wxpusher.zjiecode.com/api/send/message"
        # WxPusher summary 是消息列表预览，限 21 字
        summary = (title or "AI Radar")[:21]
        # content 单条上限 ~100KB，无需分片
        payload = {
            "appToken": self.wxpusher_app_token,
            "content": markdown,
            "summary": summary,
            "contentType": 3,   # markdown
            "uids": [self.wxpusher_uid],
        }
        try:
            with httpx.Client(timeout=self.timeout) as c:
                r = c.post(url, json=payload)
                data = r.json()
                # code=1000 成功；其余失败
                if data.get("code") == 1000:
                    return NotifyResult(ok=True, channel="wxpusher")
                return NotifyResult(ok=False, channel="wxpusher",
                                    error=str(data))
        except Exception as e:
            return NotifyResult(ok=False, channel="wxpusher", error=repr(e))

    # ---------- Server 酱 ----------
    def _push_serverchan(self, title: str, markdown: str) -> NotifyResult:
        # 免费 Turbo 接口，descp 支持 Markdown
        url = f"https://sctapi.ftqq.com/{self.serverchan_key}.send"
        # Server 酱 标题限 32 字
        t = (title or "AI Radar 日报")[:32]
        try:
            with httpx.Client(timeout=self.timeout) as c:
                r = c.post(url, data={"title": t, "desp": markdown})
                data = r.json()
                if data.get("code") == 0 or r.status_code == 200:
                    return NotifyResult(ok=True, channel="serverchan")
                return NotifyResult(ok=False, channel="serverchan",
                                    error=str(data))
        except Exception as e:
            return NotifyResult(ok=False, channel="serverchan", error=repr(e))

    # ---------- PushPlus ----------
    def _push_pushplus(self, title: str, markdown: str) -> NotifyResult:
        url = "http://www.pushplus.plus/send"
        payload = {
            "token": self.pushplus_token,
            "title": title or "AI Radar 日报",
            "content": markdown,
            "template": "markdown",
        }
        try:
            with httpx.Client(timeout=self.timeout) as c:
                r = c.post(url, json=payload)
                data = r.json()
                if data.get("code") == 200:
                    return NotifyResult(ok=True, channel="pushplus")
                return NotifyResult(ok=False, channel="pushplus",
                                    error=str(data))
        except Exception as e:
            return NotifyResult(ok=False, channel="pushplus", error=repr(e))

    # ---------- 企业微信群机器人 ----------
    def _push_wecom(self, title: str, markdown: str) -> NotifyResult:
        url = (f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
               f"?key={self.wecom_bot_key}")
        # 企业微信 markdown 单条限 4096 字节，超出分片发送
        body = markdown
        chunks = _split_utf8(body, max_bytes=3900)
        last_err = ""
        ok_all = True
        for i, chunk in enumerate(chunks):
            head = f"## {title}" + (f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else "")
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": f"{head}\n{chunk}"},
            }
            try:
                with httpx.Client(timeout=self.timeout) as c:
                    r = c.post(url, json=payload)
                    data = r.json()
                    if data.get("errcode") != 0:
                        last_err = str(data)
                        ok_all = False
            except Exception as e:
                last_err = repr(e)
                ok_all = False
        if ok_all:
            return NotifyResult(ok=True, channel="wecom_bot")
        return NotifyResult(ok=False, channel="wecom_bot", error=last_err)


def build_daily_digest(articles: Iterable[Article],
                       top_per_section: int = 8) -> str:
    """生成适合微信推送的紧凑 Markdown（控制长度）。"""
    items = list(articles)
    sections = {"ai": [], "hardware": []}
    seen: set[str] = set()
    for a in items:
        for t in a.tags:
            if t in sections and a.url_norm not in seen:
                sections[t].append(a)
                seen.add(a.url_norm)
                break
    for sec in sections:
        sections[sec].sort(key=lambda x: x.score, reverse=True)
        sections[sec] = sections[sec][:top_per_section]

    lines: list[str] = []
    for sec, label in [("ai", "🧠 AI / 大模型"), ("hardware", "🔧 硬件 / 芯片")]:
        arts = sections[sec]
        if not arts:
            continue
        lines.append(f"\n### {label}\n")
        for i, a in enumerate(arts, 1):
            zh = a.extra.get("summary_zh") or ""
            topics = a.extra.get("topics") or []
            score = f"`{a.score:.2f}`"
            src = a.source
            heat = f" 🔥{a.engagement}" if a.engagement else ""
            lines.append(f"**{i}. [{a.title}]({a.url})**\n")
            lines.append(f"_{src} · {score}{heat}_\n")
            if zh:
                lines.append(f"> {zh}\n")
            if topics:
                lines.append("> 主题: " + " · ".join(f"`{t}`" for t in topics[:5]) + "\n")
            lines.append("")
    return "\n".join(lines).strip() or "今日暂无精选内容。"


def build_compact_digest(articles: Iterable[Article]) -> str:
    """更紧凑的 Markdown，适合企业微信单条 4096 字节限制。
    每条 2-3 行：标题链接 + 源/评分/摘要一行。
    """
    items = list(articles)
    lines: list[str] = []
    cur_tag = None
    for a in items:
        tag = "🔧" if "hardware" in a.tags else "🧠"
        if tag != cur_tag:
            label = "硬件 / 芯片" if tag == "🔧" else "AI / 大模型"
            lines.append(f"\n**{tag} {label}**\n")
            cur_tag = tag
        zh = (a.extra.get("summary_zh") or a.summary or "").strip()
        if len(zh) > 80:
            zh = zh[:80] + "…"
        heat = f" 🔥{a.engagement}" if a.engagement else ""
        lines.append(f"{tag} [{a.title}]({a.url})")
        lines.append(f"  _{a.source} · `{a.score:.2f}`{heat}_")
        if zh:
            lines.append(f"  > {zh}")
        lines.append("")
    return "\n".join(lines).strip()
