#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import json
import math
import itertools
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import orchestrator as base
from tools.bgp_realism_metrics import ORIGIN_RE, iter_bgpdump_lines, normalize_as_path


ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = ROOT / "results"
CAMPAIGNS_ROOT = ROOT / "campaigns"


def clamp_percent(value: int) -> int:
    return max(0, min(100, int(value)))


def normalize_campaign_matrix(matrix: dict) -> dict:
    normalized = dict(matrix)
    if (
        "prefix_scoped_route_share_pct" not in normalized
        and "non_dominant_prefix_share_pct" in normalized
    ):
        normalized["prefix_scoped_route_share_pct"] = normalized["non_dominant_prefix_share_pct"]
    return normalized


def provider_text(actual: Iterable[int], providers_per_rule: int, filler_base: int) -> str:
    seen = []
    used = set()
    for provider in actual:
        if provider in used:
            continue
        used.add(provider)
        seen.append(provider)

    filler = filler_base
    while len(seen) < providers_per_rule:
        if filler not in used:
            seen.append(filler)
            used.add(filler)
        filler += 1

    return ", ".join(str(item) for item in seen)


def distinct_origin_count(routes: int, origin_diversity_pct: int) -> int:
    if routes <= 0:
        return 1
    pct = clamp_percent(origin_diversity_pct)
    if pct == 0:
        return 1
    return max(1, math.ceil(routes * pct / 100.0))


def routes_per_origin(routes: int, origin_count: int) -> list[int]:
    base_count = routes // origin_count
    extra = routes % origin_count
    return [base_count + (1 if i < extra else 0) for i in range(origin_count)]


def origin_asn(origin_index: int) -> int:
    return base.ASN_BASE + (origin_index * base.ASN_STRIDE)


def route_prefix(prefix_base: int, route_index: int) -> str:
    second = (route_index >> 16) & 0xFF
    third = (route_index >> 8) & 0xFF
    fourth = route_index & 0xFF
    return f"{prefix_base}.{second}.{third}.{fourth}/32"


def build_path(path_len: int, first: int, second: int) -> list[int]:
    path = [first, second]
    while len(path) < path_len:
        path.append(path[-1] + 1)
    return path


def unique_rule_prefix(prefix_base: int, rule_index: int) -> str:
    first = prefix_base + ((rule_index >> 16) & 0xFF)
    second = (rule_index >> 8) & 0xFF
    third = rule_index & 0xFF
    if first > 254:
        raise ValueError("/24 prefix space exhausted in the realistic generator")
    return f"{first}.{second}.{third}.0/24"


def inert_origin_prefix(rule_index: int) -> str:
    return unique_rule_prefix(11, rule_index)


def active_rule_prefix(rule_index: int) -> str:
    return unique_rule_prefix(10, rule_index)


def active_route_prefix(rule_index: int, host_index: int) -> str:
    first = 10 + ((rule_index >> 16) & 0xFF)
    second = (rule_index >> 8) & 0xFF
    third = rule_index & 0xFF
    fourth = host_index & 0xFF
    if first > 254:
        raise ValueError("/32 prefix space exhausted in the realistic generator")
    return f"{first}.{second}.{third}.{fourth}/32"


def synthetic_miss_prefix(route_index: int) -> str:
    return route_prefix(172, route_index)


def build_realistic_specs(
    routes: int,
    path_len: int,
    origin_diversity_pct: int,
    multi_upstream_origin_pct: int,
    prefix_scoped_route_share_pct: int,
    prefix_rules_per_origin: int,
) -> tuple[list[dict], list[dict], dict]:
    origin_count = distinct_origin_count(routes, origin_diversity_pct)
    multi_count = max(0, math.ceil(origin_count * clamp_percent(multi_upstream_origin_pct) / 100.0))
    per_origin_counts = routes_per_origin(routes, origin_count)
    prefix_rules_exact = max(0, int(prefix_rules_per_origin))

    alt_quota = {}
    for origin_index, count in enumerate(per_origin_counts):
        if origin_index >= multi_count:
            alt_quota[origin_index] = 0
            continue
        quota = math.floor(count * clamp_percent(prefix_scoped_route_share_pct) / 100.0)
        if quota == 0 and clamp_percent(prefix_scoped_route_share_pct) > 0 and count > 0:
            quota = 1
        alt_quota[origin_index] = min(count, quota)

    route_specs = []
    origin_profiles = []
    route_index = 0
    next_rule_index = 0
    for origin_index, count in enumerate(per_origin_counts):
        asn = origin_asn(origin_index)
        is_multi = origin_index < multi_count
        global_second = asn + 1
        prefix_second = asn + 101
        global_path = build_path(path_len, asn, global_second)
        prefix_path = build_path(path_len, asn, prefix_second)
        active_prefix_rules = min(prefix_rules_exact, alt_quota[origin_index]) if is_multi else 0
        total_prefix_rules = prefix_rules_exact if is_multi else 0
        active_rule_ids = list(range(next_rule_index, next_rule_index + active_prefix_rules))
        next_rule_index += active_prefix_rules
        inert_rule_ids = list(
            range(next_rule_index, next_rule_index + max(0, total_prefix_rules - active_prefix_rules))
        )
        next_rule_index += len(inert_rule_ids)

        origin_profiles.append(
            {
                "origin_index": origin_index,
                "origin_asn": asn,
                "is_multi": is_multi,
                "global_path": global_path,
                "prefix_path": prefix_path,
                "active_prefix_rules": active_prefix_rules,
                "total_prefix_rules": total_prefix_rules,
                "active_rule_ids": active_rule_ids,
                "inert_rule_ids": inert_rule_ids,
            }
        )

        prefix_scoped_counter = 0
        for local_index in range(count):
            uses_prefix_rule = is_multi and local_index < alt_quota[origin_index]
            hit_prefix = None
            if uses_prefix_rule and active_prefix_rules > 0:
                slot_index = prefix_scoped_counter % active_prefix_rules
                host_index = prefix_scoped_counter // active_prefix_rules
                if host_index >= 256:
                    raise ValueError(
                        "more than 256 routes were mapped to the same realistic prefix rule"
                    )
                rule_index = active_rule_ids[slot_index]
                hit_prefix = active_route_prefix(rule_index, host_index)
                prefix_scoped_counter += 1
            route_specs.append(
                {
                    "route_index": route_index,
                    "origin_index": origin_index,
                    "local_index": local_index,
                    "origin_asn": asn,
                    "is_multi": is_multi,
                    "uses_prefix_rule": uses_prefix_rule,
                    "global_path": global_path,
                    "prefix_path": prefix_path,
                    "global_prefix": route_prefix(203, route_index),
                    "hit_prefix": hit_prefix,
                    "miss_prefix": route_prefix(172, route_index),
                }
            )
            route_index += 1

    meta = {
        "distinct_origins": origin_count,
        "origin_diversity_pct": clamp_percent(origin_diversity_pct),
        "multi_upstream_origins": multi_count,
        "routes_per_origin_min": min(per_origin_counts) if per_origin_counts else 0,
        "routes_per_origin_max": max(per_origin_counts) if per_origin_counts else 0,
        "prefix_scoped_routes": sum(1 for spec in route_specs if spec["uses_prefix_rule"]),
        "active_prefix_rules": sum(profile["active_prefix_rules"] for profile in origin_profiles),
        "generated_prefix_rules": sum(profile["total_prefix_rules"] for profile in origin_profiles),
    }

    used_route_prefixes = set()
    for spec in route_specs:
        for prefix in (spec["global_prefix"], spec["miss_prefix"], spec["hit_prefix"]):
            if not prefix:
                continue
            if prefix in used_route_prefixes:
                raise ValueError(f"duplicate route prefix in the realistic generator: {prefix}")
            used_route_prefixes.add(prefix)

    return route_specs, origin_profiles, meta


