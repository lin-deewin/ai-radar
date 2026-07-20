"""评分排序。
score = w_auth*authority + w_rec*recency + w_eng*engagement_norm + w_nov*novelty
  recency    = exp(-Δt / half_life)
  engagement = log1p(points) / (1 + log1p(max_points))  归一到 [0,1]
  novelty    = MVP 先用源类型先验；后期接 LLM。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Iterable

from .models import Article

log = logging.getLogger("ai_radar")

_NOVELTY_PRIOR = {
    "hf": 0.85,      # 人工精选，新颖度高
    "arxiv": 0.7,
    "rss": 0.45,
    "base": 0.3,
}


def _engagement_norm(points: int, max_points: int) -> float:
    if max_points <= 0:
        return 0.0
    return math.log1p(max(0, points)) / (1.0 + math.log1p(max_points))


def score_articles(articles: Iterable[Article],
                   weights: dict | None = None,
                   now: datetime | None = None) -> list[Article]:
    w = weights or {}
    w_auth = float(w.get("authority", 0.35))
    w_rec = float(w.get("recency", 0.25))
    w_eng = float(w.get("engagement", 0.20))
    w_nov = float(w.get("novelty", 0.20))
    half_life_h = float(w.get("recency_half_life_hours", 72))

    now_dt = now or datetime.now(tz=timezone.utc)
    # 先算 engagement 归一化基准
    max_eng = max((a.engagement for a in articles), default=0)

    items = list(articles)
    for a in items:
        # recency
        if a.published:
            try:
                pub = a.published
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
                dt_h = max(0.0, (now_dt - pub).total_seconds() / 3600.0)
                recency = math.exp(-dt_h / half_life_h)
            except Exception:
                recency = 0.0
        else:
            recency = 0.0
        eng = _engagement_norm(a.engagement, max_eng)
        nov = _NOVELTY_PRIOR.get(a.source_type, 0.3)
        a.score = (w_auth * a.authority
                   + w_rec * recency
                   + w_eng * eng
                   + w_nov * nov)
    items.sort(key=lambda x: x.score, reverse=True)
    log.info("ranker: scored %d articles", len(items))
    return items
