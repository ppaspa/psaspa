#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter, defaultdict
from math import floor, ceil
from pathlib import Path


ORIGIN_RE = re.compile(r"^\d+$")


def percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])

    pos = (len(ordered) - 1) * q
    lo = floor(pos)
    hi = ceil(pos)
    if lo == hi:
        return float(ordered[lo])

    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def describe_distribution(values: list[int]) -> dict:
    if not values:
        return {
            "count": 0,
            "min": 0,
            "p50": 0.0,
            "p75": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0,
            "mean": 0.0,
        }

    return {
        "count": len(values),
        "min": min(values),
        "p50": round(percentile(values, 0.50), 3),
        "p75": round(percentile(values, 0.75), 3),
        "p90": round(percentile(values, 0.90), 3),
        "p95": round(percentile(values, 0.95), 3),
        "p99": round(percentile(values, 0.99), 3),
        "max": max(values),
        "mean": round(sum(values) / len(values), 6),
    }


def normalize_as_path(as_path: str) -> list[str]:
    raw = as_path.split()
    out: list[str] = []
    for token in raw:
        if out and out[-1] == token:
            continue
        out.append(token)
    return out


def iter_bgpdump_lines(path: str):
    proc = subprocess.Popen(
        ["bgpdump", "-m", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line.rstrip("\n")
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"bgpdump failed for {path} with rc={rc}")


def compute_metrics(paths: list[str]) -> dict:
    messages_total = 0
    announcements_total = 0
    withdrawals_total = 0
    announcements_with_new_origin_total = 0

    peer_counts = Counter()
    origin_counts = Counter()
    upstream_counts = Counter()
    prefix_origin_counts = Counter()

    unique_peers = set()
    unique_origins = set()
    unique_prefixes = set()
    unique_prefix_origin = set()
    unique_origin_upstream = set()

    origin_to_prefixes: dict[str, set[str]] = defaultdict(set)
    origin_to_upstreams: dict[str, set[str]] = defaultdict(set)
    origin_prefix_to_upstreams: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    for path in paths:
        for line in iter_bgpdump_lines(path):
            if not line.startswith("BGP4MP"):
                continue

            parts = line.split("|")
            if len(parts) < 6:
                continue

            messages_total += 1
            msg_type = parts[2]
            peer_asn = parts[4].strip()

            if ORIGIN_RE.fullmatch(peer_asn):
                unique_peers.add(peer_asn)
                peer_counts[peer_asn] += 1

            if msg_type == "W":
                withdrawals_total += 1
                continue

            if msg_type != "A":
                continue

            announcements_total += 1
            if len(parts) < 7:
                continue

            prefix = parts[5].strip()
            as_path = parts[6].strip()

            if prefix:
                unique_prefixes.add(prefix)

            if not as_path:
                continue

            seq = normalize_as_path(as_path)
            origin = seq[-1]
            if not ORIGIN_RE.fullmatch(origin):
                continue

            if origin not in unique_origins:
                announcements_with_new_origin_total += 1

            unique_origins.add(origin)
            origin_counts[origin] += 1

            if prefix:
                unique_prefix_origin.add((prefix, origin))
                prefix_origin_counts[(prefix, origin)] += 1
                origin_to_prefixes[origin].add(prefix)

            if len(seq) >= 2 and ORIGIN_RE.fullmatch(seq[-2]) and seq[-2] != origin:
                upstream = seq[-2]
                unique_origin_upstream.add((origin, upstream))
                upstream_counts[upstream] += 1
                origin_to_upstreams[origin].add(upstream)
                if prefix:
                    origin_prefix_to_upstreams[origin][prefix].add(upstream)

    multi_upstream_origins = {
        origin: upstreams for origin, upstreams in origin_to_upstreams.items() if len(upstreams) > 1
    }

    prefixes_per_origin = [len(prefixes) for prefixes in origin_to_prefixes.values()]
    upstreams_per_origin = [len(upstreams) for upstreams in origin_to_upstreams.values()]

    origins_with_prefix_split = 0
    origins_with_non_dominant_share = []
    prefixes_from_non_dominant_total = 0
    prefixes_total_multi = 0
    origins_with_observed_upstream_specific_prefixes = 0
    origins_with_observed_disjoint_upstream_prefixes = 0
    prefixes_with_single_upstream_total = 0
    sample_upstream_specific_prefixes = []

    for origin, prefixes in origin_prefix_to_upstreams.items():
        if origin not in multi_upstream_origins:
            continue

        prefix_to_ups = prefixes
        prefixes_total_multi += len(prefix_to_ups)

        upstream_cover = Counter()
        split_here = False
        for prefix, ups in prefix_to_ups.items():
            if len(ups) > 1:
                split_here = True
            for up in ups:
                upstream_cover[up] += 1

        if len(upstream_cover) > 1:
            split_here = True

        if split_here:
            origins_with_prefix_split += 1

        if upstream_cover:
            dominant_upstream, dominant_cover = upstream_cover.most_common(1)[0]
            non_dominant = max(0, len(prefix_to_ups) - dominant_cover)
            prefixes_from_non_dominant_total += non_dominant
            origins_with_non_dominant_share.append(
                {
                    "origin": origin,
                    "prefixes": len(prefix_to_ups),
                    "distinct_upstreams": len(multi_upstream_origins[origin]),
                    "dominant_upstream": dominant_upstream,
                    "dominant_prefix_cover": dominant_cover,
                    "non_dominant_prefixes": non_dominant,
                }
            )

        exclusive_cover = Counter()
        exclusive_prefixes = 0
        for prefix, ups in prefix_to_ups.items():
            if len(ups) == 1:
                only_upstream = next(iter(ups))
                exclusive_cover[only_upstream] += 1
                exclusive_prefixes += 1

        prefixes_with_single_upstream_total += exclusive_prefixes

        if exclusive_prefixes > 0:
            origins_with_observed_upstream_specific_prefixes += 1

        if len(exclusive_cover) >= 2:
            origins_with_observed_disjoint_upstream_prefixes += 1

        if exclusive_prefixes > 0:
            sample_upstream_specific_prefixes.append(
                {
                    "origin": origin,
                    "prefixes": len(prefix_to_ups),
                    "distinct_upstreams": len(multi_upstream_origins[origin]),
                    "exclusive_prefixes": exclusive_prefixes,
                    "exclusive_upstreams": len(exclusive_cover),
                    "top_exclusive_upstreams": [up for up, _ in exclusive_cover.most_common(6)],
                }
            )

    origins_total = len(unique_origins)
    peers_total = len(unique_peers)

    summary = {
        "files": paths,
        "messages_total": messages_total,
        "announcements_total": announcements_total,
        "withdrawals_total": withdrawals_total,
        "announcements_with_new_origin_total": announcements_with_new_origin_total,
        "unique_peers_total": peers_total,
        "unique_origins_total": origins_total,
        "unique_prefixes_total": len(unique_prefixes),
        "unique_prefix_origin_total": len(unique_prefix_origin),
        "unique_origin_upstream_pairs_total": len(unique_origin_upstream),
        "origin_diversity_pct_over_announcements": round((origins_total / announcements_total) * 100, 6) if announcements_total else 0.0,
        "origin_diversity_pct_over_unique_prefix_origin": round((origins_total / len(unique_prefix_origin)) * 100, 6) if unique_prefix_origin else 0.0,
        "new_origin_announcement_pct": round((announcements_with_new_origin_total / announcements_total) * 100, 6) if announcements_total else 0.0,
        "reused_origin_announcement_pct": round(((announcements_total - announcements_with_new_origin_total) / announcements_total) * 100, 6) if announcements_total else 0.0,
        "peer_diversity_pct_over_announcements": round((peers_total / announcements_total) * 100, 6) if announcements_total else 0.0,
        "origins_with_multi_upstreams_count": len(multi_upstream_origins),
        "origins_with_multi_upstreams_pct": round((len(multi_upstream_origins) / origins_total) * 100, 6) if origins_total else 0.0,
        "origins_with_prefix_split_count": origins_with_prefix_split,
        "origins_with_prefix_split_pct": round((origins_with_prefix_split / origins_total) * 100, 6) if origins_total else 0.0,
        "origins_with_observed_upstream_specific_prefixes_count": origins_with_observed_upstream_specific_prefixes,
        "origins_with_observed_upstream_specific_prefixes_pct": round((origins_with_observed_upstream_specific_prefixes / origins_total) * 100, 6) if origins_total else 0.0,
        "origins_with_observed_disjoint_upstream_prefixes_count": origins_with_observed_disjoint_upstream_prefixes,
        "origins_with_observed_disjoint_upstream_prefixes_pct": round((origins_with_observed_disjoint_upstream_prefixes / origins_total) * 100, 6) if origins_total else 0.0,
        "exclusive_prefix_share_within_multi_upstream_origins_pct": round((prefixes_with_single_upstream_total / prefixes_total_multi) * 100, 6) if prefixes_total_multi else 0.0,
        "non_dominant_prefix_share_within_multi_upstream_origins_pct": round((prefixes_from_non_dominant_total / prefixes_total_multi) * 100, 6) if prefixes_total_multi else 0.0,
        "prefixes_per_origin_distribution": describe_distribution(prefixes_per_origin),
        "upstreams_per_origin_distribution": describe_distribution(upstreams_per_origin),
        "top_origins_by_announcements": origin_counts.most_common(20),
        "top_peers_by_messages": peer_counts.most_common(20),
        "top_upstreams_before_origin": upstream_counts.most_common(20),
        "sample_multi_upstream_origins": sorted(
            (
                {
                    "origin": origin,
                    "distinct_upstreams": len(ups),
                    "upstreams": sorted(ups),
                    "prefixes": len(origin_to_prefixes.get(origin, set())),
                }
                for origin, ups in multi_upstream_origins.items()
            ),
            key=lambda item: (-item["distinct_upstreams"], -item["prefixes"], int(item["origin"])),
        )[:20],
        "sample_non_dominant_prefix_share": sorted(
            origins_with_non_dominant_share,
            key=lambda item: (-item["non_dominant_prefixes"], -item["distinct_upstreams"], -item["prefixes"], int(item["origin"])),
        )[:20],
        "sample_upstream_specific_prefixes": sorted(
            sample_upstream_specific_prefixes,
            key=lambda item: (-item["exclusive_prefixes"], -item["exclusive_upstreams"], -item["distinct_upstreams"], -item["prefixes"], int(item["origin"])),
        )[:20],
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute BGP metrics from MRT UPDATE/RIB files via bgpdump.")
    parser.add_argument("files", nargs="+", help="Input MRT files (.bz2) to parse with bgpdump -m")
    parser.add_argument("--output", help="Optional JSON output file")
    args = parser.parse_args()

    summary = compute_metrics(args.files)
    text = json.dumps(summary, indent=2)

    if args.output:
        Path(args.output).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
