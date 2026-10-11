# ATS SaaS Platform

Multi-tenant, AI-native Applicant Tracking System. Architecture: [project.md](project.md) ·
agent rules: [antigravity.md](antigravity.md) · phase playbook: [task.md](task.md).

| Service | Port |
| --- | --- |
| ATS FastAPI backend | 8001 |
| Next.js frontend | 3000 |
| M3 Resume Extractor (external) | 8000 |
| PostgreSQL 16 + pgvector | 5432 |
| Redis 7 | 6379 |

## Quick start

```bash
# 1. Infrastructure
docker compose up -d                      # or, without Docker: scripts/local_postgres.sh start

# 2. Backend environment (Python 3.11+)
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r backend/requirements.txt
cp .env.example .env

# 3. Migrations (creates tables, RLS policies and the restricted `ats_app` runtime role)
cd backend && ../.venv/bin/alembic upgrade head && cd ..

# 4. Run the API
cd backend && ../.venv/bin/uvicorn app.main:app --port 8001 --reload

# 5. Run the extraction worker (ARQ; feeds resumes to the M3 extractor on :8000)
cd backend && ../.venv/bin/arq app.workers.extraction_worker.WorkerSettings

# 6. Run the embedding worker (FastEmbed bge-small, on-device; semantic talent pool)
cd backend && ../.venv/bin/arq app.workers.embedding_worker.WorkerSettings

# 7. Run the communications worker (emails, AI pre-screen questions, Whisper transcription + rubric)
cd backend && ../.venv/bin/arq app.workers.communications_worker.WorkerSettings

# 8. Run the frontend (Next.js 15)
cd frontend && cp .env.example .env.local && npm install && npm run dev
```

- API docs: http://localhost:8001/api/v1/docs
- Career site: http://localhost:3000/careers/&lt;company-subdomain&gt;
- Recruiter Kanban: http://localhost:3000/workspace (dev token: `.venv/bin/python scripts/mint_dev_token.py --org <org_id> --role recruiter`)
- Talent pool search: http://localhost:3000/workspace/talent-pool (first use downloads the
  ~66 MB bge-small ONNX model into `.cache/fastembed`, or set `EMBEDDING_MODEL_PATH`)
- Candidate email templates: http://localhost:3000/workspace/settings/communications
  (`EMAIL_BACKEND=log` records emails in the outbox; set `smtp` + `SMTP_*` to deliver)
- AI job descriptions use local Ollama: `ollama serve` and `ollama pull llama3.2:3b`.
- Candidate screening portal: `http://localhost:3000/screen/<signed-token>` (no login, single
  use, 72 h). Recruiters send it from the dossier's **AI screening** tab; candidates answer by text
  or browser-recorded voice (autosaved). The communications worker transcribes voice answers with
  faster-whisper (`base.en`, first use downloads ~140 MB into `.cache/whisper`, or set
  `WHISPER_MODEL_PATH`) and attaches an **advisory-only** rubric (LLM, or a transparent heuristic
  when Ollama is unavailable). Nothing about screening ever moves or rejects a candidate.
- Pipeline analytics (stage conversion, drop-off, time-to-hire): http://localhost:3000/workspace/analytics
- Stage editor: `/workspace/jobs/<id>/stages` · Team (activate/deactivate, departments; roles stay
  in Clerk): `/workspace/team`
- Interviews: manual entry in the dossier, or Cal.com bookings via the signed webhook
  `POST /api/v1/webhooks/scheduling/calcom/<subdomain>` (set its secret with
  `PUT /api/v1/communications/calcom-webhook`). Offer letters are generated as PDFs in the dossier.
- GDPR Article 17: admins erase a candidate (all applications, files, recordings) from the dossier,
  or via `GET /api/v1/compliance/candidates?email=` + `DELETE …/compliance/candidates/<id>/forget`.

## Tests (headless Python scripts only — no browser testing)

```bash
.venv/bin/pytest tests/ -v
```

HR acceptance scenarios (also part of the suite):

```bash
.venv/bin/python -m pytest tests/hr_scenarios/verify_full_funnel.py -v -s      # apply → hire walkthrough
.venv/bin/python -m pytest tests/hr_scenarios/verify_tenant_security.py -v -s  # cross-tenant probes
```

The suite recreates an isolated `ats_test` database on each run and connects as the restricted
runtime role, so Row-Level Security is exercised for real.
