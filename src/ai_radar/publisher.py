"""Markdown 日报生成。
按 tag (ai / hardware / 其他) 分区，每区按 score 降序，限制条数。
输出到 reports/YYYY-MM-DD.md。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Article

log = logging.getLogger("ai_radar")


def _fmt_date(a: Article) -> str:
    if not a.published:
        return ""
    try:
        d = a.published
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        # 转本地时区显示
        return d.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _summary_excerpt(a: Article, n: int = 240) -> str:
    s = (a.summary or "").strip().replace("\n", " ")
    if len(s) > n:
        s = s[:n].rstrip() + "…"
    return s


def _lang_label(lang: str) -> str:
    return {"zh": "中文", "en": "English"}.get(lang, lang)


def render_report(articles: Iterable[Article],
                  date_str: str | None = None,
                  top_per_section: int = 30,
                  out_dir: str | Path = "reports") -> Path:
    items = list(articles)
    now = datetime.now()
    date_str = date_str or now.strftime("%Y-%m-%d")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date_str}.md"

    sections = ["ai", "hardware"]
    by_tag: dict[str, list[Article]] = {s: [] for s in sections}
    other: list[Article] = []
    for a in items:
        placed = False
        for t in a.tags:
            if t in by_tag:
                by_tag[t].append(a)
                placed = True
        if not placed:
            other.append(a)

    # 去重（同文章可能出现在多 tag，只归到第一个匹配的主 tag）
    seen_url: set[str] = set()
    for sec in sections:
        uniq = []
        for a in by_tag[sec]:
            if a.url_norm in seen_url:
                continue
            seen_url.add(a.url_norm)
            uniq.append(a)
        uniq.sort(key=lambda x: x.score, reverse=True)
        by_tag[sec] = uniq[:top_per_section]
    other = [a for a in other if a.url_norm not in seen_url]
    other.sort(key=lambda x: x.score, reverse=True)
    other = other[:top_per_section]

    lines: list[str] = []
    lines.append(f"# AI / 硬件前沿日报 — {date_str}\n")
    lines.append(f"> 自动生成于 {now.strftime('%Y-%m-%d %H:%M:%S')} "
                 f"| 共 {len(items)} 条，精选 "
                 f"{sum(len(v) for v in by_tag.values()) + len(other)} 条\n")
    lines.append("---\n")

    def render_section(title: str, arts: list[Article]):
        if not arts:
            return
        lines.append(f"\n## {title}\n")
        for i, a in enumerate(arts, 1):
            score_badge = f"`{a.score:.2f}`"
            src_badge = f"**{a.source}**"
            lang_badge = _lang_label(a.lang)
            meta = [src_badge, lang_badge, score_badge]
            if a.engagement:
                meta.append(f"热度 {a.engagement}")
            d = _fmt_date(a)
            if d:
                meta.append(d)
            # LLM 摘要优先，否则取原文
            summary_zh = a.extra.get("summary_zh") or ""
            key_points = a.extra.get("key_points") or []
            topics = a.extra.get("topics") or []
            llm_backend = a.extra.get("llm_backend") or ""
            lines.append(f"### {i}. [{a.title}]({a.url})\n")
            lines.append(f"_{' · '.join(meta)}_\n")
            if summary_zh:
                badge = ""
                if llm_backend and llm_backend != "extractive":
                    badge = f" _（{llm_backend} 摘要）_"
                lines.append(f"> **中文摘要**：{summary_zh}{badge}\n")
                if key_points:
                    lines.append("> **关键点**：\n")
                    for kp in key_points:
                        lines.append(f"> - {kp}")
                    lines.append("")
                if topics:
                    lines.append("> 主题: " + " · ".join(f"`{t}`" for t in topics) + "\n")
            else:
                ex = _summary_excerpt(a)
                if ex:
                    lines.append(f"> {ex}\n")
            extras = []
            if a.authors:
                extras.append("作者: " + ", ".join(a.authors[:3]) +
                              (f" 等{len(a.authors)}人" if len(a.authors) > 3 else ""))
            if a.extra.get("pdf"):
                extras.append(f"[PDF]({a.extra['pdf']})")
            if a.extra.get("arxiv_id"):
                extras.append(f"arXiv: `{a.extra['arxiv_id']}`")
            if a.extra.get("categories"):
                extras.append("分类: " + ", ".join(a.extra["categories"][:6]))
            if a.extra.get("github"):
                extras.append(f"[Code]({a.extra['github']})")
            if a.extra.get("project"):
                extras.append(f"[Project]({a.extra['project']})")
            alt = a.extra.get("alt_sources") or []
            if alt:
                extras.append("同源: " + ", ".join(alt[:3]))
            if extras:
                lines.append("- " + "  \n- ".join(extras) + "\n")
            lines.append("")

    render_section("🧠 AI / 大模型", by_tag["ai"])
    render_section("🔧 硬件 / 芯片", by_tag["hardware"])
    render_section("📦 其他", other)

    lines.append("---\n")
    lines.append("数据源: arXiv · HuggingFace Daily Papers · OpenAI/Anthropic/"
                 "DeepMind/Meta/MSR · Hacker News · Reddit · 机器之心 · 量子位 · "
                 "智源社区 · Chips and Cheese · ServeTheHome 等\n")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("report -> %s", out_path)
    return out_path
