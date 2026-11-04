# Genie Ontology Readiness — Deployment Instructions

A **customer-deployable** Databricks App that assesses a workspace's maturity for
**Genie Ontology / Unity Catalog Business Semantics**, explains each capability
(technical + business value), recommends best practices, and generates tailored
enablement + adoption plans via the customer's **own Foundation Model API**.

It **reads the existing environment** (Unity Catalog `information_schema`, system
tables, Genie/Domains REST). It does **not** seed demo data. Full-stack app:
**FastAPI** backend + **React/Vite** frontend, deployed with **Databricks Asset
Bundles** + `post_deploy.py`.

> **Product accuracy:** Genie Ontology is the *learned* enterprise context layer
> (gated / Private Preview) built on top of the customer's *governed* UC Business
> Semantics (metric views, Pages, domains — largely GA). The foundation
> **feeds** the ontology. The app never positions the learned layer as GA.

## Prerequisites

- **Databricks workspace** with Unity Catalog (a serverless workspace is recommended).
- **Databricks CLI 0.287+** — `databricks --version`.
- **Authenticated profile**: `databricks auth login --host <workspace-url> --profile <name>`.
- **Node.js 20.19+ or 22.12+** (per `app/frontend/package.json` engines) and **Python 3.11** with `pip install "databricks-sdk>=0.36.0"`.
- A **SQL Warehouse** (its `id` is needed for assessment queries).

## Deployment (3 commands)

No local tooling? Run [`notebooks/install.py`](notebooks/install.py) from a workspace
Git folder on serverless instead: it does the same create/grant/render/deploy steps
with the SDK, and the Databricks Apps runtime builds the frontend. Its default app
name is `genie-ontology-readiness-nb`, so it never collides with a bundle target.
See the README.

All commands run from the repository root.

### 1. Build the frontend

```bash
cd app/frontend && npm install && npm run build && cd ../..
```

### 2. Deploy infrastructure (app + SQL warehouse resource)

```bash
databricks bundle deploy -t dev \
  --profile <your-profile> \
  --var="warehouse_id=<your_warehouse_id>"
```

Each environment is a **bundle target** with its own Terraform state and a
**fixed app name** (`dev` → `genie-ontology-readiness-dev`, `stg` → `…-stg`,
`prod` → the bare `genie-ontology-readiness`). Pick the environment with
`-t <target>`; there is **no `--var app_name`** — the name is pinned per target
in `databricks.yml` on purpose, so a deploy can't rename/destroy another env's
app. For a clean production name, deploy `-t prod`. Routine redeploys of an
existing app should use the non-destructive `databricks apps deploy <name>`;
reserve `bundle deploy` for first-time create / infra changes.

### 3. Post-deploy (render app.yml, grant SP read access, publish)

```bash
export DATABRICKS_PROFILE=<your-profile>
export TARGET=dev   # dev (default) | stg | prod — must match the `-t` above;
                    # the app name is derived from the target, don't set it here
export WAREHOUSE_ID=<your_warehouse_id>
# Per-user history (assessment runs + saved plans) — recommended:
export USE_LAKEBASE=true
export LAKEBASE_INSTANCE_NAME=<your-instance>   # an existing Lakebase instance to reuse (leave unset to skip history)
export LAKEBASE_DATABASE=ontology_readiness     # a dedicated DB created on that instance
# Optional:
# export ASSESS_CATALOGS="sales,finance"     # restrict scope (default: all non-system catalogs)
# export GENIE_SPACE_ID=<id>                  # enable the live Genie answer test (pillar 5)
# export BRAND_NAME="Acme"                    # header branding
# export FORCE_SP=true                        # SP-only mode: never attempt OBO, run every read as the app SP

python3 scripts/post_deploy.py   # requires: pip install "databricks-sdk>=0.36.0" asyncpg
```

`post_deploy.py` renders `app/app.yml`, grants the **app service principal** read
access (system schemas + assessed catalogs), and republishes the app. When
`USE_LAKEBASE=true` it also creates the app's Postgres database on the shared
Lakebase instance, attaches the `lakebase-db` app resource, and pins the
connection env. Assessment history and generated plans are then persisted
**per user** (keyed by the `X-Forwarded-Email` identity Databricks Apps inject)
and survive across sessions. Scores are a deterministic function of the technical
findings, so repeated runs on the same workspace are directly comparable **for a
given identity and scope**. The overall score averages only the pillars that could
be read (weights renormalized; `overall.coverage` reports how much was), and each
saved run records its `scope` so Compare flags `scope_mismatch` instead of
reporting a scope change as movement. Adoption excludes the app SP's own activity. Under OBO the findings reflect the viewing user's grants, so
signals gated behind grants a viewer lacks (notably the workspace-wide Adoption
system tables) can read lower for a restricted viewer than for the SP — use
`FORCE_SP=true` when you need a single, viewer-independent workspace-wide score.

