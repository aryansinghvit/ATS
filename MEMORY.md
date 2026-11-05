# MEMORY.md — ATS project handoff (read this first)

> Written 2026-11-05 when moving dev from a MacBook (Apple Silicon) to a Windows laptop.
> Auto-loaded via `CLAUDE.md`. Purpose: understand the project **without reading the whole codebase**.
> Open a source file only when the task touches it — use the code map in §6 to go straight there.
> Keep this file current: when you finish a meaningful change, update §2 (status) and anything that moved.

---

## 1. What this is
Multi-tenant, AI-native **Applicant Tracking System (B2B SaaS)**. Philosophy: *"Deterministic AI for
triage, humans for decisions"* — AI parses, scores, drafts, and suggests; it **never** moves, rejects, or
hires a candidate on its own. Screening rubrics are advisory-only.

- **Backend:** FastAPI (Python 3.11+, tested on 3.12), Pydantic v2, SQLAlchemy 2 async + asyncpg, Alembic,
  ARQ workers on Redis. Postgres 16 + **pgvector** + **Row-Level Security**.
- **Frontend:** Next.js 15.5 (App Router), React 19, Tailwind v4, dnd-kit (Kanban), react-pdf.
- **Local AI (no cloud AI):** Ollama `llama3.2:3b` (JD generation, screening questions, rubrics),
  FastEmbed `BAAI/bge-small-en-v1.5` (384-dim embeddings), faster-whisper `base.en` (voice answers).
  Every AI feature has a deterministic/heuristic fallback when the model is unavailable.
- **External dependency:** the **M3 Resume Extractor**, a *separate project* (MLX + Qwen3-VL on Apple Silicon,
  port 8000). It's only reachable over REST plus an HMAC-signed webhook. See `resumeExtractor.md` (11 lines).

| Service | Port |
|---|---|
| FastAPI backend | 8001 (docs at `/api/v1/docs`) |
| Next.js frontend | 3000 |
| M3 extractor (external) | 8000 |
| Postgres + pgvector | 5432 |
| Redis | 6379 |

## 2. Status (as of 2026-11-05)
- **All 7 roadmap phases from `task.md` are implemented**, with a headless pytest suite per phase.
  Git history: a single commit, `18e30c0` ("initial commit of complete AI-native ATS platform"), plus this handoff.
- **Built:** tenancy + RLS + Clerk JWT auth/RBAC · AI JD generator + taxonomy + Google Jobs JSON-LD ·
  no-login career site + resume dropzone + presigned upload + SSE extraction progress + confirm-profile ·
  M3 queue client + HMAC webhook + score-sorted Kanban with live SSE · split-screen dossier (PDF viewer,
  notes, blind scorecards, notifications) · semantic talent pool + hybrid search + proactive resurfacing ·
  AI gap-question screening portal (text/voice, autosave) + Whisper + advisory rubric · email templates +
  outbox + polite delayed rejection (business hours) · custom stage editor + per-stage automations ·
  team management · pipeline analytics · interviews (manual or Cal.com webhook) · offer-letter PDFs ·
  GDPR Art. 17 erasure · billing summary (stub: plan from `tenants.settings_json`, seat count).
- **Not built / deviates from spec** (likely next work):
  - Google/Microsoft calendar OAuth, availability engine, and the `/schedule/[token]` self-scheduling page
    (task.md 7.1/7.3) → **replaced by** manual interview entry + Cal.com booking webhook.
  - **Clerk frontend SDK isn't wired in.** The workspace uses a pasted dev token (`/workspace/session`,
    sessionStorage). The swap point is `frontend/src/lib/auth/session.ts → getAccessToken()`.
  - No job-creation UI in the frontend (jobs are created via the API: `POST /api/v1/jobs`,
    `POST /api/v1/jobs/generate-description`). The workspace home only lists jobs.
  - No LinkedIn/Indeed syndication (Google Jobs JSON-LD only) and no DocuSign (offers are generated PDFs).
  - No real billing/payments.
- **Tests:** not re-run on the handoff day. On the Mac, port 5432 was occupied by an unrelated Homebrew
  Postgres 17 (no pgvector), so the suite aborted at `CREATE EXTENSION vector`. Not a code bug.
  **First task on the new machine: get Postgres+pgvector up and run `pytest tests/` to establish a baseline.**

## 3. Hard rules (from `antigravity.md` — do not violate)
1. **Backend = FastAPI only.** No Django/Flask/Node backends. Async everywhere; no blocking calls in the event loop.
   Layering: **routers → services → repositories → models**.