def parse_mrt_candidates(mrt_files: list[str], path_len: int) -> list[dict]:
    candidates: list[dict] = []
    seen: set[tuple[str, tuple[int, ...]]] = set()

    for mrt_path in mrt_files:
        for line in iter_bgpdump_lines(mrt_path):
            if not line.startswith("BGP4MP"):
                continue

            parts = line.split("|")
            if len(parts) < 7 or parts[2] != "A":
                continue

            prefix = parts[5].strip()
            as_path = parts[6].strip()
            if not prefix or not as_path:
                continue

            seq = normalize_as_path(as_path)
            if not seq or not all(ORIGIN_RE.fullmatch(token) for token in seq):
                continue

            path = [int(token) for token in reversed(seq)]
            if len(path) < 2:
                continue
            if path_len > 0 and len(path) != path_len:
                continue

            key = (prefix, tuple(path))
            if key in seen:
                continue
            seen.add(key)

            candidates.append(
                {
                    "prefix": prefix,
                    "path": path,
                    "origin_asn": path[0],
                    "upstream_asn": path[1],
                }
            )

    return candidates


def select_mrt_stream(candidates: list[dict], routes: int, origin_diversity_pct: int) -> tuple[list[dict], dict]:
    if not candidates:
        raise ValueError("nenhuma rota valida disponivel apos filtrar os MRTs")

    grouped: dict[int, list[dict]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate["origin_asn"], []).append(candidate)

    target_origin_count = distinct_origin_count(routes, origin_diversity_pct)
    ordered_origins = sorted(grouped, key=lambda origin: (-len(grouped[origin]), origin))
    selected_origins: list[int] = []
    selected_capacity = 0

    for origin in ordered_origins:
        selected_origins.append(origin)
        selected_capacity += len(grouped[origin])
        if len(selected_origins) >= target_origin_count and selected_capacity >= routes:
            break

    if selected_capacity < routes:
        raise ValueError(
            f"os MRTs selecionados fornecem apenas {selected_capacity} anuncios validos, "
            f"menos que as {routes} rotas solicitadas"
        )

    per_origin = {origin: list(grouped[origin]) for origin in selected_origins}
    stream: list[dict] = []

    for origin in selected_origins:
        if len(stream) >= routes:
            break
        if per_origin[origin]:
            stream.append(per_origin[origin].pop(0))

    while len(stream) < routes:
        progressed = False
        for origin in selected_origins:
            if len(stream) >= routes:
                break
            if per_origin[origin]:
                stream.append(per_origin[origin].pop(0))
                progressed = True
        if not progressed:
            break

    observed_origin_count = len({item["origin_asn"] for item in stream})
    observed_origin_diversity_pct = round((observed_origin_count / len(stream)) * 100.0, 6) if stream else 0.0

    meta = {
        "selected_origins": observed_origin_count,
        "observed_origin_diversity_pct": observed_origin_diversity_pct,
        "candidate_pool": len(candidates),
    }
    return stream, meta


def build_mrt_specs(
    mrt_files: list[str],
    routes: int,
    path_len: int,
    origin_diversity_pct: int,
    prefix_rules_per_origin: int,
) -> tuple[list[dict], list[dict], dict]:
    candidates = parse_mrt_candidates(mrt_files, path_len)
    selected_routes, selection_meta = select_mrt_stream(candidates, routes, origin_diversity_pct)

    by_origin: dict[int, list[dict]] = defaultdict(list)
    for route in selected_routes:
        by_origin[route["origin_asn"]].append(route)

    route_specs: list[dict] = []
    origin_profiles: list[dict] = []
    selected_prefix_rules: dict[tuple[int, str], int] = {}

    for origin_asn in sorted(by_origin):
        origin_routes = by_origin[origin_asn]
        prefix_to_upstreams: dict[str, set[int]] = defaultdict(set)
        for route in origin_routes:
            prefix_to_upstreams[route["prefix"]].add(route["upstream_asn"])

        chosen = 0
        for prefix in sorted(prefix_to_upstreams):
            upstreams = prefix_to_upstreams[prefix]
            if len(upstreams) != 1:
                continue
            selected_prefix_rules[(origin_asn, prefix)] = next(iter(upstreams))
            chosen += 1
            if chosen >= prefix_rules_per_origin:
                break

    for route_index, route in enumerate(selected_routes):
        origin_asn = route["origin_asn"]
        uses_prefix_rule = (origin_asn, route["prefix"]) in selected_prefix_rules
        route_specs.append(
            {
                "route_index": route_index,
                "origin_asn": origin_asn,
                "is_multi": any(
                    key_origin == origin_asn for key_origin, _prefix in selected_prefix_rules.keys()
                ),
                "uses_prefix_rule": uses_prefix_rule,
                "global_path": route["path"],
                "prefix_path": route["path"],
                "global_prefix": route["prefix"],
                "hit_prefix": route["prefix"] if uses_prefix_rule else None,
                "miss_prefix": synthetic_miss_prefix(route_index),
            }
        )

    for origin_asn in sorted(by_origin):
        origin_routes = by_origin[origin_asn]
        distinct_upstreams = sorted({route["upstream_asn"] for route in origin_routes})
        profile_rule_keys = [
            prefix for (key_origin, prefix), _provider in selected_prefix_rules.items() if key_origin == origin_asn
        ]
        global_path = origin_routes[0]["path"]
        origin_profiles.append(
            {
                "origin_index": len(origin_profiles),
                "origin_asn": origin_asn,
                "is_multi": len(distinct_upstreams) > 1 or bool(profile_rule_keys),
                "global_path": global_path,
                "prefix_path": global_path,
                "active_prefix_rules": len(profile_rule_keys),
                "total_prefix_rules": len(profile_rule_keys),
                "prefix_provider_map": {
                    prefix: provider
                    for (key_origin, prefix), provider in selected_prefix_rules.items()
                    if key_origin == origin_asn
                },
            }
        )

    meta = {
        "distinct_origins": len(by_origin),
        "origin_diversity_pct": origin_diversity_pct,
        "observed_origin_diversity_pct": selection_meta["observed_origin_diversity_pct"],
        "active_prefix_rules": len(selected_prefix_rules),
        "generated_prefix_rules": len(selected_prefix_rules),
        "candidate_pool": selection_meta["candidate_pool"],
        "mrt_files": mrt_files,
    }
    return route_specs, origin_profiles, meta


