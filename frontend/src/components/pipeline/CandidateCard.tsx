"use client";

import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

import { scoreTone, type ScoreTone } from "@/lib/pipeline";
import type { PipelineCard } from "@/lib/types";

const BADGE_STYLES: Record<ScoreTone, string> = {
  high: "bg-[color-mix(in_srgb,#16a34a_16%,transparent)] text-[#15803d] dark:text-[#4ade80] ring-[#16a34a]/30",
  medium: "bg-[color-mix(in_srgb,#eab308_18%,transparent)] text-[#a16207] dark:text-[#facc15] ring-[#eab308]/30",
  low: "bg-[color-mix(in_srgb,#dc2626_14%,transparent)] text-[#b91c1c] dark:text-[#f87171] ring-[#dc2626]/30",
  pending: "bg-surface-muted text-muted ring-border",
};

export function ScoreBadge({ card }: { card: PipelineCard }) {
  const score = card.deterministic_match_score;
  const tone = scoreTone(score);
  const label =
    score != null
      ? `${Math.round(score)}/100`
      : card.extraction_status === "failed"
        ? "No score"
        : "Scoring…";
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded-lg px-2 py-1 text-sm font-bold tabular-nums ring-1 ${BADGE_STYLES[tone]}`}
      aria-label={score != null ? `Deterministic match score ${Math.round(score)} out of 100` : label}
    >
      {label}
    </span>
  );
}

const MAX_CHIPS = 3;

function Chips({ icon, label, skills }: { icon: string; label: string; skills: string[] }) {
  if (skills.length === 0) return null;
  const shown = skills.slice(0, MAX_CHIPS);
  return (
    <li className="flex flex-wrap items-center gap-1" aria-label={`${label}: ${skills.join(", ")}`}>
      <span aria-hidden className="text-xs">
        {icon}
      </span>
      {shown.map((skill) => (
        <span key={skill} className="rounded-md bg-surface-muted px-1.5 py-0.5 text-xs">
          {skill}
        </span>
      ))}
      {skills.length > MAX_CHIPS && (
        <span className="text-xs text-muted">+{skills.length - MAX_CHIPS}</span>
      )}
    </li>
  );
}

interface CandidateCardProps {
  card: PipelineCard;
  jobTitle: string;
  pulsing?: boolean;
  overlay?: boolean;
  onOpen?: (applicationId: string) => void;
}

export function CandidateCardView({ card, jobTitle, pulsing, overlay, onOpen }: CandidateCardProps) {
  const name = card.candidate.full_name ?? card.candidate.email;
  return (
    <article
      onClick={onOpen ? () => onOpen(card.application_id) : undefined}
      className={`cursor-pointer rounded-xl border bg-surface p-3 shadow-sm transition-shadow ${
        overlay ? "rotate-1 border-brand shadow-xl" : "border-border hover:shadow-md"
      } ${pulsing ? "animate-card-pulse" : ""}`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate font-semibold leading-tight">{name}</h3>
          <p className="truncate text-xs text-muted">{jobTitle}</p>
        </div>
        <ScoreBadge card={card} />
      </div>
      <ul className="mt-2 space-y-1">
        <Chips icon="🟢" label="Matched core skills" skills={card.match.matched_required} />
        <Chips icon="🟡" label="Preferred skills" skills={card.match.matched_preferred} />
        <Chips icon="🔴" label="Missing required skills" skills={card.match.missing_required} />
      </ul>
      <div className="mt-2 flex items-center justify-between gap-2">
        <p className="text-xs text-muted">
          {card.extraction_status === "pending" ? "Resume is being analyzed…" : ""}
        </p>
        {onOpen && (
          <button
            type="button"
            // Keep keyboard/pointer activation from starting a drag.
            onPointerDown={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
            onClick={(event) => {
              event.stopPropagation();
              onOpen(card.application_id);
            }}
            className="rounded-md px-2 py-0.5 text-xs font-medium text-brand hover:bg-surface-muted"
            aria-label={`View profile of ${name}`}
          >
            View profile
          </button>
        )}
      </div>
    </article>
  );
}

export function SortableCandidateCard(props: CandidateCardProps & { disabled?: boolean }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: props.card.application_id,
    data: { stageId: props.card.stage_id },
    disabled: props.disabled,
  });
  return (
    <li
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={`touch-none ${isDragging ? "opacity-40" : ""}`}
      {...attributes}
      {...listeners}
    >
      <CandidateCardView {...props} />
    </li>
  );
}
