"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { clearAccessToken, setAccessToken } from "@/lib/auth/session";

export default function SessionPage() {
  const router = useRouter();
  const [token, setToken] = useState("");

  const save = (event: FormEvent) => {
    event.preventDefault();
    setAccessToken(token);
    router.push("/workspace");
  };

  return (
    <div className="mx-auto max-w-xl space-y-4">
      <h1 className="text-2xl font-semibold tracking-tight">Workspace session</h1>
      <p className="text-sm text-muted">
        Clerk sign-in is not wired into the frontend yet. For local development, mint a mock
        recruiter token (the API must run with <code>AUTH_MOCK_MODE=true</code>):
      </p>
      <pre className="overflow-x-auto rounded-lg bg-surface-muted p-3 text-xs">
        .venv/bin/python scripts/mint_dev_token.py --org org_demo --role recruiter
      </pre>
      <form onSubmit={save} className="space-y-3">
        <label className="block">
          <span className="text-sm font-medium">Bearer token</span>
          <textarea
            required
            rows={4}
            value={token}
            onChange={(event) => setToken(event.target.value)}
            className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 font-mono text-xs outline-none focus:border-brand"
          />
        </label>
        <div className="flex gap-3">
          <button
            type="submit"
            className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-brand-contrast"
          >
            Save for this tab
          </button>
          <button
            type="button"
            onClick={() => {
              clearAccessToken();
              setToken("");
            }}
            className="rounded-lg px-4 py-2 text-sm text-muted hover:bg-surface-muted"
          >
            Sign out
          </button>
        </div>
      </form>
    </div>
  );
}
