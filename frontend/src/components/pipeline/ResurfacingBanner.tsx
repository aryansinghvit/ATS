"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError } from "@/lib/api/errors";
import {
  dismissSuggestion,
  fetchSuggestions,
  inviteToJob,
  refreshSuggestions,
} from "@/lib/api/workspace";
import type { TalentSuggestion } from "@/lib/types";

/** Proactive sourcing alert: past applicants who strongly match this job (Module 5). */
export function ResurfacingBanner({
  jobId,
  canInvite,
  onInvited,
}: {
  jobId: string;
  canInvite: boolean;
  onInvited: () => void;
}) {
  const [suggestions, setSuggestions] = useState<TalentSuggestion[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSuggestions((await fetchSuggestions(jobId)).filter((s) => s.status === "suggested"));
    } catch {
      setSuggestions([]);
    }
  }, [jobId]);

  useEffect(() => {
    void load();
    if (new URLSearchParams(window.location.search).has("resurfaced")) setExpanded(true);
  }, [load]);

  const act = async (candidateId: string, action: "invite" | "dismiss") => {
    setBusy(candidateId);
    setMessage(null);
    try {
      if (action === "invite") {
        const result = await inviteToJob(candidateId, jobId);
        setMessage(result.email_queued ? "Invited — an invitation email is on its way." : "Invited.");
        onInvited();
      } else {
        await dismissSuggestion(jobId, candidateId);
      }
      await load();
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Action failed.");
    } finally {
      setBusy(null);
    }
  };

  const refresh = async () => {
    setBusy("refresh");
    try {
      await refreshSuggestions(jobId);
      await load();
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Refresh failed.");
    } finally {
      setBusy(null);
    }
  };

  if (suggestions.length === 0 && !message) {
    return canInvite ? (
      <button type="button" onClick={() => void refresh()} disabled={busy !== null} className="text-xs text-muted hover:text-foreground">
        {busy === "refresh" ? "Scanning talent pool…" : "Scan talent pool for past applicants"}
      </button>
    ) : null;
  }

  return (
    <section className="rounded-xl border border-[color-mix(in_srgb,var(--brand)_35%,transparent)] bg-[color-mix(in_srgb,var(--brand)_7%,transparent)] p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm">
          <span aria-hidden>💡 </span>
          <span className="font-semibold">
            {suggestions.length} past applicant{suggestions.length === 1 ? "" : "s"}
          </span>{" "}
          in your talent pool strongly match this role.
        </p>
        <button type="button" onClick={() => setExpanded((v) => !v)} className="text-sm font-medium text-brand hover:underline">
          {expanded ? "Hide" : "Review"}
        </button>
      </div>
      {message && <p className="mt-2 text-xs text-muted" role="status">{message}</p>}
      {expanded && (
        <ul className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {suggestions.map((s) => (
            <li key={s.candidate_id} className="rounded-lg border border-border bg-surface p-3 text-sm">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate font-medium">{s.full_name ?? s.email}</p>
                  <p className="truncate text-xs text-muted">
                    {s.applications[0] ? `Previously: ${s.applications[0].job_title}` : s.email}
                  </p>
                </div>
                <span className="shrink-0 rounded-md bg-surface-muted px-2 py-0.5 text-xs font-bold tabular-nums">
                  {Math.round(s.similarity * 100)}%
                </span>
              </div>
              <p className="mt-1 line-clamp-1 text-xs text-muted">{s.skills.slice(0, 6).join(" · ")}</p>
              {canInvite && (
                <div className="mt-2 flex gap-2">
                  <button
                    type="button"
                    disabled={busy !== null}
                    onClick={() => void act(s.candidate_id, "invite")}
                    className="rounded-md bg-brand px-2.5 py-1 text-xs font-semibold text-brand-contrast disabled:opacity-60"
                  >
                    Invite to role
                  </button>
                  <button
                    type="button"
                    disabled={busy !== null}
                    onClick={() => void act(s.candidate_id, "dismiss")}
                    className="rounded-md px-2.5 py-1 text-xs text-muted hover:bg-surface-muted"
                  >
                    Dismiss
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