## Auth: on-behalf-of-user by default, service-principal fallback

The assessment runs **on-behalf-of-user (OBO)** by default, falling back to the
app **service principal (SP)** whenever OBO isn't available or a specific read
can't succeed as the viewer.

**On-behalf-of-user (OBO):** if the workspace admin enables **user authorization**
(Public Preview) and the app carries the `sql` user scope, Databricks forwards the
viewer's token in `x-forwarded-access-token`. The app then runs **every** signal —
both `information_schema`/tags **and** the system-table signals
(`system.access.audit`, `system.query.history`, `system.access.table_lineage`) — as
the **logged-in user**, so the assessment reflects what that user can see. The
policy lives in `sql_client.execute_sql`:

1. `force_sp=True` → **SP-only override** (no OBO attempt); used for reads the OBO
   token can't do — the Genie REST API (not covered by the `sql` scope) and
   Lakebase credential minting.
2. Otherwise, when a forwarded token is present, the read runs **OBO**; if it
   fails (e.g. the viewer lacks a system-table grant the SP holds) it
   **transparently falls back to the SP** rather than dropping the signal —
   except reads of a scope the **user explicitly picked** (catalog/schema
   selection), which use `allow_sp_fallback=False` so the SP can't leak metadata
   the viewer has no grant on.
3. With no forwarded token (OBO disabled, scheduled/unattended, local dev), the
   read runs as the **SP** directly.

**Deploy-time override:** set `FORCE_SP=true` (env in `app.yml`, or
`export FORCE_SP=true` before `post_deploy.py`) to force **SP-only mode** — the
app never attempts OBO and runs every read as the service principal, even when
user authorization is enabled. Use it to guarantee consistent workspace-wide
system-table signals (e.g. Adoption) independent of the viewer's grants; the SP
must then hold the read grants. Default is `false` (OBO-first with SP fallback).

So the SP still needs the grants below to cover the unattended path and the
fallback; under OBO with a fully-granted viewer the SP need not be granted.

Enabling the scope: `user_api_scopes: [sql]` is declared in `databricks.yml`, but
`bundle deploy` only applies it on app **create** — on an already-created app set it
with `databricks apps update <name> --json '{"name":"<name>","user_api_scopes":["sql"],
"resources":[...]}'` (preserve the warehouse resource). `dashboards.genie` etc. are
NOT declared, so the app never calls the Genie REST API on the user's behalf — the
Genie pillar is counted from `system.access.audit` only. `BROWSE` on a catalog is the
least-privilege grant for metadata-only visibility (viewer or SP).

`scripts/setup_app_permissions.py` (called by `post_deploy.py`) grants the SP:

- `USE CATALOG`/`USE SCHEMA`/`SELECT` on `system.information_schema`, `system.access`, `system.query`
- `USE CATALOG` + `BROWSE` on each assessed catalog (metadata only — no data access)

It exits non-zero if any grant fails, and `post_deploy.py` then ends with
`RESULT: PUBLISHED WITH ERRORS` (exit 3); a failed deployment exits 2.

For an **org-wide** assessment, an account admin can instead grant at the metastore
level. If the SP can't read `system.information_schema`, the app automatically
falls back to reading each accessible catalog's own `information_schema` (only
`USE CATALOG` + `BROWSE` is needed). Pillars whose data the SP can't read degrade
gracefully to "not available" plus a self-assessment fallback — they never crash
the app.

**Genie Agents (pillar 5):** scored from `system.access.audit` only: agents
seen in the last 90 days, and how many were active in the last 30. Per-agent
curation (instructions, example SQL, benchmarks) is **not** scored; that needs
`CAN_EDIT` on each agent and is left to the self-assessment.

## Architecture

