# ATS Platform Implementation Playbook: Phase-Wise Prompt Sequence
## File: `task.md`

> **HOW TO USE THIS PLAYBOOK:**
> This document contains the exact, copy-pasteable execution prompts for each phase of building the **AI-Native Applicant Tracking System (ATS) SaaS**.
> Each prompt assigns an operational agent role (**L1 to L5**), specifies the exact technical scope, references [project.md](project.md) for architecture, and strictly enforces the rules in [antigravity.md](antigravity.md).
>
> **CORE CONSTRAINTS REMINDER:**
> * **Backend Framework:** **FastAPI ONLY** (No Django, No Flask).
> * **Testing:** **NO Web/Browser Testing**. Always execute verification via **Python test scripts (`py script test`)** through the **L5 HR Tester** persona.
> * **Microservice Boundary:** The M3 Resume Extractor is an external service accessed strictly via HTTP REST / Webhooks.

---

## Roadmap Index
* [Phase 1: Foundation, Multi-Tenant Database, RLS & Authentication](#phase-1-foundation-multi-tenant-database-rls--authentication)
* [Phase 2: Generative Job Requisitions, Taxonomy Extractor & Career Dropzone](#phase-2-generative-job-requisitions-taxonomy-extractor--career-dropzone)
* [Phase 3: M3 Extractor Webhook Integration & Deterministic Kanban Pipeline](#phase-3-m3-extractor-webhook-integration--deterministic-kanban-pipeline)
* [Phase 4: Candidate Dossier (Split-Screen) & Team Collaboration Feed](#phase-4-candidate-dossier-split-screen--team-collaboration-feed)
* [Phase 5: Semantic Talent Pool (Vector RAG) & Conversational Sourcing](#phase-5-semantic-talent-pool-vector-rag--conversational-sourcing)
* [Phase 6: Automated Pre-Screening & Auditing Engine](#phase-6-automated-pre-screening--auditing-engine)
* [Phase 7: Calendar Sync, Delayed Rejection Queue & Full Funnel Polish](#phase-7-calendar-sync-delayed-rejection-queue--full-funnel-polish)

---

## Phase 1: Foundation, Multi-Tenant Database, RLS & Authentication

### Prompt 1.1: Project Scaffolding & Environment Setup
```markdown
Act as L1 Senior Project Architect and L2 Project Manager. Take a look at project.md and keep antigravity.md strictly in mind.

We are starting Phase 1 of the ATS SaaS Platform.
Your task is to set up the clean project directory structure and environment files:
1. Create the repository layout:
   - `backend/` for the FastAPI service (runs on port 8001)
   - `frontend/` for the Next.js 15 web UI (runs on port 3000)
   - `tests/` for automated Python test scripts
2. Create `docker-compose.yml` for local containerized infrastructure:
   - `postgres`: image `pgvector/pgvector:pg16`, port 5432:5432, persistent volume, with default DB `ats_db`.
   - `redis`: image `redis:7-alpine`, port 6379:6379.
3. Initialize the Python backend environment with `pyproject.toml` / `requirements.txt`:
   - Dependencies: `fastapi`, `uvicorn[standard]`, `pydantic>=2.0`, `pydantic-settings`, `sqlalchemy[asyncio]>=2.0`, `asyncpg`, `pgvector`, `redis`, `arq`, `python-jose[cryptography]`, `httpx`, `pytest`, `pytest-asyncio`, `fastembed`.
4. Create `backend/app/core/config.py` using `pydantic-settings` to manage:
   - Database URL (PostgreSQL + asyncpg: `postgresql+asyncpg://postgres:postgres@localhost:5432/ats_db`)
   - Redis URL (`redis://localhost:6379/0`)
   - Port allocations: ATS FastAPI (8001), Next.js (3000), M3 Extractor (`http://localhost:8000`)
   - Clerk configuration: `CLERK_JWKS_URL`, `CLERK_ISSUER` (with mock-token bypass in test environment)
   - Local Ollama URL (`http://localhost:11434`) and model (`llama3.2:3b`)
   - Local file storage path (`./storage/uploads`) and S3/R2 placeholders for production
   - CORS origins and allowed hosts
5. Verify configuration loading with a simple test script.

Strict constraints:
- Backend must be FastAPI only (no Django, no Flask).
- No web testing.
- When done, report the directory layout and await confirmation.
```

### Prompt 1.2: Multi-Tenant Database Engine & Row-Level Security (RLS)
```markdown
Act as L3 Senior Backend Engineer. Take a look at Section 5 in project.md and keep antigravity.md in mind.

Your task is to implement the asynchronous database engine and core tenant isolation layer:
1. In `backend/app/core/database.py`, configure:
   - `create_async_engine` with connection pooling.
   - `async_sessionmaker` with `expire_on_commit=False`.
   - A dependency `get_db()` that extracts the `tenant_id` from the request context and executes `SET LOCAL app.current_tenant_id = :tenant_id` on the database session before executing queries.
2. In `backend/app/models/`, create SQLAlchemy 2.0 Async declarative models for:
   - `Tenant` (id, name, subdomain, custom_domain, logo_url, primary_color, settings_json)
   - `User` (id, tenant_id, email, full_name, role [Enum: admin, recruiter, hiring_manager, interviewer], department, is_active)
3. Set up the migration runner (Alembic) configured for async migrations, including enabling the `uuid-ossp` and `vector` extensions.
4. Apply the Row-Level Security (RLS) policies as specified in Section 5 of project.md.
```

### Prompt 1.3: Multi-Tenant Authentication (Clerk JWKS + RBAC Middleware)
```markdown
Act as L3 Senior Backend Engineer. Review Module 1 in project.md and keep antigravity.md in mind.

Your task is to build the multi-tenant authentication and Role-Based Access Control (RBAC) system in FastAPI:
1. In `backend/app/core/security.py`, implement JWT verification using Clerk's JWKS public keys:
   - Validates Bearer token signature against Clerk JWKS.
   - Extracts `org_id` (mapped to `tenant_id`), `sub` (user_id), and `org_role` (mapped to `user_role`).
   - Includes local mock-token verification mode for offline headless Python test scripts.
2. In `backend/app/api/v1/endpoints/auth.py`, build endpoints:
   - `POST /api/v1/auth/sync-user`: Webhook or endpoint to sync Clerk user/organization into PostgreSQL `tenants` and `users`.
   - `GET /api/v1/auth/me`: Returns current authenticated user, role, and tenant workspace profile.
3. In `backend/app/api/deps.py`, create reusable FastAPI dependencies:
   - `get_current_user`: Validates JWT, verifies tenant context, and returns active user.
   - `require_role(allowed_roles: list[UserRole])`: Enforces role-based permissions (Admin, Recruiter, Hiring Manager, Blind Interviewer) and raises HTTP 403 Forbidden on violations.
```

### Prompt 1.4: Phase 1 Verification & Tenant Isolation Testing
```markdown
Act as L5 HR Tester. Review antigravity.md testing directives and project.md.

Strict Testing Mandate: DO NOT do web testing. ALWAYS do py script test.
Your task is to create and run `tests/test_phase1_auth_tenant.py` to verify:
1. Tenant provisioning succeeds and sets up an isolated workspace.
2. Admin, Recruiter, Hiring Manager, and Interviewer logins generate valid JWTs with correct role claims.
3. Cross-Tenant Leak Test: User from Tenant A attempts to access records from Tenant B and receives HTTP 403 or 404.
4. Role Permission Test: Interviewer attempts to access admin billing/settings and receives HTTP 403 Forbidden.
5. Execute the test using `pytest tests/test_phase1_auth_tenant.py -v` and output the test summary report.
```

---

## Phase 2: Generative Job Requisitions, Taxonomy Extractor & Career Dropzone

### Prompt 2.1: Generative Job Requisitions & Structured Taxonomy
```markdown
Act as L3 Senior Backend Engineer. Review Module 2 in project.md and keep antigravity.md in mind.

Your task is to build the Job Requisition module and AI taxonomy extraction engine:
1. In `backend/app/models/job.py`, implement models for `Job` and `PipelineStage`.
2. In `backend/app/schemas/job.py`, define Pydantic v2 schemas for job creation, update, and taxonomy representation (`required_skills`, `preferred_skills`, `min_years`).
3. In `backend/app/services/job_generator.py`, implement:
   - AI Job Description Generator: Accepts rough recruiter bullet points and produces a structured, inclusive markdown job description.
   - Taxonomy Extractor: Normalizes job skills into the exact JSON format required by the M3 Resume Extractor microservice (weights, years of experience, skill names).
4. In `backend/app/api/v1/endpoints/jobs.py`, implement:
   - `POST /api/v1/jobs/generate-description` (AI generator)
   - `POST /api/v1/jobs` (Create job and save extracted taxonomy)
   - `GET /api/v1/jobs` (List jobs filtered by tenant and status)
   - `GET /api/v1/jobs/{id}` and `PATCH /api/v1/jobs/{id}`
   - `GET /api/v1/public/jobs/{tenant_subdomain}/{slug}` (Public careers view with Schema.org JSON-LD metadata)
```

### Prompt 2.2: Candidate Ingestion Gateway Backend & Storage Pre-signing
```markdown
Act as L3 Senior Backend Engineer. Review Module 3 in project.md and keep antigravity.md in mind.

Your task is to build the backend ingestion gateway for the zero-friction candidate dropzone:
1. In `backend/app/models/candidate.py`, implement models for `Candidate` and `JobApplication`.
2. In `backend/app/services/storage.py`, implement S3/Cloudflare R2 storage client with `aioboto3`:
   - `generate_presigned_upload_url(file_name, mime_type, tenant_id)`: Generates secure pre-signed PUT URLs with 15-minute TTL.
   - `get_presigned_download_url(s3_key)`: Generates expiring GET URLs for candidate resume viewing.
3. In `backend/app/api/v1/endpoints/applications.py`, build:
   - `POST /api/v1/public/applications/presign-upload`: Returns pre-signed upload URL (no login required for candidates).
   - `POST /api/v1/public/applications/submit`: Records application submission with S3 key, creates candidate record, and sets status to `pending_extraction`.
```

### Prompt 2.3: Zero-Friction Career Portal & Dropzone UI
```markdown
Act as L4 Frontend Engineer. Review Module 3 in project.md and keep antigravity.md in mind.

Your task is to build the Public Career Portal and Zero-Friction Resume Dropzone:
1. Build the public job listing and job detail pages (`/careers/[subdomain]/[slug]`).
2. Build the Drag-and-Drop Resume Dropzone component:
   - Accepts PDF and DOCX files (max 15MB) with client-side magic-byte verification.
   - Requests pre-signed upload URL from the FastAPI backend and uploads the file directly to S3/R2 storage (displaying a progress bar).
   - Fires `POST /api/v1/public/applications/submit` and opens a real-time SSE listener for extraction progress.
3. Design a sleek 5-second candidate confirmation modal:
   - When parsed data arrives from the extraction webhook, display pre-filled fields (Full Name, Email, Phone, LinkedIn, Skills) for quick candidate verification before final confirmation.
4. Maintain a modern, accessible aesthetic with responsive styling and micro-animations.
```

### Prompt 2.4: Phase 2 Verification & Candidate Ingestion Testing
```markdown
Act as L5 HR Tester. Review antigravity.md testing directives.

Strict Testing Mandate: DO NOT do web testing. ALWAYS do py script test.
Your task is to create and run `tests/test_phase2_jobs_ingestion.py` to verify:
1. Recruiter creates a job with AI-assisted description and validates that the extracted taxonomy contains `required_skills` and `preferred_skills`.
2. Public job endpoint returns properly formatted job details with Schema.org JSON-LD data.
3. Candidate dropzone flow: Requests pre-signed S3 URL, submits application payload, and validates that a `JobApplication` with status `pending_extraction` is stored in PostgreSQL.
4. Execute via `pytest tests/test_phase2_jobs_ingestion.py -v` and document results.
```

---

## Phase 3: M3 Extractor Webhook Integration & Deterministic Kanban Pipeline

### Prompt 3.1: M3 Extractor Queue Client & HMAC Webhook Receiver
```markdown
Act as L3 Senior Backend Engineer. Review Section 6 in project.md and keep resumeExtractor.md and antigravity.md in mind.

Your task is to build the integration client and webhook listener connecting the ATS with the local M3 Resume Extractor microservice:
1. In `backend/app/workers/extraction_worker.py`, set up an ARQ / Redis async task queue:
   - Outbound worker concurrency rate-limited to match the M3 service's `asyncio.Semaphore(1)` hardware lock.
   - Dispatches `POST /api/v1/resumes/extract` to the M3 service with file binary/URL, job taxonomy JSON, and webhook callback URL (`https://api.ats.com/api/v1/webhooks/extractor`).
   - Handles `HTTP 202 Accepted` and records `extractor_tracking_id`.
   - Includes exponential backoff retry on timeouts or HTTP 503 responses.
2. In `backend/app/api/v1/endpoints/webhooks.py`, implement:
   - `POST /api/v1/webhooks/extractor`: Validates incoming `X-Webhook-Signature` (HMAC-SHA256).
   - Updates `Candidate` profile with parsed JSON (work experience, education, verified skills).
   - Updates `JobApplication` with `deterministic_match_score` (0.00 - 100.00) and `match_breakdown_json`.
   - Broadcasts an SSE/WebSocket event to connected recruiter clients with the new candidate score and status.
```

### Prompt 3.2: Match-Scored Interactive Kanban Pipeline Backend
```markdown
Act as L3 Senior Backend Engineer. Review Module 4 in project.md and keep antigravity.md in mind.

Your task is to build the Kanban pipeline endpoints in FastAPI:
1. In `backend/app/api/v1/endpoints/pipeline.py`, implement:
   - `GET /api/v1/jobs/{job_id}/pipeline`: Returns all pipeline stages and candidate cards. Crucially, candidates in the "Applied" stage must be sorted by `deterministic_match_score DESC NULLS LAST`.
   - `PATCH /api/v1/applications/{application_id}/move-stage`: Updates candidate stage with optimistic concurrency checks, logs the change in `audit_logs`, and triggers automated stage actions (e.g., screening triggers).
   - `GET /api/v1/jobs/{job_id}/pipeline/stream`: Server-Sent Events (SSE) endpoint streaming real-time candidate card movements and incoming extraction updates.
```

### Prompt 3.3: Interactive Kanban Board UI with Score Sorting
```markdown
Act as L4 Frontend Engineer. Review Module 4 in project.md and keep antigravity.md in mind.

Your task is to build the recruiter Kanban Pipeline Dashboard:
1. Implement the drag-and-drop Kanban board using `@dnd-kit/core` and `@dnd-kit/sortable`.
2. Pipeline Columns: Applied, Tech Screen, Pre-Screen, Interview, Offer, Hired, Rejected.
3. Candidate Card Component:
   - Candidate Name and Target Role.
   - Prominent **Deterministic Match Score Badge** (e.g., `94/100` colored dynamically: Green >= 85, Yellow 70-84, Red < 70).
   - **Match Breakdown Chips**: Visual tags showing Matched Core Skills (🟢), Preferred Skills (🟡), and Missing Gaps (🔴).
   - Real-time pulse animation when card updates via SSE.
4. Support drag-and-drop movement across columns with optimistic UI updates and error rollback.
```

### Prompt 3.4: Phase 3 Verification & M3 Webhook End-to-End Testing
```markdown
Act as L5 HR Tester. Review antigravity.md testing directives.

Strict Testing Mandate: DO NOT do web testing. ALWAYS do py script test.
Your task is to create and run `tests/test_phase3_webhook_kanban.py` to verify:
1. Mock the M3 Extractor microservice callback: Generate an HMAC-signed webhook payload containing parsed candidate profile and deterministic match score (e.g. 94.0).
2. Post to `/api/v1/webhooks/extractor` and verify signature verification succeeds and database records are updated.
3. Submit 5 mock candidates with varying match scores (e.g. 95, 88, 72, 60, 45) and query `GET /api/v1/jobs/{job_id}/pipeline`. Verify that candidates in the "Applied" column are strictly ranked in descending order of deterministic match score.
4. Move a card from "Applied" to "Tech Screen" and verify stage transition and audit log creation.
5. Execute via `pytest tests/test_phase3_webhook_kanban.py -v` and document results.
```

---

## Phase 4: Candidate Dossier (Split-Screen) & Team Collaboration Feed

### Prompt 4.1: Candidate Dossier, Notes & Blind Scorecard Backend
```markdown
Act as L3 Senior Backend Engineer. Review Module 4 and Module 7 in project.md and keep antigravity.md in mind.

Your task is to build the backend services for the deep candidate dossier and team collaboration:
1. In `backend/app/models/collaboration.py`, implement models for:
   - `CandidateNote` (application_id, author_id, content_markdown, mentions_json)
   - `Scorecard` (interview_id, application_id, evaluator_id, overall_recommendation, scores_json, notes_markdown, is_submitted)
2. In `backend/app/api/v1/endpoints/candidates.py`, implement:
   - `GET /api/v1/applications/{id}/dossier`: Returns full candidate profile, original resume pre-signed URL, match breakdown, and stage history.
   - Blind Review Enforcement: If current user has the `interviewer` role and blind mode is active for this stage, redact PII (candidate full name, photo, graduation year, address) and only return anonymized skill/experience data.
3. In `backend/app/api/v1/endpoints/collaboration.py`, implement:
   - `POST /api/v1/applications/{id}/notes`: Adds a note with `@mention` parsing and in-app notifications.
   - `POST /api/v1/applications/{id}/scorecards`: Submits an interview scorecard.
   - Blind Scorecard Rule: An interviewer cannot see scorecards submitted by other interviewers until their own scorecard has `is_submitted = True`.
```

### Prompt 4.2: Split-Screen Candidate Dossier Modal UI with PDF.js
```markdown
Act as L4 Frontend Engineer. Review Module 4 in project.md and keep antigravity.md in mind.

Your task is to build the Split-Screen Candidate Dossier Modal:
1. Modal Layout: 50/50 desktop split-screen view:
   - **Left Panel (Structured Candidate Dossier):**
     - Candidate Header: Name, Contact info, LinkedIn/GitHub links, Deterministic Score badge.
     - Match Breakdown Section: Detailed visual cards explaining why the candidate scored what they did against the JD taxonomy.
     - Chronological Work Experience timeline and verified Education.
     - Internal Notes feed with markdown support and `@mention` autocomplete.
     - Scorecards Tab: Displaying team ratings (respecting blind review rules).
   - **Right Panel (High-Fidelity Document Viewer):**
     - Embedded Mozilla `PDF.js` / `react-pdf` rendering the original resume directly from the pre-signed URL.
     - Page navigation, zoom in/out, text search, and full-screen toggle.
2. Ensure responsive handling: Collapses into toggleable tabs on smaller screens.
```

### Prompt 4.3: Phase 4 Verification & Dossier Collaboration Testing
```markdown
Act as L5 HR Tester. Review antigravity.md testing directives.

Strict Testing Mandate: DO NOT do web testing. ALWAYS do py script test.
Your task is to create and run `tests/test_phase4_dossier_collaboration.py` to verify:
1. Querying `/api/v1/applications/{id}/dossier` as a Recruiter returns full PII, match breakdown, and pre-signed resume URL.
2. Querying the same endpoint as a Blind Interviewer returns redacted candidate profile data.
3. Interviewer A submits a scorecard. Interviewer B attempts to view Interviewer A's scorecard before submitting their own and is blocked. Once Interviewer B submits, both scorecards become visible.
4. Internal note with `@mention` is successfully created and stored with audit logs.
5. Execute via `pytest tests/test_phase4_dossier_collaboration.py -v` and output results.
```

---

## Phase 5: Semantic Talent Pool (Vector RAG) & Conversational Sourcing

### Prompt 5.1: pgvector Embedding Pipeline & Hybrid Search
```markdown
Act as L3 Senior Backend Engineer. Review Module 5 in project.md and keep antigravity.md in mind.

Your task is to implement the Semantic Talent Pool using PostgreSQL `pgvector`:
1. In `backend/app/models/candidate.py`, configure the `CandidateEmbedding` model with `vector(384)` and an IVFFlat index.
2. In `backend/app/services/embedding_service.py`, build:
   - `synthesize_candidate_profile(candidate, parsed_profile)`: Formats skills, role titles, and project highlights into a dense textual synthesis.
   - `generate_embedding(text)`: Generates 384-dimensional vector embedding using local FastEmbed (`BAAI/bge-small-en-v1.5`).
3. In `backend/app/api/v1/endpoints/talent_pool.py`, implement:
   - `POST /api/v1/talent-pool/search`: Hybrid search combining `pgvector` cosine similarity (`<=>`) with PostgreSQL full-text search (`tsvector` BM25). All queries must enforce `tenant_id` Row-Level Security.
   - Returns matched past candidates, similarity scores, and highlights.
```

### Prompt 5.2: Proactive Candidate Resurfacing Engine
```markdown
Act as L3 Senior Backend Engineer. Review Module 5 in project.md and keep antigravity.md in mind.

Your task is to implement the automated applicant resurfacing engine:
1. In `backend/app/services/resurfacing_service.py`, implement:
   - `scan_talent_pool_for_new_job(job_id, tenant_id, threshold=0.85)`: When a new job is published, embeds the job description and queries the tenant's past candidate pool.
   - Generates notifications for the recruiter: *"Found X past applicants with >85% match for your new role."*
2. In `backend/app/api/v1/endpoints/talent_pool.py`, implement:
   - `POST /api/v1/talent-pool/candidates/{id}/invite-to-job`: 1-click association of a past candidate with a new job application in the `Applied` column.
```

### Prompt 5.3: Talent Pool Search UI & Resurfacing Alerts
```markdown
Act as L4 Frontend Engineer. Review Module 5 in project.md and keep antigravity.md in mind.

Your task is to build the Conversational Sourcing UI:
1. Sourcing Search Bar: Recruiter enters natural language query (*"Find engineers with 5+ years Go and distributed consensus experience"*).
2. Results Grid: Displays matching past candidates, semantic similarity percentage badge, past roles applied for, and matched skill highlights.
3. 1-Click Action: "Invite to New Role" modal allowing the recruiter to select an active job and trigger an email re-engagement workflow.
4. Resurfacing Banner on Job Dashboard: Displays proactive alert cards highlighting existing high-match talent.
```

### Prompt 5.4: Phase 5 Verification & Vector Sourcing Testing
```markdown
Act as L5 HR Tester. Review antigravity.md testing directives.

Strict Testing Mandate: DO NOT do web testing. ALWAYS do py script test.
Your task is to create and run `tests/test_phase5_talent_pool.py` to verify:
1. Seed 10 candidate profiles with distinct technology stacks (e.g. Go backend, React frontend, DevOps, Python ML).
2. Generate embeddings and verify insertion into `candidate_embeddings` with `pgvector`.
3. Execute natural language query: *"Find me engineers skilled in distributed systems and Go"*. Assert that Go backend candidates rank at the top with similarity scores > 0.85.
4. Publish a new job and trigger the resurfacing scan. Verify that notification records are generated for matching past candidates.
5. Execute via `pytest tests/test_phase5_talent_pool.py -v` and document results.
```

---

## Phase 6: Automated Pre-Screening & Auditing Engine

### Prompt 6.1: Dynamic Gap-Question Generator & Tokenized Screening Service
```markdown
Act as L3 Senior Backend Engineer. Review Module 6 in project.md and keep antigravity.md in mind.

Your task is to build the Dynamic Pre-Screening backend:
1. In `backend/app/models/screening.py`, implement the `Screening` model (application_id, access_token, status, expires_at, questions_json, responses_json, ai_evaluation_json).
2. In `backend/app/services/screening_generator.py`, implement:
   - Analyzes candidate's `match_breakdown_json` against the Job Description taxonomy.
   - Identifies specific missing required skills or transition areas.
   - Uses structured AI generation to produce 3 targeted, contextual pre-screening questions (e.g. assessing how a React dev approaches Vue 3 state management).
3. In `backend/app/api/v1/endpoints/screenings.py`, implement:
   - `POST /api/v1/screenings/trigger/{application_id}`: Generates unique 72-hour signed access token and queues screening invite email.
   - `GET /api/v1/public/screenings/{token}`: Public candidate access endpoint returning the bespoke screening questions.
```

### Prompt 6.2: Screening Evaluation Engine & Non-Autonomous Rubric Scorecard
```markdown
Act as L3 Senior Backend Engineer. Review Module 6 in project.md and keep antigravity.md in mind.

Your task is to build the screening response submission and rubric evaluation service:
1. In `backend/app/api/v1/endpoints/screenings.py`, implement:
   - `POST /api/v1/public/screenings/{token}/submit`: Accepts candidate text answers or audio recording uploads (saved to local uploads/S3).
2. In `backend/app/services/screening_evaluator.py`, build:
   - Audio transcription via `faster-whisper` (`base.en` model running locally on-device).
   - Rubric Evaluation: Evaluates answers against an objective rubric (Technical Depth 1-5, Clarity 1-5, Relevance 1-5) using local Ollama (`llama3.2:3b`).
   - Generates an **Advisory-Only Scorecard**: Summary score, key highlights, probe questions for live interviewers, and verbatim transcripts.
   - Enforce Anti-Bias Guardrail: Pre-screen evaluation **never** auto-rejects a candidate; it flags the scorecard for human recruiter review.
```

### Prompt 6.3: Candidate Asynchronous Screening Portal UI
```markdown
Act as L4 Frontend Engineer. Review Module 6 in project.md and keep antigravity.md in mind.

Your task is to build the Candidate Asynchronous Screening Web Portal (`/screen/[token]`):
1. Clean, mobile-friendly interface requiring no logins or software downloads.
2. Step-by-step question carousel with timer per question.
3. Dual Response Modes:
   - Rich text input editor.
   - In-browser Audio/Video recorder using the HTML5 `MediaRecorder` API with countdown timer and preview playback.
4. Submission confirmation screen thanking the candidate and explaining next steps.
```

### Prompt 6.4: Phase 6 Verification & Screening Engine Testing
```markdown
Act as L5 HR Tester. Review antigravity.md testing directives.

Strict Testing Mandate: DO NOT do web testing. ALWAYS do py script test.
Your task is to create and run `tests/test_phase6_screening.py` to verify:
1. Candidate is moved to "Pre-Screen". Validate that 3 questions are generated dynamically based on missing skills from the match breakdown.
2. Verify token generation, expiration timestamp, and public endpoint retrieval.
3. Submit candidate responses and execute the evaluation pipeline.
4. Verify that the generated scorecard contains category ratings (1-5), key strengths, and that candidate status remains pending human review (no autonomous rejection).
5. Execute via `pytest tests/test_phase6_screening.py -v` and document results.
```

---

## Phase 7: Calendar Sync, Delayed Rejection Queue & Full Funnel Polish

### Prompt 7.1: Two-Way Calendar Sync & Candidate Self-Scheduling
```markdown
Act as L3 Senior Backend Engineer. Review Module 7 in project.md and keep antigravity.md in mind.

Your task is to implement interview calendar integration:
1. In `backend/app/services/calendar_service.py`, build OAuth integrations:
   - Google Calendar API and Microsoft Graph API (Outlook).
   - Availability calculation engine respecting recruiter buffer rules (e.g. minimum 15 min buffer, max 3 interviews/day).
2. In `backend/app/api/v1/endpoints/interviews.py`, implement:
   - `GET /api/v1/public/schedule/{token}/available-slots`: Returns available interview slots.
   - `POST /api/v1/public/schedule/{token}/book`: Books the slot, creates calendar event with meeting link (Google Meet / Teams), and saves `Interview` record.
```

### Prompt 7.2: Smart Delayed Rejection Queue & GDPR Article 17 Deletion
```markdown
Act as L3 Senior Backend Engineer. Review Module 7 and Section 8 in project.md and keep antigravity.md in mind.

Your task is to implement the polite rejection queue and enterprise compliance endpoints:
1. In `backend/app/workers/rejection_worker.py`, implement the **Polite Delay Queue**:
   - When a candidate is moved to "Rejected", queue the rejection email with a 36 to 48-hour delay timer during normal business hours.
   - Immediate rejections are strictly suppressed to maintain candidate dignity and brand reputation.
2. In `backend/app/api/v1/endpoints/compliance.py`, implement:
   - `DELETE /api/v1/compliance/candidates/{id}/forget` (GDPR Article 17): Cascades deletion of candidate records, applications, notes, scorecards, embeddings, and deletes S3 resume binaries.
   - Logs an anonymized entry in `audit_logs`.
```

### Prompt 7.3: Enterprise Settings & Calendar Scheduling UI
```markdown
Act as L4 Frontend Engineer. Review Module 7 in project.md and keep antigravity.md in mind.

Your task is to build the Enterprise Settings and Candidate Scheduling views:
1. Public Self-Scheduling Page (`/schedule/[token]`): Clean calendar date picker and time slot selector with instant confirmation.
2. Recruiter Workspace Settings Panel:
   - Custom stage pipeline editor (add, rename, reorder stages).
   - Email template editor with dynamic token pills (`{{candidate_name}}`, `{{job_title}}`).
   - Rejection delay timer configuration slider (e.g. 24h to 72h).
   - Team RBAC user management table with role assignments.
```

### Prompt 7.4: Phase 7 Verification & Complete Full-Funnel Acceptance Test
```markdown
Act as L5 HR Tester. Review antigravity.md testing directives.

Strict Testing Mandate: DO NOT do web testing. ALWAYS do py script test.
Your task is to create and run `tests/hr_scenarios/verify_full_funnel.py` simulating the entire hiring journey from end to end:
1. Provision tenant workspace and create job requisition with AI taxonomy.
2. Candidate drops resume (mock upload), webhook returns parsed profile and deterministic score (92%).
3. Recruiter verifies candidate card at top of Kanban Applied column.
4. Candidate moved to Pre-Screen: dynamic questions answered and rubric scorecard attached.
5. Self-scheduled interview booked with calendar slot.
6. Interviewer submits blind scorecard.
7. Offer generated.
8. Rejected candidate test: Move second candidate to Rejected, verify email is queued with delay timer and not sent immediately.
9. GDPR erasure test: Trigger Article 17 deletion and verify total removal from PostgreSQL and S3.
10. Execute via `python -m pytest tests/hr_scenarios/verify_full_funnel.py -v` and present the complete acceptance report.
```