def route_block(prefix: str, path: list[int]) -> str:
    lines = [f"    route {prefix} blackhole {{"]
    for asn in path:
        lines.append(f"        bgp_path = prepend(bgp_path, {asn});")
    lines.append("    };")
    return "\n".join(lines)


def add_global_rule_map(rule_map: dict[int, set[int]], path: list[int], start_index: int = 0) -> None:
    for i in range(start_index, len(path) - 1):
        rule_map.setdefault(path[i], set()).add(path[i + 1])


def render_aspa_objects(rule_map: dict[int, dict[str | None, str]]) -> list[str]:
    lines = []
    for asn in sorted(rule_map):
        lines.append(f"    route aspa {asn} {{")
        items = sorted(rule_map[asn].items(), key=lambda item: (item[0] is not None, item[0] or ""))
        for prefix, providers in items:
            if prefix:
                lines.append(f"        prefix {prefix} providers {providers};")
            else:
                lines.append(f"        providers {providers};")
        lines.append("    };")
    return lines


def render_aspa_objects_legacy(rule_map: dict[int, dict[str | None, str]]) -> list[str]:
    lines = []
    for asn in sorted(rule_map):
        items = sorted(rule_map[asn].items(), key=lambda item: (item[0] is not None, item[0] or ""))
        for prefix, providers in items:
            if prefix:
                lines.append(f"    route aspa {asn} prefix {prefix} providers {providers};")
            else:
                lines.append(f"    route aspa {asn} providers {providers};")
    return lines


def realistic_rules_original(route_specs: list[dict], providers_per_rule: int) -> list[str]:
    rule_map: dict[int, set[int]] = {}
    for spec in route_specs:
        add_global_rule_map(rule_map, spec["global_path"])
    object_map: dict[int, dict[str | None, str]] = {}
    for asn in sorted(rule_map):
        object_map[asn] = {
            None: provider_text(sorted(rule_map[asn]), providers_per_rule, 66000 + asn)
        }
    return render_aspa_objects_legacy(object_map)


def realistic_rules_modified(
    route_specs: list[dict],
    origin_profiles: list[dict],
    providers_per_rule: int,
    mode: str,
) -> list[str]:
    if mode == "global_only":
        return realistic_rules_original(route_specs, providers_per_rule)

    origin_global: dict[int, set[int]] = {}
    rest_global: dict[int, set[int]] = {}
    prefix_rules: dict[tuple[int, str], int] = {}

    for spec in route_specs:
        global_path = spec["global_path"]
        prefix_path = spec["prefix_path"]
        origin = spec["origin_asn"]

        origin_global.setdefault(origin, set()).add(global_path[1])
        add_global_rule_map(rest_global, global_path, start_index=1)
        if spec["is_multi"]:
            add_global_rule_map(rest_global, prefix_path, start_index=1)

    for profile in origin_profiles:
        if not profile["is_multi"]:
            continue

        origin = profile["origin_asn"]
        if "active_rule_ids" in profile:
            provider = profile["prefix_path"][1]
            for rule_index in profile["active_rule_ids"]:
                prefix = active_rule_prefix(rule_index)
                prefix_rules[(origin, prefix)] = provider

            for rule_index in profile["inert_rule_ids"]:
                prefix = inert_origin_prefix(rule_index)
                prefix_rules[(origin, prefix)] = provider
        else:
            prefix_provider_map = profile.get("prefix_provider_map", {})
            for prefix, provider in prefix_provider_map.items():
                prefix_rules[(origin, prefix)] = provider

    object_map: dict[int, dict[str | None, str]] = {}

    for asn in sorted(origin_global):
        object_map.setdefault(asn, {})[None] = provider_text(sorted(origin_global[asn]), providers_per_rule, 65000 + asn)

    for (asn, prefix), provider in sorted(prefix_rules.items(), key=lambda item: (item[0][0], item[0][1])):
        object_map.setdefault(asn, {})[prefix] = provider_text([provider], providers_per_rule, 65100 + asn)

    for asn in sorted(rest_global):
        object_map.setdefault(asn, {})[None] = provider_text(sorted(rest_global[asn]), providers_per_rule, 67000 + asn)

    return render_aspa_objects(object_map)


def realistic_route_lines(route_specs: list[dict], mode: str) -> list[str]:
    lines = []
    for spec in route_specs:
        if mode == "global_only":
            prefix = spec["global_prefix"]
            path = spec["global_path"]
        elif mode == "prefix_hit":
            if spec["uses_prefix_rule"]:
                prefix = spec["hit_prefix"]
                path = spec["prefix_path"]
            else:
                prefix = spec["global_prefix"]
                path = spec["global_path"]
        elif mode == "prefix_miss":
            prefix = spec["miss_prefix"]
            path = spec["global_path"]
        else:
            raise ValueError("modo invalido")

        lines.append(route_block(prefix, path))
    return lines


