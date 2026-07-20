"""命令行入口。

用法:
  python -m ai_radar run                  # 采集 -> 去重 -> 评分 -> 入库 -> 出日报
  python -m ai_radar collect [--source all|arxiv|hf|rss]
  python -m ai_radar report [--days 1] [--top 30]   # 仅从库重新出日报
  python -m ai_radar query --tag ai --days 1 --limit 20
"""
from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import load_config
from .collectors import ArxivCollector, HfPapersCollector, RssCollector
from .dedup import dedup
from .models import Article
from .notifier import Notifier, build_compact_digest, build_daily_digest
from .publisher import render_report
from .ranker import score_articles
from .storage import Storage
from .summarizer import Summarizer, attach_summary

log = logging.getLogger("ai_radar")


def _setup_logger(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _filter_lookback(articles: list[Article], hours: int) -> list[Article]:
    if hours <= 0:
        return articles
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    keep: list[Article] = []
    for a in articles:
        if a.published:
            try:
                pub = a.published
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
                if pub >= cutoff:
                    keep.append(a)
            except Exception:
                keep.append(a)
        else:
            # 无时间戳的（社区热门源当日 top）保留
            keep.append(a)
    return keep


def collect_all(cfg, source: str = "all") -> list[Article]:
    arts: list[Article] = []
    if source in ("all", "arxiv"):
        arts += ArxivCollector(cfg.arxiv).collect()
    if source in ("all", "hf"):
        arts += HfPapersCollector(cfg.hf_papers).collect()
    if source in ("all", "rss"):
        rss_cfgs = [s for s in cfg.rss_sources if s.get("enabled", True)]
        disabled = len(cfg.rss_sources) - len(rss_cfgs)
        log.info("RSS: %d sources (%d disabled)", len(rss_cfgs), disabled)
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {pool.submit(RssCollector(s).collect): s for s in rss_cfgs}
            for f in as_completed(futs):
                try:
                    arts += f.result()
                except Exception as e:
                    log.warning("RSS source %s error: %s",
                                futs[f].get("name"), e)
    log.info("collected total: %d", len(arts))
    return arts


def cmd_run(args, cfg) -> int:
    raw = collect_all(cfg, args.source)
    hours = args.days * 24 if args.days else cfg.lookback_hours
    raw = _filter_lookback(raw, hours)
    log.info("after lookback(%dh): %d", hours, len(raw))
    unique = dedup(raw)
    scored = score_articles(unique, weights=cfg.ranking)
    db_path = cfg.base_dir / "data" / "ai_radar.db"
    store = Storage(db_path)
    store.upsert_many(scored)
    if args.summarize:
        _summarize_top(store, args.summarize)
    # 出日报前重新从库取（含最新摘要）
    arts = store.query_recent(days=(args.days or 3), limit=500)
    arts = score_articles(arts, weights=cfg.ranking)
    out = render_report(arts, top_per_section=args.top,
                         out_dir=cfg.base_dir / "reports")
    print(f"\n✅ 日报: {out}\n   库:  {db_path}  | 入库 {len(scored)} 条")
    if args.notify:
        _push_wechat(arts, top_per_section=args.top)
    return 0


def cmd_collect(args, cfg) -> int:
    raw = collect_all(cfg, args.source)
    hours = args.days * 24 if args.days else cfg.lookback_hours
    raw = _filter_lookback(raw, hours)
    unique = dedup(raw)
    scored = score_articles(unique, weights=cfg.ranking)
    db_path = cfg.base_dir / "data" / "ai_radar.db"
    n = Storage(db_path).upsert_many(scored)
    print(f"✅ 采集 {len(scored)} 条，入库 {n} -> {db_path}")
    return 0


def cmd_report(args, cfg) -> int:
    db_path = cfg.base_dir / "data" / "ai_radar.db"
    store = Storage(db_path)
    arts = store.query_recent(days=args.days, limit=500)
    if not arts:
        print("库中无近 %d 天数据，请先 `run` 或 `collect`。", args.days)
        return 1
    arts = score_articles(arts, weights=cfg.ranking)
    out = render_report(arts, top_per_section=args.top,
                         out_dir=cfg.base_dir / "reports")
    print(f"✅ 日报: {out}  | 取 {len(arts)} 条")
    return 0


def cmd_query(args, cfg) -> int:
    db_path = cfg.base_dir / "data" / "ai_radar.db"
    store = Storage(db_path)
    arts = store.query_recent(days=args.days, limit=args.limit, tag=args.tag)
    if not arts:
        print("无匹配。")
        return 1
    for a in arts:
        print(f"[{a.score:.2f}] {a.source:18s} | {a.title[:80]}\n"
              f"          {a.url}")
    return 0


def _summarize_top(store: Storage, n: int, min_score: float = 0.5) -> int:
    """对库中 TopN 未摘要条目做 LLM 摘要并写回。"""
    sm = Summarizer()
    if sm.backend == "extractive":
        log.warning("summarize: 无 LLM 可用，将全部走抽取式兜底。")
    arts = store.query_unsummarized(limit=n, min_score=min_score)
    log.info("summarize: %d articles to do (backend=%s)",
             len(arts), sm.backend)
    done = 0
    for i, a in enumerate(arts, 1):
        try:
            r = sm.summarize(a)
            attach_summary(a, r)
            store.update_summary(a.url_norm, {
                "summary_zh": r.summary_zh,
                "key_points": r.key_points,
                "topics": r.topics,
                "llm_backend": r.backend,
                "llm_model": r.model,
            })
            done += 1
            if i % 5 == 0 or i == len(arts):
                log.info("summarize: %d/%d (%s)", i, len(arts), sm.backend)
        except Exception as e:
            log.warning("summarize fail [%s]: %s", a.url_norm, e)
    log.info("summarize: done %d/%d", done, len(arts))
    return done


def cmd_summarize(args, cfg) -> int:
    db_path = cfg.base_dir / "data" / "ai_radar.db"
    store = Storage(db_path)
    n = _summarize_top(store, args.top, min_score=args.min_score)
    print(f"✅ 摘要 {n} 条 -> {db_path}")
    return 0


def cmd_serve(args, cfg) -> int:
    from .web import create_app
    db_path = cfg.base_dir / "data" / "ai_radar.db"
    app = create_app(db_path, reports_dir=cfg.base_dir / "reports")
    print(f"✅ Web 服务启动: http://127.0.0.1:{args.port}  (DB: {db_path})")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _push_wechat(arts: list[Article], top_per_section: int = 8,
                 title: str | None = None,
                 compact: bool = False) -> bool:
    """把精选摘要推送到微信。需配置 token。
    compact=True: 用更紧凑格式，适合每小时多条推送。"""
    title = title or f"AI Radar 日报 {datetime.now().strftime('%Y-%m-%d')}"
    md = (build_compact_digest(arts) if compact
          else build_daily_digest(arts, top_per_section=top_per_section))
    nf = Notifier()
    if not nf.available_channels:
        print("⚠️  微信推送跳过：未配置 token。可选渠道：")
        print("   1) Server 酱:  https://sct.ftqq.com/        "
              "→ env AI_RADAR_SERVERCHAN_KEY  (免费 5 条/天)")
        print("   2) PushPlus:   http://www.pushplus.plus/    "
              "→ env AI_RADAR_PUSHPLUS_TOKEN  (免费 200 条/天)")
        print("   3) 企业微信机器人: 群内添加机器人         "
              "→ env AI_RADAR_WECOM_BOT_KEY   (无限制，推荐)")
        return False
    results = nf.push(title, md, compact=compact)
    for r in results:
        mark = "✅" if r.ok else "❌"
        extra = f" ({r.error[:80]})" if r.error else ""
        print(f"   {mark} {r.channel}{extra}")
    return all(r.ok for r in results)


def cmd_notify(args, cfg) -> int:
    db_path = cfg.base_dir / "data" / "ai_radar.db"
    store = Storage(db_path)
    arts = store.query_recent(days=args.days, limit=500)
    if not arts:
        print("库中无近 %d 天数据，请先 `run` 或 `collect`。", args.days)
        return 1
    arts = score_articles(arts, weights=cfg.ranking)
    ok = _push_wechat(arts, top_per_section=args.top)
    return 0 if ok else 2


def cmd_hourly(args, cfg) -> int:
    """每小时增量推送：采集 -> 评分 -> 取过去 N 小时未推送过的 TopN -> 推送 -> 标记。
    用法示例: python -m ai_radar hourly --hours 1 --top 10
    适合 crontab: 0 * * * *  cd /path && .venv/bin/python -m ai_radar hourly
    """
    db_path = cfg.base_dir / "data" / "ai_radar.db"
    store = Storage(db_path)
    # 1) 增量采集
    log.info("hourly: 采集增量...")
    raw = collect_all(cfg, "all")
    hours = args.hours
    raw = _filter_lookback(raw, hours)
    log.info("hourly: 采集到 %d 条 (近 %dh)", len(raw), hours)
    if not raw:
        print(f"hourly: 近 {hours}h 无新内容，跳过。")
        return 0
    # 2) 去重评分入库
    unique = dedup(raw)
    scored = score_articles(unique, weights=cfg.ranking)
    store.upsert_many(scored)
    # 3) 重新评分（合并历史，统一排序）
    all_recent = store.query_recent(days=max(1, hours // 24 + 1), limit=2000)
    all_recent = score_articles(all_recent, weights=cfg.ranking)
    # 4) 取未推送过的 Top N
    notified_set = {a.url_norm for a in all_recent
                    if a.extra.get("notified")}
    candidates = [a for a in all_recent
                  if a.url_norm not in notified_set]
    # 优先新进来的
    new_set = {a.url_norm for a in scored}
    candidates.sort(key=lambda a: (
        0 if a.url_norm in new_set else 1,
        -a.score,
        -(a.engagement or 0),
    ))
    top = candidates[:args.top]
    if not top:
        print(f"hourly: 无新增可推送内容（已推送过或无新条目）。")
        return 0
    # 5) LLM 摘要（可选）
    if args.summarize > 0:
        sm = Summarizer()
        todo = [a for a in top
                if not a.extra.get("summary_zh")
                or a.extra.get("llm_backend") == "extractive"]
        if todo:
            log.info("hourly: LLM 摘要 %d/%d (backend=%s)",
                     len(todo), len(top), sm.backend)
            for a in todo[:args.summarize]:
                try:
                    r = sm.summarize(a)
                    attach_summary(a, r)
                    store.update_summary(a.url_norm, {
                        "summary_zh": r.summary_zh,
                        "key_points": r.key_points,
                        "topics": r.topics,
                        "llm_backend": r.backend,
                        "llm_model": r.model,
                    })
                    a.extra["summary_zh"] = r.summary_zh
                    a.extra["key_points"] = r.key_points
                    a.extra["topics"] = r.topics
                    a.extra["llm_backend"] = r.backend
                except Exception as e:
                    log.warning("hourly summary fail: %s", e)
    # 6) 构造推送内容（紧凑格式，适合多条）
    now_str = datetime.now().strftime("%m-%d %H:%M")
    new_n = sum(1 for a in top if a.url_norm in new_set)
    title = f"AI Radar · {now_str} | +{new_n} 新 / Top{len(top)}"
    # 7) 推送
    print(f"hourly: 推送 {len(top)} 条 (新增 {new_n}, 其余为未推送历史高分)")
    ok = _push_wechat(top, top_per_section=args.top, title=title,
                       compact=True)
    # 8) 标记已推送
    if ok:
        store.mark_notified([a.url_norm for a in top])
        print(f"hourly: ✅ 已标记 {len(top)} 条为已推送")
        return 0
    else:
        print("hourly: ❌ 推送失败，未标记。")
        return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai-radar",
                                description="AI / 硬件前沿情报聚合器")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--config", default=None, help="配置文件路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="采集 -> 评分 -> 入库 -> 出日报")
    pr.add_argument("--source", choices=["all", "arxiv", "hf", "rss"],
                    default="all")
    pr.add_argument("--days", type=int, default=0,
                    help="回溯天数，覆盖配置 lookback_hours")
    pr.add_argument("--top", type=int, default=30)
    pr.add_argument("--summarize", type=int, default=0, metavar="N",
                    help="对库中 TopN 条目做 LLM 摘要后写入日报")
    pr.add_argument("--notify", action="store_true",
                    help="把日报精选推送到微信（需配置 token）")
    pr.set_defaults(func=cmd_run)

    pc = sub.add_parser("collect", help="仅采集并入库")
    pc.add_argument("--source", choices=["all", "arxiv", "hf", "rss"],
                    default="all")
    pc.add_argument("--days", type=int, default=0,
                    help="回溯天数，覆盖配置 lookback_hours")
    pc.set_defaults(func=cmd_collect)

    prp = sub.add_parser("report", help="从库重新生成日报")
    prp.add_argument("--days", type=int, default=1)
    prp.add_argument("--top", type=int, default=30)
    prp.set_defaults(func=cmd_report)

    pq = sub.add_parser("query", help="查询库中近 N 天")
    pq.add_argument("--tag", choices=["ai", "hardware"], default=None)
    pq.add_argument("--days", type=int, default=1)
    pq.add_argument("--limit", type=int, default=20)
    pq.set_defaults(func=cmd_query)

    ps = sub.add_parser("summarize", help="对库中 TopN 做 LLM 中文摘要并写回")
    ps.add_argument("--top", type=int, default=30)
    ps.add_argument("--min-score", type=float, default=0.5)
    ps.set_defaults(func=cmd_summarize)

    pv = sub.add_parser("serve", help="启动 Web 仪表盘")
    pv.add_argument("--host", default="127.0.0.1")
    pv.add_argument("--port", type=int, default=8765)
    pv.set_defaults(func=cmd_serve)

    pn = sub.add_parser("notify", help="把库中精选推送到微信")
    pn.add_argument("--days", type=int, default=1)
    pn.add_argument("--top", type=int, default=8)
    pn.set_defaults(func=cmd_notify)

    ph = sub.add_parser("hourly",
                        help="每小时增量推送：采集->TopN 未推送->微信->标记")
    ph.add_argument("--hours", type=int, default=1,
                    help="采集回溯窗口（小时，默认 1）")
    ph.add_argument("--top", type=int, default=10,
                    help="每次推送条数（默认 10）")
    ph.add_argument("--summarize", type=int, default=5,
                    help="对前 N 条做 LLM 摘要（默认 5，设 0 跳过）")
    ph.set_defaults(func=cmd_hourly)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logger(args.verbose)
    cfg = load_config(args.config)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
