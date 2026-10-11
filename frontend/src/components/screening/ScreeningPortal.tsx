"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";

import { ApiError } from "@/lib/api/errors";
import { openScreening, saveAudioAnswer, saveTextAnswer, submitScreening } from "@/lib/api/screening-portal";
import { readableTextColor } from "@/lib/format";
import type { PortalAnswer, PortalView } from "@/lib/types";

import { AudioRecorder, formatClock } from "./AudioRecorder";

type Step = "intro" | number | "review" | "done";
type SaveState = "idle" | "saving" | "saved" | "error";
type Mode = "text" | "audio";

const AUTOSAVE_DELAY_MS = 900;

function isAnswered(answer: PortalAnswer | undefined) {
  if (!answer) return false;
  return answer.type === "audio" ? answer.has_audio : !!answer.text?.trim();
}

function Shell({ view, children }: { view: PortalView | null; children: React.ReactNode }) {
  const color = view?.primary_color ?? "#2563eb";
  const style = { "--brand": color, "--brand-contrast": readableTextColor(color) } as CSSProperties;
  return (
    <div style={style} className="flex min-h-dvh flex-col">
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex h-14 max-w-2xl items-center gap-3 px-4">
          {view?.logo_url ? (
            // Tenant logos are arbitrary external URLs.
            // eslint-disable-next-line @next/next/no-img-element
            <img src={view.logo_url} alt="" className="size-8 rounded-md object-contain" />
          ) : view ? (
            <span aria-hidden className="grid size-8 place-items-center rounded-md bg-brand text-sm font-bold text-brand-contrast">
              {view.company_name.charAt(0).toUpperCase()}
            </span>
          ) : null}
          <span className="truncate font-semibold tracking-tight">{view?.company_name ?? "Screening"}</span>
        </div>
      </header>
      <main className="mx-auto w-full max-w-2xl flex-1 px-4 py-6 sm:py-10">{children}</main>
    </div>
  );
}

function Notice({ title, body }: { title: string; body: string }) {
  return (
    <div className="space-y-2 rounded-2xl border border-border bg-surface p-6 text-center">
      <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
      <p className="text-sm text-muted">{body}</p>
    </div>
  );
}