def config_original_mrt(
    mrt_files: list[str],
    routes: int,
    path_len: int,
    providers_per_rule: int,
    prefix_rules_per_origin: int,
    origin_diversity_pct: int,
) -> tuple[str, dict]:
    specs, _profiles, meta = build_mrt_specs(
        mrt_files=mrt_files,
        routes=routes,
        path_len=path_len,
        origin_diversity_pct=origin_diversity_pct,
        prefix_rules_per_origin=prefix_rules_per_origin,
    )

    text = "\n".join(
        [
            'log "/dev/null" all;',
            'router id 192.0.2.60;',
            "",
            'aspa table at;',
            "",
            'filter f {',
            '    if aspa_check(at, bgp_path, true) = ASPA_INVALID then reject;',
            '    accept;',
            '}',
            "",
            'protocol static aspa_data {',
            '    aspa { table at; };',
            "",
            *realistic_rules_original(specs, providers_per_rule),
            '}',
            "",
            'protocol static test_routes {',
            '    ipv4 { import filter f; };',
            "",
            *realistic_route_lines(specs, "global_only"),
            '}',
            "",
            'protocol device {}',
            "",
        ]
    )
    return text, meta


def config_original_plain_mrt(
    mrt_files: list[str],
    routes: int,
    path_len: int,
    providers_per_rule: int,
    prefix_rules_per_origin: int,
    origin_diversity_pct: int,
) -> tuple[str, dict]:
    specs, _profiles, meta = build_mrt_specs(
        mrt_files=mrt_files,
        routes=routes,
        path_len=path_len,
        origin_diversity_pct=origin_diversity_pct,
        prefix_rules_per_origin=prefix_rules_per_origin,
    )

    text = "\n".join(
        [
            'log "/dev/null" all;',
            'router id 192.0.2.61;',
            "",
            'filter f {',
            '    accept;',
            '}',
            "",
            'protocol static test_routes {',
            '    ipv4 { import filter f; };',
            "",
            *realistic_route_lines(specs, "global_only"),
            '}',
            "",
            'protocol device {}',
            "",
        ]
    )
    return text, meta


def config_modified_mrt(
    mrt_files: list[str],
    routes: int,
    path_len: int,
    providers_per_rule: int,
    prefix_rules_per_origin: int,
    origin_diversity_pct: int,
    mode: str,
) -> tuple[str, dict]:
    specs, profiles, meta = build_mrt_specs(
        mrt_files=mrt_files,
        routes=routes,
        path_len=path_len,
        origin_diversity_pct=origin_diversity_pct,
        prefix_rules_per_origin=prefix_rules_per_origin,
    )

    text = "\n".join(
        [
            'log "/dev/null" all;',
            'router id 192.0.2.62;',
            "",
            'aspa table at;',
            "",
            'filter f {',
            '    if aspa_check(at, bgp_path, true) = ASPA_INVALID then reject;',
            '    accept;',
            '}',
            "",
            'protocol static aspa_data {',
            '    aspa { table at; };',
            "",
            *realistic_rules_modified(specs, profiles, providers_per_rule, mode),
            '}',
            "",
            'protocol static test_routes {',
            '    ipv4 { import filter f; };',
            "",
            *realistic_route_lines(specs, mode),
            '}',
            "",
            'protocol device {}',
            "",
        ]
    )
    return text, meta


def config_original_realistic(
    routes: int,
    path_len: int,
    providers_per_rule: int,
    prefix_rules_per_origin: int,
    origin_diversity_pct: int,
    multi_upstream_origin_pct: int,
    prefix_scoped_route_share_pct: int,
) -> tuple[str, dict]:
    specs, _profiles, meta = build_realistic_specs(
        routes,
        path_len,
        origin_diversity_pct,
        multi_upstream_origin_pct,
        prefix_scoped_route_share_pct,
        prefix_rules_per_origin,
    )

    text = "\n".join(
        [
            'log "/dev/null" all;',
            'router id 192.0.2.30;',
            "",
            'aspa table at;',
            "",
            'filter f {',
            '    if aspa_check(at, bgp_path, true) = ASPA_INVALID then reject;',
            '    accept;',
            '}',
            "",
            'protocol static aspa_data {',
            '    aspa { table at; };',
            "",
            *realistic_rules_original(specs, providers_per_rule),
            '}',
            "",
            'protocol static test_routes {',
            '    ipv4 { import filter f; };',
            "",
            *realistic_route_lines(specs, "global_only"),
            '}',
            "",
            'protocol device {}',
            "",
        ]
    )
    return text, meta


def config_original_plain_realistic(
    routes: int,
    path_len: int,
    providers_per_rule: int,
    prefix_rules_per_origin: int,
    origin_diversity_pct: int,
    multi_upstream_origin_pct: int,
    prefix_scoped_route_share_pct: int,
) -> tuple[str, dict]:
    specs, _profiles, meta = build_realistic_specs(
        routes,
        path_len,
        origin_diversity_pct,
        multi_upstream_origin_pct,
        prefix_scoped_route_share_pct,
        prefix_rules_per_origin,
    )

    text = "\n".join(
        [
            'log "/dev/null" all;',
            'router id 192.0.2.29;',
            "",
            'filter f {',
            '    accept;',
            '}',
            "",
            'protocol static test_routes {',
            '    ipv4 { import filter f; };',
            "",
            *realistic_route_lines(specs, "global_only"),
            '}',
            "",
            'protocol device {}',
            "",
        ]
    )
    return text, meta


def config_modified_realistic(
    routes: int,
    path_len: int,
    providers_per_rule: int,
    prefix_rules_per_origin: int,
    origin_diversity_pct: int,
    multi_upstream_origin_pct: int,
    prefix_scoped_route_share_pct: int,
    mode: str,
) -> tuple[str, dict]:
    specs, profiles, meta = build_realistic_specs(
        routes,
        path_len,
        origin_diversity_pct,
        multi_upstream_origin_pct,
        prefix_scoped_route_share_pct,
        prefix_rules_per_origin,
    )

    text = "\n".join(
        [
            'log "/dev/null" all;',
            'router id 192.0.2.40;',
            "",
            'aspa table at;',
            "",
            'filter f {',
            '    if aspa_check(at, bgp_path, true) = ASPA_INVALID then reject;',
            '    accept;',
            '}',
            "",
            'protocol static aspa_data {',
            '    aspa { table at; };',
            "",
            *realistic_rules_modified(specs, profiles, providers_per_rule, mode),
            '}',
            "",
            'protocol static test_routes {',
            '    ipv4 { import filter f; };',
            "",
            *realistic_route_lines(specs, mode),
            '}',
            "",
            'protocol device {}',
            "",
        ]
    )
    return text, meta


