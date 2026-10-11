"use client";

import { useEffect, useState } from "react";

import { fetchApplicationEmails } from "@/lib/api/workspace";
import type { OutboxEmail } from "@/lib/types";

const STATUS_STYLE: Record<OutboxEmail["status"], string> = {
  queued: "bg-surface-muted text-muted",
  sent: "bg-[color-mix(in_srgb,var(--success)_15%,transparent)] text-success",
  cancelled: "bg-surface-muted text-muted line-through",
  failed: "bg-[color-mix(in_srgb,var(--danger)_15%,transparent)] text-danger",
};

const when = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

export function EmailsPanel({ applicationId }: { applicationId: string }) {
  const [emails, setEmails] = useState<OutboxEmail[] | null>(null);

  useEffect(() => {
    fetchApplicationEmails(applicationId).then(setEmails).catch(() => setEmails([]));
  }, [applicationId]);

  if (!emails) return <p className="text-sm text-muted">Loading…</p>;
  if (emails.length === 0) return <p className="text-sm text-muted">No emails for this candidate yet.</p>;
  return (
    <ol className="space-y-2">
      {emails.map((email) => (
        <li key={email.id} className="rounded-xl border border-border bg-surface p-3 text-sm">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium capitalize">{email.template_key.replace(/_/g, " ")}</span>
            <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${STATUS_STYLE[email.status]}`}>
              {email.status}
            </span>
          </div>
          <p className="mt-1 text-xs text-muted">
            {email.status === "sent" && email.sent_at
              ? `Sent ${when(email.sent_at)}`
              : email.status === "queued"
                ? `Scheduled for ${when(email.scheduled_for)}`
                : (email.last_error ?? "")}
          </p>
          {email.body && (
            <details className="mt-2">
              <summary className="cursor-pointer text-xs text-brand">{email.subject}</summary>
              <pre className="mt-2 whitespace-pre-wrap font-sans text-xs text-muted">{email.body}</pre>
            </details>
          )}
        </li>
      ))}
    </ol>
  );
}
