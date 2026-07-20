"""Hugging Face Daily Papers 采集器。
接口: https://huggingface.co/api/daily_papers
返回当天及近期人工精选论文列表。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from ..models import Article
from .base import BaseCollector, fetch, log

HF_DAILY = "https://huggingface.co/api/daily_papers"


class HfPapersCollector(BaseCollector):
    name = "hf_papers"

    def collect(self) -> list[Article]:
        url = self.cfg.get("url", HF_DAILY)
        log.info("HF Daily Papers: GET %s", url)
        res = fetch(url, timeout=30.0)
        if not res.ok:
            log.warning("HF fetch failed: %s", res.error)
            return []
        try:
            data = json.loads(res.text)
        except json.JSONDecodeError as e:
            log.warning("HF JSON error: %s", e)
            return []
        return self._parse(data)

    def _parse(self, data: list | dict) -> list[Article]:
        if isinstance(data, dict):
            # 有些版本包在 key 下
            data = data.get("papers") or data.get("data") or []
        authority = float(self.cfg.get("authority", 0.95))
        tags = list(self.cfg.get("tags", ["ai"]))
        lang = self.cfg.get("lang", "en")
        out: list[Article] = []

        for item in data:
            try:
                paper = item.get("paper", item) if isinstance(item, dict) else {}
                pid = paper.get("id") or paper.get("paperId") or ""
                title = (item.get("title") or paper.get("title") or "").strip()
                summary = (item.get("summary") or paper.get("summary")
                           or paper.get("abstract") or "").strip()
                authors = []
                for a in paper.get("authors", []) or []:
                    if isinstance(a, dict):
                        n = a.get("name") or a.get("fullname") or ""
                    else:
                        n = str(a)
                    if n:
                        authors.append(n)
                # 优先用顶层 publishedAt（上报道日期），更符合"每日精选"语义
                pub_str = (item.get("publishedAt")
                           or paper.get("submittedOnDailyAt")
                           or paper.get("publishedAt") or "")
                published = None
                if pub_str:
                    try:
                        published = datetime.fromisoformat(
                            pub_str.replace("Z", "+00:00"))
                    except ValueError:
                        published = None
                if not pid or not title:
                    continue
                url = f"https://huggingface.co/papers/{pid}"
                pdf = f"https://arxiv.org/pdf/{pid}.pdf"
                upvotes = int(paper.get("upvotes") or item.get("upvotes") or 0)
                num_comments = int(item.get("numComments") or 0)
                art = Article(
                    source="HF Daily Papers",
                    source_type="hf",
                    title=title,
                    url=url,
                    summary=summary,
                    published=published,
                    tags=tags,
                    lang=lang,
                    authority=authority,
                    engagement=upvotes,
                    authors=authors,
                    extra={
                        "arxiv_id": pid, "pdf": pdf,
                        "upvotes": upvotes,
                        "comments": num_comments,
                        "project": paper.get("projectPage") or "",
                        "github": paper.get("githubRepo") or "",
                    },
                )
                out.append(self._emit(art))
            except Exception as e:
                log.debug("HF entry skip: %s", e)
                continue
        log.info("HF Daily Papers: parsed %d entries", len(out))
        return out