def write_benchmark_configs_realistic(
    routes: int,
    path_len: int,
    providers_per_rule: int,
    prefix_rules_per_origin: int,
    origin_diversity_pct: int,
    multi_upstream_origin_pct: int,
    prefix_scoped_route_share_pct: int,
    source_mode: str = "synthetic",
    mrt_files: list[str] | None = None,
) -> tuple[list[Path], dict, dict]:
    created = []
    tag = base.benchmark_config_tag("realistic")
    config_names = base.benchmark_config_names(tag)
    if source_mode == "mrt":
        if not mrt_files:
            raise ValueError("source_mode=mrt exige ao menos um arquivo MRT")
        original_plain_text, plain_meta = config_original_plain_mrt(
            mrt_files,
            routes,
            path_len,
            providers_per_rule,
            prefix_rules_per_origin,
            origin_diversity_pct,
        )
        original_text, meta = config_original_mrt(
            mrt_files,
            routes,
            path_len,
            providers_per_rule,
            prefix_rules_per_origin,
            origin_diversity_pct,
        )
    else:
        original_plain_text, plain_meta = config_original_plain_realistic(
            routes,
            path_len,
            providers_per_rule,
            prefix_rules_per_origin,
            origin_diversity_pct,
            multi_upstream_origin_pct,
            prefix_scoped_route_share_pct,
        )
        original_text, meta = config_original_realistic(
            routes,
            path_len,
            providers_per_rule,
            prefix_rules_per_origin,
            origin_diversity_pct,
            multi_upstream_origin_pct,
            prefix_scoped_route_share_pct,
        )

    if plain_meta != meta:
        raise ValueError("inconsistent metadata between plain and global configurations in the realistic scenario")

    original_plain_conf = base.PROJECTS["bird"]["repo"] / config_names["bird"]["original-plain"]
    base.write_text_resilient(original_plain_conf, original_plain_text, encoding="utf-8")
    created.append(original_plain_conf)

    original_conf = base.PROJECTS["bird"]["repo"] / config_names["bird"]["original-global"]
    base.write_text_resilient(original_conf, original_text, encoding="utf-8")
    created.append(original_conf)

    for key in ("linear", "bird-prefix-trie", "bird-prefix-hybridv2"):
        repo = base.PROJECTS[key]["repo"]
        variant = base.MODIFIED_PROJECT_CASE_PREFIX[key]
        for mode, case_name in (
            ("global_only", f"{variant}-global"),
            ("prefix_hit", f"{variant}-hit"),
            ("prefix_miss", f"{variant}-miss"),
        ):
            if source_mode == "mrt":
                text, _ = config_modified_mrt(
                    mrt_files or [],
                    routes,
                    path_len,
                    providers_per_rule,
                    prefix_rules_per_origin,
                    origin_diversity_pct,
                    mode,
                )
            else:
                text, _ = config_modified_realistic(
                    routes,
                    path_len,
                    providers_per_rule,
                    prefix_rules_per_origin,
                    origin_diversity_pct,
                    multi_upstream_origin_pct,
                    prefix_scoped_route_share_pct,
                    mode,
                )
            path = repo / config_names[key][case_name]
            base.write_text_resilient(path, text, encoding="utf-8")
            created.append(path)

    return created, config_names, meta


def benchmark_results_fieldnames():
    return [
        "repeat",
        "case",
        "project",
        "routes",
        "path_len",
        "providers_per_rule",
        "prefix_rules_per_origin",
        "origin_diversity_pct",
        "generated_prefix_rules",
        "active_prefix_rules",
        "distinct_origins",
        "proto",
        "wall_us",
        "total_ns",
        "total_us",
        "calls",
        "avg_ns",
        "rss_kb",
        "user_us",
        "sys_us",
        "minflt",
        "majflt",
        "nvcsw",
        "nivcsw",
        "log_file",
    ]


def summary_fieldnames(group_keys=None):
    group_keys = group_keys or [
        "routes",
        "path_len",
        "providers_per_rule",
        "prefix_rules_per_origin",
        "origin_diversity_pct",
        "generated_prefix_rules",
        "active_prefix_rules",
        "distinct_origins",
        "case",
    ]
    ordered = [field for field in group_keys if field != "case"]
    if "case" in group_keys:
        ordered.append("case")
    return ordered + [field for field in base.SUMMARY_BASE_FIELDS if field != "case"]


