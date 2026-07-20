"""RSS 通用采集器。每个源一个实例，解析 Atom/RSS 2.0。
依赖 feedparser（兼容性最好）。
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime

from ..models import Article
from .base import BaseCollector, fetch, log

_NUM_RE = re.compile(r"(\d+)\s*(?:points?|upvotes?|赞同|赞)", re.I)
_HN_META_RE = re.compile(r"^Article URL:.*?Points:\s*(\d+)", re.I | re.DOTALL)


def _parse_published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=t.tzinfo)
            except Exception:
                pass
    for key in ("published", "updated", "date"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def _extract_engagement(entry) -> int:
    """从标题/摘要/评论字段尽力提取热度数值。"""
    for field in ("title", "summary", "description"):
        text = entry.get(field) or ""
        if not text:
            continue
        # HN: "123 points"
        # Reddit: 通常在 itunes:summary 或 title 后缀
        m = _NUM_RE.search(text)
        if m:
            return int(m.group(1))
    # Reddit sw 频道有时有 numeric 字段
    for k in ("points", "upvotes", "score", "community_score"):
        v = entry.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


class RssCollector(BaseCollector):
    name = "rss"

    def __init__(self, source_cfg: dict, on_item=None):
        super().__init__(source_cfg, on_item)
        self.source_cfg = source_cfg
        self.name = source_cfg.get("name", "rss")

    def collect(self) -> list[Article]:
        import feedparser
        url = self.source_cfg.get("url")
        if not url:
            return []
        log.info("RSS[%s]: GET %s", self.name, url)
        res = fetch(url, timeout=30.0,
                    headers={"Accept": "application/rss+xml, application/atom+xml, "
                                       "application/xml, text/xml, */*"})
        if not res.ok:
            log.warning("RSS[%s] fetch failed: %s", self.name, res.error)
            return []
        # 用字节解析更稳
        parsed = feedparser.parse(res.text.encode("utf-8", "ignore"))
        if parsed.bozo and not parsed.entries:
            log.warning("RSS[%s] parse bozo: %s", self.name, getattr(parsed, "bozo_exception", ""))
        authority = float(self.source_cfg.get("authority", 0.5))
        tags = list(self.source_cfg.get("tags", []))
        lang = self.source_cfg.get("lang", "en")
        out: list[Article] = []
        for entry in parsed.entries:
            try:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                if not title or not link:
                    continue
                summary = (entry.get("summary") or entry.get("description") or "").strip()
                # 去 HTML 标签 + 解码实体
                summary = re.sub(r"<[^>]+>", " ", summary)
                summary = html.unescape(summary)
                summary = re.sub(r"\s+", " ", summary).strip()
                # HN RSS 摘要是 "Article URL: ... Comments URL: ... Points: N" 元数据噪音
                if _HN_META_RE.match(summary):
                    summary = ""
                published = _parse_published(entry)
                eng = _extract_engagement(entry)
                art = Article(
                    source=self.name,
                    source_type="rss",
                    title=title,
                    url=link,
                    summary=summary[:2000],
                    published=published,
                    tags=tags,
                    lang=lang,
                    authority=authority,
                    engagement=eng,
                )
                out.append(self._emit(art))
            except Exception as e:
                log.debug("RSS[%s] entry skip: %s", self.name, e)
                continue
        log.info("RSS[%s]: parsed %d entries", self.name, len(out))
        return out
