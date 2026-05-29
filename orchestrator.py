#!/usr/bin/env python3

import csv
import datetime as dt
import hashlib
import importlib
import json
import math
import os
import platform
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
from itertools import product
from pathlib import Path

sys.modules.setdefault("orchestrator", sys.modules[__name__])

ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = ROOT / "results"
CAMPAIGNS_ROOT = ROOT / "campaigns"
ASN_BASE = 100
ASN_STRIDE = 1000

PROJECTS = {
    "bird": {
        "label": "bird",
        "repo": ROOT / "bird",
        "lab": ROOT / "bird" / "lab",
        "service": "bird-original",
        "kind": "original",
    },
    "linear": {
        "label": "linear",
        "repo": ROOT / "bird-prefix-scan",
        "lab": ROOT / "bird-prefix-scan" / "lab",
        "service": "bird-prefix-scan",
        "kind": "modified",
    },
    "bird-prefix-trie": {
        "label": "bird-prefix-trie",
        "repo": ROOT / "bird-prefix-trie",
        "lab": ROOT / "bird-prefix-trie" / "lab",
        "service": "bird-prefix",
        "kind": "modified",
    },
    "bird-prefix-hybridv2": {
        "label": "bird-prefix-hybridv2",
        "repo": ROOT / "bird-prefix-hybridv2",
        "lab": ROOT / "bird-prefix-hybridv2" / "lab",
        "service": "bird-prefix-hybridv2",
        "kind": "modified",
    },
}

MODIFIED_PROJECT_CASE_PREFIX = {
    "linear": "linear",
    "bird-prefix-trie": "trie",
    "bird-prefix-hybridv2": "hybridv2",
}

PROJECT_OUTPUT_NAMES = {
    "bird": "bird",
    "linear": "linear",
    "bird-prefix-trie": "trie",
    "bird-prefix-hybridv2": "hybridv2",
}


def project_available(project_key):
    project = PROJECTS[project_key]
    return project["repo"].is_dir() and project["lab"].is_dir()


def available_project_keys():
    order = [
        "bird",
        "linear",
        "bird-prefix-trie",
        "bird-prefix-hybridv2",
    ]
    return [key for key in order if project_available(key)]


def project_output_name(project_key):
    return PROJECT_OUTPUT_NAMES.get(project_key, project_key)


def normalize_project_choice(choice):
    return {
        "bird-prefix-scan": "linear",
        "trie": "bird-prefix-trie",
        "hybridv2": "bird-prefix-hybridv2",
    }.get(choice, choice)

BENCH_FIELDS = [
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
]


BENCH_MARKER = "__BIRD_BENCH__"
DOCKER_COMPOSE_CMD = None

GENERATED_DIR_NAMES = {
    "__pycache__",
    "autom4te.cache",
    "obj",
}

GENERATED_FILE_PATTERNS = [
    ".DS_Store",
    "bird",
    "birdc",
    "birdcl",
    "config.log",
    "config.status",
    "configure~",
    "configure~ *",
    "original.conf",
    "original-plain.conf",
    "modified-global.conf",
    "modified-hit.conf",
    "modified-miss.conf",
    "bench-*.conf",
    "debug-*.conf",
    "autoconf.h *.in",
    "* 2.conf",
    "* 3.conf",
]


SUMMARY_BASE_FIELDS = [
    "case",
    "wall_us_mean",
    "wall_us_ci95",
    "total_ns_mean",
    "total_ns_ci95",
    "total_us_mean",
    "total_us_ci95",
    "calls_mean",
    "calls_ci95",
    "avg_ns_mean",
    "avg_ns_ci95",
    "rss_kb_mean",
    "rss_kb_ci95",
    "user_us_mean",
    "user_us_ci95",
    "sys_us_mean",
    "sys_us_ci95",
    "minflt_mean",
    "minflt_ci95",
    "majflt_mean",
    "majflt_ci95",
    "nvcsw_mean",
    "nvcsw_ci95",
    "nivcsw_mean",
    "nivcsw_ci95",
    "delta_total_ns_pct_vs_original",
    "delta_wall_pct_vs_original",
    "delta_total_pct_vs_original",
    "delta_avg_ns_pct_vs_original",
    "delta_rss_pct_vs_original",
    "delta_user_pct_vs_original",
    "delta_sys_pct_vs_original",
]


def campaign_search_roots():
    return [CAMPAIGNS_ROOT]


def list_campaign_paths():
    seen = set()
    paths = []
    for root in campaign_search_roots():
        if not root.exists():
            continue
        for path in sorted(root.glob("*.json")):
            if path.stem in seen:
                continue
            seen.add(path.stem)
            paths.append(path)
    return paths


def is_realistic_campaign(campaign):
    matrix = campaign.get("matrix", {})
    if "prefix_rules_per_origin" in matrix:
        return True
    if campaign.get("source_mode") in {"synthetic", "mrt"} and "prefix_rules_per_origin" in matrix:
        return True
    name = campaign.get("name", "")
    return name.startswith("realistic-")


def realistic_module():
    return importlib.import_module("realistic_campaigns")


def to_num(value):
    if value is None or value == "":
        return 0
    try:
        if "." in str(value):
            return float(value)
        return int(value)
    except (TypeError, ValueError):
        return 0


def pct(part, total):
    return (part * 100.0 / total) if total else 0.0


def iqr(values):
    ordered = sorted(values)
    if not ordered:
        return 0.0

    def quantile(q):
        if len(ordered) == 1:
            return float(ordered[0])
        pos = (len(ordered) - 1) * q
        lower = int(pos)
        upper = min(lower + 1, len(ordered) - 1)
        frac = pos - lower
        return ordered[lower] * (1.0 - frac) + ordered[upper] * frac

    return quantile(0.75) - quantile(0.25)


T_CRITICAL_95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


def mean_ci95(values):
    samples = [float(v) for v in values]
    if not samples:
        return 0.0, 0.0

    mean = statistics.mean(samples)
    if len(samples) < 2:
        return mean, 0.0

    stddev = statistics.stdev(samples)
    stderr = stddev / math.sqrt(len(samples))
    df = len(samples) - 1
    t_critical = T_CRITICAL_95.get(df, 1.96)
    ci95 = t_critical * stderr
    return mean, ci95


def run(cmd, cwd, capture_output=False, echo_output=True, merge_streams=False):
    run_kwargs = {
        "cwd": cwd,
        "text": True,
    }
    if capture_output:
        if merge_streams:
            run_kwargs["stdout"] = subprocess.PIPE
            run_kwargs["stderr"] = subprocess.STDOUT
        else:
            run_kwargs["capture_output"] = True

    result = subprocess.run(cmd, **run_kwargs)
    if capture_output and echo_output:
        if result.stdout:
            print(result.stdout, end="", flush=True)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode != 0:
        if capture_output and not echo_output:
            if result.stdout:
                print(result.stdout, end="", flush=True)
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr, flush=True)
        raise RuntimeError(f"command failed with exit code {result.returncode}")
    return result


def docker_compose_run(project, script):
    return [
        *docker_compose_command(),
        "run",
        "--rm",
        "--remove-orphans",
        project["service"],
        "bash",
        "-lc",
        script,
    ]