def selected_benchmark_cases(target: str, mode_choice: str, config_names=None):
    mode_map = {
        "plain": ["plain"],
        "global": ["global"],
        "hit": ["hit"],
        "miss": ["miss"],
        "plain+global": ["plain", "global"],
        "hit+miss": ["hit", "miss"],
        "global+hit+miss": ["global", "hit", "miss"],
        "plain+global+hit+miss": ["plain", "global", "hit", "miss"],
    }
    suffixes = mode_map[mode_choice]
    config_names = config_names or base.benchmark_config_names()

    if target == "bird":
        cases = []
        if "plain" in suffixes:
            cases.append(("bird", "original-plain", config_names["bird"]["original-plain"]))
        if "global" in suffixes or not cases:
            cases.append(("bird", "original-global", config_names["bird"]["original-global"]))
        return cases

    if target == "linear":
        if not base.project_available("linear"):
            raise RuntimeError("projeto indisponivel: linear")
        cases = []
        if "plain" in suffixes:
            cases.append(("bird", "original-plain", config_names["bird"]["original-plain"]))
        if "global" in suffixes or not cases:
            cases.append(("bird", "original-global", config_names["bird"]["original-global"]))
        cases.extend(
            [
                ("linear", f"linear-{suffix}", config_names["linear"][f"linear-{suffix}"])
                for suffix in suffixes
                if suffix != "plain"
            ]
        )
        return cases

    if target == "bird-prefix-trie":
        if not base.project_available("bird-prefix-trie"):
            raise RuntimeError("projeto indisponivel: bird-prefix-trie")
        cases = []
        if "plain" in suffixes:
            cases.append(("bird", "original-plain", config_names["bird"]["original-plain"]))
        if "global" in suffixes or not cases:
            cases.append(("bird", "original-global", config_names["bird"]["original-global"]))
        cases.extend(
            [
                ("bird-prefix-trie", f"trie-{suffix}", config_names["bird-prefix-trie"][f"trie-{suffix}"])
                for suffix in suffixes
                if suffix != "plain"
            ]
        )
        return cases

    if target == "bird-prefix-hybridv2":
        if not base.project_available("bird-prefix-hybridv2"):
            raise RuntimeError("projeto indisponivel: bird-prefix-hybridv2")
        cases = []
        if "plain" in suffixes:
            cases.append(("bird", "original-plain", config_names["bird"]["original-plain"]))
        if "global" in suffixes or not cases:
            cases.append(("bird", "original-global", config_names["bird"]["original-global"]))
        cases.extend(
            [
                ("bird-prefix-hybridv2", f"hybridv2-{suffix}", config_names["bird-prefix-hybridv2"][f"hybridv2-{suffix}"])
                for suffix in suffixes
                if suffix != "plain"
            ]
        )
        return cases

    if target == "linear+trie+hybridv2":
        cases = []
        if "plain" in suffixes:
            cases.append(("bird", "original-plain", config_names["bird"]["original-plain"]))
        if "global" in suffixes or not cases:
            cases.append(("bird", "original-global", config_names["bird"]["original-global"]))
        for suffix in suffixes:
            if suffix == "plain":
                continue
            if base.project_available("linear"):
                cases.append(
                    ("linear", f"linear-{suffix}", config_names["linear"][f"linear-{suffix}"])
                )
            if base.project_available("bird-prefix-trie"):
                cases.append(
                    ("bird-prefix-trie", f"trie-{suffix}", config_names["bird-prefix-trie"][f"trie-{suffix}"])
                )
            if base.project_available("bird-prefix-hybridv2"):
                cases.append(
                    ("bird-prefix-hybridv2", f"hybridv2-{suffix}", config_names["bird-prefix-hybridv2"][f"hybridv2-{suffix}"])
                )
        return cases

    cases = []
    if "plain" in suffixes:
        cases.append(("bird", "original-plain", config_names["bird"]["original-plain"]))
    if "global" in suffixes or not cases:
        cases.append(("bird", "original-global", config_names["bird"]["original-global"]))
    for suffix in suffixes:
        if suffix == "plain":
            continue
        cases.append(
            ("linear", f"linear-{suffix}", config_names["linear"][f"linear-{suffix}"])
        )
        if base.project_available("bird-prefix-trie"):
            cases.append(
                ("bird-prefix-trie", f"trie-{suffix}", config_names["bird-prefix-trie"][f"trie-{suffix}"])
            )
        if base.project_available("bird-prefix-hybridv2"):
            cases.append(
                ("bird-prefix-hybridv2", f"hybridv2-{suffix}", config_names["bird-prefix-hybridv2"][f"hybridv2-{suffix}"])
            )
    return cases


def execute_benchmark_run(
    target: str,
    mode_choice: str,
    routes: int,
    path_len: int,
    providers_per_rule: int,
    prefix_rules_per_origin: int,
    origin_diversity_pct: int,
    multi_upstream_origin_pct: int,
    prefix_scoped_route_share_pct: int,
    repeats: int,
    rebuild: bool,
    run_dir: Path,
    source_mode: str = "synthetic",
    mrt_files: list[str] | None = None,
    warmup_repeats: int = 0,
    write_logs: bool = True,
    batch_by_project: bool = False,
    point_index: int = 0,
    order_strategy: str = "rotate-projects",
):
    temp_confs = []
    try:
        temp_confs, config_names, generation_meta = write_benchmark_configs_realistic(
            routes,
            path_len,
            providers_per_rule,
            prefix_rules_per_origin,
            origin_diversity_pct,
            multi_upstream_origin_pct,
            prefix_scoped_route_share_pct,
            source_mode=source_mode,
            mrt_files=mrt_files,
        )

        cases = base.ordered_cases_for_point(
            selected_benchmark_cases(target, mode_choice, config_names=config_names),
            point_index=point_index,
            strategy=order_strategy,
        )
        if rebuild:
            selected_projects = {project_key for project_key, _, _ in cases}
            for project_key in selected_projects:
                base.rebuild_project(base.PROJECTS[project_key])

        rows = []
        logs_dir = run_dir / "logs"

        if batch_by_project:
            grouped_cases = {}
            for project_key, case_name, config_name in cases:
                grouped_cases.setdefault(project_key, []).append((case_name, config_name))

            for project_key, case_specs in grouped_cases.items():
                project = base.PROJECTS[project_key]
                try:
                    output, batch_records = base.run_benchmark_cases_batched(
                        project,
                        case_specs,
                        warmup_repeats=warmup_repeats,
                        repeats=repeats,
                    )
                except RuntimeError as exc:
                    batch_records = base.run_benchmark_cases_serial(
                        project,
                        case_specs,
                        warmup_repeats=warmup_repeats,
                        repeats=repeats,
                        logs_dir=logs_dir,
                        write_logs=False,
                    )
                    output = ""
                log_file = ""
                if write_logs:
                    log_path = logs_dir / f"batch-{project_key}.log"
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(output, encoding="utf-8")
                    log_file = str(log_path)

                for record in batch_records:
                    metrics = record["metrics"]
                    rows.append(
                        {
                            "repeat": record["repeat"],
                            "case": record["case"],
                            "project": project_key,
                            "routes": routes,
                            "path_len": path_len,
                            "providers_per_rule": providers_per_rule,
                            "prefix_rules_per_origin": prefix_rules_per_origin,
                            "origin_diversity_pct": origin_diversity_pct,
                            "generated_prefix_rules": generation_meta["generated_prefix_rules"],
                            "active_prefix_rules": generation_meta["active_prefix_rules"],
                            "distinct_origins": generation_meta["distinct_origins"],
                            "proto": metrics["proto"],
                            "wall_us": metrics["wall_us"],
                            "total_ns": metrics["total_ns"],
                            "total_us": metrics["total_us"],
                            "calls": metrics["calls"],
                            "avg_ns": metrics["avg_ns"],
                            "rss_kb": metrics["rss_kb"],
                            "user_us": metrics["user_us"],
                            "sys_us": metrics["sys_us"],
                            "minflt": metrics["minflt"],
                            "majflt": metrics["majflt"],
                            "nvcsw": metrics["nvcsw"],
                            "nivcsw": metrics["nivcsw"],
                            "log_file": log_file,
                        }
                    )
        else:
            for _warmup in range(warmup_repeats):
                for project_key, _case_name, config_name in cases:
                    project = base.PROJECTS[project_key]
                    base.run_benchmark_case(project, config_name)

            for repeat in range(1, repeats + 1):
                for project_key, case_name, config_name in cases:
                    project = base.PROJECTS[project_key]
                    output, metrics = base.run_benchmark_case(project, config_name)
                    log_file = ""
                    if write_logs:
                        log_path = logs_dir / f"{repeat:02d}-{case_name}.log"
                        log_path.parent.mkdir(parents=True, exist_ok=True)
                        log_path.write_text(output, encoding="utf-8")
                        log_file = str(log_path)

                    rows.append(
                        {
                            "repeat": repeat,
                            "case": case_name,
                            "project": project_key,
                            "routes": routes,
                            "path_len": path_len,
                            "providers_per_rule": providers_per_rule,
                            "prefix_rules_per_origin": prefix_rules_per_origin,
                            "origin_diversity_pct": origin_diversity_pct,
                            "generated_prefix_rules": generation_meta["generated_prefix_rules"],
                            "active_prefix_rules": generation_meta["active_prefix_rules"],
                            "distinct_origins": generation_meta["distinct_origins"],
                            "proto": metrics["proto"],
                            "wall_us": metrics["wall_us"],
                            "total_ns": metrics["total_ns"],
                            "total_us": metrics["total_us"],
                            "calls": metrics["calls"],
                            "avg_ns": metrics["avg_ns"],
                            "rss_kb": metrics["rss_kb"],
                            "user_us": metrics["user_us"],
                            "sys_us": metrics["sys_us"],
                            "minflt": metrics["minflt"],
                            "majflt": metrics["majflt"],
                            "nvcsw": metrics["nvcsw"],
                            "nivcsw": metrics["nivcsw"],
                            "log_file": log_file,
                        }
                    )

        return rows
    finally:
        base.cleanup_temp_confs(temp_confs)


