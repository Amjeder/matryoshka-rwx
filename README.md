# Blast Radius selector

The selector reads the official test inventory and independent patches under `/data`. It selects tests from a map of the pinned SQLGlot source, then partitions the selection by estimated test time and the official two-second worker cost. Missing or invalid source evidence triggers the all-tests schedule.

## Scoring a local run

Unpack the [official starter](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz) outside this directory and make its sample data readable by the Docker runtime user. This command scores the simulation using the organizer's scorer.

```sh
python selector.py --data-dir /path/to/starter/sample > /tmp/blast-radius.jsonl
python /path/to/starter/score.py /path/to/starter/sample \
  /path/to/starter/sample/answers.json /tmp/blast-radius.jsonl
```

Use `--strategy all` for an optimized all-tests baseline or `--strategy all --workers 3` for the original worker count. Use `python -m unittest discover -v` to exercise parsing, patch compilation, and scheduling.

## Evidence provenance

The offline map records pytest line coverage, file reads, and static source imports at SQLGlot revision `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. It contains test node IDs and source relationships. The scoring inputs and answer labels are never read during map construction. `sources.zip` contains the Python files at that same revision and their MIT license. The selector applies each candidate's textual edit in memory and compiles the resulting source before narrowing. It does not import or execute changed code.

To regenerate, use the starter's pinned Linux/amd64 Python image and a checkout of the pinned SQLGlot revision. Install the starter's `requirements.lock` plus `coverage==7.10.7`.

```sh
SKIP_INTEGRATION=0 python build_coverage.py /path/to/pinned/sqlglot \
  "$PWD/coverage.json.gz" --sources "$PWD/sources.zip"
```

The runtime image contains only the selector and source artifacts. Its root Dockerfile uses the organizer's pinned Python image and requires no network during execution.