2. **No browser/UI testing** (no Playwright/Selenium/browser agents). **All verification is headless Python**
   (`pytest` + `httpx.AsyncClient`) in `tests/`. HR acceptance scenarios go in `tests/hr_scenarios/verify_*.py`.
3. **M3 extractor is external.** Don't share code with it. Talk to it only over REST + webhook, and always
   go through the ARQ queue (it has a `Semaphore(1)` GPU lock; never call it inline from a request).
4. **Every tenant table has RLS.** Never bypass it, never connect the app as a superuser or table owner.
5. AI is advisory; humans make the decisions (see §1).

## 4. Architecture in 60 seconds
```
Browser ─► Next.js (3000) ─► FastAPI (8001) ─► Postgres (RLS, pgvector)
                                  │  └─► Redis: ARQ queues + pub/sub for Kanban SSE
                                  ├─► ARQ extraction_worker ──HTTP──► M3 extractor (8000)
                                  │        ▲                              │
                                  │        └──── POST /api/v1/webhooks/extractor (HMAC) ◄┘
                                  ├─► ARQ embedding_worker   (FastEmbed → candidate_embeddings, resurfacing)
                                  └─► ARQ communications_worker (email outbox cron/min, screening gen, Whisper+rubric)
```
- Workers (run from `backend/`):
  `arq app.workers.extraction_worker.WorkerSettings` (also cron `sweep_stalled_extractions` every 5 min),
  `…embedding_worker.WorkerSettings`, `…communications_worker.WorkerSettings` (cron `dispatch_emails` every minute).
- Real-time: `services/events.py`. `EVENT_BROKER_BACKEND=redis` (cross-process) or `memory` (tests/single process).
  SSE endpoints: `GET /jobs/{id}/pipeline/stream`, `GET /public/applications/{id}/events`.
- Storage: `services/storage.py`. `STORAGE_BACKEND=local|s3|r2`. The local driver serves HMAC-signed URLs
  via `/api/v1/storage/local/{key}` and stores files under `storage/uploads/`.

## 5. Multi-tenancy & auth (the most important invariants)
- RLS policy on every tenant table: `tenant_id = NULLIF(current_setting('app.current_tenant_id', true),'')::uuid`
  (builders in `core/rls.py`, applied by migrations). **Fail-closed:** no tenant bound ⇒ zero rows.
- `core/database.py`: the tenant lives in `session.info` and is re-applied with `set_config(..., true)`
  (= `SET LOCAL`) by an `after_begin` hook, so it survives `commit()`. At startup,
  `verify_runtime_role_enforces_rls` **refuses to run** if the DB role is a superuser, has BYPASSRLS, or owns tables.
- Two DB roles: **`DATABASE_URL` → `ats_app`** (restricted runtime role, created by migration 0001) and
  **`DATABASE_ADMIN_URL` → `postgres`** (Alembic only; `alembic/env.py` uses the admin URL).
- How a request gets its tenant (`api/deps.py`):
  - Authenticated: Clerk JWT → `org_id` → `tenants.clerk_org_id` → bind. Also cross-checked against the
    Host-header tenant if there is one. Use the deps `CurrentUser`, `TenantDB`, `require_role([...])`.
  - Public by Host header (`acme.<PLATFORM_ROOT_DOMAIN>` or custom domain, `core/tenancy.py`): `PublicTenantDB`.
  - Public by path `/careers/{subdomain}` / `/public/jobs/{tenant_subdomain}`: `PathTenantId`.
  - Signed tokens (screening portal, application status, local storage): `core/signing.py`
    (HMAC bound to a purpose string).
- Roles (`UserRole`): `admin`, `recruiter`, `hiring_manager`, `interviewer` (interviewer = blind view).
  **Clerk is the source of truth for roles**; the DB copy is synced on every request.
- First-time identity: `POST /api/v1/auth/sync-user` provisions the tenant and user from the JWT.
- **Dev auth:** `AUTH_MOCK_MODE=true` + `MOCK_JWT_SECRET` → mint a token with
  `python scripts/mint_dev_token.py --org org_demo --role recruiter`, then paste it at `/workspace/session`.

## 6. Code map — where to look
Backend root: `backend/app/`. Endpoints are in `api/v1/endpoints/`, registered in `api/v1/router.py`, prefix `/api/v1`.

