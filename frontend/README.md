# ATS Frontend — Next.js 15 · React 19 · TypeScript · Tailwind CSS v4

Public career portal and zero-friction resume dropzone (port 3000).

```bash
cp .env.example .env.local
npm install
npm run dev          # http://localhost:3000/careers/<company-subdomain>
```

| Route | Purpose |
| --- | --- |
| `/careers/[subdomain]` | Job board for a company (server-rendered) |
| `/careers/[subdomain]/[slug]` | Job detail with Schema.org `JobPosting` JSON-LD and the apply dropzone |

Verification follows the project mandate: no browser testing. The backend is verified with
Python test scripts (`pytest tests/` from the repo root); the frontend with `npm run build`
(type-check + lint).
