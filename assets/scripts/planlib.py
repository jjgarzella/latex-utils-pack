#!/usr/bin/env python3
"""planlib — shared read/extend/write plumbing for the mol-latex-concat plan.json.

plan.json is the agent<->script contract (schema: ../plan.schema.json). The
inspect step seeds it; every later helper in this pack READS the plan, EXTENDS
it, and writes it back atomically; write_report.py renders REPORT.md from it.

This module is intentionally tiny and dependency-free at import time. It is the
contract's reference implementation — helpers SHOULD use it so the on-disk shape
stays consistent and every tool reads/writes the same plan.json the same way.

Typical helper usage:

    import planlib
    plan = planlib.load(args.plan)
    ... mutate plan ...
    planlib.add_flag(plan, tier=2, stage="hoist", kind="option-conflict",
                     message="geometry requested with conflicting options",
                     slug=None, severity="warn")
    planlib.add_log(plan, "hoist", "union of 27 packages; dropped 3 class-owned")
    planlib.save(args.plan, plan)

Validation against plan.schema.json is best-effort: if the `jsonschema` package
is installed, `validate()` enforces the schema; otherwise it is a no-op so the
helpers never hard-depend on a third-party library.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

SCHEMA_VERSION = "1"
FORMULA = "mol-latex-concat"

# plan.schema.json sits one directory up from assets/scripts/.
SCHEMA_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "plan.schema.json")
)


def utcnow() -> str:
    """UTC ISO-8601 timestamp, e.g. 2026-06-06T03:00:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new(sources, dest="", tex_engine="pdflatex") -> dict:
    """Build a fresh, schema-valid skeleton plan for a run.

    `sources` is the ordered list of source dirs (chapter order). The per-source
    inventory is pre-seeded with index/dir so inspect_sources.py only fills it in.
    """
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    return {
        "schema_version": SCHEMA_VERSION,
        "formula": FORMULA,
        "vars": {"sources": list(sources), "dest": dest, "tex_engine": tex_engine},
        "dest": {},
        "sources": [{"index": i, "dir": d, "slug": ""} for i, d in enumerate(sources)],
        "preamble": {"packages_hoisted": [], "packages_dropped": []},
        "renames": {"macros": [], "labels": [], "bib_keys": []},
        "bib": {},
        "flags": [],
        "log": [],
    }


def load(path: str) -> dict:
    """Read plan.json from `path`."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save(path: str, plan: dict) -> None:
    """Atomically write plan.json (temp file + rename) so a crashed write never
    leaves a half-truncated contract on disk."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(os.path.abspath(path)) or ".", prefix=".plan.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def source_by_slug(plan: dict, slug: str):
    """Return the source-inventory entry for a slug (or None)."""
    for s in plan.get("sources", []):
        if s.get("slug") == slug:
            return s
    return None


def source_by_dir(plan: dict, dir_: str):
    """Return the source-inventory entry for a source dir (or None)."""
    for s in plan.get("sources", []):
        if s.get("dir") == dir_:
            return s
    return None


def next_flag_id(plan: dict) -> str:
    """Mint the next stable flag id (F1, F2, ...)."""
    return "F%d" % (len(plan.get("flags", [])) + 1)


def add_flag(plan, *, tier, stage, kind, message, slug=None, severity="warn",
             location=None, resolution=None) -> dict:
    """Append a Tier-2 flag-and-defer record and return it. NEVER guess in a
    helper — append a flag here and let the agent (Tier 3) resolve it."""
    flag = {
        "id": next_flag_id(plan),
        "tier": int(tier),
        "severity": severity,
        "stage": stage,
        "slug": slug,
        "kind": kind,
        "message": message,
        "location": location,
        "resolution": resolution,
    }
    plan.setdefault("flags", []).append(flag)
    return flag


def add_log(plan, step: str, summary: str) -> None:
    """Append a step entry to the report spine."""
    plan.setdefault("log", []).append({"step": step, "ts": utcnow(), "summary": summary})


def validate(plan: dict) -> None:
    """Best-effort schema validation. No-op (with a stderr note) when the
    optional `jsonschema` package is absent, so helpers never hard-depend on it.
    Raises jsonschema.ValidationError on a real schema violation."""
    try:
        import jsonschema  # type: ignore
    except Exception:
        import sys
        sys.stderr.write("planlib: jsonschema not installed; skipping plan.json validation.\n")
        return
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    jsonschema.validate(instance=plan, schema=schema)


if __name__ == "__main__":
    # `planlib.py <plan.json>` validates an existing plan against the schema.
    import sys

    if len(sys.argv) != 2:
        sys.stderr.write("usage: planlib.py <plan.json>   # validate a plan against plan.schema.json\n")
        raise SystemExit(2)
    p = load(sys.argv[1])
    validate(p)
    print("planlib: %s is a valid plan.json (schema %s)." % (sys.argv[1], p.get("schema_version")))