```
app/
  app.py                     — FastAPI entry (port from DATABRICKS_APP_PORT)
  app.yml.template           — env template; post_deploy.py renders app.yml
  server/
    config.py                — SP/local auth, host resolution, env config
    sql_client.py            — Statement Execution API
    genie_client.py          — Genie Conversation API (answer-quality test)
    lakebase_client.py       — optional snapshot persistence pool
    snapshots.py             — optional assessment history (Lakebase)
    pillars.py               — canonical 7 readiness pillars + maturity model
    assessment/
      probes.py              — read-only SQL/REST probes (graceful degradation)
      scoring.py             — combine technical + self-assessment → scorecard
    content/library.py       — embedded technical+business enablement content
    content/accelerators.py  — runnable accelerators that lift a pillar's score
    content/methodology.py   — build methodology (5 phases); grounds the Plan LLM
    security.py              — log redaction, error sanitization, identifier quoting, per-user principal
    workspace_filter.py      — request-scoped workspace_id predicate for system-table signals
    bindings.py              — catalog↔workspace binding resolution (UC REST)
    assessment/compare.py    — diff two saved assessments
    routes/
      config.py  assess.py  catalogs.py  workspaces.py  content.py  plan.py  genie.py  _shared.py
  accelerators/              — downloadable Databricks notebooks (excluded from ruff; dbutils/spark globals)
  frontend/                  — React 19 + Vite + Tailwind v4 (3 tabs: Assess/Plan/Learn)
notebooks/
  install.py                 — workspace notebook installer (no CLI / Node.js needed)
scripts/
  post_deploy.py             — render app.yml, grant SP, publish
  setup_app_permissions.py   — UC read grants for the app SP
databricks.yml               — DABs: app + sql-warehouse resource
```

## Readiness pillars (each scored 0-4)

Unity Catalog Foundation · Metadata Richness · Relationships & Modeling ·
Metrics · Genie Agents · Domains & Stewardship ·
Adoption & Activity. The overall score maps to a generic, engagement-neutral
readiness tier (Foundation building, Core foundation in place, Semantics and
Genie forming, Curated and validating, Ontology-ready), and the next-step
guidance is derived from the customer's actual pillar gaps rather than a fixed
per-tier agenda.

## Quality gate (there is no CI)

GitHub Actions is disabled at the org level — `scripts/check.sh` **is** the CI. Run it before pushing:

```bash
bash scripts/check.sh                          # frontend build+tsc, vitest, build assertion, ruff, pytest, compliance scan
GOR_VERIFY_DOC_LINKS=1 bash scripts/check.sh   # + network doc-link check (release only)
```

Individual pieces:

```bash
pytest -q                                              # collects tests/ and app/server/ (pythonpath=app)
pytest tests/test_pillars.py::test_name -q             # single test
ruff check app scripts                                 # lint: only E9 + F (pyflakes/syntax), line-length 120
cd app/frontend && npm test                            # vitest; tests live in app/frontend/__tests__/
bash scripts/__tests__/compliance_scan.test.sh         # secrets / raw-data scan — must pass
```

Backend tests sit next to code (`app/server/test_*.py`, `app/server/assessment/test_*.py`) as well as in
`tests/` (import-light suites). They import runtime deps, so `pip install -r app/requirements.txt pytest`.

## Local development

```bash
# Backend (uses your CLI profile)
cd app
export DATABRICKS_CLI_PROFILE=<your-profile>
export DATABRICKS_WAREHOUSE_ID=<warehouse_id>
pip install -r requirements.txt
python app.py            # http://localhost:8000

# Frontend (proxies /api → :8000)
cd app/frontend && npm install && npm run dev   # http://localhost:3000
```

## Optional: snapshot history (Lakebase)

To trend readiness over time, add a Lakebase instance in `databricks.yml`
(`database_instances` + a `lakebase-db` app resource, see the commented block),
set `USE_LAKEBASE=true` before `post_deploy.py`, and uncomment `PGHOST`/`PGDATABASE`
in `app.yml.template`. The app creates its snapshot table on startup.

## Branding for a customer

Apply the customer's brand per the `customer-branding/` convention: set
`BRAND_NAME`, and update the `--color-databricks-*` scale in the `@theme` block of
`app/frontend/src/index.css` to the customer's primary/secondary colors
before `npm run build`. Default is Databricks brand.

## Common issues

| Issue | Fix |
|-------|-----|
| Pillars show "not available" | Grant the app SP read access — re-run `scripts/setup_app_permissions.py`. |
| Assessment empty | Confirm `system.information_schema` is enabled and the SP has SELECT on target catalogs. |
| AI plan / chat returns an error | The workspace must have Foundation Model API / model serving enabled; check the selected model exists. |
| Metric views show 0 / "not available" | Metric view metadata depends on UC version; the self-assessment covers the semantic layer regardless. |
| App won't start | Ensure the app binds `DATABRICKS_APP_PORT`; `DATABRICKS_HOST` is scheme-less (handled in `config.py`). |
| Frontend not served | Run `npm run build` so `app/frontend/dist` exists before publishing. |
```