export function ScreeningPortal({ token }: { token: string }) {
  const [view, setView] = useState<PortalView | null>(null);
  const [failure, setFailure] = useState<{ status: number; message: string } | null>(null);
  const [step, setStep] = useState<Step>("intro");
  const [answers, setAnswers] = useState<Record<number, PortalAnswer>>({});
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const [modes, setModes] = useState<Record<number, Mode>>({});
  const [saveState, setSaveState] = useState<Record<number, SaveState>>({});
  const [elapsed, setElapsed] = useState<Record<number, number>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const pending = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());
  const draftsRef = useRef(drafts);
  draftsRef.current = drafts;

  const fail = useCallback((caught: unknown) => {
    if (caught instanceof ApiError && (caught.status === 403 || caught.status === 410 || caught.status === 404)) {
      setFailure({ status: caught.status, message: caught.message });
      return true;
    }
    return false;
  }, []);

  useEffect(() => {
    openScreening(token)
      .then((data) => {
        setView(data);
        const byIndex = Object.fromEntries(data.answers.map((a) => [a.index, a]));
        setAnswers(byIndex);
        setDrafts(Object.fromEntries(data.answers.filter((a) => a.type === "text").map((a) => [a.index, a.text ?? ""])));
        setModes(Object.fromEntries(data.answers.map((a) => [a.index, a.type])));
      })
      .catch((caught: unknown) => {
        if (!fail(caught)) setFailure({ status: 0, message: "We couldn't load your questions. Please refresh the page." });
      });
  }, [token, fail]);

  // Soft per-question timer: shows time spent, never enforces a limit on typed answers.
  useEffect(() => {
    if (typeof step !== "number") return;
    const id = setInterval(() => setElapsed((prev) => ({ ...prev, [step]: (prev[step] ?? 0) + 1 })), 1000);
    return () => clearInterval(id);
  }, [step]);

  const persistText = useCallback(
    async (index: number, keepalive = false) => {
      const timeout = pending.current.get(index);
      if (timeout) clearTimeout(timeout);
      pending.current.delete(index);
      const text = draftsRef.current[index] ?? "";
      setSaveState((prev) => ({ ...prev, [index]: "saving" }));
      try {
        const saved = await saveTextAnswer(token, index, text, keepalive);
        setAnswers((prev) => ({ ...prev, [index]: saved }));
        setSaveState((prev) => ({ ...prev, [index]: "saved" }));
      } catch (caught) {
        if (!fail(caught)) setSaveState((prev) => ({ ...prev, [index]: "error" }));
      }
    },
    [token, fail],
  );

  const flushAll = useCallback(
    async (keepalive = false) => {
      await Promise.all([...pending.current.keys()].map((index) => persistText(index, keepalive)));
    },
    [persistText],
  );

  // Don't lose a half-typed answer if the tab is hidden or closed.
  useEffect(() => {
    const onHide = () => {
      if (document.visibilityState === "hidden") void flushAll(true);
    };
    document.addEventListener("visibilitychange", onHide);
    return () => document.removeEventListener("visibilitychange", onHide);
  }, [flushAll]);

  const onType = (index: number, text: string) => {
    setDrafts((prev) => ({ ...prev, [index]: text }));
    setSaveState((prev) => ({ ...prev, [index]: "idle" }));
    const existing = pending.current.get(index);
    if (existing) clearTimeout(existing);
    pending.current.set(index, setTimeout(() => void persistText(index), AUTOSAVE_DELAY_MS));
  };

  const onRecorded = async (index: number, audio: Blob) => {
    try {
      const saved = await saveAudioAnswer(token, index, audio);
      setAnswers((prev) => ({ ...prev, [index]: saved }));
      setDrafts((prev) => ({ ...prev, [index]: "" }));
    } catch (caught) {
      if (!fail(caught)) throw caught;
    }
  };

  const goTo = async (next: Step) => {
    await flushAll();
    setStep(next);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const submit = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await flushAll();
      await submitScreening(token);
      setStep("done");
    } catch (caught) {
      if (!fail(caught)) setSubmitError(caught instanceof ApiError ? caught.message : "Submission failed. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };

  if (step === "done") {
    return (
      <Shell view={view}>
        <div className="space-y-3 rounded-2xl border border-border bg-surface p-6 text-center">
          <p aria-hidden className="text-4xl">✓</p>
          <h1 className="text-xl font-semibold tracking-tight">Thank you — your answers are in</h1>
          <p className="text-sm text-muted">
            The {view?.company_name} hiring team will review your responses for the {view?.job_title} role. A person
            reviews every application; you&apos;ll hear back by email. You can close this page.
          </p>
        </div>
      </Shell>
    );
  }

  if (failure) {
    const title =
      failure.status === 410
        ? failure.message.includes("submitted")
          ? "Already submitted"
          : "This link has expired"
        : failure.status === 403
          ? "This link isn't valid"
          : "Something went wrong";
    const body =
      failure.status === 410 && failure.message.includes("submitted")
        ? "Your answers were already received. Thank you!"
        : failure.status === 410
          ? "Screening links are valid for a limited time. Reply to the invitation email and the recruiting team can send you a fresh link."
          : failure.status === 403
            ? "The link may be incomplete or replaced by a newer one. Please use the most recent link from your email."
            : failure.message;
    return (
      <Shell view={view}>
        <Notice title={title} body={body} />
      </Shell>
    );
  }

  if (!view) {
    return (
      <Shell view={null}>
        <p className="text-sm text-muted">Loading your questions…</p>
      </Shell>
    );
  }

  const total = view.questions.length;
  const answeredCount = view.questions.filter((q) => isAnswered(answers[q.index])).length;
  const expires = new Date(view.expires_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

  if (step === "intro") {
    return (
      <Shell view={view}>
        <div className="space-y-6">
          <div className="space-y-2">
            <p className="text-sm font-medium text-brand">{view.job_title}</p>
            <h1 className="text-2xl font-semibold tracking-tight">
              {view.candidate_first_name ? `Hi ${view.candidate_first_name}, a` : "A"} few questions for you
            </h1>
            <p className="text-muted">
              {total} short questions. Answer each one by typing or by recording your voice — whichever you prefer.
            </p>
          </div>
          <ul className="space-y-2 rounded-2xl border border-border bg-surface p-5 text-sm">
            <li>💾 Your answers save automatically, so you can leave and come back with the same link.</li>
            <li>🎙️ Voice answers can be up to {formatClock(view.limits.max_audio_seconds)} long.</li>
            <li>⏳ Please submit before {expires}.</li>
            <li>🧑‍💼 A person on the hiring team reviews every answer. Nothing is decided automatically.</li>
          </ul>
          <button
            type="button"
            onClick={() => void goTo(total > 0 ? view.questions[0].index : "review")}
            className="min-h-12 w-full rounded-xl bg-brand px-5 text-base font-semibold text-brand-contrast sm:w-auto"
          >
            {answeredCount > 0 ? "Continue" : "Start"}
          </button>
        </div>
      </Shell>
    );
  }

  const progressBar = (
    <nav aria-label="Questions" className="flex items-center gap-1.5">
      {view.questions.map((q) => (
        <button
          key={q.index}
          type="button"
          onClick={() => void goTo(q.index)}
          aria-label={`Question ${q.index + 1}${isAnswered(answers[q.index]) ? " (answered)" : ""}`}
          aria-current={step === q.index ? "step" : undefined}
          className={`h-2 flex-1 rounded-full transition ${
            step === q.index
              ? "bg-brand"
              : isAnswered(answers[q.index])
                ? "bg-[color-mix(in_srgb,var(--brand)_45%,transparent)]"
                : "bg-surface-muted"
          }`}
        />
      ))}
      <button
        type="button"
        onClick={() => void goTo("review")}
        className={`ml-2 text-xs font-medium ${step === "review" ? "text-brand" : "text-muted"}`}
      >
        Review
      </button>
    </nav>
  );

  if (step === "review") {
    const unanswered = view.questions.filter((q) => !isAnswered(answers[q.index]));
    return (
      <Shell view={view}>
        <div className="space-y-6">
          {progressBar}
          <h1 className="text-xl font-semibold tracking-tight">Review your answers</h1>
          <ol className="space-y-3">
            {view.questions.map((q) => {
              const answer = answers[q.index];
              return (
                <li key={q.index} className="rounded-xl border border-border bg-surface p-4">
                  <p className="text-sm font-medium">
                    {q.index + 1}. {q.question}
                  </p>
                  <p className={`mt-2 text-sm ${isAnswered(answer) ? "text-muted" : "text-danger"}`}>
                    {!isAnswered(answer)
                      ? "Not answered yet"
                      : answer.type === "audio"
                        ? "🎙️ Voice answer recorded"
                        : answer.text!.length > 220
                          ? `${answer.text!.slice(0, 220)}…`
                          : answer.text}
                  </p>
                  <button
                    type="button"
                    onClick={() => void goTo(q.index)}
                    className="mt-2 text-sm font-medium text-brand hover:underline"
                  >
                    {isAnswered(answer) ? "Edit" : "Answer"}
                  </button>
                </li>
              );
            })}
          </ol>
          {submitError && (
            <p role="alert" className="text-sm text-danger">
              {submitError}
            </p>
          )}
          <div className="flex flex-col-reverse gap-3 sm:flex-row sm:items-center">
            <button
              type="button"
              onClick={() => void goTo(view.questions[total - 1].index)}
              className="min-h-12 rounded-xl border border-border px-5 text-sm font-medium"
            >
              Back
            </button>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={submitting || unanswered.length > 0}
              className="min-h-12 flex-1 rounded-xl bg-brand px-5 text-base font-semibold text-brand-contrast disabled:opacity-50 sm:flex-none"
            >
              {submitting ? "Submitting…" : "Submit answers"}
            </button>
            {unanswered.length > 0 && (
              <span className="text-sm text-muted">Answer every question to submit.</span>
            )}
          </div>
          <p className="text-xs text-muted">You can&apos;t change your answers after submitting.</p>
        </div>
      </Shell>
    );
  }

  const question = view.questions.find((q) => q.index === step)!;
  const position = view.questions.indexOf(question);
  const answer = answers[question.index];
  const mode: Mode = modes[question.index] ?? "text";
  const draft = drafts[question.index] ?? "";
  const saving = saveState[question.index] ?? "idle";
  const next: Step = position + 1 < total ? view.questions[position + 1].index : "review";
  const previous: Step = position > 0 ? view.questions[position - 1].index : "intro";

  return (
    <Shell view={view}>
      <div className="space-y-6">
        {progressBar}
        <div className="flex items-center justify-between text-xs text-muted">
          <span>
            Question {position + 1} of {total}
          </span>
          <span className="tabular-nums" title="Time spent on this question">
            ⏱ {formatClock(elapsed[question.index] ?? 0)}
          </span>
        </div>
        <h1 className="text-xl font-semibold leading-snug tracking-tight">{question.question}</h1>

        <div role="radiogroup" aria-label="Answer format" className="inline-flex rounded-xl border border-border bg-surface p-1">
          {(["text", "audio"] as const).map((option) => (
            <button
              key={option}
              type="button"
              role="radio"
              aria-checked={mode === option}
              onClick={() => setModes((prev) => ({ ...prev, [question.index]: option }))}
              className={`min-h-10 rounded-lg px-4 text-sm font-medium ${
                mode === option ? "bg-brand text-brand-contrast" : "text-muted"
              }`}
            >
              {option === "text" ? "✍️ Type" : "🎙️ Record"}
            </button>
          ))}
        </div>

        {mode === "text" ? (
          <div className="space-y-2">
            {answer?.type === "audio" && answer.has_audio && (
              <p className="text-xs text-muted">Typing an answer will replace your voice recording.</p>
            )}
            <label htmlFor={`answer-${question.index}`} className="sr-only">
              Your answer
            </label>
            <textarea
              id={`answer-${question.index}`}
              value={draft}
              onChange={(event) => onType(question.index, event.target.value)}
              onBlur={() => pending.current.has(question.index) && void persistText(question.index)}
              maxLength={view.limits.max_text_chars}
              rows={8}
              placeholder="Type your answer…"
              className="w-full rounded-xl border border-border bg-surface p-4 text-base leading-relaxed focus:outline-none focus:ring-2 focus:ring-[var(--ring)]"
            />
            <div className="flex justify-between text-xs text-muted" aria-live="polite">
              <span className={saving === "error" ? "text-danger" : undefined}>
                {saving === "saving"
                  ? "Saving…"
                  : saving === "saved"
                    ? "✓ Saved"
                    : saving === "error"
                      ? "Couldn't save — check your connection. We'll retry when you continue."
                      : pending.current.has(question.index)
                        ? "Unsaved changes"
                        : ""}
              </span>
              <span className="tabular-nums">
                {draft.length}/{view.limits.max_text_chars}
              </span>
            </div>
          </div>
        ) : (
          <div className="space-y-2">
            {answer?.type === "text" && answer.text?.trim() && (
              <p className="text-xs text-muted">Recording an answer will replace your typed answer.</p>
            )}
            <AudioRecorder
              key={question.index}
              maxSeconds={view.limits.max_audio_seconds}
              maxBytes={view.limits.max_audio_bytes}
              savedBytes={answer?.type === "audio" && answer.has_audio ? answer.audio_bytes : null}
              onRecorded={(audio) => onRecorded(question.index, audio)}
            />
          </div>
        )}

        <div className="flex gap-3">
          <button
            type="button"
            onClick={() => void goTo(previous)}
            className="min-h-12 rounded-xl border border-border px-5 text-sm font-medium"
          >
            Back
          </button>
          <button
            type="button"
            onClick={() => void goTo(next)}
            className="min-h-12 flex-1 rounded-xl bg-brand px-5 text-base font-semibold text-brand-contrast sm:flex-none"
          >
            {next === "review" ? "Review answers" : "Next question"}
          </button>
        </div>
      </div>
    </Shell>
  );
}
