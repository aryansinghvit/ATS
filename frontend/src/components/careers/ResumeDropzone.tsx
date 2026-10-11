"use client";

import { useId, useState, type DragEvent, type FormEvent, type ReactNode } from "react";

import { ApiError } from "@/lib/api/errors";
import {
  confirmApplication,
  requestResumeUpload,
  submitApplication,
  uploadResume,
} from "@/lib/api/applications";
import { RESUME_ACCEPT, validateResume } from "@/lib/resume-validation";
import type { ParsedCandidate, SubmitApplicationResponse } from "@/lib/types";
import { useApplicationEvents } from "@/lib/use-application-events";

import { ConfirmProfileModal } from "./ConfirmProfileModal";
import { UploadProgress } from "./UploadProgress";

interface ResumeDropzoneProps {
  tenantSubdomain: string;
  jobId: string;
  jobTitle: string;
}

type Step =
  | { name: "select" }
  | { name: "uploading"; progress: number }
  | { name: "submitting" }
  | { name: "submitted"; application: SubmitApplicationResponse }
  | { name: "confirmed" };

// Manual-entry fallback when M3 could not extract the resume (project.md Module 3).
const EMPTY_PROFILE: ParsedCandidate = {
  full_name: null,
  email: null,
  phone: null,
  linkedin_url: null,
  github_url: null,
  location: null,
  skills: [],
};

function messageFor(error: unknown) {
  return error instanceof ApiError ? error.message : "Something went wrong. Please try again.";
}

