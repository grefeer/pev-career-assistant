# Supervisor Site-Wide JD Discovery Design

> Date: 2026-07-21
> Scope: Improve PATH C Supervisor and Web Navigation internals without changing the Strategy Router or PATH A/B/C routing behavior.

## 1. Goal

For any public recruitment URL that does not require login, captcha, or special permissions, PATH C must reliably discover the recruitment area, enumerate jobs across the site up to configured budgets, load each reachable job-detail page, preserve the complete JD as evidence, and produce evidence-backed structured candidates.

The input may be a company homepage, recruitment homepage, job-list page, or job-detail page. Agent final-message formatting must never cause already collected evidence or candidates to be lost.

## 2. Non-Goals and Constraints

- Do not change Strategy Router matching or PATH A Adapter / PATH B SnapshotExecutor / PATH C Supervisor selection.
- Do not bypass login, captcha, anti-bot, permission, or paywall barriers.
- Do not create an Adapter for each URL. Adapters remain optional platform-family optimizations.
- Do not invent missing JD fields. Preserve unrecognized content in the authoritative raw evidence and emit warnings.
- Keep student-facing visibility rules, review workflow, evidence requirements, and security boundaries unchanged.
- Default task limits: 200 jobs, 30 minutes total runtime, 45 seconds per page, and two retries per job.
- The page-timeout setting must support the approved 40-50 second operating range.

## 3. Chosen Approach

Use a hybrid site-discovery engine inside PATH C:

- Supervisor makes ambiguous navigation and recovery decisions.
- A persistent Playwright browser session performs stateful navigation.
- Deterministic components own pagination, job inventory, deduplication, evidence persistence, budgets, checkpoints, status calculation, and final serialization.
- Structured JSON and semantic DOM extraction run before constrained LLM extraction.
- Platform-family Adapters may accelerate known ATS products but are not required for an unknown public URL.

This approach preserves generalization while removing correctness-critical bookkeeping from the LLM.

## 4. Architecture

```text
Strategy Router (unchanged)
|-- PATH A Adapter (unchanged)
|-- PATH B SnapshotExecutor (unchanged)
`-- PATH C Supervisor
    |-- SiteDiscoveryOrchestrator
    |-- BrowserSession
    |-- PageClassifier
    |-- NavigationPlanner
    |-- JobInventory
    |-- EvidenceCollector
    |-- JDExtractor
    |-- CompletenessVerifier
    `-- DiscoveryRunState
```

### 4.1 Supervisor responsibilities

Supervisor decides only:

1. How to resolve an ambiguous page classification.
2. Which recruitment entry, pagination control, job card, or recovery action to try.
3. How to infer an unfamiliar navigation pattern from rendered DOM and captured network observations.
4. When an external barrier requires manual review.

Supervisor does not own job deduplication, page counts, time budgets, pagination termination, evidence association, checkpointing, task status, or final result serialization.

### 4.2 Internal component boundaries

- `BrowserSession`: one persistent Playwright context/page set per discovery task; owns cookies, storage, history, rendered DOM, interaction, scrolling, waits, and network capture.
- `PageClassifier`: classifies pages as `company_home`, `career_home`, `job_list`, `job_detail`, `blocked`, or `unknown` using URL, rendered DOM, structured data, network observations, and deterministic signals before asking an LLM.
- `SiteDiscoveryOrchestrator`: coordinates discovery without changing router behavior; enforces allowed-domain transitions and task budgets.
- `JobInventory`: stores discovered job IDs/URLs, category coverage, pagination state, deduplication keys, retry state, and completion state.
- `EvidenceCollector`: captures complete visible text and structured payloads, calculates hashes, stores full artifacts, and creates database evidence metadata.
- `JDExtractor`: extracts one job at a time using structured data, semantic DOM, and finally constrained LLM extraction.
- `CompletenessVerifier`: validates source spans, job identity, field completeness, content boundaries, and one-job-to-one-detail-evidence association.
- `DiscoveryRunState`: typed authoritative state from which Python constructs the final `DiscoveryRunResult`.

## 5. Persistent Browser Contract

All navigation tools for one task share the same browser context. The generic browser interface must support:

