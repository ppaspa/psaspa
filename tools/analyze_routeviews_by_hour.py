#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from collections import defaultdict
from pathlib import Path

from bgp_realism_metrics import compute_metrics


ARCHIVE_BASE = "https://archive.routeviews.org"
PAPER_ROUTEVIEWS_DATES = [
    "2026-05-04",
    "2026-05-05",
]


def parse_collector_list(raw: str) -> list[str]:
    collectors = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            collectors.append(item)
    if not collectors:
        raise ValueError("nenhum coletor informado")
    return collectors


def parse_hour_list(raw: str) -> list[int]:
    hours = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        hour = int(item)
        if hour < 0 or hour > 23:
            raise ValueError(f"hora invalida: {item}")
        hours.append(hour)
    if not hours:
        raise ValueError("nenhuma hora informada")
    return hours


def parse_date_list(raw: str) -> list[dt.date]:
    dates = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        dates.append(dt.date.fromisoformat(item))
    if not dates:
        raise ValueError("nenhuma data informada")
    return dates


def parse_days(args) -> list[dt.date]:
    if args.date:
        return [dt.date.fromisoformat(args.date)]
    if args.dates:
        return parse_date_list(args.dates)
    if args.date_start and args.date_end:
        start = dt.date.fromisoformat(args.date_start)
        end = dt.date.fromisoformat(args.date_end)
        if end < start:
            raise ValueError("date_end deve ser >= date_start")
        out = []
        cur = start
        while cur <= end:
            out.append(cur)
            cur += dt.timedelta(days=1)
        return out
    raise ValueError("informe --date, --dates, ou --date-start com --date-end")


def build_slots(day: dt.date, hour: int, window_files: int, slot_minutes: int) -> list[dt.datetime]:
    start = dt.datetime.combine(day, dt.time(hour=hour, minute=0))
    return [start + dt.timedelta(minutes=slot_minutes * i) for i in range(window_files)]


def routeviews_url(collector: str, stamp: dt.datetime) -> str:
    month = stamp.strftime("%Y.%m")
    filename = stamp.strftime("updates.%Y%m%d.%H%M.bz2")
    return f"{ARCHIVE_BASE}/{collector}/bgpdata/{month}/UPDATES/{filename}"


def cache_path(cache_dir: Path, collector: str, stamp: dt.datetime) -> Path:
    month = stamp.strftime("%Y.%m")
    filename = stamp.strftime("updates.%Y%m%d.%H%M.bz2")
    return cache_dir / collector / month / filename


