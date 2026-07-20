"""去重与聚簇合并。
策略:
  1) URL 归一化相同 -> 合并
  2) 标题 SimHash 汉明距离 <= title_threshold -> 近重复聚簇
聚簇内保留权威分+热度最高的为代表，合并 tags/authors/源信息。
"""
from __future__ import annotations

import logging
from typing import Iterable

from .models import Article, hamming

log = logging.getLogger("ai_radar")


def _article_key_score(a: Article) -> float:
    return a.authority * 1.0 + a.engagement * 0.001 + (1.0 if a.summary else 0.0)


def dedup(articles: list[Article],
          title_threshold: int = 4,
          url_strict: bool = True) -> list[Article]:
    """返回去重后的文章列表（保持出现顺序，聚簇代表在前）。"""
    # 1) URL 精确合并
    by_url: dict[str, Article] = {}
    url_order: list[str] = []
    no_url: list[Article] = []
    for a in articles:
        if url_strict and a.url_norm:
            if a.url_norm in by_url:
                _merge_into(by_url[a.url_norm], a)
            else:
                by_url[a.url_norm] = a
                url_order.append(a.url_norm)
        else:
            no_url.append(a)

    merged_list = [by_url[k] for k in url_order] + no_url

    # 2) SimHash 近重复聚簇（贪心）
    clusters: list[list[Article]] = []
    for a in merged_list:
        placed = False
        for cl in clusters:
            if hamming(a.title_hash, cl[0].title_hash) <= title_threshold:
                cl.append(a)
                placed = True
                break
        if not placed:
            clusters.append([a])

    out: list[Article] = []
    for cl in clusters:
        cl.sort(key=_article_key_score, reverse=True)
        rep = cl[0]
        for other in cl[1:]:
            _merge_into(rep, other)
        out.append(rep)

    log.info("dedup: %d -> %d (clusters=%d)",
             len(articles), len(out), len(clusters))
    return out


def _merge_into(primary: Article, other: Article) -> None:
    """将 other 的信息合并进 primary（不覆盖已有非空值）。"""
    if not primary.summary and other.summary:
        primary.summary = other.summary
    if not primary.published and other.published:
        primary.published = other.published
    for t in other.tags:
        if t not in primary.tags:
            primary.tags.append(t)
    for au in other.authors:
        if au not in primary.authors:
            primary.authors.append(au)
    primary.engagement = max(primary.engagement, other.engagement)
    primary.authority = max(primary.authority, other.authority)
    # 记录同源链接，便于追溯
    alts = primary.extra.setdefault("alt_urls", [])
    if other.url and other.url != primary.url and other.url not in alts:
        alts.append(other.url)
    srcs = primary.extra.setdefault("alt_sources", [])
    if other.source and other.source != primary.source and other.source not in srcs:
        srcs.append(other.source)
