#!/usr/bin/env python3
"""Tests for the add-bib-references helpers (find_undefined_cites, insert_bib_entry) and
the plan.schema.json extension.

Self-contained: each test builds a plan.json + LaTeX/.bib fixtures in a tmp dir and
asserts the extended plan.json / .bib, per the pack's 'helpers, not prose' convention.
Schema validation runs only when the optional `jsonschema` package is installed
(mirroring planlib.validate), so the suite runs in the pack's minimal environment too.

Run: python3 -m unittest test_add_bib_references     (from assets/scripts/)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import planlib  # noqa: E402

SCHEMA_PATH = os.path.normpath(os.path.join(HERE, os.pardir, "plan.schema.json"))

try:
    import jsonschema  # type: ignore
    with open(SCHEMA_PATH, encoding="utf-8") as _fh:
        _SCHEMA = json.load(_fh)
except Exception:  # jsonschema absent -> schema assertions are skipped
    jsonschema = None
    _SCHEMA = None


def _run(script, *args):
    """Run a helper CLI; return the CompletedProcess."""
    return subprocess.run([sys.executable, os.path.join(HERE, script), *args],
                          capture_output=True, text=True)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _is_valid(plan):
    """(ok, message) against plan.schema.json, or (True, 'skipped') without jsonschema."""
    if jsonschema is None:
        return True, "skipped (no jsonschema)"
    errs = sorted(jsonschema.Draft7Validator(_SCHEMA).iter_errors(plan), key=lambda e: list(e.path))
    return (not errs), "; ".join(e.message for e in errs[:3])


TWO_BIB_MASTER = r"""
\documentclass{article}
\usepackage{natbib}
\begin{document}
\input{intro}
See also \cite{knuth1984} and \citep{lamport1994}.
% \cite{commented_out}
\begin{verbatim}
\cite{verbatim_key}
\end{verbatim}
\cites{multiA}{multiB}
\citeauthor{onlyauthor}
\nocite{*}
\bibliography{published,preprints}
\end{document}
"""

INTRO = r"""
Intro cites \citet{einstein1905} and \cite{knuth1984}.
\autocite[p.~5]{preprintX}
"""

PUBLISHED_BIB = """
@book{knuth1984, author = {Knuth, D.}, title = {The TeXbook}, year = {1984}}
@article{lamport1994, author = {Lamport, L.}, title = {LaTeX}, year = {1994}}
@article{einstein1905, author = {Einstein, A.}, title = {Relativity}, year = {1905}}
"""


class BaseFixture(unittest.TestCase):
    def make_sources(self):
        root = tempfile.mkdtemp()
        _write(os.path.join(root, "master.tex"), TWO_BIB_MASTER)
        _write(os.path.join(root, "intro.tex"), INTRO)
        _write(os.path.join(root, "published.bib"), PUBLISHED_BIB)
        _write(os.path.join(root, "preprints.bib"),
               "@misc{somethingelse, author = {X}, title = {Y}, year = {2020}}\n")
        return root

    def assertValidPlan(self, plan):
        ok, msg = _is_valid(plan)
        self.assertTrue(ok, msg)


class FindUndefinedCitesTest(BaseFixture):
    def _seed(self, root, single_bib=False, vars=None):
        plan = os.path.join(root, "plan.json")
        planlib.save(plan, planlib.new_bib_plan(
            master_source=os.path.join(root, "master.tex"),
            published_bib=os.path.join(root, "published.bib"),
            preprints_bib=os.path.join(root, "preprints.bib"),
            single_bib=single_bib, vars=vars))
        return plan

    def test_undefined_across_input_tree(self):
        root = self.make_sources()
        plan = self._seed(root)
        r = _run("find_undefined_cites.py", "--plan", plan)
        self.assertEqual(r.returncode, 0, r.stderr)
        p = planlib.load(plan)
        keys = [e["key"] for e in p["worklist"]]
        self.assertEqual(keys, ["preprintX", "multiA", "multiB", "onlyauthor"])
        self.assertTrue(all(e["status"] == "pending" for e in p["worklist"]))
        self.assertValidPlan(p)

    def test_ignores_comments_verbatim_and_nocite_star(self):
        root = self.make_sources()
        plan = self._seed(root)
        _run("find_undefined_cites.py", "--plan", plan)
        keys = [e["key"] for e in planlib.load(plan)["worklist"]]
        self.assertNotIn("commented_out", keys)
        self.assertNotIn("verbatim_key", keys)
        self.assertNotIn("*", keys)

    def test_references_var_intersects_and_includes_uncited(self):
        root = self.make_sources()
        plan = self._seed(root, vars={"references": ["knuth1984", "newref"]})
        _run("find_undefined_cites.py", "--plan", plan)
        p = planlib.load(plan)
        byk = {e["key"]: e for e in p["worklist"]}
        self.assertEqual(list(byk), ["knuth1984", "newref"])
        self.assertIn("already defined", byk["knuth1984"]["note"])
        self.assertIn("not yet", byk["newref"]["note"])
        self.assertValidPlan(p)

    def test_idempotent_preserves_human_edits(self):
        root = self.make_sources()
        plan = self._seed(root)
        _run("find_undefined_cites.py", "--plan", plan)
        p = planlib.load(plan)
        p["worklist"][0]["status"] = "approved"
        p["worklist"][0]["classification"] = "preprint"
        planlib.save(plan, p)
        _run("find_undefined_cites.py", "--plan", plan)
        p2 = planlib.load(plan)
        self.assertEqual([e["key"] for e in p2["worklist"]], [e["key"] for e in p["worklist"]])
        self.assertEqual(p2["worklist"][0]["status"], "approved")
        self.assertEqual(p2["worklist"][0]["classification"], "preprint")

    def test_runs_without_preseeded_plan(self):
        # Standalone: no plan.json yet — config comes entirely from CLI flags.
        root = self.make_sources()
        plan = os.path.join(root, "plan.json")
        r = _run("find_undefined_cites.py", "--plan", plan,
                 "--master-source", os.path.join(root, "master.tex"),
                 "--bib", os.path.join(root, "published.bib"),
                 "--bib", os.path.join(root, "preprints.bib"))
        self.assertEqual(r.returncode, 0, r.stderr)
        p = planlib.load(plan)
        self.assertEqual([e["key"] for e in p["worklist"]],
                         ["preprintX", "multiA", "multiB", "onlyauthor"])
        self.assertValidPlan(p)

    def test_missing_master_is_usage_error(self):
        root = self.make_sources()
        r = _run("find_undefined_cites.py", "--plan", os.path.join(root, "plan.json"),
                 "--master-source", os.path.join(root, "does_not_exist.tex"),
                 "--bib", os.path.join(root, "published.bib"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("master source not found", r.stderr)

    def test_log_crosscheck_flags_and_adds_missing(self):
        root = tempfile.mkdtemp()
        _write(os.path.join(root, "m.tex"),
               "\\documentclass{article}\\begin{document}\\cite{realkey}"
               "\\bibliography{r}\\end{document}\n")
        _write(os.path.join(root, "r.bib"), "@misc{other, title={x}}\n")
        _write(os.path.join(root, "build.log"),
               "LaTeX Warning: Citation `ghostkey' on page 1 undefined on input line 3.\n"
               "LaTeX Warning: Citation `realkey' on page 1 undefined.\n")
        plan = os.path.join(root, "plan.json")
        planlib.save(plan, planlib.new_bib_plan(master_source=os.path.join(root, "m.tex"),
                                                published_bib=os.path.join(root, "r.bib")))
        _run("find_undefined_cites.py", "--plan", plan, "--log", os.path.join(root, "build.log"))
        p = planlib.load(plan)
        keys = [e["key"] for e in p["worklist"]]
        self.assertIn("realkey", keys)
        self.assertIn("ghostkey", keys)
        self.assertEqual(set(p["baseline"]["undefined"]), {"ghostkey", "realkey"})
        self.assertTrue(any(f["kind"] == "cite-scan-miss" for f in p["flags"]))
        n1 = len(p["flags"])
        _run("find_undefined_cites.py", "--plan", plan, "--log", os.path.join(root, "build.log"))
        self.assertEqual(len(planlib.load(plan)["flags"]), n1)  # flags don't accumulate


class InsertBibEntryTest(BaseFixture):
    def test_two_bib_routes_by_classification_idempotently(self):
        root = self.make_sources()
        pub, pre = os.path.join(root, "published.bib"), os.path.join(root, "preprints.bib")
        plan = os.path.join(root, "plan.json")
        planlib.save(plan, planlib.new_bib_plan(
            master_source=os.path.join(root, "master.tex"),
            published_bib=pub, preprints_bib=pre, single_bib=False))
        entry = "@misc{preprintX, author = {A}, title = {P}, year = {2024}}"
        r = _run("insert_bib_entry.py", "--plan", plan, "--classification", "preprint", "--bibtex", entry)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("preprintX", _read(pre))
        self.assertNotIn("preprintX", _read(pub))
        _run("insert_bib_entry.py", "--plan", plan, "--classification", "preprint", "--bibtex", entry)
        self.assertEqual(_read(pre).count("{preprintX,"), 1)  # no double-insert
        _run("insert_bib_entry.py", "--plan", plan, "--classification", "thesis",
             "--bibtex", "@phdthesis{onlyauthor, author = {B}, title = {T}, year = {2019}}")
        self.assertIn("onlyauthor", _read(pub))  # thesis files with published
        p = planlib.load(plan)
        byk = {e["key"]: e for e in p["worklist"]}
        self.assertEqual(byk["preprintX"]["status"], "written")
        self.assertTrue(byk["preprintX"]["target_bib"].endswith("preprints.bib"))
        self.assertValidPlan(p)

    def test_one_bib_sections_and_placement(self):
        root = self.make_sources()
        refs = os.path.join(root, "refs.bib")
        _write(refs, "@article{existing2000, author = {Q}, title = {Old}, year = {2000}}\n")
        plan = os.path.join(root, "plan.json")
        planlib.save(plan, planlib.new_bib_plan(
            master_source=os.path.join(root, "master.tex"),
            published_bib=refs, preprints_bib=refs, single_bib=True))
        _run("insert_bib_entry.py", "--plan", plan, "--single-bib", "--classification", "published",
             "--bibtex", "@article{pubNew, author = {C}, title = {New}, year = {2023}}")
        _run("insert_bib_entry.py", "--plan", plan, "--single-bib", "--classification", "preprint",
             "--bibtex", "@misc{preNew, author = {D}, title = {Pre}, year = {2025}}")
        txt = _read(refs)
        ipub, ipre = txt.index("===== Published ====="), txt.index("===== Preprints =====")
        self.assertLess(ipub, ipre)
        self.assertLess(ipub, txt.index("existing2000"))
        self.assertLess(txt.index("existing2000"), ipre)
        self.assertLess(ipub, txt.index("pubNew"))
        self.assertLess(txt.index("pubNew"), ipre)
        self.assertGreater(txt.index("preNew"), ipre)
        # idempotent
        _run("insert_bib_entry.py", "--plan", plan, "--single-bib", "--classification", "preprint",
             "--bibtex", "@misc{preNew, author = {D}, title = {Pre}, year = {2025}}")
        self.assertEqual(_read(refs).count("{preNew,"), 1)
        self.assertValidPlan(planlib.load(plan))

    def test_runs_without_preseeded_plan(self):
        # Standalone: no plan.json yet — target .bib comes entirely from CLI flags.
        root = self.make_sources()
        pub = os.path.join(root, "published.bib")
        plan = os.path.join(root, "plan.json")
        r = _run("insert_bib_entry.py", "--plan", plan, "--two-bib",
                 "--published-bib", pub, "--classification", "published",
                 "--bibtex", "@article{fresh2027, author = {Z}, title = {Fresh}, year = {2027}}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("fresh2027", _read(pub))
        p = planlib.load(plan)
        self.assertEqual(p["formula"], "add-bib-references")
        self.assertEqual(p["worklist"][0]["status"], "written")
        self.assertValidPlan(p)

    def test_rejects_non_single_entry(self):
        root = self.make_sources()
        plan = os.path.join(root, "plan.json")
        planlib.save(plan, planlib.new_bib_plan(
            master_source=os.path.join(root, "master.tex"),
            published_bib=os.path.join(root, "published.bib"), single_bib=False))
        r = _run("insert_bib_entry.py", "--plan", plan, "--classification", "published",
                 "--bibtex", "@a{k1, title={x}}\n@b{k2, title={y}}")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("exactly one", r.stderr)


class SchemaTest(BaseFixture):
    def setUp(self):
        if jsonschema is None:
            self.skipTest("jsonschema not installed")

    def test_mol_latex_concat_backcompat(self):
        self.assertValidPlan(planlib.new(["chapA", "chapB"], dest="", tex_engine="pdflatex"))

    def test_mol_latex_concat_missing_sources_rejected(self):
        bad = planlib.new(["x"])
        del bad["sources"]
        self.assertFalse(_is_valid(bad)[0])

    def test_add_bib_requires_config_and_worklist(self):
        bad = planlib.new_bib_plan(master_source="m.tex")
        del bad["config"]
        del bad["worklist"]
        self.assertFalse(_is_valid(bad)[0])

    def test_worklist_status_enum_enforced(self):
        bad = planlib.new_bib_plan(master_source="m.tex")
        bad["worklist"] = [{"key": "k", "status": "bogus"}]
        self.assertFalse(_is_valid(bad)[0])


if __name__ == "__main__":
    unittest.main()