- open a URL and wait for a deterministic readiness condition;
- inspect rendered text, semantic DOM, forms, frames, and interactive elements;
- assign stable element IDs for links, buttons, cards, tabs, and expandable sections;
- click an element and report DOM, route, popup, and network changes;
- scroll, load more, go back, open a detail in another page, and retain the list-page state;
- inspect XHR, Fetch, GraphQL, JSON-LD, `__NEXT_DATA__`, and hydration state;
- capture the current page as complete evidence;
- report login, captcha, anti-bot, permission, timeout, and browser failures distinctly.

Static HTTP helpers may remain as fast observations, but they cannot be treated as substitutes for rendered DOM interaction on an SPA.

Allowed-domain expansion is evidence based: the task may follow a recruitment link from the company domain to an official ATS domain, but it may not visit an unrelated domain invented by the model.

## 6. Site-Wide Discovery Algorithm

### 6.1 Bootstrap and classification

Open the input URL in the persistent session and classify it. A company homepage triggers recruitment-entry discovery; a recruitment homepage or list starts inventory discovery; a detail page is collected immediately and may be followed back to its list only when a public list relationship is discoverable.

Recruitment-entry candidates are ranked from rendered navigation, footer links, buttons, and labels such as recruitment, careers, jobs, join us, campus, and internships. Supervisor selects only from observed candidates.

### 6.2 Build the job inventory

Collect job identity hints from:

- rendered cards and semantic links;
- buttons, role links, onclick handlers, and SPA route changes;
- XHR, Fetch, and GraphQL payloads;
- JSON-LD, Next.js data, and other embedded application state;
- public sitemap or declared job counts.

Each inventory item records a stable job key, title hint, detail URL, platform job ID, list URL, category/filter, discovery source, retry state, and completion state.

Deduplicate in this order:

1. Platform job ID.
2. Canonical detail URL.
3. Normalized company, title, and location.
4. Content hash.

URL canonicalization must preserve hash routes and identity-bearing query parameters.

### 6.3 Pagination and category coverage

Support numbered pagination, next-page controls, load-more buttons, infinite scroll, and API cursors.

Deterministic termination conditions are:

- explicit last page or disabled next control;
- API `has_more=false` or equivalent;
- discovered count reaches a trustworthy declared total;
- two consecutive pages add no jobs;
- three infinite-scroll cycles add no cards or job-bearing network records;
- configured job, time, or page budget is reached.

Prefer an `all` filter. When none exists, traverse top-level mutually exclusive recruitment categories and maintain a coverage ledger. Do not expand arbitrary filter Cartesian products.

### 6.4 Detail collection

Keep the list state alive while details are opened in a separate page or inspected through a drawer/route transition. For every job, try in order:

1. Structured detail API or embedded detail object.
2. Independent detail URL.
3. SPA route transition triggered by its observed card.
4. Drawer, modal, or expandable detail panel.
5. Supervisor-selected unfamiliar interaction.

Wait up to the configured page timeout, expand truncated sections, and store complete visible text plus relevant structured payloads. Retry transient failures no more than twice. A failed job remains visible in the inventory with a precise reason.

## 7. Evidence and Extraction

### 7.1 Authoritative full evidence

`JobDiscoveryEvidence.text_excerpt` remains a review preview. Complete evidence is stored as an artifact referenced by `storage_uri` and includes canonical URL, platform job ID, page title, full visible text, relevant structured payloads, collection time, content hash, and collection metadata.

Full evidence must not be truncated to the existing 1,500/5,000/10,000-character prompt limits. Only prompt projections may be truncated or chunked.

### 7.2 Three-stage extraction

For each individual job:

1. Extract from JSON-LD, XHR, GraphQL, or embedded structured state.
2. Extract from semantic DOM sections and label/value relationships.
3. Use constrained LLM extraction only for unresolved fields or unfamiliar structures.

LLM extraction returns strict schema plus a JSON path or text span for every populated field. A field without a verifiable source reference is discarded or marked unresolved.

### 7.3 Complete JD definition

A job is `complete` only when it has:

- a stable job identity or detail URL;
- independent detail evidence;
- a title;
- complete captured detail text;
- responsibilities or requirements, unless the source genuinely omits them and a warning is recorded;
- per-field evidence traceability;
- no indication that a collapsed or load-more section remains unopened.

List evidence cannot substitute for detail evidence. Missing fields are never invented.

## 8. Typed State, Checkpointing, and Final Results

