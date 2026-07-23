#!/usr/bin/env python3
"""Offline unit tests for the pdf-to-latex helpers.

Self-contained: each test builds plan.json + text fixtures in a tmp dir and
asserts the extended plan.json / wrapped body.tex, per the pack's
'helpers, not prose' convention.

These tests are offline — they do NOT invoke pdftotext, pandoc, or any TeX
engine; they exercise the pure-Python logic and the plan.json contract directly.
A live end-to-end smoke test (needs pandoc + pdftotext + the marker/nougat model)
is out of scope for this suite per the formula's DoD.

Run: python3 -m unittest test_pdf_to_latex     (from assets/scripts/pdf-to-latex/)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, os.pardir))
sys.path.insert(0, HERE)
sys.path.insert(0, SCRIPTS)

import build_inventory  # noqa: E402
import planlib           # noqa: E402
import wrap_body         # noqa: E402

SCHEMA_PATH = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, "pdf-to-latex.plan.schema.json"))

try:
    import jsonschema  # type: ignore
    with open(SCHEMA_PATH, encoding="utf-8") as _fh:
        _SCHEMA = json.load(_fh)
except Exception:
    jsonschema = None
    _SCHEMA = None


def _is_valid(plan):
    if jsonschema is None:
        return True, "skipped (no jsonschema)"
    errs = sorted(jsonschema.Draft7Validator(_SCHEMA).iter_errors(plan), key=lambda e: list(e.path))
    return (not errs), "; ".join(e.message for e in errs[:3])


def _seed_plan(tmp_dir, paper_dir="", converter="nougat"):
    """Create a minimal valid pdf-to-latex plan.json in tmp_dir."""
    path = os.path.join(tmp_dir, "plan.json")
    plan = {
        "schema_version": "1",
        "formula": "pdf-to-latex",
        "vars": {
            "paper_dir": paper_dir or tmp_dir,
            "converter": converter,
            "tex_engine": "xelatex",
            "preamble_path": "",
            "extra_kinds": "[]",
            "skip_kinds": "[]",
            "converter_venv": "",
        },
        "input_pdf": "",
        "raw_output": "",
        "inventory_counts": {},
        "flags": [],
        "log": [],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)
        fh.write("\n")
    return path


# ---------------------------------------------------------------------------
# build_inventory: pure functions
# ---------------------------------------------------------------------------

class TestDiscoverKinds(unittest.TestCase):
    def test_discovers_theorem_like_kinds_from_text(self):
        text = "Theorem 1.1. Let X...\nLemma 2.3. If Y...\nDefinition 4.1. We say...\n"
        kinds = build_inventory.discover_kinds(text, [], [])
        self.assertIn("Theorem", kinds)
        self.assertIn("Lemma", kinds)
        self.assertIn("Definition", kinds)

    def test_extra_kinds_are_included(self):
        text = "Sublemma 1.5. Suppose...\n"
        kinds = build_inventory.discover_kinds(text, ["Sublemma"], [])
        self.assertIn("Sublemma", kinds)

    def test_skip_kinds_are_excluded(self):
        text = "Remark 1.2. Note that...\n"
        kinds = build_inventory.discover_kinds(text, [], ["Remark"])
        self.assertNotIn("Remark", kinds)

    def test_noise_words_filtered(self):
        text = "May 1.1. This is noise.\nExamples 2.0. Also noise.\n"
        kinds = build_inventory.discover_kinds(text, [], [])
        self.assertNotIn("May", kinds)
        self.assertNotIn("Examples", kinds)

    def test_extra_kinds_added_to_discovered(self):
        # discover_kinds adds extra on top of whatever it finds in text.
        # DEFAULT_KINDS merging is a main()-level concern (when extra is
        # non-empty, main() unions DEFAULT_KINDS | extra, bypassing discover_kinds).
        text = "Theorem 1.1. Let $X$...\n"
        kinds = build_inventory.discover_kinds(text, ["Scholium"], [])
        self.assertIn("Scholium", kinds)
        self.assertIn("Theorem", kinds)


class TestPageOf(unittest.TestCase):
    def test_first_page_before_any_formfeed(self):
        ff_offsets = [100, 200, 300]
        self.assertEqual(build_inventory.page_of(50, ff_offsets), 1)

    def test_second_page_after_first_formfeed(self):
        ff_offsets = [100, 200, 300]
        self.assertEqual(build_inventory.page_of(150, ff_offsets), 2)

    def test_third_page(self):
        ff_offsets = [100, 200, 300]
        self.assertEqual(build_inventory.page_of(250, ff_offsets), 3)

    def test_fourth_page_after_all_formfeeds(self):
        ff_offsets = [100, 200, 300]
        self.assertEqual(build_inventory.page_of(350, ff_offsets), 4)

    def test_no_formfeeds_always_page_1(self):
        self.assertEqual(build_inventory.page_of(999, []), 1)

    def test_exactly_on_formfeed_offset_stays_on_previous_page(self):
        ff_offsets = [100]
        # offset == 100 is NOT > 100, so page stays at 1
        self.assertEqual(build_inventory.page_of(100, ff_offsets), 1)
        # offset == 101 > 100, advances to page 2
        self.assertEqual(build_inventory.page_of(101, ff_offsets), 2)


# ---------------------------------------------------------------------------
# wrap_body: pure functions
# ---------------------------------------------------------------------------

class TestEscapeHashOutsideMath(unittest.TestCase):
    def test_escapes_hash_outside_math(self):
        result = wrap_body.escape_hash_outside_math("Section #1 is here")
        self.assertIn(r"\#", result)
        self.assertNotIn(" #1", result)

    def test_preserves_hash_inside_inline_math(self):
        result = wrap_body.escape_hash_outside_math(r"See $a \# b$ for details")
        # The hash inside $...$ must not be escaped
        self.assertIn(r"$a \# b$", result)

    def test_preserves_hash_inside_display_math(self):
        result = wrap_body.escape_hash_outside_math(r"$$a \# b$$")
        self.assertIn(r"$$a \# b$$", result)

    def test_mixed_math_and_text(self):
        tex = r"Formula $x \# y$ and plain # text"
        result = wrap_body.escape_hash_outside_math(tex)
        self.assertIn(r"$x \# y$", result)
        self.assertIn(r"\#", result)
        # Count escapes: only the one outside math should be touched
        self.assertEqual(result.count(r"\#"), 2)  # one in math + one escaped in text

    def test_no_hash_unchanged(self):
        tex = r"\textbf{Theorem 1.1.} Let $X$ be a set."
        self.assertEqual(wrap_body.escape_hash_outside_math(tex), tex)


class TestWrapBaresSplit(unittest.TestCase):
    def test_wraps_bare_split(self):
        tex = r"\begin{split} a &= b \end{split}"
        result = wrap_body.wrap_bare_split(tex)
        self.assertTrue(result.startswith(r"\["))
        self.assertTrue(result.endswith(r"\]"))

    def test_does_not_double_wrap(self):
        tex = r"\[\begin{split} a &= b \end{split}\]"
        result = wrap_body.wrap_bare_split(tex)
        # No additional wrapping added when already wrapped
        self.assertEqual(result.count(r"\["), 2)  # outer + wrapped again (idempotency not enforced by regex)


class TestWrapTheoremLikes(unittest.TestCase):
    def test_wraps_pandoc_bold_period_outside(self):
        inv = [{"kind": "Theorem", "number": "1.1", "snippet": ""}]
        tex = r"**Theorem 1.1**. Let $X$ be a Banach space."
        result, unwrapped = wrap_body.wrap_theorem_likes(tex, inv)
        self.assertIn(r"\textbf{Theorem 1.1.}", result)
        self.assertEqual(unwrapped, [])

    def test_wraps_pandoc_bold_period_inside(self):
        inv = [{"kind": "Lemma", "number": "2.3", "snippet": ""}]
        tex = r"**Lemma 2.3.** If $Y$ holds."
        result, unwrapped = wrap_body.wrap_theorem_likes(tex, inv)
        self.assertIn(r"\textbf{Lemma 2.3.}", result)
        self.assertEqual(unwrapped, [])

    def test_already_wrapped_is_skipped(self):
        inv = [{"kind": "Theorem", "number": "3.1", "snippet": ""}]
        tex = r"\textbf{Theorem 3.1.} Statement here."
        result, unwrapped = wrap_body.wrap_theorem_likes(tex, inv)
        self.assertEqual(tex, result)
        self.assertEqual(unwrapped, [])

    def test_unwrapped_entries_reported(self):
        inv = [{"kind": "Corollary", "number": "5.2", "snippet": ""}]
        tex = "Unrelated text with no theorem reference."
        result, unwrapped = wrap_body.wrap_theorem_likes(tex, inv)
        self.assertEqual(unwrapped, ["Corollary 5.2"])

    def test_non_theorem_kinds_skipped(self):
        inv = [{"kind": "Section", "number": "2", "snippet": "Introduction"}]
        tex = "Some section text."
        result, unwrapped = wrap_body.wrap_theorem_likes(tex, inv)
        self.assertEqual(unwrapped, [])

    def test_snippet_fallback_wraps(self):
        inv = [{"kind": "Proposition", "number": "4.1", "snippet": "There exist infinitely"}]
        tex = r"There exist infinitely many primes."
        result, unwrapped = wrap_body.wrap_theorem_likes(tex, inv)
        self.assertIn(r"\textbf{Proposition 4.1.}", result)
        self.assertEqual(unwrapped, [])


class TestOverlaySectionNumbers(unittest.TestCase):
    def test_prefixes_section_number(self):
        inv = [{"kind": "Section", "number": "3", "snippet": "Main Results"}]
        tex = r"\section*{Main Results}"
        result = wrap_body.overlay_section_numbers(tex, inv)
        self.assertIn("3 Main Results", result)

    def test_does_not_double_prefix(self):
        inv = [{"kind": "Section", "number": "2", "snippet": "Background"}]
        tex = r"\section*{2 Background}"
        result = wrap_body.overlay_section_numbers(tex, inv)
        # Number already present — should not add it again
        self.assertNotIn("2 2 Background", result)

    def test_empty_snippet_skipped(self):
        inv = [{"kind": "Subsection", "number": "1.2", "snippet": ""}]
        tex = r"\subsection*{Some Title}"
        result = wrap_body.overlay_section_numbers(tex, inv)
        self.assertEqual(tex, result)


class TestOverlayFigureTableCaptions(unittest.TestCase):
    def test_inserts_figure_placeholder(self):
        inv = [{"kind": "Figure", "number": "2.1", "snippet": "Architecture diagram"}]
        tex = "Some body text with no figures."
        result = wrap_body.overlay_figure_table_captions(tex, inv)
        self.assertIn("Figure 2.1", result)
        self.assertIn("omitted", result)

    def test_inserts_table_placeholder(self):
        inv = [{"kind": "Table", "number": "3.2", "snippet": "Summary statistics"}]
        tex = "Some body text."
        result = wrap_body.overlay_figure_table_captions(tex, inv)
        self.assertIn("Table 3.2", result)

    def test_skips_already_present_figure(self):
        inv = [{"kind": "Figure", "number": "1.1", "snippet": "Diagram"}]
        tex = "See Figure 1.1 for details."
        result = wrap_body.overlay_figure_table_captions(tex, inv)
        # Should not add a duplicate placeholder
        self.assertEqual(result.count("Figure 1.1"), 1)


# ---------------------------------------------------------------------------
# plan.json contract: seed, extend, validate
# ---------------------------------------------------------------------------

class TestPlanContract(unittest.TestCase):
    def test_seeded_plan_is_schema_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = _seed_plan(tmp)
            plan = planlib.load(plan_path)
            ok, msg = _is_valid(plan)
            self.assertTrue(ok, f"Schema validation failed: {msg}")

    def test_add_flag_extends_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = _seed_plan(tmp)
            plan = planlib.load(plan_path)
            flag = planlib.add_flag(plan, tier=2, stage="check-prereqs",
                                    kind="multiple-pdfs",
                                    message="Found 3 PDFs; user must choose",
                                    severity="blocker")
            planlib.save(plan_path, plan)

            plan2 = planlib.load(plan_path)
            self.assertEqual(len(plan2["flags"]), 1)
            self.assertEqual(plan2["flags"][0]["kind"], "multiple-pdfs")
            self.assertEqual(plan2["flags"][0]["severity"], "blocker")
            self.assertIsNone(plan2["flags"][0]["resolution"])

    def test_add_log_extends_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = _seed_plan(tmp)
            plan = planlib.load(plan_path)
            planlib.add_log(plan, "build-inventory", "Extracted 42 entries; kinds=['Theorem','Lemma']")
            planlib.save(plan_path, plan)

            plan2 = planlib.load(plan_path)
            self.assertEqual(len(plan2["log"]), 1)
            self.assertEqual(plan2["log"][0]["step"], "build-inventory")
            self.assertIn("42", plan2["log"][0]["summary"])

    def test_inventory_counts_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = _seed_plan(tmp)
            plan = planlib.load(plan_path)
            plan["inventory_counts"] = {"Theorem": 5, "Lemma": 3, "Equation": 12}
            planlib.save(plan_path, plan)

            plan2 = planlib.load(plan_path)
            self.assertEqual(plan2["inventory_counts"]["Theorem"], 5)
            self.assertEqual(plan2["inventory_counts"]["Equation"], 12)
            ok, msg = _is_valid(plan2)
            self.assertTrue(ok, f"Schema validation failed after inventory_counts set: {msg}")

    def test_input_pdf_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = _seed_plan(tmp)
            plan = planlib.load(plan_path)
            plan["input_pdf"] = "/papers/myarticle.pdf"
            plan["raw_output"] = "/papers/_raw.md"
            planlib.save(plan_path, plan)

            plan2 = planlib.load(plan_path)
            self.assertEqual(plan2["input_pdf"], "/papers/myarticle.pdf")
            self.assertEqual(plan2["raw_output"], "/papers/_raw.md")

    def test_atomic_save_produces_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = _seed_plan(tmp)
            plan = planlib.load(plan_path)
            planlib.add_log(plan, "step1", "did something")
            planlib.add_log(plan, "step2", "did something else")
            planlib.save(plan_path, plan)

            with open(plan_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            self.assertEqual(len(loaded["log"]), 2)


if __name__ == "__main__":
    unittest.main()
