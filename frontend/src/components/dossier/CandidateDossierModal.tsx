"use client";

import * as Dialog from "@radix-ui/react-dialog";
import dynamic from "next/dynamic";
import { useEffect, useState, type ReactNode } from "react";

import { ApiError } from "@/lib/api/errors";
import { eraseCandidate, fetchDossier } from "@/lib/api/workspace";
import { scoreTone } from "@/lib/pipeline";
import type { CandidateDossier, SkillEvidence } from "@/lib/types";
import { useCurrentUser } from "@/lib/use-current-user";

import { EmailsPanel } from "./EmailsPanel";
import { InterviewsOfferPanel } from "./InterviewsOfferPanel";
import { NotesPanel } from "./NotesPanel";
import { ScorecardPanel } from "./ScorecardPanel";
import { ScreeningPanel } from "./ScreeningPanel";

// PDF.js touches browser-only APIs, so the viewer is loaded on the client only.
const ResumeViewer = dynamic(() => import("./ResumeViewer"), {
  ssr: false,
  loading: () => <p className="p-4 text-sm text-muted">Loading viewer…</p>,
});

const TONE_TEXT = {
  high: "text-[#15803d] dark:text-[#4ade80]",
  medium: "text-[#a16207] dark:text-[#facc15]",
  low: "text-[#b91c1c] dark:text-[#f87171]",
  pending: "text-muted",
} as const;

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">{title}</h3>
      {children}
    </section>
  );
}