def ensure_file(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return destination

    subprocess.run(
        ["curl", "-L", "-o", str(destination), url],
        check=True,
    )
    return destination


def summarize_windows(results: list[dict]) -> dict:
    if not results:
        return {}

    def stats(key: str) -> dict:
        values = [item["metrics"][key] for item in results]
        return {
            "min": min(values),
            "max": max(values),
            "avg": round(sum(values) / len(values), 6),
        }

    return {
        "windows": len(results),
        "announcements_total": stats("announcements_total"),
        "withdrawals_total": stats("withdrawals_total"),
        "unique_origins_total": stats("unique_origins_total"),
        "unique_prefix_origin_total": stats("unique_prefix_origin_total"),
        "origin_diversity_pct_over_announcements": stats("origin_diversity_pct_over_announcements"),
        "origin_diversity_pct_over_unique_prefix_origin": stats("origin_diversity_pct_over_unique_prefix_origin"),
        "new_origin_announcement_pct": stats("new_origin_announcement_pct"),
        "origins_with_multi_upstreams_pct": stats("origins_with_multi_upstreams_pct"),
        "origins_with_observed_upstream_specific_prefixes_pct": stats("origins_with_observed_upstream_specific_prefixes_pct"),
        "origins_with_observed_disjoint_upstream_prefixes_pct": stats("origins_with_observed_disjoint_upstream_prefixes_pct"),
        "exclusive_prefix_share_within_multi_upstream_origins_pct": stats("exclusive_prefix_share_within_multi_upstream_origins_pct"),
        "non_dominant_prefix_share_within_multi_upstream_origins_pct": stats("non_dominant_prefix_share_within_multi_upstream_origins_pct"),
    }


def summarize_grouped(results: list[dict], key_name: str) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in results:
        grouped[str(item[key_name])].append(item)

    return {group: summarize_windows(items) for group, items in sorted(grouped.items())}


def format_distribution(name: str, data: dict) -> list[str]:
    return [
        f"{name}:",
        f"  count={data['count']} min={data['min']} p50={data['p50']} p75={data['p75']} p90={data['p90']} p95={data['p95']} p99={data['p99']} max={data['max']} mean={data['mean']}",
    ]


def render_report(
    collectors: list[str],
    days: list[dt.date],
    hours: list[int],
    window_files: int,
    slot_minutes: int,
    results: list[dict],
) -> str:
    lines = []
    lines.append(f"Analise Route Views por horario")
    if len(collectors) == 1:
        lines.append(f"coletor: {collectors[0]}")
    else:
        lines.append(f"coletores: {', '.join(collectors)}")
    if len(days) == 1:
        lines.append(f"data: {days[0].isoformat()}")
    else:
        lines.append(f"dias analisados: {', '.join(day.isoformat() for day in days)}")
    lines.append(f"horas analisadas: {', '.join(f'{h:02d}:00' for h in hours)}")
    lines.append(f"janela por horario: {window_files} arquivos de {slot_minutes} min")
    lines.append("")

    overall = summarize_windows(results)
    if overall:
        lines.append("Resumo consolidado entre janelas")
        for key, stats in overall.items():
            if key == "windows":
                lines.append(f"- windows: {stats}")
            else:
                lines.append(f"- {key}: min={stats['min']} max={stats['max']} avg={stats['avg']}")
        lines.append("")

    if len(collectors) > 1:
        lines.append("Resumo consolidado por coletor")
        by_collector = summarize_grouped(results, "collector")
        for collector_key, summary in by_collector.items():
            lines.append(f"- coletor {collector_key}")
            lines.append(
                f"  windows={summary['windows']} "
                f"announcements_avg={summary['announcements_total']['avg']} "
                f"origins_avg={summary['unique_origins_total']['avg']} "
                f"origin_diversity_prefix_origin_avg={summary['origin_diversity_pct_over_unique_prefix_origin']['avg']} "
                f"multi_upstream_avg={summary['origins_with_multi_upstreams_pct']['avg']} "
                f"upstream_specific_avg={summary['origins_with_observed_upstream_specific_prefixes_pct']['avg']} "
                f"non_dominant_avg={summary['non_dominant_prefix_share_within_multi_upstream_origins_pct']['avg']}"
            )
        lines.append("")

    if len(days) > 1:
        lines.append("Resumo consolidado por dia")
        by_day = summarize_grouped(results, "day")
        for day_key, summary in by_day.items():
            lines.append(f"- dia {day_key}")
            lines.append(
                f"  windows={summary['windows']} "
                f"announcements_avg={summary['announcements_total']['avg']} "
                f"origins_avg={summary['unique_origins_total']['avg']} "
                f"origin_diversity_prefix_origin_avg={summary['origin_diversity_pct_over_unique_prefix_origin']['avg']} "
                f"multi_upstream_avg={summary['origins_with_multi_upstreams_pct']['avg']} "
                f"upstream_specific_avg={summary['origins_with_observed_upstream_specific_prefixes_pct']['avg']} "
                f"non_dominant_avg={summary['non_dominant_prefix_share_within_multi_upstream_origins_pct']['avg']}"
            )
        lines.append("")

    lines.append("Resumo consolidado por horario")
    by_hour = summarize_grouped(results, "hour")
    for hour_key, summary in by_hour.items():
        lines.append(f"- horario {int(hour_key):02d}:00")
        lines.append(
            f"  windows={summary['windows']} "
            f"announcements_avg={summary['announcements_total']['avg']} "
            f"origins_avg={summary['unique_origins_total']['avg']} "
            f"origin_diversity_prefix_origin_avg={summary['origin_diversity_pct_over_unique_prefix_origin']['avg']} "
            f"multi_upstream_avg={summary['origins_with_multi_upstreams_pct']['avg']} "
            f"upstream_specific_avg={summary['origins_with_observed_upstream_specific_prefixes_pct']['avg']} "
            f"non_dominant_avg={summary['non_dominant_prefix_share_within_multi_upstream_origins_pct']['avg']}"
        )
    lines.append("")

    for result in results:
        metrics = result["metrics"]
        lines.append(f"Janela {result['label']}")
        lines.append(f"coletor: {result['collector']}")
        lines.append(f"arquivos: {len(result['files'])}")
        for path in result["files"]:
            lines.append(f"  - {path}")
        lines.append("")
        lines.append("Carga")
        lines.append(f"- messages_total: {metrics['messages_total']}")
        lines.append(f"- announcements_total: {metrics['announcements_total']}")
        lines.append(f"- withdrawals_total: {metrics['withdrawals_total']}")
        lines.append("")
        lines.append("Origens e reutilizacao")
        lines.append(f"- unique_origins_total: {metrics['unique_origins_total']}")
        lines.append(f"- unique_prefix_origin_total: {metrics['unique_prefix_origin_total']}")
        lines.append(f"- origin_diversity_pct_over_announcements: {metrics['origin_diversity_pct_over_announcements']}")
        lines.append(f"- origin_diversity_pct_over_unique_prefix_origin: {metrics['origin_diversity_pct_over_unique_prefix_origin']}")
        lines.append(f"- announcements_with_new_origin_total: {metrics['announcements_with_new_origin_total']}")
        lines.append(f"- new_origin_announcement_pct: {metrics['new_origin_announcement_pct']}")
        lines.append(f"- reused_origin_announcement_pct: {metrics['reused_origin_announcement_pct']}")
        lines.append("")
        lines.append("Vizinhanca e transito parcial")
        lines.append(f"- unique_origin_upstream_pairs_total: {metrics['unique_origin_upstream_pairs_total']}")
        lines.append(f"- origins_with_multi_upstreams_count: {metrics['origins_with_multi_upstreams_count']}")
        lines.append(f"- origins_with_multi_upstreams_pct: {metrics['origins_with_multi_upstreams_pct']}")
        lines.append(f"- origins_with_prefix_split_count: {metrics['origins_with_prefix_split_count']}")
        lines.append(f"- origins_with_prefix_split_pct: {metrics['origins_with_prefix_split_pct']}")
        lines.append(f"- origins_with_observed_upstream_specific_prefixes_count: {metrics['origins_with_observed_upstream_specific_prefixes_count']}")
        lines.append(f"- origins_with_observed_upstream_specific_prefixes_pct: {metrics['origins_with_observed_upstream_specific_prefixes_pct']}")
        lines.append(f"- origins_with_observed_disjoint_upstream_prefixes_count: {metrics['origins_with_observed_disjoint_upstream_prefixes_count']}")
        lines.append(f"- origins_with_observed_disjoint_upstream_prefixes_pct: {metrics['origins_with_observed_disjoint_upstream_prefixes_pct']}")
        lines.append(f"- exclusive_prefix_share_within_multi_upstream_origins_pct: {metrics['exclusive_prefix_share_within_multi_upstream_origins_pct']}")
        lines.append(f"- non_dominant_prefix_share_within_multi_upstream_origins_pct: {metrics['non_dominant_prefix_share_within_multi_upstream_origins_pct']}")
        lines.append("")
        lines.extend(format_distribution("prefixes_per_origin_distribution", metrics["prefixes_per_origin_distribution"]))
        lines.extend(format_distribution("upstreams_per_origin_distribution", metrics["upstreams_per_origin_distribution"]))
        lines.append("")
        lines.append("Top origins by announcements")
        for origin, count in metrics["top_origins_by_announcements"][:10]:
            lines.append(f"- AS{origin}: {count}")
        lines.append("")
        lines.append("Sample of multi-upstream origins")
        for item in metrics["sample_multi_upstream_origins"][:10]:
            lines.append(
                f"- AS{item['origin']}: upstreams={item['distinct_upstreams']} prefixes={item['prefixes']} ups={','.join(item['upstreams'][:6])}"
            )
        lines.append("")
        lines.append("Sample of non-dominant share")
        for item in metrics["sample_non_dominant_prefix_share"][:10]:
            lines.append(
                f"- AS{item['origin']}: prefixes={item['prefixes']} upstreams={item['distinct_upstreams']} dominant={item['dominant_upstream']} dominant_cover={item['dominant_prefix_cover']} non_dominant={item['non_dominant_prefixes']}"
            )
        lines.append("")
        lines.append("Sample of upstream-specific prefixes")
        for item in metrics["sample_upstream_specific_prefixes"][:10]:
            lines.append(
                f"- AS{item['origin']}: prefixes={item['prefixes']} upstreams={item['distinct_upstreams']} exclusive_prefixes={item['exclusive_prefixes']} exclusive_upstreams={item['exclusive_upstreams']} ups={','.join(item['top_exclusive_upstreams'])}"
            )
        lines.append("")
        lines.append("=" * 72)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Download and analyze Route Views updates by hour, saving reports as txt files.")
    parser.add_argument("--collector", default="route-views2")
    parser.add_argument("--collectors", help="Comma-separated collector list, e.g. route-views2,route-views.sg,route-views.eqix")
    parser.add_argument("--date", help="Single date in YYYY-MM-DD format")
    parser.add_argument("--dates", help="Comma-separated date list, e.g. 2026-05-04,2026-05-05")
    parser.add_argument("--date-start", help="Start date for an interval in YYYY-MM-DD format")
    parser.add_argument("--date-end", help="End date for an interval in YYYY-MM-DD format")
    parser.add_argument("--hours", required=True, help="Comma-separated hours, e.g. 0,6,12,18")
    parser.add_argument("--window-files", type=int, default=8, help="Number of 15-minute files per window")
    parser.add_argument("--slot-minutes", type=int, default=15)
    parser.add_argument("--cache-dir", default="/tmp/routeviews-cache")
    parser.add_argument("--output-dir", default=str(Path(__file__).resolve().parents[1] / "outputs" / "routeviews-hourly"))
    parser.add_argument("--reuse-only", action="store_true", help="Do not download anything new; fail if the file is not already in cache")
    args = parser.parse_args()

    days = parse_days(args)
    hours = parse_hour_list(args.hours)
    collectors = parse_collector_list(args.collectors) if args.collectors else [args.collector]
    cache_dir = Path(args.cache_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for collector in collectors:
        for day in days:
            for hour in hours:
                stamps = build_slots(day, hour, args.window_files, args.slot_minutes)
                files = []
                for stamp in stamps:
                    path = cache_path(cache_dir, collector, stamp)
                    if not path.exists():
                        if args.reuse_only:
                            raise FileNotFoundError(f"arquivo ausente no cache: {path}")
                        ensure_file(routeviews_url(collector, stamp), path)
                    files.append(str(path))

                metrics = compute_metrics(files)
                label = f"{day.isoformat()} {hour:02d}:00"
                results.append(
                    {
                        "collector": collector,
                        "label": label,
                        "day": day.isoformat(),
                        "hour": hour,
                        "files": files,
                        "metrics": metrics,
                    }
                )

                slot_name = f"{day.strftime('%Y%m%d')}-{hour:02d}00"
                (output_dir / f"{collector}-{slot_name}.json").write_text(json.dumps(metrics, indent=2))

    report = render_report(collectors, days, hours, args.window_files, args.slot_minutes, results)
    if len(days) == 1:
        day_part = days[0].strftime("%Y%m%d")
    else:
        day_part = f"{days[0].strftime('%Y%m%d')}-to-{days[-1].strftime('%Y%m%d')}"
    collector_part = collectors[0] if len(collectors) == 1 else "multi-collector"
    report_name = f"{collector_part}-{day_part}-hours-{'-'.join(f'{h:02d}' for h in hours)}.txt"
    report_path = output_dir / report_name
    report_path.write_text(report, encoding="utf-8")

    print(report_path)


if __name__ == "__main__":
    main()