| Area | Endpoint file | Service(s) |
|---|---|---|
| Auth / me / sync-user | `auth.py`, `users.py` | `auth_service.py`, `core/security.py` (Clerk JWKS + mock HS256) |
| Workspace settings/billing | `workspace.py`, `public.py` (`/public/tenant` branding) | `workspace_service.py` |
| Jobs + AI JD + public board | `jobs.py` (`router` + `public_router`) | `job_service.py`, `job_generator.py`, `job_seo.py`, `llm_client.py` |
| Apply (presign → submit → SSE → confirm) | `applications.py` (`/public/applications/*`) | `application_service.py`, `storage.py`, `extraction_queue.py` |
| M3 webhook | `webhooks.py` (`POST /webhooks/extractor`, header `X-Webhook-Signature`) | `webhook_service.py`, `m3_client.py`, `workers/extraction_worker.py` |
| Kanban | `pipeline.py` (board, `move-stage`, stream) | `pipeline_service.py`, `stage_actions.py` (on-enter automations), `events.py` |
| Stage editor, team, analytics, GDPR, interviews, Cal.com, offers | `admin.py` | `stage_service.py`, `admin_services.py`, `analytics_service.py`, `core/pdf.py` |
| Dossier | `candidates.py` (`/applications/{id}/dossier`) | `dossier_service.py` |
| Notes / scorecards / notifications | `collaboration.py` | `collaboration_service.py`, `notification_service.py` |
| Talent pool / resurfacing | `talent_pool.py` | `talent_pool_service.py`, `embedding_service.py`, `resurfacing_service.py`, `embedding_queue.py` |
| Screening (recruiter + public portal) | `screenings.py` (`router` + `/public/screenings/{token}`) | `screening_generator.py`, `screening_portal.py`, `screening_evaluator.py` |
| Email templates/outbox/rejection delay | `communications.py` | `communications.py` (service, 555 lines), `communications_queue.py` |

- Config: `core/config.py` (pydantic-settings; every env var with its default). Errors: `core/exceptions.py`
  (`ServiceError` subclasses → JSON `{detail}`).
- Models (`models/`): `tenant.py`, `user.py`, `job.py` (Job, PipelineStage), `candidate.py` (Candidate,
  JobApplication, CandidateEmbedding), `collaboration.py` (Scorecard, CandidateNote, Notification, Screening,
  Interview, EmailTemplate, EmailOutbox, JobTalentMatch), `audit.py`.
- Migrations `backend/alembic/versions/0001…0006` (foundation+RLS+role → jobs/candidates → extraction/audit →
  embeddings → collaboration/screening/comms → portal/interviews/offers). **New tenant tables need
  `enable_tenant_rls_sql()` and grants for `ats_app`.**
- Frontend `frontend/src/`:
  - `app/careers/[subdomain]/…`: public job board and job page.
  - `app/screen/[token]`: candidate screening portal.
  - `app/workspace/…`: jobs list, `jobs/[jobId]/pipeline`, `jobs/[jobId]/stages`, `talent-pool`, `analytics`,
    `team`, `settings/communications`, `session`.
  - Components: `components/{careers,pipeline,dossier,screening,workspace}`.
  - API clients in `lib/api/*.ts`. All DTO types in `lib/types.ts` (mirror the backend schemas).
  - SSE hooks: `lib/use-pipeline-stream.ts`, `lib/use-application-events.ts`.

## 7. Key domain facts
- `ApplicationStatus`: pending_extraction → applied → screening_invited → screening_completed →
  interview_scheduled → interview_completed → offered → hired | rejected | withdrawn.
- Default pipeline for every new job (`job_service.DEFAULT_PIPELINE`): Applied → Pre-Screen (`ai_pre_screen`)
  → Tech Screen → Interview (`interview_invitation_email`) → Offer (`offer_email`) → Hired | Rejected
  (`polite_rejection_email`). The parentheses are `automated_actions_json.on_enter` handlers in `stage_actions.py`;
  slow work goes to the workers. There is **exactly one "Applied" stage, it stays first**, and it can't be deleted or retyped.
- Kanban "Applied" column is sorted by `job_applications.deterministic_match_score` (comes from M3, 0–100).
- Rejection email: queued in `email_outbox` and delivered after `rejection_delay_hours` (tenant setting, 24–72, default 48), moved into
  business hours (Mon–Fri 09:00–17:00, tenant timezone). Cancelled if the candidate leaves Rejected.
  `EMAIL_BACKEND=log` only records emails; `smtp` delivers them.
