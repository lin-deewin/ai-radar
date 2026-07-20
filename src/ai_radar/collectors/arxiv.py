"""arXiv 采集器。使用官方 Atom API，按类别与提交时间检索。
API 文档: https://info.arxiv.org/help/api/index.html
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from ..models import Article
from .base import BaseCollector, fetch, log

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
API = "http://export.arxiv.org/api/query"


class ArxivCollector(BaseCollector):
    name = "arxiv"

    def collect(self) -> list[Article]:
        cats = self.cfg.get("categories", [])
        if not cats:
            return []
        query = "+OR+".join(f"cat:{c}" for c in cats)
        max_r = int(self.cfg.get("max_results", 200))
        url = (f"{API}?search_query={query}"
               f"&sortBy=submittedDate&sortOrder=descending&max_results={max_r}")
        log.info("arXiv: GET %s", url)
        res = fetch(url, timeout=60.0)
        if not res.ok:
            log.warning("arXiv fetch failed: %s", res.error)
            return []
        return self._parse(res.text)

    def _parse(self, xml_text: str) -> list[Article]:
        out: list[Article] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            log.warning("arXiv parse error: %s", e)
            return out

        authority = float(self.cfg.get("authority", 0.9))
        lang = self.cfg.get("lang", "en")
        hw_cats = {"cs.AR", "cs.PF", "cs.DC", "eess.AR", "eess.SP"}

        for entry in root.findall(f"{ATOM}entry"):
            try:
                raw_id = (entry.findtext(f"{ATOM}id") or "").strip()
                # id 形如 http://arxiv.org/abs/2401.00001v1 -> 取 abs URL
                url = raw_id
                title = " ".join((entry.findtext(f"{ATOM}title") or "").split())
                summary = " ".join((entry.findtext(f"{ATOM}summary") or "").split())
                pub = entry.findtext(f"{ATOM}published") or ""
                published = None
                if pub:
                    published = datetime.fromisoformat(
                        pub.replace("Z", "+00:00"))
                authors = []
                for a in entry.findall(f"{ATOM}author"):
                    n = a.findtext(f"{ATOM}name")
                    if n:
                        authors.append(n.strip())
                pdf = ""
                for link in entry.findall(f"{ATOM}link"):
                    if link.get("title") == "pdf":
                        pdf = link.get("href", "")
                        break
                doi = entry.findtext(f"{ARXIV}doi") or ""
                comment = entry.findtext(f"{ARXIV}comment") or ""
                cats_e = [c.get("term") for c in entry.findall(f"{ATOM}category")
                          if c.get("term")]
                if not title or not url:
                    continue
                # 按类别打 tag：含硬件/体系结构类别归 hardware，否则 ai
                if any(c in hw_cats for c in cats_e):
                    tags = ["hardware"]
                else:
                    tags = ["ai"]
                art = Article(
                    source="arXiv",
                    source_type="arxiv",
                    title=title,
                    url=url,
                    summary=summary,
                    published=published,
                    tags=tags,
                    lang=lang,
                    authority=authority,
                    authors=authors,
                    extra={
                        "pdf": pdf, "doi": doi, "comment": comment,
                        "categories": cats_e,
                    },
                )
                out.append(self._emit(art))
            except Exception as e:
                log.debug("arXiv entry skip: %s", e)
                continue
        log.info("arXiv: parsed %d entries", len(out))
        return out
