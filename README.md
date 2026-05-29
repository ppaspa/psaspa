# ppaspa

BIRD-based ASPA experiments with prefix-granular authorization and benchmark automation.

License: GPL-2.0-or-later.

## Trees

- `bird/`: baseline
- `linear`: linear prefix-rule lookup, implemented in `bird-prefix-scan/`
- `bird-prefix-trie/`: trie lookup
- `bird-prefix-hybridv2/`: hybrid lookup

Physical directories:

- `bird/`
- `bird-prefix-scan/`
- `bird-prefix-trie/`
- `bird-prefix-hybridv2/`

## Layout

- `orchestrator.py`: main benchmark runner
- `realistic_campaigns.py`: realistic campaign generator
- `campaigns/`: retained campaign definitions
- `results/`: retained result directories
- `tools/`: Route Views analysis scripts

## Implementation

Main code paths:

- `proto/static/`: parser and static injection
- `nest/route.h`: ASPA payload fields
- `nest/rt-table.c`: ASPA lookup and rule selection

Variants:

- baseline: one global ASPA rule per ASN
- linear: linear scan over prefix rules
- trie: longest-prefix match over prefix rules
- hybridv2: starts with linear scan and promotes active origins to trie

Prefix-aware selection is applied only at the origin side of ASPA validation.

## Requirements

- Python 3
- Docker
- Docker Compose
- `curl`
- `bgpdump` for the Route Views scripts

Run commands from the repository root.

## Build

The benchmark runner rebuilds the BIRD trees inside Docker when needed.
You dnot need to run the build commands manually on the host.

Inside the containers, the rebuild uses:

```bash
autoreconf -fi
./configure --with-sysconfig=linux --with-protocols=bgp,static,perf --enable-libssh=no
make bird birdc
```

## Campaigns

List campaigns:

```bash
python3 orchestrator.py campaigns
```


Run:

```bash
python3 -u orchestrator.py campaign synthetic-diversity-10k-main linux-diversity-main
python3 -u orchestrator.py campaign synthetic-rules-10k-main linux-rules-main
python3 -u orchestrator.py campaign realistic-rules-1m-main linux-realistic-main
```

Optional flags:

- `--rebuild`
- `--no-rebuild`
- `--analyze`

## Retained campaigns

- `synthetic-diversity-10k-main.json`
- `synthetic-rules-10k-main.json`
- `realistic-rules-1m-main.json`

## Retained results

- `results/synthetic-diversity-10k-main-linux-diversity-main`
- `results/synthetic-rules-10k-main-linux-rules-main`
- `results/realistic-rules-1m-main-linux-realistic-main`

Each run writes:

- `results.csv`
- `summary.csv`
- `meta.json`


## Clean local artifacts

```bash
python3 orchestrator.py clean
python3 orchestrator.py clean logs
python3 orchestrator.py clean all
```

## Route Views scripts

Two scripts are kept:

- `tools/analyze_routeviews_by_hour.py`: downloads Route Views update files and writes per-window reports
- `tools/bgp_realism_metrics.py`: computes metrics from local MRT files with `bgpdump -m`

Dates used in the paper:

- `2026-05-04`
- `2026-05-05`

Example: download and analyze the paper dates for `route-views2`, at `00:00`, `06:00`, `12:00`, and `18:00`:

```bash
python3 tools/analyze_routeviews_by_hour.py \
  --collector route-views2 \
  --dates 2026-05-04,2026-05-05 \
  --hours 0,6,12,18
```

Defaults:

- cache: `/tmp/routeviews-cache`
- output: `outputs/routeviews-hourly/`

Example with explicit paths:

```bash
python3 tools/analyze_routeviews_by_hour.py \
  --collector route-views2 \
  --dates 2026-05-04,2026-05-05 \
  --hours 0,6,12,18 \
  --cache-dir /tmp/routeviews-cache \
  --output-dir outputs/routeviews-hourly
```

To reuse only files already present in cache:

```bash
python3 tools/analyze_routeviews_by_hour.py \
  --collector route-views2 \
  --dates 2026-05-04,2026-05-05 \
  --hours 0,6,12,18 \
  --reuse-only
```

To compute metrics from local MRT files directly:

```bash
python3 tools/bgp_realism_metrics.py /path/to/file1.bz2 /path/to/file2.bz2
```

Optional JSON output:

```bash
python3 tools/bgp_realism_metrics.py /path/to/file1.bz2 /path/to/file2.bz2 --output metrics.json
```

## ASPA config format

Baseline:

```bird
route aspa 65001 providers 65010, 65020;
```

Modified variants:

```bird
route aspa 65001 {
  providers 65010, 65020;
  prefix 203.0.113.0/24 providers 65010;
  prefix 198.51.100.0/24 providers 65020;
};
```