export function ResumeDropzone({ tenantSubdomain, jobId, jobTitle }: ResumeDropzoneProps) {
  const inputId = useId();
  const [step, setStep] = useState<Step>({ name: "select" });
  const [file, setFile] = useState<{ file: File; mimeType: string } | null>(null);
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  const application = step.name === "submitted" ? step.application : null;
  const extraction = useApplicationEvents(application?.events_url ?? null);

  const chooseFile = async (candidate: File | undefined) => {
    if (!candidate) return;
    setError(null);
    const result = await validateResume(candidate);
    if (result.ok) {
      setFile({ file: candidate, mimeType: result.mimeType });
    } else {
      setFile(null);
      setError(result.error);
    }
  };

  const onDrop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    setDragging(false);
    void chooseFile(event.dataTransfer.files[0]);
  };

  const apply = async (event: FormEvent) => {
    event.preventDefault();
    if (!file) return;
    setError(null);
    try {
      setStep({ name: "uploading", progress: 0 });
      const upload = await requestResumeUpload({
        tenantSubdomain,
        jobId,
        file: file.file,
        mimeType: file.mimeType,
      });
      await uploadResume(upload, file.file, (progress) =>
        setStep({ name: "uploading", progress }),
      );
      setStep({ name: "submitting" });
      const submitted = await submitApplication({
        tenantSubdomain,
        jobId,
        s3Key: upload.s3_key,
        fileName: file.file.name,
        email,
        fullName,
      });
      setStep({ name: "submitted", application: submitted });
    } catch (caught) {
      setError(messageFor(caught));
      setStep({ name: "select" });
    }
  };

  const confirm = async (profile: ParsedCandidate) => {
    if (!application) return;
    setConfirming(true);
    setConfirmError(null);
    try {
      await confirmApplication(application.application_id, application.status_token, profile);
      setStep({ name: "confirmed" });
    } catch (caught) {
      setConfirmError(messageFor(caught));
    } finally {
      setConfirming(false);
    }
  };

  if (step.name === "confirmed") {
    return (
      <StatusCard tone="success" title="Application complete!">
        Thanks for applying to {jobTitle}. The hiring team will review your profile and reach out
        by email.
      </StatusCard>
    );
  }

  if (step.name === "submitted") {
    return (
      <div className="space-y-4" aria-live="polite">
        {extraction.status === "error" ? (
          <StatusCard tone="neutral" title="Application received">
            Your application is saved, but we couldn&apos;t read your resume automatically. Add your
            details so the team has everything they need.
            <button
              type="button"
              onClick={() => setModalOpen(true)}
              className="mt-3 block font-semibold text-brand underline-offset-2 hover:underline"
            >
              Enter details
            </button>
          </StatusCard>
        ) : extraction.status === "parsed" ? (
          <StatusCard tone="success" title="Resume analyzed">
            Please review the details we extracted to finish your application.
            <button
              type="button"
              onClick={() => setModalOpen(true)}
              className="mt-3 block font-semibold text-brand underline-offset-2 hover:underline"
            >
              Review details
            </button>
          </StatusCard>
        ) : (
          <div className="flex items-start gap-4 rounded-xl border border-border bg-surface-muted p-4">
            <span className="relative mt-1 grid size-3 shrink-0 place-items-center" aria-hidden>
              <span className="absolute size-3 rounded-full bg-brand animate-pulse-ring" />
              <span className="size-3 rounded-full bg-brand" />
            </span>
            <div>
              <p className="font-semibold">Analyzing your resume…</p>
              <p className="mt-1 text-sm text-muted">
                Your application is already saved. We&apos;re extracting your details so you can
                review them — this usually takes under a minute. You can safely close this page.
              </p>
            </div>
          </div>
        )}
        {(extraction.status === "parsed" || extraction.status === "error") && (
          <ConfirmProfileModal
            open={modalOpen}
            candidate={
              extraction.status === "parsed"
                ? extraction.candidate
                : { ...EMPTY_PROFILE, email, full_name: fullName || null }
            }
            submitting={confirming}
            error={confirmError}
            onOpenChange={setModalOpen}
            onConfirm={confirm}
          />
        )}
      </div>
    );
  }

  const busy = step.name === "uploading" || step.name === "submitting";

  return (
    <form onSubmit={apply} className="space-y-4">
      {busy && file ? (
        <UploadProgress
          fileName={file.file.name}
          progress={step.name === "uploading" ? step.progress : 1}
          label={step.name === "uploading" ? "Uploading securely…" : "Submitting application…"}
        />
      ) : (
        <label
          htmlFor={inputId}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed px-6 py-8 text-center transition-all duration-200 ${
            dragging
              ? "scale-[1.01] border-brand bg-[color-mix(in_srgb,var(--brand)_8%,transparent)]"
              : "border-border bg-surface-muted hover:border-brand/60"
          }`}
        >
          <svg aria-hidden viewBox="0 0 24 24" className="size-8 text-brand" fill="none" stroke="currentColor" strokeWidth={1.6}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 16V4m0 0-4 4m4-4 4 4M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
          </svg>
          {file ? (
            <>
              <span className="font-semibold">{file.file.name}</span>
              <span className="text-sm text-muted">Click or drop to replace</span>
            </>
          ) : (
            <>
              <span className="font-semibold">Drop your resume here</span>
              <span className="text-sm text-muted">or click to browse · PDF or DOCX, up to 15 MB</span>
            </>
          )}
          <input
            id={inputId}
            type="file"
            accept={RESUME_ACCEPT}
            className="sr-only"
            onChange={(event) => void chooseFile(event.target.files?.[0])}
          />
        </label>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="text-sm font-medium">Email</span>
          <input
            type="email"
            required
            autoComplete="email"
            value={email}
            disabled={busy}
            onChange={(event) => setEmail(event.target.value)}
            className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm outline-none transition focus:border-brand focus:ring-2 focus:ring-[var(--ring)]"
          />
        </label>
        <label className="block">
          <span className="text-sm font-medium">
            Full name <span className="font-normal text-muted">(optional)</span>
          </span>
          <input
            type="text"
            autoComplete="name"
            value={fullName}
            disabled={busy}
            onChange={(event) => setFullName(event.target.value)}
            className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm outline-none transition focus:border-brand focus:ring-2 focus:ring-[var(--ring)]"
          />
        </label>
      </div>

      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={!file || !email || busy}
        className="w-full rounded-lg bg-brand px-4 py-3 font-semibold text-brand-contrast shadow-sm transition hover:opacity-90 active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-50"
      >
        {busy ? "Sending…" : "Apply in seconds"}
      </button>
      <p className="text-center text-xs text-muted">No account needed. Your resume is uploaded securely.</p>
    </form>
  );
}

function StatusCard({
  tone,
  title,
  children,
}: {
  tone: "success" | "neutral";
  title: string;
  children: ReactNode;
}) {
  return (
    <div
      className={`rounded-xl border p-4 animate-fade-in ${
        tone === "success"
          ? "border-[color-mix(in_srgb,var(--success)_35%,transparent)] bg-[color-mix(in_srgb,var(--success)_8%,transparent)]"
          : "border-border bg-surface-muted"
      }`}
    >
      <p className="font-semibold">{title}</p>
      <div className="mt-1 text-sm text-muted">{children}</div>
    </div>
  );
}