function EvidenceList({ icon, label, items }: { icon: string; label: string; items: SkillEvidence[] }) {
  return (
    <div className="rounded-xl border border-border bg-surface p-3">
      <p className="text-sm font-medium">
        <span aria-hidden>{icon}</span> {label}{" "}
        <span className="text-muted tabular-nums">({items.length})</span>
      </p>
      {items.length === 0 ? (
        <p className="mt-1 text-xs text-muted">None</p>
      ) : (
        <ul className="mt-2 space-y-1.5">
          {items.map((item) => (
            <li key={item.skill} className="text-sm">
              <span className="font-medium">{item.skill}</span>
              {item.evidence && <span className="block text-xs text-muted">{item.evidence}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ProfilePane({ dossier }: { dossier: CandidateDossier }) {
  const { candidate, match, profile } = dossier;
  const score = match?.deterministic_match_score ?? null;
  const verified = new Set(profile.verified_skills.map((skill) => skill.toLowerCase()));
  const links = [
    candidate.email && { label: candidate.email, href: `mailto:${candidate.email}` },
    candidate.phone && { label: candidate.phone, href: `tel:${candidate.phone}` },
    candidate.linkedin_url && { label: "LinkedIn", href: candidate.linkedin_url },
    candidate.github_url && { label: "GitHub", href: candidate.github_url },
    candidate.portfolio_url && { label: "Portfolio", href: candidate.portfolio_url },
  ].filter(Boolean) as { label: string; href: string }[];

  return (
    <div className="space-y-6 p-5">
      {dossier.redacted && (
        <p className="rounded-lg border border-border bg-surface-muted px-3 py-2 text-xs text-muted">
          Blind review: name, contact details, graduation years, the match score and the original
          resume are hidden to reduce bias.
        </p>
      )}

      <header className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <Dialog.Title className="truncate text-xl font-semibold tracking-tight">
            {candidate.display_name}
          </Dialog.Title>
          <Dialog.Description className="text-sm text-muted">
            {dossier.job.title} · {dossier.stage?.name ?? "Unassigned"} ·{" "}
            <span className="capitalize">{dossier.status.replace(/_/g, " ")}</span>
            {dossier.confirmed_at && " · Profile confirmed by candidate"}
          </Dialog.Description>
          {candidate.location && <p className="mt-1 text-sm text-muted">{candidate.location}</p>}
          {links.length > 0 && (
            <ul className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-sm">
              {links.map((link) => (
                <li key={link.href}>
                  <a href={link.href} target="_blank" rel="noopener noreferrer" className="text-brand hover:underline">
                    {link.label}
                  </a>
                </li>
              ))}
            </ul>
          )}
        </div>
        {match && (
          <div className="shrink-0 text-right">
            <p className={`text-3xl font-bold tabular-nums ${TONE_TEXT[scoreTone(score)]}`}>
              {score != null ? Math.round(score) : "—"}
              <span className="text-base font-medium text-muted">/100</span>
            </p>
            <p className="text-xs text-muted">Deterministic match</p>
          </div>
        )}
      </header>

      {match && (
        <Section title="Match breakdown">
          {match.extraction_status === "pending" && (
            <p className="text-sm text-muted">The resume is still being analyzed.</p>
          )}
          {match.extraction_status === "failed" && (
            <p className="text-sm text-danger">Extraction failed: {match.extraction_error}</p>
          )}
          {match.extraction_status === "complete" && (
            <>
              <div className="grid gap-3 sm:grid-cols-2">
                <EvidenceList icon="🟢" label="Matched required" items={match.matched_required} />
                <EvidenceList icon="🔴" label="Missing required" items={match.missing_required} />
                <EvidenceList icon="🟡" label="Matched preferred" items={match.matched_preferred} />
                <EvidenceList icon="⚪" label="Missing preferred" items={match.missing_preferred} />
              </div>
              {match.experience_match && (
                <p className="text-sm text-muted">
                  Experience: {match.experience_match.candidate_years ?? "?"} yrs vs{" "}
                  {match.experience_match.required_years ?? "?"} required —{" "}
                  {match.experience_match.meets_criteria ? "meets criteria" : "below requirement"}
                </p>
              )}
            </>
          )}
        </Section>
      )}

      {profile.summary && (
        <Section title="Summary">
          <p className="text-sm leading-relaxed">{profile.summary}</p>
        </Section>
      )}

      {profile.skills.length > 0 && (
        <Section title="Skills">
          <ul className="flex flex-wrap gap-1.5">
            {profile.skills.map((skill) => (
              <li key={skill} className="rounded-md bg-surface-muted px-2 py-0.5 text-sm">
                {skill}
                {verified.has(skill.toLowerCase()) && (
                  <span className="ml-1 text-success" title="Verified by the candidate">✓</span>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {profile.work_experience.length > 0 && (
        <Section title="Experience">
          <ol className="relative space-y-4 border-l border-border pl-4">
            {profile.work_experience.map((role, index) => (
              <li key={`${role.company}-${index}`} className="relative">
                <span aria-hidden className="absolute -left-[21px] top-1.5 size-2.5 rounded-full bg-brand" />
                <p className="font-medium">
                  {role.title}
                  {role.company && <span className="text-muted"> · {role.company}</span>}
                </p>
                <p className="text-xs text-muted">
                  {[role.start_date, role.end_date].filter(Boolean).join(" – ")}
                  {role.location && ` · ${role.location}`}
                </p>
                {role.highlights && role.highlights.length > 0 && (
                  <ul className="mt-1 list-disc space-y-0.5 pl-4 text-sm">
                    {role.highlights.map((highlight) => (
                      <li key={highlight}>{highlight}</li>
                    ))}
                  </ul>
                )}
                {role.technologies && role.technologies.length > 0 && (
                  <p className="mt-1 text-xs text-muted">{role.technologies.join(" · ")}</p>
                )}
              </li>
            ))}
          </ol>
        </Section>
      )}

      {profile.projects.length > 0 && (
        <Section title="Projects">
          <ul className="space-y-3">
            {profile.projects.map((project, index) => (
              <li key={`${project.name}-${index}`}>
                <p className="font-medium">
                  {project.url ? (
                    <a href={project.url} target="_blank" rel="noopener noreferrer" className="hover:underline">
                      {project.name}
                    </a>
                  ) : (
                    project.name
                  )}
                </p>
                {project.description && <p className="text-sm">{project.description}</p>}
                {project.technologies && (
                  <p className="text-xs text-muted">{project.technologies.join(" · ")}</p>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {profile.education.length > 0 && (
        <Section title="Education">
          <ul className="space-y-2">
            {profile.education.map((school, index) => (
              <li key={`${school.institution}-${index}`} className="text-sm">
                <span className="font-medium">{school.degree}</span>
                {school.institution && <span className="text-muted"> · {school.institution}</span>}
                {school.graduation_year && <span className="text-muted"> · {school.graduation_year}</span>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {dossier.stage_history.length > 0 && (
        <Section title="Stage history">
          <ol className="space-y-1 text-sm">
            {dossier.stage_history.map((event) => (
              <li key={event.at} className="text-muted">
                <span className="text-foreground">
                  {event.from_stage ?? "—"} → {event.to_stage}
                </span>{" "}
                · {event.actor_name ?? "System"} ·{" "}
                {new Date(event.at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
              </li>
            ))}
          </ol>
        </Section>
      )}
    </div>
  );
}

type LeftTab = "profile" | "scorecards" | "notes" | "screening" | "interviews" | "emails";
const TAB_LABELS: Record<LeftTab, string> = {
  profile: "Profile",
  scorecards: "Scorecards",
  notes: "Notes",
  screening: "AI screening",
  interviews: "Interviews & offer",
  emails: "Emails",
};

/** GDPR Article 17: permanently erase the candidate, every application, file and recording. */
function ErasePanel({ dossier, onErased }: { dossier: CandidateDossier; onErased: () => void }) {
  const [open, setOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const expected = dossier.candidate.email ?? "ERASE";

  const erase = async () => {
    setBusy(true);
    setError(null);
    try {
      await eraseCandidate(dossier.candidate.id);
      onErased();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Erasure failed.");
      setBusy(false);
    }
  };

  return (
    <section className="mx-5 mb-6 space-y-2 rounded-xl border border-[color-mix(in_srgb,var(--danger)_40%,transparent)] p-4">
      <h3 className="text-sm font-semibold text-danger">Right to erasure (GDPR Art. 17)</h3>
      {!open ? (
        <button type="button" onClick={() => setOpen(true)} className="text-sm font-medium text-danger hover:underline">
          Permanently erase this candidate…
        </button>
      ) : (
        <div className="space-y-2 text-sm">
          <p className="text-muted">
            Deletes the candidate&apos;s profile, every application across all jobs, resumes, screening recordings,
            offer letters, scorecards and notes. Only an anonymous audit record remains. This cannot be undone.
          </p>
          <label className="block space-y-1 text-xs text-muted">
            <span className="block">
              Type <strong className="font-mono text-foreground">{expected}</strong> to confirm
            </span>
            <input
              value={confirmText}
              onChange={(event) => setConfirmText(event.target.value)}
              className="w-full rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm text-foreground"
            />
          </label>
          {error && (
            <p role="alert" className="text-danger">
              {error}
            </p>
          )}
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => void erase()}
              disabled={busy || confirmText.trim() !== expected}
              className="rounded-lg bg-danger px-3 py-1.5 text-sm font-semibold text-background disabled:opacity-50"
            >
              {busy ? "Erasing…" : "Erase permanently"}
            </button>
            <button type="button" onClick={() => setOpen(false)} className="rounded-lg border border-border px-3 py-1.5">
              Cancel
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

/** Profile plus collaboration tabs; blind interviewers only get Profile and their scorecard. */
function LeftPane({ dossier, onErased }: { dossier: CandidateDossier; onErased: () => void }) {
  const me = useCurrentUser();
  const role = me?.user.role;
  const tabs: LeftTab[] = dossier.redacted
    ? ["profile", "scorecards"]
    : ["profile", "scorecards", "notes", "screening", "interviews", "emails"];
  const [tab, setTab] = useState<LeftTab>("profile");

  return (
    <div className="flex min-h-full flex-col">
      <div role="tablist" aria-label="Candidate sections" className="sticky top-0 z-10 flex gap-1 overflow-x-auto border-b border-border bg-background px-4 pt-2">
        {tabs.map((name) => (
          <button
            key={name}
            role="tab"
            aria-selected={tab === name}
            onClick={() => setTab(name)}
            className={`whitespace-nowrap rounded-t-md px-3 py-2 text-sm ${
              tab === name ? "border-b-2 border-brand font-medium" : "text-muted hover:text-foreground"
            }`}
          >
            {TAB_LABELS[name]}
          </button>
        ))}
      </div>
      {tab === "profile" ? (
        <>
          <ProfilePane dossier={dossier} />
          {role === "admin" && !dossier.redacted && <ErasePanel dossier={dossier} onErased={onErased} />}
        </>
      ) : (
        <div className="p-5">
          <Dialog.Title className="sr-only">{dossier.candidate.display_name}</Dialog.Title>
          {tab === "scorecards" && <ScorecardPanel applicationId={dossier.application_id} />}
          {tab === "notes" && <NotesPanel applicationId={dossier.application_id} />}
          {tab === "screening" && (
            <ScreeningPanel
              applicationId={dossier.application_id}
              canGenerate={role === "admin" || role === "recruiter"}
            />
          )}
          {tab === "interviews" && (
            <InterviewsOfferPanel
              applicationId={dossier.application_id}
              canGenerateOffer={role === "admin" || role === "recruiter"}
              jobTitle={dossier.job.title}
            />
          )}
          {tab === "emails" && <EmailsPanel applicationId={dossier.application_id} />}
        </div>
      )}
    </div>
  );
}

function ResumePane({ dossier }: { dossier: CandidateDossier }) {
  const { resume } = dossier;
  if (!resume.url) {
    return (
      <div className="grid h-full place-items-center p-6 text-center text-sm text-muted">
        The original resume is hidden during blind review.
      </div>
    );
  }
  if (resume.kind === "docx") {
    return (
      <div className="grid h-full place-items-center p-6 text-center text-sm">
        <div className="space-y-2">
          <p className="text-muted">Word documents can&apos;t be previewed in the browser.</p>
          <a href={resume.url} className="font-medium text-brand hover:underline">
            Download {resume.file_name ?? "resume"}
          </a>
        </div>
      </div>
    );
  }
  return (
    <ResumeViewer
      url={resume.url}
      fileName={resume.file_name}
      highlightTerms={dossier.match?.matched_required.map((item) => item.skill) ?? []}
    />
  );
}

interface CandidateDossierModalProps {
  applicationId: string | null;
  onClose: () => void;
  /** Called after an admin erased the candidate (GDPR); the caller should refresh its list. */
  onErased?: () => void;
}

export function CandidateDossierModal({ applicationId, onClose, onErased }: CandidateDossierModalProps) {
  const [dossier, setDossier] = useState<CandidateDossier | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"profile" | "resume">("profile");

  useEffect(() => {
    if (!applicationId) return;
    let cancelled = false;
    setDossier(null);
    setError(null);
    setTab("profile");
    fetchDossier(applicationId)
      .then((data) => !cancelled && setDossier(data))
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof ApiError ? caught.message : "Could not load profile.");
      });
    return () => {
      cancelled = true;
    };
  }, [applicationId]);

  return (
    <Dialog.Root open={applicationId !== null} onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/50 backdrop-blur-[2px] animate-fade-in" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex h-[92vh] w-[min(96vw,90rem)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-2xl border border-border bg-background shadow-2xl animate-pop-in">
          <div className="flex items-center justify-between border-b border-border bg-surface px-4 py-2">
            <div className="flex gap-1 lg:hidden" role="tablist" aria-label="Dossier views">
              {(["profile", "resume"] as const).map((name) => (
                <button
                  key={name}
                  role="tab"
                  aria-selected={tab === name}
                  onClick={() => setTab(name)}
                  className={`rounded-md px-3 py-1 text-sm capitalize ${tab === name ? "bg-surface-muted font-medium" : "text-muted"}`}
                >
                  {name}
                </button>
              ))}
            </div>
            <span className="hidden text-sm text-muted lg:block">Candidate dossier</span>
            <Dialog.Close className="rounded-md px-2 py-1 text-muted hover:bg-surface-muted" aria-label="Close">
              ✕
            </Dialog.Close>
          </div>

          {error ? (
            <div className="p-6">
              <Dialog.Title className="sr-only">Candidate dossier</Dialog.Title>
              <p role="alert" className="text-sm text-danger">
                {error}
              </p>
            </div>
          ) : !dossier ? (
            <div className="p-6">
              <Dialog.Title className="sr-only">Candidate dossier</Dialog.Title>
              <p className="text-sm text-muted">Loading profile…</p>
            </div>
          ) : (
            <div className="grid min-h-0 flex-1 lg:grid-cols-2">
              <div className={`min-h-0 overflow-y-auto ${tab === "profile" ? "block" : "hidden"} lg:block`}>
                <LeftPane
                  dossier={dossier}
                  onErased={() => {
                    onClose();
                    onErased?.();
                  }}
                />
              </div>
              <div className={`min-h-0 border-l border-border ${tab === "resume" ? "block" : "hidden"} lg:block`}>
                <ResumePane dossier={dossier} />
              </div>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
