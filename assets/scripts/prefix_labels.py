#!/usr/bin/env python3
"""prefix_labels — slug-prefix every \\label and rewrite the full ref family.

Tier 1 (mechanical, proactive). Owner: pt-dyq (T4).
Shares one TeX-aware safe find-replace core (texrewrite.py) with resolve_macros.py.
Part of the mol-latex-concat formula (latex-utils pack).

Reads  plan.json: sources[].slug, sources[].labels, sources[].macros.
Writes plan.json: renames.labels[]; rewrites each chapter's mirrored files in place.
       Every ``\\label{k}`` => ``\\label{<slug>:k}``, and every reference to a key
       defined in that chapter is rewritten across the whole ref family
       (``\\ref \\eqref \\cref \\Cref \\autoref \\pageref \\nameref \\labelcref \\vref
       \\cpageref \\hyperref[...] …``). Done PROACTIVELY across ALL chapters so a
       cross-paper label clash cannot occur even when two papers reuse ``eq:main``.

It rewrites the files under ``<dest>/contents/<slug>/`` (the mirror the transform
step laid down), using the chapter's *actual* on-disk labels as the authoritative
set. A reference whose key is NOT one of this chapter's labels (a package- or
externally-defined target) is left untouched — only the chapter's own labels are
namespaced, so external references keep working.

Tier-2 flag (never guess): a chapter that defines its own ``\\ref``-like wrapper
macro (e.g. ``\\newcommand\\myref[1]{...\\ref{#1}}``) is flagged — the core cannot
know which argument of ``\\myref`` is the label key, so the agent extends the family
or rewrites those uses by hand. The compile gate (undefined reference) is the
backstop for anything missed.

CLI:
  prefix_labels.py --plan PLAN [--dest DIR]
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planlib  # noqa: E402
import texrewrite  # noqa: E402

# Flag kinds this helper owns (cleared and re-derived on every idempotent re-run).
MY_FLAG_KINDS = {"custom-ref-macro", "chapter-dir-missing", "label-set-mismatch"}


def _save_my_flags(plan, kinds):
    """Snapshot this helper's existing flags so an idempotent re-run can reuse their
    stable ids and preserve any agent-written resolution."""
    return {(f.get("kind"), f.get("slug"), f.get("message")): f
            for f in plan.get("flags", []) if f.get("kind") in kinds}


def _stabilize_my_flag_ids(plan, kinds, saved):
    """Give this helper's freshly-derived flags collision-free, stable ids: reuse the
    prior id (and any agent resolution) for a flag that recurs, else mint the next
    free ``Fn``. Other helpers' flag ids are left untouched."""
    flags = plan.get("flags", [])
    used = {f["id"] for f in flags if f.get("kind") not in kinds and f.get("id")}

    def fresh():
        n = 1
        while ("F%d" % n) in used:
            n += 1
        used.add("F%d" % n)
        return "F%d" % n

    for f in flags:
        if f.get("kind") not in kinds:
            continue
        prev = saved.get((f.get("kind"), f.get("slug"), f.get("message")))
        if prev and prev.get("id") and prev["id"] not in used:
            f["id"] = prev["id"]
            used.add(prev["id"])
        else:
            f["id"] = fresh()
        if prev and prev.get("resolution") is not None and not f.get("resolution"):
            f["resolution"] = prev["resolution"]


def _dest_dir(plan, dest_arg):
    d = dest_arg or plan.get("dest", {}).get("dir") or ""
    return os.path.abspath(d) if d else ""


def _chapter_tex_files(chapter_dir):
    out = []
    for root, dirs, files in os.walk(chapter_dir):
        dirs[:] = [d for d in dirs if d not in (".git", ".svn", "node_modules")]
        for f in files:
            if f.endswith(".tex"):
                out.append(os.path.join(root, f))
    return sorted(out)


def _custom_ref_macros(source):
    """Names of macros this chapter defines whose body itself calls a reference
    command — i.e. custom \\ref-like wrappers whose uses the core cannot rewrite."""
    names = []
    for mac in source.get("macros", []):
        if texrewrite.body_uses_ref(mac.get("body") or ""):
            nm = mac.get("name")
            if nm and nm not in names:
                names.append(nm)
    return names


