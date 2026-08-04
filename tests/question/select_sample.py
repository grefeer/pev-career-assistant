"""Deterministically sample ~20 evaluation questions (balanced across all axes).

Selection invariants (no randomness, reproducible):

  - 20 files: 10 simple + 10 complex.
  - site types: 5 categories x 4 questions each (2 simple + 2 complex).
  - simple 10: job-discovery 3, job-matching 2, resume-tailoring 2, career-planning 3.
  - complex 10: full-chain 2 + one each of JD+JM+CP, JD+JM+RT, JD+RT+CP,
    JM+RT+CP, JD+CP, JM+CP, JD+JM, JD+RT (9 distinct combinations).
  - profiles P1..P4 rotate across the sampled list.

Writes ``sample_20.json`` containing the selected file paths (repo-relative).
"""

from __future__ import annotations

import json
import pathlib

OUT_DIR = pathlib.Path(__file__).resolve().parent
SAMPLE_FILE = OUT_DIR / "sample_20.json"

FULL_CHAIN = ("job-discovery", "job-matching", "resume-tailoring", "career-planning")

# Per site bucket (in bucket order 0..4): which combination to take first.
# Bucket composition (from the actual Q files):
#   company-official: simple {JD, RT}; complex {full-chain, JM+RT+CP, JD+RT+CP}
#   state-owned:      simple {JD, RT}; complex {full-chain, JD+JM}
#   aggregator:       simple {JD, RT, CP, JM}; complex {JD+JM+CP, JD+CP}
#   campus:           simple {JM, CP}; complex {JD+JM+CP, JM+CP, JD+JM+RT, JD+RT}
#   tech-vertical:    simple {JM, CP}; complex {JD+JM+RT, JD+RT, JM+RT+CP, JM+RT}
COMPLEX_PICKS = [
    [FULL_CHAIN, ("job-discovery", "resume-tailoring", "career-planning")],
    [FULL_CHAIN, ("job-discovery", "job-matching")],
    [("job-discovery", "job-matching", "career-planning"), ("job-discovery", "career-planning")],
    [("job-discovery", "job-matching", "resume-tailoring"), ("job-matching", "career-planning")],
    [("job-matching", "resume-tailoring", "career-planning"), ("job-matching", "resume-tailoring")],
]

# Per site bucket: which simple skill to take (2 picks each).
SIMPLE_PICKS = [
    ["job-discovery", "resume-tailoring"],
    ["job-discovery", "resume-tailoring"],
    ["job-discovery", "career-planning"],
    ["job-matching", "career-planning"],
    ["job-matching", "career-planning"],
]


def _bucket_key(doc: dict) -> tuple[str, str]:
    return (doc["meta"]["site_types"][0], doc["meta"]["complexity"])


def main() -> None:
    docs = []
    for path in sorted(OUT_DIR.glob("Q*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["_path"] = str(path.relative_to(OUT_DIR.parent.parent))
        docs.append(doc)

    buckets: dict[tuple[str, str], list[dict]] = {}
    for doc in docs:
        buckets.setdefault(_bucket_key(doc), []).append(doc)

    selected: list[dict] = []
    site_order = [
        "company-official",
        "state-owned",
        "aggregator",
        "campus",
        "tech-vertical",
    ]
    for bucket_index, site_type in enumerate(site_order):
        simple_bucket = sorted(
            buckets[(site_type, "simple")], key=lambda d: d["id"]
        )
        complex_bucket = sorted(
            buckets[(site_type, "complex")], key=lambda d: d["id"]
        )

        for skill in SIMPLE_PICKS[bucket_index]:
            match = next(
                d for d in simple_bucket if d["meta"]["skills"] == [skill]
            )
            selected.append(match)

        for combination in COMPLEX_PICKS[bucket_index]:
            match = next(
                d for d in complex_bucket if tuple(d["meta"]["skills"]) == combination
            )
            selected.append(match)

    selected.sort(key=lambda d: d["id"])

    import collections

    distribution = {
        "complexity": dict(
            sorted(collections.Counter(d["meta"]["complexity"] for d in selected).items())
        ),
        "site_types": dict(
            sorted(
                collections.Counter(d["meta"]["site_types"][0] for d in selected).items()
            )
        ),
        "skills": dict(
            sorted(
                collections.Counter(
                    s for d in selected for s in d["meta"]["skills"]
                ).items()
            )
        ),
        "profiles": dict(
            sorted(collections.Counter(d["profile"]["id"] for d in selected).items())
        ),
    }

    document = {
        "sample_size": len(selected),
        "sampled": [d["_path"] for d in selected],
        "distribution": distribution,
    }
    SAMPLE_FILE.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"sampled {len(selected)} questions -> {SAMPLE_FILE.name}")
    for key, counts in distribution.items():
        print(f"{key}: {counts}")


if __name__ == "__main__":
    main()
