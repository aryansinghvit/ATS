"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError } from "@/lib/api/errors";
import { fetchScreening, generateScreening, inviteToScreening } from "@/lib/api/workspace";
import type { Screening, ScreeningEvaluation } from "@/lib/types";

const STATUS_LABEL: Record<Screening["status"], string> = {
  pending: "Not opened yet",
  opened: "Candidate is answering",
  completed: "Answers submitted",
  expired: "Link expired",
};

const CATEGORY_LABEL: Record<string, string> = {
  technical_depth: "Technical depth",
  clarity: "Clarity",
  relevance: "Relevance",
};

const when = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : null;

function Timeline({ screening }: { screening: Screening }) {
  const steps = [
    { label: "Invited", at: screening.invited_at },
    { label: "Opened", at: screening.opened_at },
    { label: "Submitted", at: screening.submitted_at },
    {
      label: "Evaluated",
      at: screening.evaluation?.evaluated_at ?? null,
      pending: screening.evaluation_status === "pending",
    },
  ];
  return (
    <ol className="grid grid-cols-4 gap-2 text-xs">
      {steps.map((step) => (
        <li key={step.label} className="space-y-1">
          <div className={`h-1 rounded-full ${step.at ? "bg-brand" : "bg-surface-muted"}`} />
          <p className={step.at ? "font-medium" : "text-muted"}>{step.label}</p>
          <p className="text-muted">{step.at ? when(step.at) : step.pending ? "In progress…" : "—"}</p>
        </li>
      ))}
    </ol>
  );
}