def validate_campaign(campaign: dict):
    problems = []
    warnings = []
    valid_modes = {
        "plain",
        "global",
        "hit",
        "miss",
        "plain+global",
        "hit+miss",
        "global+hit+miss",
        "plain+global+hit+miss",
    }

    required = {"name", "matrix"}
    missing = sorted(required - set(campaign))
    if missing:
        problems.append(f"faltando chaves: {', '.join(missing)}")

    matrix = normalize_campaign_matrix(campaign.get("matrix", {}))
    source_mode = campaign.get("source_mode", "synthetic")
    if source_mode not in {"synthetic", "mrt"}:
        problems.append(f"source_mode invalido: {source_mode!r}")
    if source_mode == "mrt" and not campaign.get("mrt_file") and not campaign.get("mrt_files"):
        problems.append("source_mode=mrt exige mrt_file ou mrt_files")

    keys = [
        "routes",
        "path_len",
        "providers_per_rule",
        "prefix_rules_per_origin",
        "origin_diversity_pct",
    ]
    for key in keys:
        values = matrix.get(key)
        if not isinstance(values, list) or not values:
            problems.append(f"matrix.{key} must be a non-empty list")

    measured = int(campaign.get("measured_repeats", 0))
    warmup = int(campaign.get("warmup_repeats", 0))
    mode = campaign.get("mode", "hit")
    if measured <= 0:
        problems.append("measured_repeats deve ser > 0")
    if warmup < 0:
        problems.append("warmup_repeats deve ser >= 0")
    if mode not in valid_modes:
        problems.append(f"mode invalido: {mode!r}")
    if campaign.get("name", "").endswith("-official") and measured < 5:
        warnings.append("official campaign with fewer than 5 measured repeats")
    if campaign.get("name", "").endswith("-official") and warmup < 2:
        warnings.append("official campaign with fewer than 2 warmups")
    return problems, warnings


