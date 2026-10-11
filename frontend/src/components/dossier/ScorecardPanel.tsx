"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError } from "@/lib/api/errors";
import { fetchScorecards, saveScorecard } from "@/lib/api/workspace";
import type { Recommendation, Scorecard, ScorecardBoard, ScorecardInput } from "@/lib/types";

const RATINGS = [
  { key: "technical_competence", label: "Technical competence" },
  { key: "communication", label: "Communication" },
  { key: "problem_solving", label: "Problem-solving" },
] as const;

export const RECOMMENDATIONS: { value: Recommendation; label: string }[] = [
  { value: "strong_yes", label: "Strong Yes" },
  { value: "yes", label: "Yes" },
  { value: "neutral", label: "Neutral" },
  { value: "no", label: "No" },
  { value: "strong_no", label: "Strong No" },
];
const LABEL = Object.fromEntries(RECOMMENDATIONS.map((r) => [r.value, r.label]));

const EMPTY: ScorecardInput = {
  technical_competence: null,
  communication: null,
  problem_solving: null,
  overall_recommendation: null,
  notes_markdown: "",
  submit: false,
};

function fromCard(card: Scorecard | null): ScorecardInput {
  if (!card) return EMPTY;
  return {
    technical_competence: card.technical_competence,
    communication: card.communication,
    problem_solving: card.problem_solving,
    overall_recommendation: card.overall_recommendation,
    notes_markdown: card.notes_markdown ?? "",
    submit: false,
  };
}

function ReadOnlyCard({ card }: { card: Scorecard }) {
  return (
    <li className="rounded-xl border border-border bg-surface p-3 text-sm">
      <div className="flex items-baseline justify-between gap-2">
        <p className="font-medium">
          {card.evaluator_name ?? "Teammate"}{" "}
          <span className="text-xs capitalize text-muted">{card.evaluator_role.replace("_", " ")}</span>
        </p>
        <span className="rounded-md bg-surface-muted px-2 py-0.5 text-xs font-semibold">
          {card.overall_recommendation ? LABEL[card.overall_recommendation] : "—"}
        </span>
      </div>
      <p className="mt-1 text-xs text-muted">
        {RATINGS.map((r) => `${r.label}: ${card[r.key] ?? "–"}/5`).join(" · ")}
      </p>
      {card.notes_markdown && <p className="mt-2 whitespace-pre-wrap">{card.notes_markdown}</p>}
    </li>
  );
}

export function ScorecardPanel({ applicationId }: { applicationId: string }) {
  const [board, setBoard] = useState<ScorecardBoard | null>(null);
  const [form, setForm] = useState<ScorecardInput>(EMPTY);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    const data = await fetchScorecards(applicationId);
    setBoard(data);
    setForm(fromCard(data.mine));
  }, [applicationId]);

  useEffect(() => {
    load().catch((caught: unknown) =>
      setMessage(caught instanceof ApiError ? caught.message : "Could not load scorecards."),
    );
  }, [load]);

  const save = async (submit: boolean) => {
    if (submit && !window.confirm("Submit and lock your scorecard? It can't be edited afterwards.")) return;
    setBusy(true);
    setMessage(null);
    try {
      await saveScorecard(applicationId, { ...form, notes_markdown: form.notes_markdown || null, submit });
      await load();
      setMessage(submit ? "Scorecard submitted." : "Draft saved.");
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  };

  if (!board) return <p className="text-sm text-muted">{message ?? "Loading scorecards…"}</p>;
  const locked = board.mine?.is_submitted ?? false;

  return (
    <div className="space-y-5">
      <section className="space-y-3 rounded-xl border border-border bg-surface p-4">
        <div className="flex items-center justify-between">
          <h4 className="font-semibold">Your scorecard</h4>
          {locked && <span className="text-xs font-medium text-success">Submitted · locked</span>}
        </div>
        {RATINGS.map((rating) => (
          <fieldset key={rating.key} disabled={locked || busy} className="flex flex-wrap items-center gap-2">
            <legend className="w-44 text-sm">{rating.label}</legend>
            {[1, 2, 3, 4, 5].map((value) => (
              <label key={value} className="cursor-pointer">
                <input
                  type="radio"
                  name={rating.key}
                  className="peer sr-only"
                  checked={form[rating.key] === value}
                  onChange={() => setForm({ ...form, [rating.key]: value })}
                />
                <span className="grid size-8 place-items-center rounded-lg border border-border text-sm transition peer-checked:border-brand peer-checked:bg-brand peer-checked:text-brand-contrast peer-focus-visible:ring-2 peer-focus-visible:ring-[var(--ring)]">
                  {value}
                </span>
              </label>
            ))}
          </fieldset>
        ))}
        <label className="flex flex-wrap items-center gap-2 text-sm">
          <span className="w-44">Overall recommendation</span>
          <select
            disabled={locked || busy}
            value={form.overall_recommendation ?? ""}
            onChange={(event) =>
              setForm({ ...form, overall_recommendation: (event.target.value || null) as Recommendation | null })
            }
            className="rounded-lg border border-border bg-background px-2 py-1.5"
          >
            <option value="">Select…</option>
            {RECOMMENDATIONS.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
        <textarea
          disabled={locked || busy}
          rows={3}
          value={form.notes_markdown ?? ""}
          onChange={(event) => setForm({ ...form, notes_markdown: event.target.value })}
          placeholder="Evidence for your ratings…"
          className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-brand"
        />
        {!locked && (
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => void save(false)}
              disabled={busy}
              className="rounded-lg px-3 py-1.5 text-sm text-muted hover:bg-surface-muted"
            >
              Save draft
            </button>
            <button
              type="button"
              onClick={() => void save(true)}
              disabled={busy}
              className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-brand-contrast disabled:opacity-60"
            >
              Submit
            </button>
          </div>
        )}
        {message && <p className="text-xs text-muted" role="status">{message}</p>}
      </section>

      <section className="space-y-2">
        <h4 className="text-xs font-semibold uppercase tracking-wider text-muted">Team feedback</h4>
        <p className="text-xs text-muted">{board.visibility_rule}</p>
        {board.summary && board.summary.submitted > 0 && (
          <p className="text-sm">
            {board.summary.submitted} submitted ·{" "}
            {RATINGS.map((r) => `${r.label} ${board.summary?.averages[r.key] ?? "–"}`).join(" · ")}
          </p>
        )}
        {board.hidden_count > 0 && (
          <p className="rounded-lg bg-surface-muted px-3 py-2 text-sm text-muted">
            {board.hidden_count} colleague scorecard{board.hidden_count === 1 ? "" : "s"} hidden.
          </p>
        )}
        <ul className="space-y-2">
          {board.others.map((card) => (
            <ReadOnlyCard key={card.id} card={card} />
          ))}
        </ul>
      </section>
    </div>
  );
}
