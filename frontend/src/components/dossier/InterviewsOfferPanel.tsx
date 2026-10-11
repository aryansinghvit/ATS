"use client";

import { useEffect, useState } from "react";

import { ApiError } from "@/lib/api/errors";
import {
  fetchInterviews,
  fetchOfferLetter,
  fetchTeam,
  generateOfferLetter,
  scheduleInterview,
  setInterviewStatus,
} from "@/lib/api/workspace";
import type { Interview, OfferLetter, OfferLetterInput, TeamMember } from "@/lib/types";

const input = "w-full rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm";
const label = "space-y-1 text-xs text-muted";

const message = (caught: unknown, fallback: string) => (caught instanceof ApiError ? caught.message : fallback);

/** `datetime-local` value (browser local time) -> ISO string with an explicit offset. */
function withOffset(local: string) {
  return new Date(local).toISOString();
}

function Interviews({ applicationId }: { applicationId: string }) {
  const [interviews, setInterviews] = useState<Interview[] | null>(null);
  const [team, setTeam] = useState<TeamMember[]>([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ title: "Interview", interviewer_id: "", start: "", minutes: 45, meeting_url: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchInterviews(applicationId).then(setInterviews).catch(() => setInterviews([]));
    fetchTeam()
      .then((members) => setTeam(members.filter((member) => member.is_active)))
      .catch(() => setTeam([]));
  }, [applicationId]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!form.start) return;
    setBusy(true);
    setError(null);
    try {
      const start = new Date(form.start);
      const created = await scheduleInterview(applicationId, {
        title: form.title.trim() || "Interview",
        interviewer_id: form.interviewer_id || null,
        scheduled_start: withOffset(form.start),
        scheduled_end: new Date(start.getTime() + form.minutes * 60_000).toISOString(),
        meeting_url: form.meeting_url.trim() || null,
      });
      setInterviews((current) =>
        [...(current ?? []), created].sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start)),
      );
      setOpen(false);
      setForm((current) => ({ ...current, start: "", meeting_url: "" }));
    } catch (caught) {
      setError(message(caught, "The interview could not be scheduled."));
    } finally {
      setBusy(false);
    }
  };

  const changeStatus = async (interview: Interview, status: Interview["status"]) => {
    try {
      const updated = await setInterviewStatus(interview.id, status);
      setInterviews((current) => current?.map((item) => (item.id === updated.id ? updated : item)) ?? null);
    } catch (caught) {
      setError(message(caught, "The interview could not be updated."));
    }
  };

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">Interviews</h3>
        <button type="button" onClick={() => setOpen((value) => !value)} className="text-sm font-medium text-brand hover:underline">
          {open ? "Cancel" : "+ Schedule"}
        </button>
      </div>
      <p className="text-xs text-muted">
        Bookings made through your Cal.com scheduling link appear here automatically; you can also add one manually.
      </p>
      {open && (
        <form onSubmit={(event) => void submit(event)} className="grid gap-3 rounded-xl border border-border bg-surface p-3 sm:grid-cols-2">
          <label className={label}>
            <span className="block">Title</span>
            <input className={input} value={form.title} maxLength={200} onChange={(e) => setForm({ ...form, title: e.target.value })} />
          </label>
          <label className={label}>
            <span className="block">Interviewer</span>
            <select className={input} value={form.interviewer_id} onChange={(e) => setForm({ ...form, interviewer_id: e.target.value })}>
              <option value="">Unassigned</option>
              {team.map((member) => (
                <option key={member.id} value={member.id}>
                  {member.full_name}
                </option>
              ))}
            </select>
          </label>
          <label className={label}>
            <span className="block">Starts (your local time)</span>
            <input type="datetime-local" required className={input} value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} />
          </label>
          <label className={label}>
            <span className="block">Duration</span>
            <select className={input} value={form.minutes} onChange={(e) => setForm({ ...form, minutes: Number(e.target.value) })}>
              {[30, 45, 60, 90].map((minutes) => (
                <option key={minutes} value={minutes}>
                  {minutes} min
                </option>
              ))}
            </select>
          </label>
          <label className={`${label} sm:col-span-2`}>
            <span className="block">Meeting link (optional, https)</span>
            <input type="url" pattern="https://.*" className={input} value={form.meeting_url} onChange={(e) => setForm({ ...form, meeting_url: e.target.value })} placeholder="https://meet.example.com/…" />
          </label>
          <div className="sm:col-span-2">
            <button type="submit" disabled={busy} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-brand-contrast disabled:opacity-60">
              {busy ? "Saving…" : "Add interview"}
            </button>
          </div>
        </form>
      )}
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      {!interviews ? (
        <p className="text-sm text-muted">Loading…</p>
      ) : interviews.length === 0 ? (
        <p className="text-sm text-muted">No interviews scheduled.</p>
      ) : (
        <ul className="space-y-2">
          {interviews.map((interview) => (
            <li key={interview.id} className={`rounded-xl border border-border bg-surface p-3 ${interview.status === "cancelled" ? "opacity-60" : ""}`}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <p className={`text-sm font-medium ${interview.status === "cancelled" ? "line-through" : ""}`}>{interview.title}</p>
                  <p className="text-xs text-muted">
                    {new Date(interview.scheduled_start).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })} –{" "}
                    {new Date(interview.scheduled_end).toLocaleTimeString(undefined, { timeStyle: "short" })}
                    {interview.interviewer_name && ` · ${interview.interviewer_name}`}
                    {interview.source === "calcom" && " · via Cal.com"}
                  </p>
                  {interview.meeting_url && interview.status === "scheduled" && (
                    <a href={interview.meeting_url} target="_blank" rel="noopener noreferrer" className="text-xs text-brand hover:underline">
                      Join meeting
                    </a>
                  )}
                </div>
                <div className="flex items-center gap-2 text-xs">
                  <span className="rounded-full bg-surface-muted px-2 py-0.5 capitalize">{interview.status}</span>
                  {interview.status === "scheduled" && (
                    <>
                      <button type="button" onClick={() => void changeStatus(interview, "completed")} className="text-brand hover:underline">
                        Mark done
                      </button>
                      <button type="button" onClick={() => void changeStatus(interview, "cancelled")} className="text-danger hover:underline">
                        Cancel
                      </button>
                    </>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

const EMPTY_OFFER: OfferLetterInput = {
  base_salary: "",
  currency: "USD",
  start_date: "",
  job_title: null,
  equity: null,
  respond_by: null,
  additional_terms: null,
};

function Offer({ applicationId, canGenerate, defaultTitle }: { applicationId: string; canGenerate: boolean; defaultTitle: string }) {
  const [offer, setOffer] = useState<OfferLetter | null>(null);
  const [form, setForm] = useState<OfferLetterInput>({ ...EMPTY_OFFER, job_title: defaultTitle });
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchOfferLetter(applicationId)
      .then((letter) => {
        setOffer(letter);
        const { details } = letter;
        setForm({
          ...EMPTY_OFFER,
          base_salary: details.base_salary ?? "",
          currency: details.currency ?? "USD",
          start_date: details.start_date ?? "",
          job_title: details.job_title ?? defaultTitle,
          equity: details.equity ?? null,
          respond_by: details.respond_by ?? null,
        });
      })
      .catch(() => setOffer(null));
  }, [applicationId, defaultTitle]);

  const set = (key: keyof OfferLetterInput, value: string) => setForm((current) => ({ ...current, [key]: value }));

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const blank = (value: string | null) => (value && value.trim() ? value.trim() : null);
      const letter = await generateOfferLetter(applicationId, {
        ...form,
        currency: form.currency.toUpperCase(),
        job_title: blank(form.job_title),
        equity: blank(form.equity),
        respond_by: blank(form.respond_by),
        additional_terms: blank(form.additional_terms),
      });
      setOffer(letter);
      setEditing(false);
    } catch (caught) {
      setError(message(caught, "The offer letter could not be generated."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">Offer letter</h3>
        {canGenerate && !editing && (
          <button type="button" onClick={() => setEditing(true)} className="text-sm font-medium text-brand hover:underline">
            {offer ? "Revise" : "+ Create"}
          </button>
        )}
      </div>
      {offer && !editing && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-surface p-3">
          <div className="text-sm">
            <p className="font-medium">
              {offer.details.job_title ?? defaultTitle} · {offer.details.currency} {Number(offer.details.base_salary).toLocaleString()}
            </p>
            <p className="text-xs text-muted">
              Starts {offer.details.start_date} · generated{" "}
              {new Date(offer.generated_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
            </p>
          </div>
          <a href={offer.download_url} target="_blank" rel="noopener noreferrer" className="rounded-lg border border-border px-3 py-1.5 text-sm font-medium">
            Download PDF
          </a>
        </div>
      )}
      {!offer && !editing && <p className="text-sm text-muted">No offer letter yet.</p>}
      {editing && (
        <form onSubmit={(event) => void submit(event)} className="grid gap-3 rounded-xl border border-border bg-surface p-3 sm:grid-cols-2">
          <label className={label}>
            <span className="block">Base salary (annual)</span>
            <input required inputMode="decimal" pattern="\d+(\.\d{1,2})?" className={input} value={form.base_salary} onChange={(e) => set("base_salary", e.target.value)} />
          </label>
          <label className={label}>
            <span className="block">Currency</span>
            <input required maxLength={3} pattern="[A-Za-z]{3}" className={input} value={form.currency} onChange={(e) => set("currency", e.target.value)} />
          </label>
          <label className={label}>
            <span className="block">Job title</span>
            <input className={input} maxLength={255} value={form.job_title ?? ""} onChange={(e) => set("job_title", e.target.value)} />
          </label>
          <label className={label}>
            <span className="block">Start date</span>
            <input required type="date" className={input} value={form.start_date} onChange={(e) => set("start_date", e.target.value)} />
          </label>
          <label className={label}>
            <span className="block">Equity (optional)</span>
            <input className={input} maxLength={200} value={form.equity ?? ""} onChange={(e) => set("equity", e.target.value)} />
          </label>
          <label className={label}>
            <span className="block">Respond by (optional)</span>
            <input type="date" className={input} value={form.respond_by ?? ""} onChange={(e) => set("respond_by", e.target.value)} />
          </label>
          <label className={`${label} sm:col-span-2`}>
            <span className="block">Additional terms (optional)</span>
            <textarea rows={3} maxLength={2000} className={input} value={form.additional_terms ?? ""} onChange={(e) => set("additional_terms", e.target.value)} />
          </label>
          {error && (
            <p role="alert" className="text-sm text-danger sm:col-span-2">
              {error}
            </p>
          )}
          <div className="flex gap-2 sm:col-span-2">
            <button type="submit" disabled={busy} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-brand-contrast disabled:opacity-60">
              {busy ? "Generating…" : "Generate PDF"}
            </button>
            <button type="button" onClick={() => setEditing(false)} className="rounded-lg border border-border px-3 py-1.5 text-sm">
              Cancel
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

export function InterviewsOfferPanel({
  applicationId,
  canGenerateOffer,
  jobTitle,
}: {
  applicationId: string;
  canGenerateOffer: boolean;
  jobTitle: string;
}) {
  return (
    <div className="space-y-8">
      <Interviews applicationId={applicationId} />
      <Offer applicationId={applicationId} canGenerate={canGenerateOffer} defaultTitle={jobTitle} />
    </div>
  );
}