def campaign_points(matrix: dict):
    matrix = normalize_campaign_matrix(matrix)
    keys = [
        "routes",
        "path_len",
        "providers_per_rule",
        "prefix_rules_per_origin",
        "origin_diversity_pct",
    ]
    values = [matrix[key] for key in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def load_campaign(path_or_name: str):
    path = Path(path_or_name)
    if not path.is_absolute():
        path = CAMPAIGNS_ROOT / path
    if path.suffix != ".json":
        path = path.with_suffix(".json")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if "matrix" in data:
        data["matrix"] = normalize_campaign_matrix(data["matrix"])
    data["_path"] = str(path)
    return data


def resolve_mrt_files(campaign: dict) -> list[str]:
    raw_files = []
    if campaign.get("mrt_file"):
        raw_files.append(campaign["mrt_file"])
    raw_files.extend(campaign.get("mrt_files", []))

    resolved: list[str] = []
    for item in raw_files:
        path = Path(item)
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        if not path.exists():
            raise RuntimeError(f"MRT file not found: {path}")
        resolved.append(str(path))
    return resolved


def campaign_output_dir(campaign: dict, suffix: str | None = None):
    name = campaign["name"]
    if suffix:
        name = f"{name}-{suffix}"
    return RESULTS_ROOT / name


def write_campaign_outputs(run_dir: Path, all_rows: list[dict], group_keys: list[str], meta: dict):
    results_path = run_dir / "results.csv"
    base.save_csv(results_path, benchmark_results_fieldnames(), all_rows)

    summary = base.summarize_rows(all_rows, group_keys=group_keys) if all_rows else []
    summary_path = run_dir / "summary.csv"
    base.save_csv(summary_path, summary_fieldnames(group_keys), summary)

    base.write_text_resilient(run_dir / "meta.json", json.dumps(meta, indent=2), encoding="utf-8")
    return results_path, summary_path


def run_campaign(campaign: dict, output_suffix: str | None = None, force_rebuild=None):
    run_dir = campaign_output_dir(campaign, output_suffix)
    base.shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    problems, warnings = validate_campaign(campaign)
    if problems:
        raise RuntimeError("; ".join(problems))
    for warning in warnings:
        print(f"aviso: {warning}", flush=True)

    all_rows = []
    target = campaign.get("target", "teste geral")
    mode_choice = campaign.get("mode", "hit+miss")
    measured_repeats = int(campaign.get("measured_repeats", 3))
    warmup_repeats = int(campaign.get("warmup_repeats", 1))
    rebuild_once = bool(campaign.get("rebuild_once", True))
    source_mode = campaign.get("source_mode", "synthetic")
    mrt_files = resolve_mrt_files(campaign) if source_mode == "mrt" else None
    if force_rebuild is not None:
        rebuild_once = force_rebuild
    write_logs = bool(campaign.get("write_logs", False))
    runner_mode = campaign.get("runner_mode", "batched-per-project")
    order_strategy = campaign.get("order_strategy", "rotate-projects")

    points = campaign_points(campaign["matrix"])
    group_keys = [
        "routes",
        "path_len",
        "providers_per_rule",
        "prefix_rules_per_origin",
        "origin_diversity_pct",
        "generated_prefix_rules",
        "active_prefix_rules",
        "distinct_origins",
        "case",
    ]
    meta = {
        "campaign": campaign,
        "output_dir": str(run_dir),
        "measured_points": len(points),
        "measured_repeats": measured_repeats,
        "warmup_repeats": warmup_repeats,
        "rebuild_once": rebuild_once,
        "runner_mode": runner_mode,
        "order_strategy": order_strategy,
        "source_mode": source_mode,
        "mrt_files": mrt_files or [],
        "host": base.host_metadata(),
        "status": "running",
        "completed_points": 0,
    }
    results_path, summary_path = write_campaign_outputs(run_dir, all_rows, group_keys, meta)
    try:
        for index, point in enumerate(points):
            rows = execute_benchmark_run(
                target=target,
                mode_choice=mode_choice,
                routes=int(point["routes"]),
                path_len=int(point["path_len"]),
                providers_per_rule=int(point["providers_per_rule"]),
                prefix_rules_per_origin=int(point["prefix_rules_per_origin"]),
                origin_diversity_pct=int(point["origin_diversity_pct"]),
                multi_upstream_origin_pct=100,
                prefix_scoped_route_share_pct=100,
                repeats=measured_repeats,
                rebuild=rebuild_once and index == 0,
                run_dir=run_dir,
                source_mode=source_mode,
                mrt_files=mrt_files,
                warmup_repeats=warmup_repeats,
                write_logs=write_logs,
                batch_by_project=(runner_mode == "batched-per-project"),
                point_index=index,
                order_strategy=order_strategy,
            )
            all_rows.extend(rows)
            meta["completed_points"] = index + 1
            results_path, summary_path = write_campaign_outputs(run_dir, all_rows, group_keys, meta)
    except Exception as exc:
        meta["status"] = "failed"
        meta["error"] = str(exc)
        write_campaign_outputs(run_dir, all_rows, group_keys, meta)
        raise

    meta["status"] = "completed"
    results_path, summary_path = write_campaign_outputs(run_dir, all_rows, group_keys, meta)

    print(f"\nCampaign: {campaign['name']}", flush=True)
    print(f"Results: {results_path}", flush=True)
    print(f"Summary: {summary_path}", flush=True)
    print(f"Meta: {run_dir / 'meta.json'}", flush=True)
    return run_dir


def run_benchmark_menu():
    target = base.prompt_choice(
        "Which BIRD tree do you want to run in the realistic benchmark?",
        ["bird", "linear", "trie", "hybridv2", "full benchmark set"],
    )
    target = base.normalize_project_choice(target)
    mode_choice = "global"
    if target == "full benchmark set":
        mode_choice = base.prompt_choice(
            "Which scenarios do you want to include for prefixed variants?",
            ["hit+miss", "global+hit+miss", "plain+global+hit+miss", "plain+global", "global", "hit", "miss"],
        )
    elif target != "bird":
        mode_choice = base.prompt_choice(
            "Which scenario do you want to run?",
            ["global", "hit", "miss", "hit+miss", "global+hit+miss"],
        )
    else:
        mode_choice = base.prompt_choice(
            "Which scenario do you want to run?",
            ["plain+global", "plain", "global"],
        )

    routes = base.prompt_int("Number of updates/routes", 10000)
    path_len = base.prompt_int("AS_PATH length", 4)
    providers_per_rule = base.prompt_int("Number of providers per rule", 2)
    prefix_rules_per_origin = base.prompt_int("Number of prefix rules per participating origin", 16)
    origin_diversity_pct = base.prompt_percent("Origin diversity percentage", 1)
    repeats = base.prompt_int("Number of repeats", 7)
    rebuild = base.prompt_yes_no("Rebuild binaries before running?", False)

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RESULTS_ROOT / timestamp
    rows = execute_benchmark_run(
        target=target,
        mode_choice=mode_choice,
        routes=routes,
        path_len=path_len,
        providers_per_rule=providers_per_rule,
        prefix_rules_per_origin=prefix_rules_per_origin,
        origin_diversity_pct=origin_diversity_pct,
        multi_upstream_origin_pct=100,
        prefix_scoped_route_share_pct=100,
        repeats=repeats,
        rebuild=rebuild,
        run_dir=run_dir,
        warmup_repeats=0,
        write_logs=True,
    )

    group_keys = [
        "routes",
        "path_len",
        "providers_per_rule",
        "prefix_rules_per_origin",
        "origin_diversity_pct",
        "generated_prefix_rules",
        "active_prefix_rules",
        "distinct_origins",
        "case",
    ]
    csv_path = run_dir / "results.csv"
    base.save_csv(csv_path, benchmark_results_fieldnames(), rows)
    summary = base.summarize_rows(rows, group_keys=group_keys)
    summary_path = run_dir / "summary.csv"
    base.save_csv(summary_path, summary_fieldnames(group_keys), summary)

    print(f"\nCSV: {csv_path}")
    print(f"Summary: {summary_path}")
    print(f"Logs: {run_dir / 'logs'}")
    if base.prompt_yes_no("Interpretar esse benchmark agora?", True):
        base.run_analysis_menu(summary_path)


def main():
    base.main()


if __name__ == "__main__":
    if hasattr(base.sys.stdout, "reconfigure"):
        base.sys.stdout.reconfigure(line_buffering=True)
    if hasattr(base.sys.stderr, "reconfigure"):
        base.sys.stderr.reconfigure(line_buffering=True)
    main()