function EvaluationCard({ evaluation, questionCount }: { evaluation: ScreeningEvaluation; questionCount: number }) {
  return (
    <section className="space-y-3 rounded-xl border border-border bg-surface p-4">
      <p className="rounded-lg bg-surface-muted px-3 py-2 text-xs text-muted">
        <strong className="text-foreground">Advisory only.</strong> This rubric never moves, rejects or ranks the
        candidate automatically — a person makes every decision.
        {evaluation.note && <span className="block pt-1">{evaluation.note}</span>}
      </p>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-3xl font-bold tabular-nums">
            {evaluation.overall_score.toFixed(1)}
            <span className="text-base font-medium text-muted">/5</span>
          </p>
          <p className="text-xs text-muted">
            {evaluation.source === "ai" ? `AI rubric · ${evaluation.model ?? "local LLM"}` : "Heuristic rubric (LLM unavailable)"}
          </p>
        </div>
        <dl className="flex gap-4 text-sm">
          {Object.entries(evaluation.category_averages).map(([key, value]) => (
            <div key={key} className="text-right">
              <dt className="text-xs text-muted">{CATEGORY_LABEL[key] ?? key}</dt>
              <dd className="font-semibold tabular-nums">{value.toFixed(1)}</dd>
            </div>
          ))}
        </dl>
      </div>
      {evaluation.summary && <p className="text-sm leading-relaxed">{evaluation.summary}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Rubric ratings per question (1–5)</caption>
          <thead className="text-xs text-muted">
            <tr>
              <th scope="col" className="py-1 pr-3 font-medium">Q</th>
              <th scope="col" className="py-1 pr-3 font-medium">Depth</th>
              <th scope="col" className="py-1 pr-3 font-medium">Clarity</th>
              <th scope="col" className="py-1 pr-3 font-medium">Relevance</th>
              <th scope="col" className="py-1 font-medium">Why</th>
            </tr>
          </thead>
          <tbody>
            {evaluation.rubric
              .filter((row) => row.index < questionCount)
              .map((row) => (
                <tr key={row.index} className="border-t border-border align-top">
                  <td className="py-1.5 pr-3 font-medium">{row.index + 1}</td>
                  <td className="py-1.5 pr-3 tabular-nums">{row.technical_depth}</td>
                  <td className="py-1.5 pr-3 tabular-nums">{row.clarity}</td>
                  <td className="py-1.5 pr-3 tabular-nums">{row.relevance}</td>
                  <td className="py-1.5 text-xs text-muted">{row.justification}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      {evaluation.strengths.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wider text-muted">Strengths</h4>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm">
            {evaluation.strengths.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}
      {evaluation.probe_questions.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wider text-muted">Probe in the interview</h4>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm">
            {evaluation.probe_questions.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export function ScreeningPanel({ applicationId, canGenerate }: { applicationId: string; canGenerate: boolean }) {
  const [screening, setScreening] = useState<Screening | null>(null);
  const [count, setCount] = useState(3);
  const [busy, setBusy] = useState<"generate" | "invite" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(
    () => fetchScreening(applicationId).then(setScreening).catch(() => setScreening(null)),
    [applicationId],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // While the evaluation worker runs, poll so the scorecard appears without a reload.
  useEffect(() => {
    if (screening?.evaluation_status !== "pending") return;
    const id = setInterval(() => void load(), 5000);
    return () => clearInterval(id);
  }, [screening?.evaluation_status, load]);

  const run = async (kind: "generate" | "invite") => {
    setBusy(kind);
    setError(null);
    try {
      setScreening(
        kind === "generate" ? await generateScreening(applicationId, count) : await inviteToScreening(applicationId),
      );
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Something went wrong.");
    } finally {
      setBusy(null);
    }
  };

  const copyLink = async () => {
    if (!screening?.candidate_link) return;
    await navigator.clipboard.writeText(screening.candidate_link);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const locked = screening?.status === "completed" || screening?.status === "opened";

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">
        Questions target this candidate&apos;s skill gaps. The candidate answers by text or voice at a private link; the
        rubric score is advisory only — it never rejects anyone automatically.
      </p>
      {canGenerate && (
        <div className="flex flex-wrap items-center gap-2">
          {!locked && (
            <>
              <label className="text-sm">
                Questions{" "}
                <select
                  value={count}
                  onChange={(event) => setCount(Number(event.target.value))}
                  className="rounded-lg border border-border bg-background px-2 py-1"
                >
                  {[3, 4, 5].map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                onClick={() => void run("generate")}
                disabled={busy !== null}
                className="rounded-lg border border-border px-3 py-1.5 text-sm font-medium disabled:opacity-60"
              >
                {busy === "generate" ? "Generating…" : screening ? "Regenerate" : "Generate questions"}
              </button>
            </>
          )}
          {screening && screening.status !== "completed" && (
            <button
              type="button"
              onClick={() => void run("invite")}
              disabled={busy !== null}
              className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-brand-contrast disabled:opacity-60"
            >
              {busy === "invite" ? "Sending…" : screening.invited_at ? "Resend invitation" : "Send to candidate"}
            </button>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      {!screening ? (
        <p className="text-sm text-muted">No screening questions yet.</p>
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            <span className="rounded-full bg-surface-muted px-2 py-0.5 font-semibold text-foreground">
              {screening.invited_at || screening.status !== "pending" ? STATUS_LABEL[screening.status] : "Draft — not sent"}
            </span>
            <span
              className={`rounded-full px-2 py-0.5 font-semibold ${
                screening.source === "ai" ? "bg-[color-mix(in_srgb,var(--brand)_15%,transparent)] text-brand" : "bg-surface-muted"
              }`}
              title={screening.source_detail ?? undefined}
            >
              {screening.source === "ai" ? `AI · ${screening.model ?? "local LLM"}` : "Template questions"}
            </span>
            {screening.skill_gaps.length > 0 && <span>Gaps: {screening.skill_gaps.join(", ")}</span>}
          </div>

          {screening.invited_at && <Timeline screening={screening} />}

          {screening.candidate_link && (
            <div className="space-y-1 rounded-xl border border-border bg-surface p-3">
              <p className="text-xs text-muted">
                Candidate link · expires {when(screening.expires_at)} · single use
              </p>
              <div className="flex gap-2">
                <input
                  readOnly
                  value={screening.candidate_link}
                  aria-label="Candidate screening link"
                  onFocus={(event) => event.currentTarget.select()}
                  className="min-w-0 flex-1 rounded-lg border border-border bg-background px-2 py-1 font-mono text-xs"
                />
                <button
                  type="button"
                  onClick={() => void copyLink()}
                  className="rounded-lg border border-border px-3 py-1 text-xs font-medium"
                >
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
            </div>
          )}

          {screening.evaluation_status === "pending" && (
            <p className="text-sm text-muted" aria-live="polite">
              Transcribing and scoring the answers…
            </p>
          )}
          {screening.evaluation && (
            <EvaluationCard evaluation={screening.evaluation} questionCount={screening.questions.length} />
          )}

          <ol className="space-y-2">
            {screening.questions.map((question, index) => {
              const response = screening.responses.find((item) => item.index === index);
              return (
                <li key={index} className="space-y-2 rounded-xl border border-border bg-surface p-3">
                  <p className="text-sm">
                    <span className="mr-1 font-semibold text-muted">{index + 1}.</span>
                    {question.question}
                  </p>
                  <p className="text-xs text-muted">Targets: {question.targets}</p>
                  {response?.type === "text" && response.text && (
                    <blockquote className="whitespace-pre-wrap border-l-2 border-brand pl-3 text-sm">{response.text}</blockquote>
                  )}
                  {response?.type === "audio" && (
                    <div className="space-y-1">
                      {response.audio_url && (
                        <audio controls preload="none" src={response.audio_url} className="w-full" aria-label={`Answer ${index + 1} recording`} />
                      )}
                      {response.transcript ? (
                        <blockquote className="whitespace-pre-wrap border-l-2 border-brand pl-3 text-sm">
                          {response.transcript}
                          <span className="mt-1 block text-xs text-muted">
                            Transcript · {response.transcription_engine ?? "speech-to-text"}
                          </span>
                        </blockquote>
                      ) : response.transcription_error ? (
                        <p className="text-xs text-danger">
                          Transcription failed ({response.transcription_error}). Listen to the recording and review
                          manually.
                        </p>
                      ) : (
                        <p className="text-xs text-muted">Voice answer · transcript pending</p>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </div>
  );
}