def docker_compose_command():
    global DOCKER_COMPOSE_CMD
    if DOCKER_COMPOSE_CMD is not None:
        return DOCKER_COMPOSE_CMD

    docker_bin = shutil.which("docker")
    if docker_bin:
        try:
            result = subprocess.run(
                [docker_bin, "compose", "version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            if result.returncode == 0:
                DOCKER_COMPOSE_CMD = ["docker", "compose"]
                return DOCKER_COMPOSE_CMD
        except OSError:
            pass

    docker_compose_bin = shutil.which("docker-compose")
    if docker_compose_bin:
        DOCKER_COMPOSE_CMD = ["docker-compose"]
        return DOCKER_COMPOSE_CMD

    raise RuntimeError(
        "nenhum comando Compose encontrado; instale 'docker compose' ou 'docker-compose'"
    )


def safe_pct_delta(current, baseline):
    if not baseline:
        return "0.00"
    return f"{((current - baseline) * 100.0 / baseline):.2f}"


def write_text_resilient(path, text, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        try:
            temp_path.write_text(text, encoding=encoding)
        except TimeoutError:
            path.unlink(missing_ok=True)
            temp_path.write_text(text, encoding=encoding)
        try:
            os.replace(temp_path, path)
        except TimeoutError:
            path.unlink(missing_ok=True)
            os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def benchmark_config_tag(prefix="bench"):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"{prefix}-{os.getpid()}-{stamp}"


def benchmark_config_names(tag=None):
    if not tag:
        return {
            "bird": {
                "original-plain": "original-plain.conf",
                "original-global": "original.conf",
            },
            "linear": {
                "linear-global": "modified-global.conf",
                "linear-hit": "modified-hit.conf",
                "linear-miss": "modified-miss.conf",
            },
            "bird-prefix-trie": {
                "trie-global": "modified-global.conf",
                "trie-hit": "modified-hit.conf",
                "trie-miss": "modified-miss.conf",
            },
            "bird-prefix-hybridv2": {
                "hybridv2-global": "modified-global.conf",
                "hybridv2-hit": "modified-hit.conf",
                "hybridv2-miss": "modified-miss.conf",
            },
        }

    safe_tag = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(tag))
    return {
        "bird": {
            "original-plain": f"bench-{safe_tag}-original-plain.conf",
            "original-global": f"bench-{safe_tag}-original.conf",
        },
        "linear": {
            "linear-global": f"bench-{safe_tag}-modified-global.conf",
            "linear-hit": f"bench-{safe_tag}-modified-hit.conf",
            "linear-miss": f"bench-{safe_tag}-modified-miss.conf",
        },
        "bird-prefix-trie": {
            "trie-global": f"bench-{safe_tag}-modified-global.conf",
            "trie-hit": f"bench-{safe_tag}-modified-hit.conf",
            "trie-miss": f"bench-{safe_tag}-modified-miss.conf",
        },
        "bird-prefix-hybridv2": {
            "hybridv2-global": f"bench-{safe_tag}-modified-global.conf",
            "hybridv2-hit": f"bench-{safe_tag}-modified-hit.conf",
            "hybridv2-miss": f"bench-{safe_tag}-modified-miss.conf",
        },
    }


def workspace_artifact_paths(include_results=False, include_logs=False):
    seen = set()

    for dir_name in {"__pycache__"}:
        for path in ROOT.rglob(dir_name):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path

    for pattern in (".DS_Store",):
        for path in ROOT.rglob(pattern):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path

    for project in PROJECTS.values():
        root = project["repo"]
        for dir_name in GENERATED_DIR_NAMES:
            for path in root.rglob(dir_name):
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    yield path

        for pattern in GENERATED_FILE_PATTERNS:
            for path in root.rglob(pattern):
                if path.is_dir():
                    continue
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    yield path

    if include_logs and (ROOT / "campaign_logs").exists():
        yield ROOT / "campaign_logs"

    if include_results and RESULTS_ROOT.exists():
        yield RESULTS_ROOT


def remove_path(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink(missing_ok=True)


def clean_workspace_artifacts(include_results=False, include_logs=False):
    removed = []
    for path in sorted(workspace_artifact_paths(include_results=include_results, include_logs=include_logs)):
        remove_path(path)
        removed.append(path)
    return removed


def parse_bench_tokens(bench_line):
    metrics = {field: "" for field in BENCH_FIELDS}
    for token in bench_line.split()[1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key in metrics:
            metrics[key] = value

    required = ["proto", "wall_us", "total_ns", "total_us", "calls", "avg_ns", "rss_kb", "user_us", "sys_us"]
    missing = [field for field in required if not metrics[field]]
    if missing:
        raise RuntimeError(f"BENCH line incompleta, faltando: {', '.join(missing)}")

    validate_bench_metrics(metrics)
    return metrics


def validate_bench_metrics(metrics):
    wall_us = int(metrics["wall_us"])
    total_ns = int(metrics["total_ns"])
    total_us = int(metrics["total_us"])
    calls = int(metrics["calls"])
    avg_ns = int(metrics["avg_ns"])

    if wall_us < 0 or total_ns < 0 or total_us < 0 or calls < 0 or avg_ns < 0:
        raise RuntimeError(f"BENCH line invalida: metrica negativa detectada: {metrics}")

    if calls == 0 and (total_ns != 0 or total_us != 0 or avg_ns != 0):
        raise RuntimeError(
            f"invalid BENCH line: calls=0, but total_ns/total_us/avg_ns are not zero: {metrics}"
        )

    if calls > 0:
        expected_avg_ns = total_ns // calls
        if avg_ns != expected_avg_ns:
            raise RuntimeError(
                f"invalid BENCH line: avg_ns={avg_ns} does not match total_ns/calls={expected_avg_ns}: {metrics}"
            )

    if total_us != total_ns // 1000:
        raise RuntimeError(
            f"invalid BENCH line: total_us={total_us} does not match total_ns//1000={total_ns // 1000}: {metrics}"
        )

    if total_us > wall_us:
        raise RuntimeError(
            f"BENCH line invalida: total_us={total_us} maior que wall_us={wall_us}: {metrics}"
        )


def parse_bench_line(text):
    for line in text.splitlines():
        if line.startswith("BENCH "):
            return parse_bench_tokens(line)

    raise RuntimeError("BENCH line not found")


def parse_marker_line(line):
    if not line.startswith(f"{BENCH_MARKER} "):
        return None

    marker = {}
    for token in line.split()[1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        marker[key] = value
    return marker


def parse_batched_bench_output(text):
    records = []
    current_marker = None
    current_benches = []

    def flush_current():
        nonlocal current_marker, current_benches, records
        if current_marker is None:
            if current_benches:
                raise RuntimeError("saida batelada invalida: linha BENCH sem marcador anterior")
            return

        if len(current_benches) != 1:
            case_name = current_marker.get("case", "<desconhecido>")
            repeat = current_marker.get("repeat", "<desconhecido>")
            phase = current_marker.get("phase", "<desconhecido>")
            raise RuntimeError(
                f"saida batelada invalida para case={case_name} repeat={repeat} phase={phase}: "
                f"esperada 1 linha BENCH, obtidas {len(current_benches)}"
            )

        if current_marker.get("phase") == "measure":
            records.append(
                {
                    "case": current_marker["case"],
                    "repeat": int(current_marker["repeat"]),
                    "metrics": current_benches[0],
                }
            )

    for line in text.splitlines():
        marker = parse_marker_line(line)
        if marker is not None:
            flush_current()
            current_marker = marker
            current_benches = []
            continue

        if line.startswith("BENCH "):
            current_benches.append(parse_bench_tokens(line))

    flush_current()
    return records


def cleanup_local_build_artifacts(project):
    repo = project["repo"]
    patterns = ["obj", "obj *", "bird", "birdc", "birdcl", "bird *", "birdc *", "birdcl *"]
    for pattern in patterns:
        for path in repo.glob(pattern):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()


def cleanup_temp_confs(paths):
    for path in paths:
        if not path:
            continue
        try:
            if path.exists():
                path.unlink()
        except FileNotFoundError:
            pass


def prompt_choice(title, options):
    print(f"\n{title}")
    for idx, option in enumerate(options, 1):
        print(f"{idx}. {option}")

    while True:
        raw = input("> ").strip()
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(options):
                return options[index]
        print("Opcao invalida, tente novamente.")


def prompt_int(title, default):
    while True:
        raw = input(f"{title} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print("Digite um numero inteiro.")


def prompt_percent(title, default):
    while True:
        value = prompt_int(title, default)
        if 0 <= value <= 100:
            return value
        print("Digite um percentual entre 0 e 100.")


def prompt_yes_no(title, default=False):
    suffix = "Y/n" if default else "y/N"
    raw = input(f"{title} [{suffix}]: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "s", "sim"}


def provider_list(good, providers_per_rule, base):
    providers = [str(good)]
    for j in range(1, providers_per_rule):
        providers.append(str(base + j))
    return ", ".join(providers)


def path_valid(path_len, start, second):
    path = [start, second]
    while len(path) < path_len:
        path.append(path[-1] + 1)
    return path


def path_invalid(path, invalid_second):
    bad = list(path)
    bad[1] = invalid_second
    return bad


def distinct_seed_count(routes, origin_diversity_pct):
    if routes <= 0:
        return 1
    if origin_diversity_pct <= 0:
        return 1
    if origin_diversity_pct >= 100:
        return routes
    return max(1, math.ceil(routes * origin_diversity_pct / 100.0))


def synthetic_generation_meta(routes, prefix_rules, origin_diversity_pct):
    origins = distinct_seed_count(routes, origin_diversity_pct)
    return {
        "distinct_origins": origins,
        "generated_prefix_rules": origins * max(0, prefix_rules),
    }


def route_seed(route_index, routes, origin_diversity_pct):
    seed_index = route_index % distinct_seed_count(routes, origin_diversity_pct)
    return ASN_BASE + (seed_index * ASN_STRIDE)


def original_paths(route_index, routes, path_len, origin_diversity_pct):
    start = route_seed(route_index, routes, origin_diversity_pct)
    good = path_valid(path_len, start, start + 1)
    bad = path_invalid(good, start + 900)
    return good, bad


def modified_paths(route_index, routes, path_len, origin_diversity_pct):
    start = route_seed(route_index, routes, origin_diversity_pct)
    specific = path_valid(path_len, start, start + 1)
    fallback = path_valid(path_len, start, start + 101)
    specific_bad = path_invalid(specific, start + 901)
    fallback_bad = path_invalid(fallback, start + 902)
    return specific, specific_bad, fallback, fallback_bad


def route_block(prefix, path):
    lines = [f"    route {prefix} blackhole {{"]
    for asn in path:
        lines.append(f"        bgp_path = prepend(bgp_path, {asn});")
    lines.append("    };")
    return "\n".join(lines)


def route_prefix(first_octet, route_index):
    second = (route_index >> 16) & 0xFF
    third = (route_index >> 8) & 0xFF
    fourth = route_index & 0xFF
    return f"{first_octet}.{second}.{third}.{fourth}/32"


def add_rule(rule_map, asn, providers, prefix=None):
    per_asn = rule_map.setdefault(asn, {})
    if prefix in per_asn:
        return
    per_asn[prefix] = providers


def render_aspa_rule_map(rule_map):
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


def render_aspa_rule_map_legacy(rule_map):
    lines = []
    for asn in sorted(rule_map):
        items = sorted(rule_map[asn].items(), key=lambda item: (item[0] is not None, item[0] or ""))
        for prefix, providers in items:
            if prefix:
                lines.append(f"    route aspa {asn} prefix {prefix} providers {providers};")
            else:
                lines.append(f"    route aspa {asn} providers {providers};")
    return lines


def add_global_rules(rule_map, seen, path, providers_per_rule, start_index=0):
    for i in range(start_index, len(path) - 1):
        asn = path[i]
        good = path[i + 1]
        rule = (asn, None)
        if rule in seen:
            continue
        seen.add(rule)
        add_rule(
            rule_map,
            asn,
            provider_list(good, providers_per_rule, 66000 + asn),
        )


def rules_global(paths, providers_per_rule):
    rule_map = {}
    seen = set()
    for path in paths:
        add_global_rules(rule_map, seen, path, providers_per_rule)
    return render_aspa_rule_map_legacy(rule_map)


def add_origin_rule(rule_map, seen, asn, providers, prefix=None):
    rule = (asn, prefix)
    if rule in seen:
        return
    seen.add(rule)
    add_rule(rule_map, asn, providers, prefix=prefix)


def rules_modified(path_sets, providers_per_rule, prefix_rules, mode):
    rule_map = {}
    seen = set()

    if mode == "global_only":
        for specific, _fallback in path_sets:
            add_global_rules(rule_map, seen, specific, providers_per_rule)
        return render_aspa_rule_map(rule_map)

    for specific, fallback in path_sets:
        origin = specific[0]
        add_origin_rule(
            rule_map,
            seen,
            origin,
            provider_list(specific[1], providers_per_rule, 65000 + origin),
        )
        add_origin_rule(
            rule_map,
            seen,
            origin,
            provider_list(specific[1], providers_per_rule, 65100 + origin),
            prefix="10.0.0.0/8",
        )

        for extra in range(1, prefix_rules):
            px2 = (extra // 256) % 256
            px3 = extra % 256
            add_origin_rule(
                rule_map,
                seen,
                origin,
                provider_list(origin + 500 + extra, providers_per_rule, origin + 600 + extra),
                prefix=f"11.{px2}.{px3}.0/24",
            )

        add_global_rules(rule_map, seen, specific, providers_per_rule, start_index=1)

    return render_aspa_rule_map(rule_map)


def config_original(routes, path_len, providers_per_rule, origin_diversity_pct):
    route_lines = []
    all_paths = []
    for i in range(routes):
        good, _bad = original_paths(i, routes, path_len, origin_diversity_pct)
        prefix = route_prefix(203, i)
        path = good
        all_paths.append(good)
        route_lines.append(route_block(prefix, path))

    return "\n".join([
        'log "/dev/null" all;',
        'router id 192.0.2.10;',
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
        *rules_global(all_paths, providers_per_rule),
        '}',
        "",
        'protocol static test_routes {',
        '    ipv4 { import filter f; };',
        "",
        *route_lines,
        '}',
        "",
        'protocol device {}',
        "",
    ])


def config_original_plain(routes, path_len, origin_diversity_pct):
    route_lines = []
    for i in range(routes):
        good, _bad = original_paths(i, routes, path_len, origin_diversity_pct)
        prefix = route_prefix(203, i)
        route_lines.append(route_block(prefix, good))

    return "\n".join([
        'log "/dev/null" all;',
        'router id 192.0.2.11;',
        "",
        'filter f {',
        '    accept;',
        '}',
        "",
        'protocol static test_routes {',
        '    ipv4 { import filter f; };',
        "",
        *route_lines,
        '}',
        "",
        'protocol device {}',
        "",
    ])


def config_modified(routes, path_len, providers_per_rule, prefix_rules, mode, origin_diversity_pct):
    route_lines = []
    path_sets = []
    for i in range(routes):
        specific, _specific_bad, fallback, _fallback_bad = modified_paths(
            i, routes, path_len, origin_diversity_pct
        )
        path_sets.append((specific, fallback))
        if mode == "global_only":
            prefix = route_prefix(203, i)
            path = specific
        elif mode == "prefix_hit":
            prefix = route_prefix(10, i)
            path = specific
        elif mode == "prefix_miss":
            prefix = route_prefix(172, i)
            path = specific
        else:
            raise ValueError("modo invalido")

        route_lines.append(route_block(prefix, path))

    return "\n".join([
        'log "/dev/null" all;',
        'router id 192.0.2.20;',
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
        *rules_modified(path_sets, providers_per_rule, prefix_rules, mode),
        '}',
        "",
        'protocol static test_routes {',
        '    ipv4 { import filter f; };',
        "",
        *route_lines,
        '}',
        "",
        'protocol device {}',
        "",
    ])


def write_benchmark_configs(routes, path_len, providers_per_rule, prefix_rules, origin_diversity_pct, tag=None):
    created = []
    config_names = benchmark_config_names(tag)

    original_plain_conf = PROJECTS["bird"]["repo"] / config_names["bird"]["original-plain"]
    write_text_resilient(
        original_plain_conf,
        config_original_plain(routes, path_len, origin_diversity_pct),
        encoding="utf-8",
    )
    created.append(original_plain_conf)

    original_conf = PROJECTS["bird"]["repo"] / config_names["bird"]["original-global"]
    write_text_resilient(
        original_conf,
        config_original(routes, path_len, providers_per_rule, origin_diversity_pct),
        encoding="utf-8",
    )
    created.append(original_conf)

    for key in available_project_keys():
        if key == "bird":
            continue
        repo = PROJECTS[key]["repo"]
        variant = MODIFIED_PROJECT_CASE_PREFIX[key]
        for mode, case_name in (
            ("global_only", f"{variant}-global"),
            ("prefix_hit", f"{variant}-hit"),
            ("prefix_miss", f"{variant}-miss"),
        ):
            path = repo / config_names[key][case_name]
            write_text_resilient(
                path,
                config_modified(routes, path_len, providers_per_rule, prefix_rules, mode, origin_diversity_pct),
                encoding="utf-8",
            )
            created.append(path)

    return created, config_names


def write_inspection_debug_config(project_key, source_name, tag=None):
    repo = PROJECTS[project_key]["repo"]
    source = repo / source_name
    target_name = f"debug-{source_name}" if not tag else f"debug-{tag}-{source_name}"
    target = repo / target_name
    text = source.read_text(encoding="utf-8")
    text = text.replace("protocol static test_routes {", "protocol static test_routes_dbg {", 1)
    target.write_text(text, encoding="utf-8")
    return target


def rebuild_project(project):
    cleanup_local_build_artifacts(project)
    script = """
set -eux
rm -rf /tmp/bird-build
mkdir -p /tmp/bird-build
cp -a /bird-source/. /tmp/bird-build/
cd /tmp/bird-build
autoreconf -fi
./configure --with-sysconfig=linux --with-protocols=bgp,static,perf --enable-libssh=no
make bird birdc
cp -f ./bird /bird-source/bird
cp -f ./birdc /bird-source/birdc
"""
    last_error = None
    for attempt in range(1, 3):
        try:
            run(
                docker_compose_run(project, script),
                cwd=project["lab"],
            )
            return
        except RuntimeError as exc:
            last_error = exc
            if attempt == 1:
                continue
            raise
    if last_error:
        raise last_error


def project_binaries_ready(project):
    repo = project["repo"]
    return (repo / "bird").is_file() and (repo / "birdc").is_file()


def ensure_project_binaries(project):
    if project_binaries_ready(project):
        return

    rebuild_project(project)


def run_benchmark_case(project, config_name):
    ensure_project_binaries(project)
    script = f"""
set -eu
mkdir -p /run
rm -rf /tmp/bird-run
mkdir -p /tmp/bird-run
cd /bird-source
cp -f ./bird /tmp/bird-run/bird
cp -f /bird-source/{shlex.quote(config_name)} /tmp/bird-run/{shlex.quote(config_name)}
/tmp/bird-run/bird -p -c /tmp/bird-run/{shlex.quote(config_name)}
/tmp/bird-run/bird -d -c /tmp/bird-run/{shlex.quote(config_name)} -s /run/bird.ctl
"""
    result = run(
        docker_compose_run(project, script),
        cwd=project["lab"],
        capture_output=True,
        echo_output=False,
    )
    text = result.stdout + result.stderr
    return text, parse_bench_line(text)


def build_batched_benchmark_script(case_specs, warmup_repeats, repeats):
    config_names = sorted({config_name for _, config_name in case_specs})
    lines = [
        "set -eu",
        "mkdir -p /run",
        "rm -rf /tmp/bird-run",
        "mkdir -p /tmp/bird-run",
        "cd /bird-source",
        "cp -f ./bird /tmp/bird-run/bird",
        "run_case() {",
        '  phase="$1"',
        '  repeat="$2"',
        '  case_name="$3"',
        '  config_name="$4"',
        "  rm -f /run/bird.ctl",
        f'  echo "{BENCH_MARKER} phase=${{phase}} repeat=${{repeat}} case=${{case_name}}"',
        '  /tmp/bird-run/bird -p -c "/tmp/bird-run/${config_name}"',
        '  /tmp/bird-run/bird -d -c "/tmp/bird-run/${config_name}" -s /run/bird.ctl',
        "}",
    ]

    for config_name in config_names:
        lines.append(f"cp -f /bird-source/{shlex.quote(config_name)} /tmp/bird-run/{shlex.quote(config_name)}")

    for repeat in range(1, warmup_repeats + 1):
        for case_name, config_name in case_specs:
            lines.append(f"run_case warmup {repeat} {shlex.quote(case_name)} {shlex.quote(config_name)}")

    for repeat in range(1, repeats + 1):
        for case_name, config_name in case_specs:
            lines.append(f"run_case measure {repeat} {shlex.quote(case_name)} {shlex.quote(config_name)}")

    lines.append("rm -f /run/bird.ctl")
    return "\n".join(lines)


def run_benchmark_cases_batched(project, case_specs, warmup_repeats, repeats):
    ensure_project_binaries(project)
    script = build_batched_benchmark_script(case_specs, warmup_repeats, repeats)
    result = run(
        docker_compose_run(project, script),
        cwd=project["lab"],
        capture_output=True,
        echo_output=False,
        merge_streams=True,
    )
    text = result.stdout or ""
    records = parse_batched_bench_output(text)
    expected = repeats * len(case_specs)
    if len(records) != expected:
        raise RuntimeError(f"saida batelada incompleta: esperado {expected} medicoes, obtido {len(records)}")
    return text, records


def run_benchmark_cases_serial(project, case_specs, warmup_repeats, repeats, logs_dir=None, write_logs=False):
    rows = []

    for _warmup in range(warmup_repeats):
        for _case_name, config_name in case_specs:
            run_benchmark_case(project, config_name)

    for repeat in range(1, repeats + 1):
        for case_name, config_name in case_specs:
            output, metrics = run_benchmark_case(project, config_name)
            log_file = ""
            if write_logs and logs_dir is not None:
                log_path = logs_dir / f"{project['label']}-{repeat:02d}-{case_name}.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(output, encoding="utf-8")
                log_file = str(log_path)

            rows.append(
                {
                    "repeat": repeat,
                    "case": case_name,
                    "project": project_output_name(next(key for key, value in PROJECTS.items() if value is project)),
                    "metrics": metrics,
                    "log_file": log_file,
                }
            )

    return rows


def save_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def latest_results_dir():
    dirs = [path for path in RESULTS_ROOT.iterdir() if path.is_dir()]
    if not dirs:
        raise RuntimeError("no experiment found in results/")
    return sorted(dirs)[-1]


def load_csv_rows(path):
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def safe_command_output(cmd, cwd=None):
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    output = result.stdout.strip()
    return output or None


def read_text_if_exists(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def source_fingerprint(repo):
    digest = hashlib.sha256()
    for path in sorted(repo.rglob("*")):
        if path.is_dir():
            continue
        if any(part in GENERATED_DIR_NAMES for part in path.parts):
            continue
        if path.name in {"bird", "birdc", "birdcl", ".DS_Store", "config.log", "config.status"}:
            continue
        if path.name.startswith("configure~"):
            continue
        if path.name in {"original.conf", "original-plain.conf", "modified-global.conf", "modified-hit.conf", "modified-miss.conf"}:
            continue
        if path.name.startswith("bench-") and path.suffix == ".conf":
            continue
        if path.name.startswith("debug-") and path.suffix == ".conf":
            continue
        try:
            rel = path.relative_to(repo).as_posix().encode("utf-8")
            digest.update(rel)
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        except OSError:
            continue
    return digest.hexdigest()


def project_revision_metadata():
    revisions = {}
    for key, project in PROJECTS.items():
        repo = project["repo"]
        revisions[project_output_name(key)] = {
            "git_head": safe_command_output(["git", "rev-parse", "HEAD"], cwd=repo),
            "git_status_short": safe_command_output(["git", "status", "--short"], cwd=repo) or "",
            "source_fingerprint_sha256": source_fingerprint(repo),
        }
    return revisions


def docker_environment_metadata():
    compose_cmd = docker_compose_command()
    return {
        "docker_client_version": safe_command_output(["docker", "version", "--format", "{{.Client.Version}}"]),
        "docker_server_version": safe_command_output(["docker", "version", "--format", "{{.Server.Version}}"]),
        "docker_compose_version": safe_command_output([*compose_cmd, "version", "--short"]),
    }


def linux_environment_metadata():
    if platform.system() != "Linux":
        return {"platform": platform.system()}

    return {
        "cpu_governors": safe_command_output(
            ["bash", "-lc", "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u"]
        ),
        "irqbalance_active": safe_command_output(["systemctl", "is-active", "irqbalance"]),
        "bench_cpuset_cpus": read_text_if_exists("/sys/fs/cgroup/bench/cpuset.cpus"),
        "bench_cpuset_mems": read_text_if_exists("/sys/fs/cgroup/bench/cpuset.mems"),
        "housekeeping_cpuset_cpus": read_text_if_exists("/sys/fs/cgroup/housekeeping/cpuset.cpus"),
        "clocksource": read_text_if_exists("/sys/devices/system/clocksource/clocksource0/current_clocksource"),
    }


def validate_campaign(campaign):
    if is_realistic_campaign(campaign):
        return realistic_module().validate_campaign(campaign)

    problems = []
    warnings = []
    valid_modes = {
        "plain",
        "global",
        "miss",
        "hit",
        "plain+global",
        "hit+miss",
        "global+hit+miss",
        "plain+global+hit+miss",
    }

    required_keys = {"name", "matrix"}
    missing = sorted(required_keys - set(campaign))
    if missing:
        problems.append(f"missing required fields: {', '.join(missing)}")

    matrix = campaign.get("matrix", {})
    for key in ("routes", "path_len", "providers_per_rule", "prefix_rules", "origin_diversity_pct"):
        values = matrix.get(key)
        if not isinstance(values, list) or not values:
            problems.append(f"matrix.{key} must be a non-empty list")

    measured = int(campaign.get("measured_repeats", 0))
    warmup = int(campaign.get("warmup_repeats", 0))
    mode = campaign.get("mode", "hit+miss")
    if measured <= 0:
        problems.append("measured_repeats must be > 0")
    if warmup < 0:
        problems.append("warmup_repeats must be >= 0")
    if mode not in valid_modes:
        problems.append(f"invalid mode: {mode!r}")

    if campaign.get("name", "").endswith("-official") and measured < 5:
        warnings.append("official campaign with fewer than 5 measured repeats")
    if campaign.get("name", "").endswith("-official") and warmup < 2:
        warnings.append("official campaign with fewer than 2 warmups")

    return problems, warnings


def emit_preflight(campaign=None):
    print("\n=== Preflight ===", flush=True)
    print(f"workspace={ROOT}", flush=True)
    print(f"platform={platform.platform()}", flush=True)
    docker = docker_environment_metadata()
    print(f"docker_client={docker.get('docker_client_version')}", flush=True)
    print(f"docker_server={docker.get('docker_server_version')}", flush=True)
    print(f"docker_compose={docker.get('docker_compose_version')}", flush=True)

    linux_meta = linux_environment_metadata()
    if platform.system() == "Linux":
        print(f"cpu_governors={linux_meta.get('cpu_governors') or 'unknown'}", flush=True)
        print(f"irqbalance_active={linux_meta.get('irqbalance_active') or 'unknown'}", flush=True)
        print(f"bench_cpuset_cpus={linux_meta.get('bench_cpuset_cpus') or 'not configured'}", flush=True)
        print(f"housekeeping_cpuset_cpus={linux_meta.get('housekeeping_cpuset_cpus') or 'not configured'}", flush=True)
        print(f"clocksource={linux_meta.get('clocksource') or 'unknown'}", flush=True)

    artifact_count = sum(1 for _ in workspace_artifact_paths(include_results=False, include_logs=False))
    print(f"workspace_artifacts_detected={artifact_count}", flush=True)

    if campaign:
        problems, warnings = validate_campaign(campaign)
        print(f"campaign={campaign['name']}", flush=True)
        if problems:
            print("problems:", flush=True)
            for item in problems:
                print(f"- {item}", flush=True)
        if warnings:
            print("warnings:", flush=True)
            for item in warnings:
                print(f"- {item}", flush=True)
        if not problems and not warnings:
            print("ok", flush=True)


def print_analysis(rows, label):
    print(f"\nAnalysis: {label}\n", flush=True)
    group_fields = [
        field
        for field in ("routes", "path_len", "providers_per_rule", "prefix_rules", "origin_diversity_pct")
        if rows and field in rows[0]
    ]

    grouped = {}
    if group_fields:
        for row in rows:
            grouped.setdefault(tuple(row[field] for field in group_fields), []).append(row)
    else:
        grouped[()] = rows

    for group_key, group_rows in grouped.items():
        if group_fields:
            details = ", ".join(f"{field}={value}" for field, value in zip(group_fields, group_key))
            print(f"--- {details} ---\n", flush=True)

        for row in group_rows:
            case = row["case"]
            wall_us = to_num(row.get("wall_us_mean", row.get("wall_us_median", row.get("wall_us", 0))))
            wall_ci95 = to_num(row.get("wall_us_ci95", 0))
            wall_iqr = to_num(row.get("wall_us_iqr", 0))
            total_ns = to_num(row.get("total_ns_mean", row.get("total_ns", 0)))
            total_us = to_num(row.get("total_us_mean", row.get("total_us_median", row.get("total_us", 0))))
            total_ci95 = to_num(row.get("total_us_ci95", 0))
            total_iqr = to_num(row.get("total_us_iqr", 0))
            avg_ns = to_num(row.get("avg_ns_mean", row.get("avg_ns_median", row.get("avg_ns", 0))))
            avg_ci95 = to_num(row.get("avg_ns_ci95", 0))
            avg_iqr = to_num(row.get("avg_ns_iqr", 0))
            rss = to_num(row.get("rss_kb_mean", row.get("rss_kb_median", row.get("rss_kb", 0))))
            rss_ci95 = to_num(row.get("rss_kb_ci95", 0))
            rss_iqr = to_num(row.get("rss_kb_iqr", 0))
            user_us = to_num(row.get("user_us_mean", row.get("user_us_median", row.get("user_us", 0))))
            user_ci95 = to_num(row.get("user_us_ci95", 0))
            user_iqr = to_num(row.get("user_us_iqr", 0))
            sys_us = to_num(row.get("sys_us_mean", row.get("sys_us_median", row.get("sys_us", 0))))
            sys_ci95 = to_num(row.get("sys_us_ci95", 0))
            sys_iqr = to_num(row.get("sys_us_iqr", 0))

            delta_wall = to_num(row.get("delta_wall_pct_vs_original", 0))
            delta_total_ns = to_num(row.get("delta_total_ns_pct_vs_original", row.get("delta_total_pct_vs_original", 0)))
            delta_total = to_num(row.get("delta_total_pct_vs_original", 0))
            delta_avg = to_num(row.get("delta_avg_ns_pct_vs_original", 0))
            delta_rss = to_num(row.get("delta_rss_pct_vs_original", 0))
            delta_user = to_num(row.get("delta_user_pct_vs_original", 0))
            delta_sys = to_num(row.get("delta_sys_pct_vs_original", 0))

            print(f"=== {case} ===", flush=True)
            print("general metrics:", flush=True)
            if wall_ci95:
                print(f"wall_us={wall_us:.2f} ± {wall_ci95:.2f} ({delta_wall:+.2f}% vs original)", flush=True)
            elif wall_iqr:
                print(f"wall_us={wall_us:.0f} (IQR={wall_iqr:.2f}, {delta_wall:+.2f}% vs original)", flush=True)
            else:
                print(f"wall_us={wall_us:.0f} ({delta_wall:+.2f}% vs original)", flush=True)
            if rss_ci95 or user_ci95 or sys_ci95:
                print(f"rss_kb={rss:.2f} ± {rss_ci95:.2f} ({delta_rss:+.2f}% vs original)", flush=True)
                print(f"user_us={user_us:.2f} ± {user_ci95:.2f} ({delta_user:+.2f}% vs original)", flush=True)
                print(f"sys_us={sys_us:.2f} ± {sys_ci95:.2f} ({delta_sys:+.2f}% vs original)", flush=True)
            elif rss_iqr or user_iqr or sys_iqr:
                print(f"rss_kb={rss:.0f} (IQR={rss_iqr:.2f}, {delta_rss:+.2f}% vs original)", flush=True)
                print(f"user_us={user_us:.0f} (IQR={user_iqr:.2f}, {delta_user:+.2f}% vs original)", flush=True)
                print(f"sys_us={sys_us:.0f} (IQR={sys_iqr:.2f}, {delta_sys:+.2f}% vs original)", flush=True)
            else:
                print(f"rss_kb={rss:.0f} ({delta_rss:+.2f}% vs original)", flush=True)
                print(f"user_us={user_us:.0f} ({delta_user:+.2f}% vs original)", flush=True)
                print(f"sys_us={sys_us:.0f} ({delta_sys:+.2f}% vs original)", flush=True)
            print("ASPA metrics:", flush=True)
            if total_ci95 or avg_ci95:
                print(
                    f"total_ns={total_ns:.2f} ({delta_total_ns:+.2f}% vs original) "
                    f"total_us={total_us:.2f} ± {total_ci95:.2f} ({delta_total:+.2f}% vs original) "
                    f"avg_ns={avg_ns:.2f} ± {avg_ci95:.2f} ({delta_avg:+.2f}% vs original)",
                    flush=True,
                )
            elif total_iqr or avg_iqr:
                print(
                    f"total_ns={total_ns:.0f} ({delta_total_ns:+.2f}% vs original) "
                    f"total_us={total_us:.0f} (IQR={total_iqr:.2f}, {delta_total:+.2f}% vs original) "
                    f"avg_ns={avg_ns:.0f} (IQR={avg_iqr:.2f}, {delta_avg:+.2f}% vs original)",
                    flush=True,
                )
            else:
                print(
                    f"total_ns={total_ns:.0f} ({delta_total_ns:+.2f}% vs original) "
                    f"total_us={total_us:.0f} ({delta_total:+.2f}% vs original) "
                    f"avg_ns={avg_ns:.0f} ({delta_avg:+.2f}% vs original)",
                    flush=True,
                )

            if case == "original-global":
                print("interpretation: baseline used to compare memory, CPU, and internal cost.", flush=True)
            print(flush=True)


def run_analysis_menu(default_path=None):
    if default_path:
        target = Path(default_path)
    else:
        source = prompt_choice(
            "Which result do you want to interpret?",
            ["latest experiment", "choose path"],
        )
        if source == "latest experiment":
            target = latest_results_dir() / "summary.csv"
        else:
            raw = input("Enter the path to summary.csv, results.csv, or the experiment directory: ").strip()
            if not raw:
                print("No path provided.")
                return
            target = Path(raw).expanduser()

    if target.is_dir():
        if (target / "summary.csv").exists():
            target = target / "summary.csv"
        elif (target / "results.csv").exists():
            target = target / "results.csv"
        else:
            raise RuntimeError("the provided directory does not contain summary.csv or results.csv")

    if not target.exists():
        raise RuntimeError(f"file not found: {target}")

    if target.name == "summary.csv":
        rows = load_csv_rows(target)
    elif target.name == "results.csv":
        rows = summarize_rows(load_csv_rows(target))
    else:
        raise RuntimeError("provide summary.csv, results.csv, or an experiment directory")

    print_analysis(rows, str(target))


def summarize_rows(rows, group_keys=None):
    group_keys = group_keys or ["case"]
    grouped = {}
    for row in rows:
        key = tuple(row[field] for field in group_keys)
        grouped.setdefault(key, []).append(row)

    summary = []
    for key, items in grouped.items():
        identity = dict(zip(group_keys, key))
        wall_us_mean, wall_us_ci95 = mean_ci95(int(item["wall_us"]) for item in items)
        total_ns_mean, total_ns_ci95 = mean_ci95(int(item["total_ns"]) for item in items)
        total_us_mean, total_us_ci95 = mean_ci95(int(item["total_us"]) for item in items)
        calls_mean, calls_ci95 = mean_ci95(int(item["calls"]) for item in items)
        avg_ns_mean, avg_ns_ci95 = mean_ci95(int(item["avg_ns"]) for item in items)
        rss_kb_mean, rss_kb_ci95 = mean_ci95(int(item["rss_kb"]) for item in items)
        user_us_mean, user_us_ci95 = mean_ci95(int(item["user_us"]) for item in items)
        sys_us_mean, sys_us_ci95 = mean_ci95(int(item["sys_us"]) for item in items)
        minflt_mean, minflt_ci95 = mean_ci95(int(item["minflt"]) for item in items)
        majflt_mean, majflt_ci95 = mean_ci95(int(item["majflt"]) for item in items)
        nvcsw_mean, nvcsw_ci95 = mean_ci95(int(item["nvcsw"]) for item in items)
        nivcsw_mean, nivcsw_ci95 = mean_ci95(int(item["nivcsw"]) for item in items)
        data = {
            **identity,
            "wall_us_mean": round(wall_us_mean, 2),
            "wall_us_ci95": round(wall_us_ci95, 2),
            "total_ns_mean": round(total_ns_mean, 2),
            "total_ns_ci95": round(total_ns_ci95, 2),
            "total_us_mean": round(total_us_mean, 2),
            "total_us_ci95": round(total_us_ci95, 2),
            "calls_mean": round(calls_mean, 2),
            "calls_ci95": round(calls_ci95, 2),
            "avg_ns_mean": round(avg_ns_mean, 2),
            "avg_ns_ci95": round(avg_ns_ci95, 2),
            "rss_kb_mean": round(rss_kb_mean, 2),
            "rss_kb_ci95": round(rss_kb_ci95, 2),
            "user_us_mean": round(user_us_mean, 2),
            "user_us_ci95": round(user_us_ci95, 2),
            "sys_us_mean": round(sys_us_mean, 2),
            "sys_us_ci95": round(sys_us_ci95, 2),
            "minflt_mean": round(minflt_mean, 2),
            "minflt_ci95": round(minflt_ci95, 2),
            "majflt_mean": round(majflt_mean, 2),
            "majflt_ci95": round(majflt_ci95, 2),
            "nvcsw_mean": round(nvcsw_mean, 2),
            "nvcsw_ci95": round(nvcsw_ci95, 2),
            "nivcsw_mean": round(nivcsw_mean, 2),
            "nivcsw_ci95": round(nivcsw_ci95, 2),
        }
        summary.append(data)

    baseline_keys = [field for field in group_keys if field != "case"]
    baselines = {}
    for row in summary:
        if row["case"] == "original-global":
            baselines[tuple(row[field] for field in baseline_keys)] = row

    if baselines:
        for row in summary:
            baseline = baselines.get(tuple(row[field] for field in baseline_keys))
            if not baseline:
                continue
            if row["case"] == "original-global":
                row["delta_wall_pct_vs_original"] = "0.00"
                row["delta_total_ns_pct_vs_original"] = "0.00"
                row["delta_total_pct_vs_original"] = "0.00"
                row["delta_avg_ns_pct_vs_original"] = "0.00"
                row["delta_rss_pct_vs_original"] = "0.00"
                row["delta_user_pct_vs_original"] = "0.00"
                row["delta_sys_pct_vs_original"] = "0.00"
                continue
            row["delta_wall_pct_vs_original"] = safe_pct_delta(row["wall_us_mean"], baseline["wall_us_mean"])
            row["delta_total_ns_pct_vs_original"] = safe_pct_delta(row["total_ns_mean"], baseline["total_ns_mean"])
            row["delta_total_pct_vs_original"] = safe_pct_delta(row["total_us_mean"], baseline["total_us_mean"])
            row["delta_avg_ns_pct_vs_original"] = safe_pct_delta(row["avg_ns_mean"], baseline["avg_ns_mean"])
            row["delta_rss_pct_vs_original"] = safe_pct_delta(row["rss_kb_mean"], baseline["rss_kb_mean"])
            row["delta_user_pct_vs_original"] = safe_pct_delta(row["user_us_mean"], baseline["user_us_mean"])
            row["delta_sys_pct_vs_original"] = safe_pct_delta(row["sys_us_mean"], baseline["sys_us_mean"])

    return sorted(summary, key=lambda row: tuple(row[field] for field in group_keys))


def benchmark_results_fieldnames():
    return [
        "repeat",
        "case",
        "project",
        "routes",
        "path_len",
        "providers_per_rule",
        "prefix_rules",
        "origin_diversity_pct",
        "generated_prefix_rules",
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
    group_keys = group_keys or ["case"]
    ordered = [field for field in group_keys if field != "case"]
    if "case" in group_keys:
        ordered.append("case")
    return ordered + [field for field in SUMMARY_BASE_FIELDS if field != "case"]


def execute_benchmark_run(
    target,
    mode_choice,
    routes,
    path_len,
    providers_per_rule,
    prefix_rules,
    origin_diversity_pct,
    repeats,
    rebuild,
    run_dir,
    warmup_repeats=0,
    write_logs=True,
    batch_by_project=False,
    point_index=0,
    order_strategy="rotate-projects",
):
    temp_confs = []
    try:
        generation_meta = synthetic_generation_meta(routes, prefix_rules, origin_diversity_pct)
        temp_confs, config_names = write_benchmark_configs(
            routes, path_len, providers_per_rule, prefix_rules, origin_diversity_pct, tag=benchmark_config_tag()
        )

        cases = ordered_cases_for_point(
            selected_benchmark_cases(target, mode_choice, config_names=config_names),
            point_index=point_index,
            strategy=order_strategy,
        )
        if rebuild:
            selected_projects = {project_key for project_key, _, _ in cases}
            for project_key in selected_projects:
                rebuild_project(PROJECTS[project_key])

        rows = []
        logs_dir = run_dir / "logs"

        if batch_by_project:
            grouped_cases = {}
            for project_key, case_name, config_name in cases:
                grouped_cases.setdefault(project_key, []).append((case_name, config_name))

            for project_key, case_specs in grouped_cases.items():
                project = PROJECTS[project_key]
                try:
                    output, batch_records = run_benchmark_cases_batched(
                        project,
                        case_specs,
                        warmup_repeats=warmup_repeats,
                        repeats=repeats,
                    )
                    log_file = ""
                    if write_logs:
                        log_path = logs_dir / f"batch-{project_key}.log"
                        log_path.parent.mkdir(parents=True, exist_ok=True)
                        log_path.write_text(output, encoding="utf-8")
                        log_file = str(log_path)
                except RuntimeError as exc:
                    batch_records = run_benchmark_cases_serial(
                        project,
                        case_specs,
                        warmup_repeats=warmup_repeats,
                        repeats=repeats,
                        logs_dir=logs_dir,
                        write_logs=write_logs,
                    )
                    log_file = ""

                for record in batch_records:
                    metrics = record["metrics"]
                    record_log_file = record.get("log_file", log_file)
                    rows.append(
                        {
                            "repeat": record["repeat"],
                            "case": record["case"],
                            "project": project_key,
                            "routes": routes,
                            "path_len": path_len,
                            "providers_per_rule": providers_per_rule,
                            "prefix_rules": prefix_rules,
                            "origin_diversity_pct": origin_diversity_pct,
                            "generated_prefix_rules": generation_meta["generated_prefix_rules"],
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
                            "log_file": record_log_file,
                        }
                    )
        else:
            for _warmup in range(warmup_repeats):
                for project_key, _case_name, config_name in cases:
                    project = PROJECTS[project_key]
                    run_benchmark_case(project, config_name)

            for repeat in range(1, repeats + 1):
                for project_key, case_name, config_name in cases:
                    project = PROJECTS[project_key]
                    output, metrics = run_benchmark_case(project, config_name)
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
                            "project": project_output_name(project_key),
                            "routes": routes,
                            "path_len": path_len,
                            "providers_per_rule": providers_per_rule,
                            "prefix_rules": prefix_rules,
                            "origin_diversity_pct": origin_diversity_pct,
                            "generated_prefix_rules": generation_meta["generated_prefix_rules"],
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
        cleanup_temp_confs(temp_confs)


def host_metadata():
    uname = platform.uname()
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "system": uname.system,
        "release": uname.release,
        "version": uname.version,
        "machine": uname.machine,
        "processor": uname.processor,
        "cpu_count": os.cpu_count(),
        "python": sys.version,
        "timestamp": dt.datetime.now().isoformat(),
        "cwd": str(ROOT),
        "docker": docker_environment_metadata(),
        "linux": linux_environment_metadata(),
        "projects": project_revision_metadata(),
    }


def campaign_points(matrix):
    keys = ["routes", "path_len", "providers_per_rule", "prefix_rules", "origin_diversity_pct"]
    values = [matrix[key] for key in keys]
    return [dict(zip(keys, combo)) for combo in product(*values)]


def load_campaign(path_or_name):
    path = Path(path_or_name)
    if path.suffix != ".json":
        path = path.with_suffix(".json")
    if not path.is_absolute():
        direct = (ROOT / path).resolve()
        if direct.exists():
            path = direct
        else:
            resolved = None
            for root in campaign_search_roots():
                candidate = root / path.name
                if candidate.exists():
                    resolved = candidate
                    break
            if resolved is None:
                raise FileNotFoundError(f"campaign not found: {path_or_name}")
            path = resolved
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    data["_path"] = str(path)
    return data


def campaign_output_dir(campaign, suffix=None):
    name = campaign["name"]
    if suffix:
        name = f"{name}-{suffix}"
    return RESULTS_ROOT / name


def write_campaign_outputs(run_dir, all_rows, group_keys, meta):
    results_path = run_dir / "results.csv"
    save_csv(results_path, benchmark_results_fieldnames(), all_rows)

    summary = summarize_rows(all_rows, group_keys=group_keys) if all_rows else []
    summary_path = run_dir / "summary.csv"
    save_csv(summary_path, summary_fieldnames(group_keys), summary)

    write_text_resilient(run_dir / "meta.json", json.dumps(meta, indent=2), encoding="utf-8")
    return results_path, summary_path


def ordered_cases_for_point(cases, point_index, strategy="rotate-projects"):
    if strategy == "fixed":
        return cases

    grouped = {}
    for project_key, case_name, config_name in cases:
        grouped.setdefault(project_key, []).append((project_key, case_name, config_name))

    project_order = available_project_keys()
    active_projects = [key for key in project_order if key in grouped]
    if not active_projects:
        return cases

    offset = point_index % len(active_projects)
    rotated_projects = active_projects[offset:] + active_projects[:offset]

    ordered = []
    for project_key in rotated_projects:
        project_cases = grouped[project_key]
        if len(project_cases) > 1:
            case_offset = point_index % len(project_cases)
            project_cases = project_cases[case_offset:] + project_cases[:case_offset]
        ordered.extend(project_cases)
    return ordered


def run_campaign(campaign, output_suffix=None, force_rebuild=None):
    if is_realistic_campaign(campaign):
        return realistic_module().run_campaign(
            campaign,
            output_suffix,
            force_rebuild=force_rebuild,
        )

    run_dir = campaign_output_dir(campaign, output_suffix)
    shutil.rmtree(run_dir, ignore_errors=True)
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
        "prefix_rules",
        "origin_diversity_pct",
        "generated_prefix_rules",
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
        "host": host_metadata(),
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
                prefix_rules=int(point["prefix_rules"]),
                origin_diversity_pct=int(point["origin_diversity_pct"]),
                repeats=measured_repeats,
                rebuild=rebuild_once and index == 0,
                run_dir=run_dir,
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


def selected_benchmark_cases(target, mode_choice, config_names=None):
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
    config_names = config_names or benchmark_config_names()

    if target == "bird":
        cases = []
        if "plain" in suffixes:
            cases.append(("bird", "original-plain", config_names["bird"]["original-plain"]))
        if "global" in suffixes or not cases:
            cases.append(("bird", "original-global", config_names["bird"]["original-global"]))
        return cases

    if target == "linear":
        if not project_available("linear"):
            raise RuntimeError("projeto indisponivel: linear")
        return [
            ("linear", f"linear-{suffix}", config_names["linear"][f"linear-{suffix}"])
            for suffix in suffixes
            if suffix != "plain"
        ]

    if target == "bird-prefix-trie":
        if not project_available("bird-prefix-trie"):
            raise RuntimeError("projeto indisponivel: bird-prefix-trie")
        return [
            ("bird-prefix-trie", f"trie-{suffix}", config_names["bird-prefix-trie"][f"trie-{suffix}"])
            for suffix in suffixes
            if suffix != "plain"
        ]

    if target == "bird-prefix-hybridv2":
        if not project_available("bird-prefix-hybridv2"):
            raise RuntimeError("projeto indisponivel: bird-prefix-hybridv2")
        return [
            ("bird-prefix-hybridv2", f"hybridv2-{suffix}", config_names["bird-prefix-hybridv2"][f"hybridv2-{suffix}"])
            for suffix in suffixes
            if suffix != "plain"
        ]

    if target == "linear+trie+hybridv2":
        cases = []
        if "plain" in suffixes:
            cases.append(("bird", "original-plain", config_names["bird"]["original-plain"]))
        if "global" in suffixes or not cases:
            cases.append(("bird", "original-global", config_names["bird"]["original-global"]))
        for suffix in suffixes:
            if suffix == "plain":
                continue
            if project_available("linear"):
                cases.append(
                    ("linear", f"linear-{suffix}", config_names["linear"][f"linear-{suffix}"])
                )
            if project_available("bird-prefix-trie"):
                cases.append(
                    ("bird-prefix-trie", f"trie-{suffix}", config_names["bird-prefix-trie"][f"trie-{suffix}"])
                )
            if project_available("bird-prefix-hybridv2"):
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
        if project_available("bird-prefix-trie"):
            cases.append(
                ("bird-prefix-trie", f"trie-{suffix}", config_names["bird-prefix-trie"][f"trie-{suffix}"])
            )
        if project_available("bird-prefix-hybridv2"):
            cases.append(
                ("bird-prefix-hybridv2", f"hybridv2-{suffix}", config_names["bird-prefix-hybridv2"][f"hybridv2-{suffix}"])
            )
    return cases


def run_benchmark_menu():
    target = prompt_choice(
        "Which BIRD tree do you want to run in the benchmark?",
        ["bird", "linear", "trie", "hybridv2", "full benchmark set"],
    )
    target = normalize_project_choice(target)
    mode_choice = "global"
    if target == "full benchmark set":
        mode_choice = prompt_choice(
            "Which scenarios do you want to include for prefixed variants?",
            ["hit+miss", "global+hit+miss", "plain+global+hit+miss", "plain+global", "global", "hit", "miss"],
        )
    elif target != "bird":
        mode_choice = prompt_choice(
            "Which scenario do you want to run?",
            ["global", "hit", "miss", "hit+miss", "global+hit+miss"],
        )
    else:
        mode_choice = prompt_choice(
            "Which scenario do you want to run?",
            ["plain+global", "plain", "global"],
        )

    routes = prompt_int("Number of routes", 10000)
    path_len = prompt_int("AS_PATH length", 5)
    providers_per_rule = prompt_int("Number of providers per rule", 4)
    prefix_rules = prompt_int("Number of prefix rules", 64)
    origin_diversity_pct = prompt_percent("Percentage of distinct origins", 100)
    repeats = prompt_int("Number of repeats", 7)
    rebuild = prompt_yes_no("Rebuild binaries before running?", False)

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RESULTS_ROOT / timestamp
    rows = execute_benchmark_run(
        target=target,
        mode_choice=mode_choice,
        routes=routes,
        path_len=path_len,
        providers_per_rule=providers_per_rule,
        prefix_rules=prefix_rules,
        origin_diversity_pct=origin_diversity_pct,
        repeats=repeats,
        rebuild=rebuild,
        run_dir=run_dir,
        warmup_repeats=0,
        write_logs=True,
    )

    csv_path = run_dir / "results.csv"
    save_csv(csv_path, benchmark_results_fieldnames(), rows)

    summary = summarize_rows(
        rows,
        group_keys=[
            "routes",
            "path_len",
            "providers_per_rule",
            "prefix_rules",
            "origin_diversity_pct",
            "generated_prefix_rules",
            "distinct_origins",
            "case",
        ],
    )
    summary_path = run_dir / "summary.csv"
    save_csv(
        summary_path,
        summary_fieldnames(
            [
                "routes",
                "path_len",
                "providers_per_rule",
                "prefix_rules",
                "origin_diversity_pct",
                "generated_prefix_rules",
                "distinct_origins",
                "case",
            ]
        ),
        summary,
    )

    print(f"\nCSV: {csv_path}")
    print(f"Summary: {summary_path}")
    print(f"Logs: {run_dir / 'logs'}")
    if prompt_yes_no("Interpret this benchmark now?", True):
        run_analysis_menu(summary_path)


def inspect_output_blocks(text, limit):
    blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
    if limit <= 0 or limit >= len(blocks):
      return "\n\n".join(blocks)
    return "\n\n".join(blocks[:limit])


def run_inspection_menu():
    temp_confs = []
    project_key = prompt_choice(
        "Which BIRD tree do you want to inspect?",
        ["bird", "linear", "trie", "hybridv2"],
    )
    project_key = normalize_project_choice(project_key)
    project = PROJECTS[project_key]
    rebuild = prompt_yes_no("Rebuild this project before running?", False)
    route_limit = prompt_int("How many route blocks should be printed?", 5)

    mode_choice = "global"
    if project_key == "bird":
        mode_choice = prompt_choice("Which benchmark scenario do you want to inspect?", ["plain", "global"])
    else:
        mode_choice = prompt_choice("Which benchmark scenario do you want to inspect?", ["global", "hit", "miss"])
    routes = prompt_int("Number of routes", 20)
    path_len = prompt_int("AS_PATH length", 5)
    providers_per_rule = prompt_int("Number of providers per rule", 4)
    prefix_rules = prompt_int("Number of prefix rules", 16)
    origin_diversity_pct = prompt_percent("Percentage of distinct origins", 100)
    created_confs, config_names = write_benchmark_configs(
        routes,
        path_len,
        providers_per_rule,
        prefix_rules,
        origin_diversity_pct,
        tag=benchmark_config_tag("inspect"),
    )
    temp_confs.extend(created_confs)
    if project_key == "bird":
        case_name = "original-plain" if mode_choice == "plain" else "original-global"
        target_name = config_names["bird"][case_name]
    else:
        suffix = {"global": "global", "hit": "hit", "miss": "miss"}[mode_choice]
        case_name = f"{MODIFIED_PROJECT_CASE_PREFIX[project_key]}-{suffix}"
        target_name = config_names[project_key][case_name]
    debug_path = write_inspection_debug_config(
        project_key,
        target_name,
        tag=benchmark_config_tag("inspect-debug"),
    )
    temp_confs.append(debug_path)
    config_path = f"/bird-source/{debug_path.name}"

    try:
        if rebuild:
            rebuild_project(project)

        script = f"""
set -eu
mkdir -p /run
cd /bird-source
./bird -p -c {config_path}
./bird -d -c {config_path} -s /run/verify.ctl &
pid=$!
sleep 1
echo '=== ASPA TABLE ==='
./birdc -s /run/verify.ctl show route all table at
echo
echo '=== MASTER4 ALL ==='
./birdc -s /run/verify.ctl show route all table master4
kill $pid
wait $pid || true
"""
        result = run(
            docker_compose_run(project, script),
            cwd=project["lab"],
            capture_output=True,
            echo_output=False,
        )

        text = result.stdout + result.stderr
        aspa_marker = "=== ASPA TABLE ==="
        master_marker = "=== MASTER4 ALL ==="
        if aspa_marker not in text or master_marker not in text:
            print(text)
            raise RuntimeError("could not split inspection output")

        after_aspa = text.split(aspa_marker, 1)[1]
        aspa_text, master_text = after_aspa.split(master_marker, 1)

        print("\n=== ASPA TABLE ===")
        print(aspa_text.strip())
        print("\n=== MASTER4 SAMPLE ===")
        print(inspect_output_blocks(master_text.strip(), route_limit))
    finally:
        cleanup_temp_confs(temp_confs)


def main():
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    CAMPAIGNS_ROOT.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) >= 2:
        command = sys.argv[1]
        if command == "campaign":
            if len(sys.argv) < 3:
                raise RuntimeError(
                    "usage: orchestrator.py campaign <file-or-name> [output-suffix] "
                    "[--no-rebuild|--rebuild] [--analyze]"
                )
            campaign = load_campaign(sys.argv[2])
            suffix = None
            force_rebuild = None
            analyze_after = False
            for arg in sys.argv[3:]:
                if arg == "--no-rebuild":
                    force_rebuild = False
                elif arg == "--rebuild":
                    force_rebuild = True
                elif arg == "--analyze":
                    analyze_after = True
                elif suffix is None:
                    suffix = arg
                else:
                    raise RuntimeError(
                        "usage: orchestrator.py campaign <file-or-name> [output-suffix] "
                        "[--no-rebuild|--rebuild] [--analyze]"
                    )
            run_dir = run_campaign(campaign, suffix, force_rebuild=force_rebuild)
            if analyze_after:
                run_analysis_menu(str(run_dir))
            return
        if command == "preflight":
            campaign = load_campaign(sys.argv[2]) if len(sys.argv) >= 3 else None
            emit_preflight(campaign)
            return
        if command == "clean":
            mode = sys.argv[2] if len(sys.argv) >= 3 else "workspace"
            include_results = mode == "all"
            include_logs = mode in {"logs", "all"}
            removed = clean_workspace_artifacts(include_results=include_results, include_logs=include_logs)
            print(f"removed={len(removed)}")
            for path in removed:
                print(path)
            return
        if command == "analyze":
            if len(sys.argv) < 3:
                raise RuntimeError("usage: orchestrator.py analyze <summary.csv|results.csv|directory>")
            run_analysis_menu(sys.argv[2])
            return
        if command == "campaigns":
            for path in list_campaign_paths():
                print(path.stem)
            return

    while True:
        action = prompt_choice(
            "What do you want to do?",
            ["benchmark", "inspection", "analysis", "exit"],
        )

        if action == "benchmark":
            run_benchmark_menu()
        elif action == "inspection":
            run_inspection_menu()
        elif action == "analysis":
            run_analysis_menu()
        else:
            print("Exiting.")
            return


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
    main()
