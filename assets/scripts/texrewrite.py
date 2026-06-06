#!/usr/bin/env python3
"""texrewrite — the shared TeX-aware safe find-replace core for mol-latex-concat.

Owner: pt-dyq (T4). Imported by resolve_macros.py and prefix_labels.py.
Part of the mol-latex-concat formula (latex-utils pack).

This module is the "TeX-aware safe find-replace core" the epic asks the two
collision helpers to share. It is deliberately small, dependency-free, and sound
*over the well-behaved subset* — it NEVER rewrites text inside a region where a
replacement could change meaning:

  * line comments (an unescaped ``%`` to end of line — ``\\%`` is a literal percent),
  * verbatim-like environments (verbatim/Verbatim/lstlisting/minted/comment/alltt …),
  * inline verbatim (``\\verb|…|``, ``\\verb*|…|``, ``\\lstinline|…|``).

Anything outside those regions is fair game. Control sequences are matched on a
real TeX boundary (``\\foo`` never matches inside ``\\foobar``); reference keys are
rewritten by reading balanced ``{…}`` / ``[…]`` arguments, honouring comma-lists
and the two-argument *range* commands.

The two callers carry the Tier-2 "flag-and-defer, never guess" responsibility:
this core only does the mechanical rewrite and reports what it did. A construct it
cannot see (a custom ``\\ref``-like macro, ``\\csname``-built control sequences) is
the caller's flag to raise — and the formula's compile gate is the final backstop.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# Low-level balanced-argument readers (same semantics as inspect_sources).      #
# --------------------------------------------------------------------------- #


class ParseError(Exception):
    """A construct could not be read soundly -> the caller flags, never guesses."""


def skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def brace_arg(s: str, i: int):
    """If ``s[i]`` is ``{``, return ``(content, index_past_close)`` honouring nested
    braces and escaped ``\\{``/``\\}``. Otherwise ``(None, i)``. Raises ParseError on
    an unterminated group."""
    if i >= len(s) or s[i] != "{":
        return None, i
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    raise ParseError("unterminated brace group")


def bracket_arg(s: str, i: int):
    """If ``s[i]`` is ``[``, return ``(content, index_past_close)`` (brackets do not
    nest but may wrap a brace group). Otherwise ``(None, i)``."""
    if i >= len(s) or s[i] != "[":
        return None, i
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            _, j = brace_arg(s, j)
            continue
        if c == "]":
            return s[i + 1:j], j + 1
        j += 1
    raise ParseError("unterminated optional argument")


# --------------------------------------------------------------------------- #
# Protected-region mask (comments + verbatim, inline and environment).          #
# --------------------------------------------------------------------------- #

# Environments whose body must be copied verbatim (never rewritten inside).
VERBATIM_ENVS = {
    "verbatim", "Verbatim", "BVerbatim", "LVerbatim", "SaveVerbatim",
    "lstlisting", "minted", "comment", "alltt", "listing", "pyglist",
    "filecontents", "filecontents*", "spverbatim",
}

# Inline-verbatim macros: the next character after the (optional ``*``) is the
# delimiter, and everything up to its next occurrence is verbatim.
_INLINE_VERB = ("verb", "lstinline", "mintinline", "spverb")

_CS_RE = re.compile(r"\\([A-Za-z@]+|.)", re.DOTALL)
_ENVNAME_RE = re.compile(r"\s*\{([^}]*)\}")


def protected_spans(text: str):
    """Return a sorted list of ``(start, end)`` half-open spans that must NOT be
    rewritten: line comments, verbatim environments, and inline verbatim. Spans do
    not overlap (each region is consumed once, left to right)."""
    spans = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "%":
            j = text.find("\n", i)
            j = n if j == -1 else j
            spans.append((i, j))
            i = j
            continue
        if ch == "\\":
            m = _CS_RE.match(text, i)
            if not m:
                i += 1
                continue
            name = m.group(1)
            after = m.end()
            if name in _INLINE_VERB:
                k = after
                if k < n and text[k] == "*":
                    k += 1
                if k < n:
                    delim = text[k]
                    end = text.find(delim, k + 1)
                    end = n if end == -1 else end + 1
                    spans.append((i, end))
                    i = end
                    continue
                i = after
                continue
            if name == "begin":
                em = _ENVNAME_RE.match(text, after)
                if em:
                    env = em.group(1).strip()
                    if env in VERBATIM_ENVS:
                        endre = re.compile(r"\\end\s*\{" + re.escape(env) + r"\}")
                        e = endre.search(text, em.end())
                        end = e.end() if e else n
                        spans.append((i, end))
                        i = end
                        continue
                i = after
                continue
            # ordinary control sequence / escaped char: skip it so e.g. "\%" is not
            # treated as a comment and "\verbatim"-ish names are not mis-detected.
            i = after
            continue
        i += 1
    return spans


def _in_spans(pos: int, spans) -> bool:
    for a, b in spans:
        if a <= pos < b:
            return True
        if pos < a:
            break
    return False


# --------------------------------------------------------------------------- #
# Control-sequence rename (\foo -> \foobar), boundary- and comment-aware.        #
# --------------------------------------------------------------------------- #


def _csname_pattern(name_with_backslash: str) -> re.Pattern:
    """Compile a regex matching exactly the control sequence ``name_with_backslash``
    (a leading ``\\`` plus the name). Alphabetic names get a real TeX right boundary
    so ``\\foo`` never matches inside ``\\foobar``; a control symbol (``\\,``) matches
    literally."""
    name = name_with_backslash[1:]
    if re.fullmatch(r"[A-Za-z@]+", name):
        return re.compile(r"\\" + re.escape(name) + r"(?![A-Za-z@])")
    return re.compile(r"\\" + re.escape(name))


def count_csname(text: str, name_with_backslash: str, spans=None) -> int:
    """Count real (unprotected) uses of a control sequence."""
    if spans is None:
        spans = protected_spans(text)
    pat = _csname_pattern(name_with_backslash)
    return sum(1 for m in pat.finditer(text) if not _in_spans(m.start(), spans))


def rename_csname(text: str, old_full: str, new_full: str, spans=None, limit: int = 0):
    """Rewrite control sequence ``old_full`` -> ``new_full`` outside protected
    regions. ``limit`` > 0 stops after that many replacements (use ``limit=1`` to
    rename only the defining occurrence in a macro definition). Returns
    ``(new_text, count)``."""
    if spans is None:
        spans = protected_spans(text)
    pat = _csname_pattern(old_full)
    out, cur, count = [], 0, 0
    for m in pat.finditer(text):
        if _in_spans(m.start(), spans):
            continue
        out.append(text[cur:m.start()])
        out.append(new_full)
        cur = m.end()
        count += 1
        if limit and count >= limit:
            break
    out.append(text[cur:])
    return "".join(out), count


# --------------------------------------------------------------------------- #
# Reference-family key rewriting (\label / \ref / \cref / \hyperref[...] / …).   #
# --------------------------------------------------------------------------- #

# Single-brace-argument reference commands (key, or comma-list of keys, in {…}).
# \label is included so the *definition* sites are prefixed by the same machinery.
BRACE_REF_COMMANDS = {
    "label",
    "ref", "eqref", "pageref", "nameref", "autoref", "autopageref",
    "cref", "Cref", "cpageref", "Cpageref", "namecref", "nameCref", "Namecref",
    "labelcref", "labelcpageref",
    "vref", "Vref", "vpageref", "Vpageref", "fullref", "vrefpagenum",
    "thmref", "lemref", "secref", "figref", "tabref", "eqnref", "appref",
    "zref", "zcref", "zcpageref", "zpageref", "zvref",
    "smartref", "fancyref", "Aref", "Acup",
}

# Two-brace-argument *range* commands (\crefrange{a}{b}); both keys are rewritten.
RANGE_REF_COMMANDS = {
    "crefrange", "Crefrange", "cpagerefrange", "Cpagerefrange",
    "labelcrefrange", "vrefrange", "Vrefrange",
}

# Bracket-keyed commands: \hyperref[key]{display text} — only the [key] is a label.
BRACKET_REF_COMMANDS = {"hyperref"}


def _map_keys(content: str, map_key) -> tuple:
    """Rewrite a comma-list of reference keys with ``map_key``; return
    ``(new_content, n_changed)``. Whitespace around each key is preserved."""
    parts = content.split(",")
    changed = 0
    new_parts = []
    for p in parts:
        stripped = p.strip()
        mapped = map_key(stripped) if stripped else stripped
        if mapped != stripped:
            changed += 1
            # preserve any surrounding whitespace of the original token
            lead = p[: len(p) - len(p.lstrip())]
            trail = p[len(p.rstrip()):]
            new_parts.append(lead + mapped + trail)
        else:
            new_parts.append(p)
    return ",".join(new_parts), changed


def rewrite_refs(text: str, map_key, *, brace_cmds=None, range_cmds=None,
                 bracket_cmds=None, spans=None):
    """Rewrite reference keys via ``map_key`` (a callable ``key -> key`` that returns
    the key unchanged when it should not be touched), across the recognised
    reference family, outside protected regions.

    Returns ``(new_text, n_keys_rewritten)``. Command heads are matched on a real
    boundary so ``\\ref`` is not seen inside ``\\refstepcounter``."""
    if spans is None:
        spans = protected_spans(text)
    brace_cmds = BRACE_REF_COMMANDS if brace_cmds is None else set(brace_cmds)
    range_cmds = RANGE_REF_COMMANDS if range_cmds is None else set(range_cmds)
    bracket_cmds = BRACKET_REF_COMMANDS if bracket_cmds is None else set(bracket_cmds)

    all_cmds = brace_cmds | range_cmds | bracket_cmds
    if not all_cmds:
        return text, 0
    names = sorted(all_cmds, key=len, reverse=True)
    head = re.compile(r"\\(" + "|".join(re.escape(c) for c in names) + r")(\*?)(?![A-Za-z@])")

    out, cur, total = [], 0, 0
    for m in head.finditer(text):
        if m.start() < cur or _in_spans(m.start(), spans):
            continue
        cmd = m.group(1)
        i = skip_ws(text, m.end())
        try:
            if cmd in bracket_cmds and i < len(text) and text[i] == "[":
                content, end = bracket_arg(text, i)
                open_pos = i  # text[open_pos] == '['
                new_content, ch = _map_keys(content or "", map_key)
                out.append(text[cur:open_pos + 1])
                out.append(new_content)
                cur = end - 1  # the closing ']' is re-emitted next
                total += ch
            elif i < len(text) and text[i] == "{":
                nargs = 2 if cmd in range_cmds else 1
                j = i
                for _ in range(nargs):
                    j = skip_ws(text, j)
                    if j >= len(text) or text[j] != "{":
                        break
                    content, end = brace_arg(text, j)
                    new_content, ch = _map_keys(content or "", map_key)
                    out.append(text[cur:j + 1])
                    out.append(new_content)
                    cur = end - 1  # closing '}' re-emitted next
                    total += ch
                    j = end
            else:
                continue
        except ParseError:
            # malformed argument: leave it untouched, let the compile gate surface it
            continue
    out.append(text[cur:])
    return "".join(out), total


def find_labels(text: str, spans=None):
    """Return the ordered, de-duplicated list of ``\\label{key}`` keys defined in
    ``text`` (outside protected regions). The authoritative per-chapter label set."""
    if spans is None:
        spans = protected_spans(text)
    head = re.compile(r"\\label\s*\{")
    out = []
    for m in head.finditer(text):
        if _in_spans(m.start(), spans):
            continue
        try:
            content, _ = brace_arg(text, m.end() - 1)
        except ParseError:
            continue
        for k in (content or "").split(","):
            k = k.strip()
            if k and k not in out:
                out.append(k)
    return out


# Reference-family heads, for detecting custom \ref-like macros by their bodies.
_REF_IN_BODY_RE = re.compile(
    r"\\(?:ref|eqref|pageref|nameref|autoref|cref|Cref|cpageref|Cpageref|"
    r"labelcref|vref|Vref|vpageref|namecref|hyperref|zref|zcref)(?![A-Za-z@])"
)


def body_uses_ref(body: str) -> bool:
    """True if a macro body itself calls a reference command — i.e. the macro is a
    custom ``\\ref``-like wrapper whose *uses* take a label key and therefore need
    flagging (the core cannot know which argument is the key)."""
    return bool(body and _REF_IN_BODY_RE.search(body))
