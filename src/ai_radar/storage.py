"""SQLite 持久化。轻量本地存储，无需外部数据库。
schema:
  articles(url_norm TEXT PRIMARY KEY, ...)
重复入库时 upsert：保留更高 authority/engagement，更新 score。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Article

log = logging.getLogger("ai_radar")


def _to_signed64(h: int) -> int:
    """SQLite INTEGER 是有符号 64 位；SimHash 最高位为 1 时需转换。"""
    h &= (1 << 64) - 1
    return h if h < (1 << 63) else h - (1 << 64)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url_norm      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    source        TEXT,
    source_type   TEXT,
    summary       TEXT,
    published     TEXT,
    tags          TEXT,        -- JSON array
    lang          TEXT,
    authority     REAL,
    engagement    INTEGER,
    authors       TEXT,        -- JSON array
    extra         TEXT,        -- JSON object
    score         REAL,
    title_hash    INTEGER,
    collected_at  TEXT,
    seen_days     TEXT         -- JSON array of YYYY-MM-DD，记录出现日
);
CREATE INDEX IF NOT EXISTS idx_score ON articles(score DESC);
CREATE INDEX IF NOT EXISTS idx_pub   ON articles(published DESC);
CREATE INDEX IF NOT EXISTS idx_src   ON articles(source);
"""


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def upsert_many(self, articles: Iterable[Article]) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        n = 0
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for a in articles:
                row = conn.execute(
                    "SELECT authority, engagement, score, seen_days, summary "
                    "FROM articles WHERE url_norm=?",
                    (a.url_norm,)).fetchone()
                if row:
                    # 合并：取更优字段，append seen_days
                    seen = json.loads(row["seen_days"] or "[]")
                    if today not in seen:
                        seen.append(today)
                    authority = max(row["authority"] or 0, a.authority)
                    engagement = max(row["engagement"] or 0, a.engagement)
                    score = max(row["score"] or 0, a.score)
                    summary = a.summary or row["summary"] or ""
                    conn.execute(
                        """UPDATE articles SET
                           authority=?, engagement=?, score=?, summary=?,
                           seen_days=?, collected_at=?
                         WHERE url_norm=?""",
                        (authority, engagement, score, summary,
                         json.dumps(seen, ensure_ascii=False),
                         datetime.fromtimestamp(a.collected_at,
                                                tz=timezone.utc).isoformat(),
                         a.url_norm))
                else:
                    conn.execute(
                        """INSERT INTO articles
                           (url_norm,title,url,source,source_type,summary,
                            published,tags,lang,authority,engagement,authors,
                            extra,score,title_hash,collected_at,seen_days)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (a.url_norm, a.title, a.url, a.source, a.source_type,
                         a.summary,
                         a.published.isoformat() if a.published else None,
                         json.dumps(a.tags, ensure_ascii=False),
                         a.lang, a.authority, a.engagement,
                         json.dumps(a.authors, ensure_ascii=False),
                         json.dumps(a.extra, ensure_ascii=False),
                         a.score, _to_signed64(a.title_hash),
                         datetime.fromtimestamp(a.collected_at,
                                                tz=timezone.utc).isoformat(),
                         json.dumps([today], ensure_ascii=False)))
                n += 1
        log.info("storage: upsert %d articles -> %s", n, self.db_path)
        return n

    def query_recent(self, days: int = 1, limit: int = 100,
                     tag: str | None = None) -> list[Article]:
        # 用 collected_at 判定时间窗：代表"最近 N 天采集进来"的内容，
        # 避免无 published 的社区/硬件条目被误删。
        sql = ("SELECT * FROM articles "
               "WHERE collected_at >= datetime('now', ?) ")
        params: list = [f"-{int(days)} day"]
        if tag:
            sql += "AND tags LIKE ? "
            params.append(f'%"{tag}"%')
        sql += "ORDER BY score DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_article(r) for r in rows]

    def all_since(self, since_iso: str, limit: int = 200) -> list[Article]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM articles WHERE collected_at >= ? "
                "ORDER BY score DESC LIMIT ?",
                (since_iso, limit)).fetchall()
        return [self._row_to_article(r) for r in rows]

    def query_unsummarized(self, limit: int = 50,
                           min_score: float = 0.0,
                           include_extractive: bool = True) -> list[Article]:
        """返回未做 LLM 摘要的条目。
        include_extractive=True: 抽取式兜底也算"未摘要"，会被 LLM 覆盖（默认）。
        """
        if include_extractive:
            sql = ("SELECT * FROM articles "
                   'WHERE (extra NOT LIKE \'%"summary_zh"%\' '
                   '       OR extra LIKE \'%"llm_backend": "extractive"%\' '
                   '       OR extra NOT LIKE \'%"llm_backend"%\') '
                   "AND score >= ? ORDER BY score DESC LIMIT ?")
        else:
            sql = ("SELECT * FROM articles "
                   'WHERE extra NOT LIKE \'%"summary_zh"%\' '
                   "AND score >= ? ORDER BY score DESC LIMIT ?")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, (min_score, limit)).fetchall()
        return [self._row_to_article(r) for r in rows]

    def update_summary(self, url_norm: str, extra: dict) -> None:
        """更新 extra（合并），用于写回 LLM 摘要结果。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT extra FROM articles WHERE url_norm=?",
                               (url_norm,)).fetchone()
            if not row:
                return
            cur = json.loads(row["extra"] or "{}")
            cur.update(extra)
            conn.execute("UPDATE articles SET extra=? WHERE url_norm=?",
                         (json.dumps(cur, ensure_ascii=False), url_norm))

    # ---------- 推送去重 ----------
    def mark_notified(self, url_norms: list[str]) -> int:
        """把 url_norm 标记为已推送（写入 extra.notified=true 与 notified_at）。"""
        if not url_norms:
            return 0
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        n = 0
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for u in url_norms:
                row = conn.execute("SELECT extra FROM articles WHERE url_norm=?",
                                   (u,)).fetchone()
                if not row:
                    continue
                cur = json.loads(row["extra"] or "{}")
                cur["notified"] = True
                cur["notified_at"] = ts
                conn.execute("UPDATE articles SET extra=? WHERE url_norm=?",
                             (json.dumps(cur, ensure_ascii=False), u))
                n += 1
        return n

    def query_top_unnotified(self, days: int = 1, limit: int = 10,
                             tag: str | None = None,
                             min_score: float = 0.0) -> list[Article]:
        """返回近 N 天内未推送过的高分文章。"""
        sql = ("SELECT * FROM articles "
               "WHERE collected_at >= datetime('now', ?) "
               "AND score >= ? "
               "AND (extra NOT LIKE '%\"notified\":true%') "
               "AND (extra NOT LIKE '%\"notified\": true%')")
        params: list = [f"-{int(days)} day", min_score]
        if tag:
            sql += " AND tags LIKE ?"
            params.append(f'%"{tag}"%')
        sql += " ORDER BY score DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_article(r) for r in rows]

    @staticmethod
    def _row_to_article(r: sqlite3.Row) -> Article:
        published = None
        if r["published"]:
            try:
                published = datetime.fromisoformat(r["published"])
            except ValueError:
                published = None
        collected = 0.0
        if r["collected_at"]:
            try:
                collected = datetime.fromisoformat(
                    r["collected_at"]).timestamp()
            except ValueError:
                collected = 0.0
        return Article(
            source=r["source"] or "",
            source_type=r["source_type"] or "",
            title=r["title"],
            url=r["url"],
            summary=r["summary"] or "",
            published=published,
            tags=json.loads(r["tags"] or "[]"),
            lang=r["lang"] or "en",
            authority=r["authority"] or 0.0,
            engagement=r["engagement"] or 0,
            authors=json.loads(r["authors"] or "[]"),
            extra=json.loads(r["extra"] or "{}"),
            score=r["score"] or 0.0,
            collected_at=collected,
        )