def prefix_one(plan, source, dest_dir):
    """Slug-prefix one chapter's labels (mutating its files in place) and return the
    list of ``renames.labels`` records produced for it."""
    slug = source.get("slug") or ""
    if not slug:
        planlib.add_flag(plan, tier=2, stage="resolve", kind="label-set-mismatch",
                         severity="warn", slug=None,
                         message="A source has no slug; cannot namespace its labels.",
                         location=source.get("dir"))
        return []

    chapter_dir = os.path.join(dest_dir, "contents", slug)
    if not os.path.isdir(chapter_dir):
        planlib.add_flag(plan, tier=2, stage="resolve", kind="chapter-dir-missing",
                         severity="warn", slug=slug,
                         message=("Mirrored chapter dir %s not found — run the transform "
                                  "step first; labels for this chapter were not prefixed."
                                  % os.path.relpath(chapter_dir, dest_dir or ".")),
                         location=chapter_dir)
        return []

    file_text = {}
    on_disk_labels = []
    for p in _chapter_tex_files(chapter_dir):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                t = fh.read()
        except OSError:
            continue
        file_text[p] = t
        for k in texrewrite.find_labels(t):
            if k not in on_disk_labels:
                on_disk_labels.append(k)

    prefix = slug + ":"
    # Build the rename records and the (unprefixed -> prefixed) rewrite mapping. An
    # already-prefixed label is still recorded (so the report and an idempotent
    # re-run stay complete) but is not rewritten a second time.
    records, mapping = [], {}
    for k in on_disk_labels:
        if k.startswith(prefix):
            records.append({"slug": slug, "from": k[len(prefix):], "to": k})
        else:
            to = prefix + k
            records.append({"slug": slug, "from": k, "to": to})
            mapping[k] = to

    # Advisory cross-check: catalogued labels that never showed up on disk (e.g. a
    # label living inside an include inspect could not resolve).
    catalogued = set(source.get("labels") or [])
    seen_bare = {k for k in on_disk_labels if not k.startswith(prefix)}
    seen_bare |= {k[len(prefix):] for k in on_disk_labels if k.startswith(prefix)}
    missing = sorted(catalogued - seen_bare)
    if missing:
        planlib.add_flag(plan, tier=2, stage="resolve", kind="label-set-mismatch",
                         severity="info", slug=slug,
                         message=("inspect catalogued label(s) %s not found in the mirrored "
                                  "chapter (possibly inside an unresolved include) — confirm "
                                  "their references are namespaced." % ", ".join(missing[:8])),
                         location=chapter_dir)

    rewritten = 0
    if mapping:
        def map_key(k):
            return mapping.get(k, k)

        for p, t in file_text.items():
            new_t, n = texrewrite.rewrite_refs(t, map_key)
            if n and new_t != t:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(new_t)
                rewritten += n

    # Tier-2: custom \ref-like wrapper macros this chapter defines.
    custom = _custom_ref_macros(source)
    if custom:
        planlib.add_flag(plan, tier=2, stage="resolve", kind="custom-ref-macro",
                         severity="warn", slug=slug,
                         message=("Chapter defines custom reference-like macro(s) %s whose "
                                  "uses were NOT auto-prefixed (the core cannot tell which "
                                  "argument is the label key). Extend the ref family or "
                                  "rewrite those uses by hand." % ", ".join(custom)),
                         location=chapter_dir)

    sys.stderr.write(
        "[prefix-labels] %-8s labels=%-4d prefixed=%-4d ref-rewrites=%-4d%s\n"
        % (slug, len(on_disk_labels), len(mapping), rewritten,
           " custom-ref:" + ",".join(custom) if custom else ""))
    return records


def main(argv=None):
    ap = argparse.ArgumentParser(description="slug-prefix labels and rewrite the ref family")
    ap.add_argument("--plan", required=True, help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", default="", help="destination project dir (default: plan.dest.dir)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.plan):
        sys.stderr.write("prefix_labels: plan.json not found: %s\n" % args.plan)
        return 2
    plan = planlib.load(args.plan)

    dest_dir = _dest_dir(plan, args.dest)
    if not dest_dir or not os.path.isdir(dest_dir):
        sys.stderr.write("prefix_labels: destination dir not found: %r\n" % dest_dir)
        return 2

    # Idempotent re-run: drop this helper's prior flags and rebuild renames.labels,
    # but remember their stable ids + resolutions to re-apply after re-derivation.
    saved_flags = _save_my_flags(plan, MY_FLAG_KINDS)
    plan["flags"] = [f for f in plan.get("flags", []) if f.get("kind") not in MY_FLAG_KINDS]
    plan.setdefault("renames", {})["labels"] = []

    all_records = []
    for s in sorted(plan.get("sources", []), key=lambda e: e.get("index", 0)):
        all_records.extend(prefix_one(plan, s, dest_dir))
    plan["renames"]["labels"] = all_records

    _stabilize_my_flag_ids(plan, MY_FLAG_KINDS, saved_flags)

    n_chapters = len(plan.get("sources", []))
    planlib.add_log(plan, "resolve",
                    "Slug-prefixed labels across %d chapter(s): %d label namespace(s) recorded."
                    % (n_chapters, len(all_records)))

    try:
        planlib.validate(plan)
    except Exception as e:
        sys.stderr.write("prefix_labels: plan.json failed schema validation: %s\n" % e)
        planlib.save(args.plan, plan)
        return 1

    planlib.save(args.plan, plan)
    sys.stderr.write("[prefix-labels] wrote %s — %d label rename record(s).\n"
                     % (args.plan, len(all_records)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
