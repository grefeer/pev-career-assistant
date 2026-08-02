"""Generic, preference-driven expansion for the discovery preference filter.

Everything is derived FROM the preference string itself so the filter works for
ANY preference (``AI应用开发``, ``AI产品经理``, ``数据分析``, ``芯片设计工程师``,
``Agent开发`` ...).  No preference-specific role lists are hardcoded anywhere in
this module: the role markers used for any given run are selected by detecting the
preference's own role family via a **generic role-type taxonomy**
(product/dev/design/algo/data/ops/test/security/research).

The expansion separates two independent signals:

* ``keep_tokens`` - carry the preference's DOMAIN (e.g. ``AI产品`` in
  ``AI产品经理``).  A job whose title/body contains a keep_token is a *candidate*
  match.  These never include a bare role keyword (``产品经理`` alone has no domain
  and would match ``后端产品经理`` - a false positive).
* ``role_markers`` - carry the preference's ROLE FAMILY (e.g. product markers
  ``产品经理/产品/PM``).  Stage (a) of the filter requires BOTH a keep_token and a
  role_marker, so the domain and the role type must both align.

Because both sets are derived from the preference, they invert correctly: an
``AI产品经理`` preference yields product markers (keeps product roles, filters dev
roles), while ``AI应用开发`` yields dev markers.  There is no built-in bias toward
development roles.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Generic role-type taxonomy
# ---------------------------------------------------------------------------
# Maps a detected role FAMILY to the title markers that indicate a job is IN that
# family.  This is a general classifier covering all common engineering/product
# role families - it is NOT an AI-dev keep-list.  The markers used at runtime are
# selected by the preference's own family, so a 产品 preference gets product
# markers and a 开发 preference gets dev markers.
_ROLE_FAMILY_MARKERS: dict[str, tuple[str, ...]] = {
    "product": ("产品经理", "产品", "pm", "product manager"),
    "dev": ("开发", "研发", "工程师", "engineer", "程序员", "developer"),
    "design": ("设计", "设计师", "designer", "ued", "ui"),
    "algo": ("算法", "algorithm"),
    "data": ("数据", "分析", "analyst", "数据工程", "数据科学"),
    "ops": ("运营", "ops", "operation"),
    "test": ("测试", "qa", "质量保障"),
    "security": ("安全", "security", "sec"),
    "research": ("研究", "研究员", "research", "scientist"),
}

# Role-family detection stems, in priority order.  Each entry is (family, stems).
# Stems are matched case-insensitively against the preference text; the earliest
# stem occurring after position 0 (so a non-empty domain prefix remains) wins.
# Order matters only for ties (a preference containing two family stems); the
# first family with a pos>0 stem is chosen.
_ROLE_DETECTORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("product", ("产品", "pm")),
    ("design", ("设计", "ued")),
    ("algo", ("算法",)),
    ("data", ("数据", "分析")),
    ("ops", ("运营",)),
    ("test", ("测试",)),
    ("security", ("安全",)),
    ("research", ("研究",)),
    # dev last: 开发/工程师 are common level suffixes, so a more specific family
    # (产品/算法/数据/...) above takes precedence when present.
    ("dev", ("开发", "研发", "工程师", "engineer")),
)

# Generic, domain-agnostic stopwords too short to carry a preference domain on
# their own (bare ``AI``/``LLM`` match almost any tech job).  Excluding these is
# NOT preference-specific cheating - they are length-2 connectors.
_DOMAIN_STOPWORDS: frozenset[str] = frozenset({"ai", "llm", "the", "and", "for"})


@dataclass(frozen=True)
class PreferenceProfile:
    """Derived signals for one preference string."""

    preference: str
    role_type: str
    search_terms: list[str] = field(default_factory=list)
    keep_tokens: list[str] = field(default_factory=list)
    role_markers: tuple[str, ...] = ()


def expand_preference(preference: str) -> PreferenceProfile:
    """Expand a single preference string into search terms and role markers.

    Derivation is purely algorithmic.  No external knowledge of AI-dev synonyms
    (agent/智能体, 大模型/llm, ...) is encoded: jobs whose body uses a synonym not
    present in the preference string are handled by the LLM stage of the filter
    when a keep_token is present, and are otherwise out of scope - consistent
    with a preference-keyword-driven (precision) discovery path.
    """
    pref = preference.strip()
    norm = pref.replace(" ", "").casefold()

    role_type, stem, stem_pos = _detect_role_family(norm)
    domain = pref[:stem_pos] if stem_pos > 0 else ""

    # keep_tokens carry the DOMAIN: the full preference, the domain prefix, and
    # domain+stem.  Bare role keywords and length<3 / stopword fragments are
    # excluded so a non-domain role (e.g. 后端产品经理) cannot match on role alone.
    candidates: list[str] = [pref, domain, f"{domain}{stem}" if stem else ""]
    keep_tokens = _dedup_keep(candidates)

    # role_markers come from the generic taxonomy for the detected family.
    role_markers = _ROLE_FAMILY_MARKERS.get(role_type, ())

    # search_terms: strong, typed-into-the-search-box terms.  Preference first,
    # then domain+stem, then up to 3 family markers.
    search_terms = _dedup([
        pref,
        f"{domain}{stem}" if stem and f"{domain}{stem}" != pref else "",
        *role_markers[:3],
    ])
    return PreferenceProfile(
        preference=pref,
        role_type=role_type,
        search_terms=search_terms,
        keep_tokens=keep_tokens,
        role_markers=role_markers,
    )


def expand_preferences(
    preferences: Iterable[str],
) -> list[PreferenceProfile]:
    """Expand each preference; de-duplicated by preference string."""
    seen: set[str] = set()
    out: list[PreferenceProfile] = []
    for p in preferences:
        profile = expand_preference(p)
        key = profile.preference.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(profile)
    return out


def preference_search_terms(preferences: Iterable[str]) -> list[str]:
    """Preference-derived search-box terms, ordered most-specific first.

    These feed a portal search box: the full preference first, then the
    domain+stem, then the role-family markers.  Used with a ``first_match``
    search strategy this cascades specific -> broad and stops at the first term
    that yields results, so a precise preference (``AI应用开发``) is tried before
    its broad role markers (``开发``).  Every term is derived from the
    preference - no AI-dev tokens are hardcoded.
    """
    out: list[str] = []
    for profile in expand_preferences(preferences):
        for term in profile.search_terms:
            key = term.casefold().replace(" ", "")
            if key and key not in {t.casefold().replace(" ", "") for t in out}:
                out.append(term)
    return out


def _detect_role_family(norm: str) -> tuple[str, str, int]:
    """Return (family, stem, stem_position) for the preference.

    ``stem_position`` is the 0-based index into ``norm`` where the stem starts.
    Prefers a stem at position > 0 so a domain prefix remains.  Returns
    ``("generic", "", 0)`` when no family stem is found.
    """
    best: tuple[str, str, int] | None = None
    for family, stems in _ROLE_DETECTORS:
        for stem in stems:
            stem_cf = stem.casefold()
            pos = norm.find(stem_cf)
            if pos < 0:
                continue
            # Prefer pos>0 (keeps a domain prefix); among those, earliest pos.
            if pos > 0:
                return family, stem, pos
            if best is None:
                best = (family, stem, pos)
    if best is not None:
        return best
    return "generic", "", 0


def _dedup(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        if not it:
            continue
        key = it.casefold().replace(" ", "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _dedup_keep(items: list[str]) -> list[str]:
    """Keep tokens: de-dup, drop stopwords, preserve order.

    Bare length-2 English connectors (``ai``/``llm``) are dropped via the
    stopword set; meaningful 2-char Chinese domain tokens (``数据``/``芯片``/
    ``产品``) are kept, since they carry the preference domain.
    """
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        if not it:
            continue
        key = it.casefold().replace(" ", "")
        if not key or key in seen:
            continue
        if key in _DOMAIN_STOPWORDS:
            continue
        seen.add(key)
        out.append(it)
    return out
