"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useState, type FormEvent, type KeyboardEvent } from "react";

import type { ParsedCandidate } from "@/lib/types";

interface ConfirmProfileModalProps {
  open: boolean;
  candidate: ParsedCandidate;
  submitting: boolean;
  error: string | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: (profile: ParsedCandidate) => void;
}

const FIELDS = [
  { key: "full_name", label: "Full name", type: "text", autoComplete: "name" },
  { key: "email", label: "Email", type: "email", autoComplete: "email" },
  { key: "phone", label: "Phone", type: "tel", autoComplete: "tel" },
  { key: "linkedin_url", label: "LinkedIn", type: "url", autoComplete: "url" },
] as const;

/** The 5-second review: candidates verify what the M3 extractor parsed before confirming. */
export function ConfirmProfileModal({
  open,
  candidate,
  submitting,
  error,
  onOpenChange,
  onConfirm,
}: ConfirmProfileModalProps) {
  const [profile, setProfile] = useState<ParsedCandidate>(candidate);
  const [skillDraft, setSkillDraft] = useState("");

  const addSkill = () => {
    const skill = skillDraft.trim();
    if (skill && !profile.skills.some((s) => s.toLowerCase() === skill.toLowerCase())) {
      setProfile({ ...profile, skills: [...profile.skills, skill] });
    }
    setSkillDraft("");
  };

  const onSkillKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      addSkill();
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    onConfirm(profile);
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/50 backdrop-blur-[2px] animate-fade-in" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 max-h-[90vh] w-[min(92vw,32rem)] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-2xl border border-border bg-surface p-6 shadow-2xl animate-pop-in">
          <Dialog.Title className="text-lg font-semibold tracking-tight">
            Quick check — does this look right?
          </Dialog.Title>
          <Dialog.Description className="mt-1 text-sm text-muted">
            We pulled these details from your resume. Fix anything that&apos;s off, then confirm.
          </Dialog.Description>

          <form onSubmit={submit} className="mt-5 space-y-4">
            {FIELDS.map((field) => (
              <label key={field.key} className="block">
                <span className="text-sm font-medium">{field.label}</span>
                <input
                  type={field.type}
                  autoComplete={field.autoComplete}
                  required={field.key === "email" || field.key === "full_name"}
                  value={profile[field.key] ?? ""}
                  onChange={(event) => setProfile({ ...profile, [field.key]: event.target.value })}
                  className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none transition focus:border-brand focus:ring-2 focus:ring-[var(--ring)]"
                />
              </label>
            ))}

            <div>
              <span id="skills-label" className="text-sm font-medium">
                Skills
              </span>
              <ul aria-labelledby="skills-label" className="mt-2 flex flex-wrap gap-2">
                {profile.skills.map((skill) => (
                  <li
                    key={skill}
                    className="flex items-center gap-1 rounded-full bg-surface-muted py-1 pl-3 pr-1 text-sm"
                  >
                    {skill}
                    <button
                      type="button"
                      onClick={() =>
                        setProfile({ ...profile, skills: profile.skills.filter((s) => s !== skill) })
                      }
                      className="grid size-5 place-items-center rounded-full text-muted transition hover:bg-border hover:text-foreground"
                      aria-label={`Remove ${skill}`}
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
              <input
                value={skillDraft}
                onChange={(event) => setSkillDraft(event.target.value)}
                onKeyDown={onSkillKeyDown}
                onBlur={addSkill}
                placeholder="Add a skill and press Enter"
                aria-label="Add a skill"
                className="mt-2 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none transition focus:border-brand focus:ring-2 focus:ring-[var(--ring)]"
              />
            </div>

            {error && (
              <p role="alert" className="text-sm text-danger">
                {error}
              </p>
            )}

            <div className="flex justify-end gap-3 pt-2">
              <Dialog.Close className="rounded-lg px-4 py-2 text-sm font-medium text-muted transition hover:bg-surface-muted">
                Review later
              </Dialog.Close>
              <button
                type="submit"
                disabled={submitting}
                className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-brand-contrast transition hover:opacity-90 active:scale-[0.98] disabled:opacity-60"
              >
                {submitting ? "Confirming…" : "Confirm application"}
              </button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