`DiscoveryRunState` contains the task identity, start URL, allowed domains, browser checkpoint, coverage ledger, job inventory, completed keys, retry queue, evidence references, candidates, budgets, errors, and continuation cursor.

Evidence and candidate progress is persisted after recruitment-entry discovery, each completed list page, each completed job detail, and before a budget-controlled exit. A durable task checkpoint stores only resumable metadata and artifact references, not complete JD bodies.

The final result is assembled by Python from typed state. Agent message formatting is not part of the correctness path.

Status invariants:

- `succeeded`: traversal is complete and every discovered job is complete or explicitly classified as not a JD/removed.
- `partial_success`: at least one complete JD exists, but a configured budget or unresolved job prevents full coverage.
- `needs_manual_review`: recruitment content exists but an external barrier or unrecoverable interaction blocks progress.
- `failed`: no complete JD exists and there is no meaningful manual-review recovery condition.

Every result reports declared, discovered, completed, failed, and candidate counts; category coverage; coverage completeness; continuation availability; and unresolved job keys. Silent job loss must be zero.

## 9. Error Handling and Recovery

- `transient`: timeout, temporary 5xx, browser crash; retry up to two times.
- `recoverable_navigation`: selector drift, unchanged route, unopened panel; Supervisor selects a different observed action.
- `permanent_or_blocked`: 404, removed job, login, captcha, anti-bot, or permission barrier; do not consume retry budget repeatedly.

After a browser crash, create a new session and resume from durable inventory and pagination state. Do not require serializing Playwright objects.

Reaching 200 jobs, 30 minutes, or another configured budget yields `partial_success` when complete jobs exist, together with a continuation cursor. A resumed task skips evidence whose job key and content hash are already complete.

## 10. Accuracy and Observability

Accuracy is enforced through evidence, not confidence prose:

- every populated field has a valid source span or JSON path;
- list/detail title and job identity are cross-checked;
- each candidate references its own detail evidence;
- multi-job content is separated before extraction;
- navigation boilerplate and neighboring-job contamination are rejected;
- unresolved fields remain unresolved;
- complete raw evidence remains reviewable even when structured extraction is incomplete.

Diagnostic telemetry records page classification, observed action candidates, selected actions, route/DOM/network changes, pagination coverage, job-state transitions, retry classification, extraction method, verifier rejection reason, and final stop reason. Logs and trajectory records must not expose secrets, tokens, full resumes, or unnecessary complete JD payloads.

## 11. Testing and Acceptance

### 11.1 Deterministic fixtures

Provide local fixtures for:

- company homepage to recruitment entry to paginated details;
- hash-router details;
- clickable card drawers;
- infinite scroll;
- load more;
- XHR-only jobs;
- Next.js embedded state;
- multiple recruitment categories;
- partial detail failures;
- 200-job limit and continuation;
- browser crash and recovery;
- login and captcha barriers.

Fixture acceptance:

- recruitment-entry discovery: 100%;
- inventory coverage: 100%;
- silent job loss: 0;
- independent detail evidence for every completed candidate: 100%;
- full-body content hash for every completed job: 100%;
- duplicates after continuation: 0;
- generated fields absent from evidence: 0.

### 11.2 Golden dataset

Create sanitized fixed samples representing Moka, Feishu recruitment, Xiaomi/Mioffice, PDD/Next.js, and a conventional static site. Human annotations cover title, location, responsibilities, requirements, apply URL, job ID, and body boundaries.

Acceptance targets:

- title, job ID, and detail URL precision: 100%;
- job recall: at least 98%;
- responsibilities/requirements text coverage: at least 95%;
- cross-job contamination: 0%;
- unsupported field generation: 0%;
- final-result protocol parsing failure: 0%.

Live tests report external barriers separately. Login walls, removed jobs, and captchas do not count as parser failures and cannot be presented as successful extraction.

## 12. Delivery Boundaries

The work should be delivered incrementally inside PATH C:

1. Result parsing, status invariants, and diagnostic preservation.
2. Persistent browser session and rendered interaction primitives.
3. Typed inventory, pagination coverage, and checkpoints.
4. Complete evidence artifacts and per-job extraction.
5. Evidence-span verification and deterministic final assembly.
6. Golden fixtures, live acceptance, performance tuning, and documentation refresh.

Each stage must remain compatible with PATH A/B fallback into PATH C and must not alter router matching or strategy state transitions.