- Screening tokens: single-use, 72 h TTL. Audio ≤ 10 MB / 90 s, text ≤ 4000 chars.
- Talent pool: pgvector HNSW (cosine) + GIN full-text, i.e. hybrid search. Resurfacing threshold 0.70, limit 20, `ef_search` 200.
- Max resume size: 15 MB (backend `MAX_RESUME_SIZE_BYTES` and frontend `lib/config.ts` must match).

## 8. Running on the Windows laptop
**Recommended: WSL2 (Ubuntu) + Docker Desktop.** Inside WSL, the README commands work unchanged
(`.venv/bin/...`, bash scripts). Keep the repo inside the WSL filesystem (`~/…`), not `/mnt/c`,
or file watching and installs get slow.

Native PowerShell also works, with these translations:
- `.venv/bin/X` → `.venv\Scripts\X.exe`.
- `scripts/local_postgres.sh` is bash-only, so use `docker compose up -d` (pgvector/pgvector:pg16 + redis:7).
- Setup: `uv venv --python 3.12 .venv` → `uv pip install --python .venv\Scripts\python.exe -r backend\requirements.txt`
  → `copy .env.example .env` (or bring the old `.env` over by hand; it's gitignored) →
  `cd backend; ..\.venv\Scripts\alembic upgrade head` → `..\.venv\Scripts\uvicorn app.main:app --port 8001 --reload`.
- Frontend: `cd frontend; copy .env.example .env.local; npm install; npm run dev`.

Windows gotchas:
- **The M3 extractor can't run on Windows** (needs MLX/Apple Silicon). Without it, uploaded resumes stay
  `pending_extraction` and the sweeper keeps retrying. Options: (a) run the extractor on the Mac and set
  `M3_EXTRACTOR_URL=http://<mac-lan-ip>:8000`, with `M3_WEBHOOK_URL` reachable from the Mac; (b) for tests
  you don't need it, because `tests/helpers.py` builds signed webhook envelopes.
- Ollama: install the Windows build, then `ollama pull llama3.2:3b`. FastEmbed (~66 MB) and Whisper (~140 MB)
  models download into `.cache/` on first use.
- `.gitattributes` forces LF line endings, so bash scripts and diffs stay clean. Don't "fix" CRLF churn by hand.
- If port 5432 is taken by another Postgres without pgvector, migrations and tests fail with
  `extension "vector" is not available`. Make sure the pgvector container owns 5432.
- ARQ workers rely on signal handlers. If they misbehave natively on Windows, run them in WSL2.

## 9. Testing
- `pytest tests/ -v` from the repo root (`pytest.ini`: `pythonpath=backend`, asyncio auto mode, collects `test_*.py` and `verify_*.py`).
- `tests/conftest.py` **drops and recreates `ats_test`** (refuses names not ending in `_test`), runs real
  Alembic, connects as `ats_app` so RLS is really exercised, and uses Redis DB 15. Override with
  `TEST_DATABASE_ADMIN_URL`, `TEST_DATABASE_URL`, `TEST_REDIS_URL`.
- `tests/helpers.py` is the toolbox: `provision_workspace`, `create_job`, `apply`, `m3_payload` + `post_webhook`
  (signed M3 envelope), `board`/`column`, `mint_token`, `Recording{Extraction,Embedding,Communications}Queue`,
  `FakeLLM`, `HashingEmbedder`, `FakeTranscriber`. The LLM is injected via `get_llm_client` (dependency override).
  **Reuse these helpers instead of writing new setup code.**
- Files: `test_phase1_*` … `test_phase7_*`, plus `hr_scenarios/verify_full_funnel.py` (apply → hire) and
  `verify_tenant_security.py` (cross-tenant probes).
- Lint: `ruff` (`ruff.toml`, line length 100, py311). Frontend: `npm run lint`.

## 10. Reference docs (read selectively — they're large)
- `project.md` (927 lines, master blueprint): §5 DB schema at **L381–665**, §6 M3 contract
  (request/webhook envelope/rate limiting) at **L666–814**, §8 security/GDPR/blind hiring at **L845–862**.
- `task.md` (452 lines): phase-by-phase prompt playbook. Phase 7 is at **L395+**.
- `plan.md`: product/feature spec per module. `antigravity.md`: agent rules and L1–L5 personas (summarised in §3).
- `README.md`: quick start and feature URLs.

## 11. Not in git (carry over manually if needed)
`.env` (real secrets), `frontend/.env.local` (just API URLs, so it can be rebuilt from the example), `.venv/`,
`node_modules/`, `.pgdata/` (local dev DB data), `.cache/` (re-downloadable models), `storage/uploads/tenants/`
(dev uploads), `.logs/`.
