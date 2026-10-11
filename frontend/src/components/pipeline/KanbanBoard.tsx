"use client";

import {
  closestCorners,
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  sortableKeyboardCoordinates,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api/errors";
import { fetchPipeline, moveStage } from "@/lib/api/workspace";
import { findCard, upsertCard } from "@/lib/pipeline";
import type { PipelineBoard, PipelineColumn, PipelineEvent, PipelineFilters } from "@/lib/types";
import { usePipelineStream } from "@/lib/use-pipeline-stream";

import { useCurrentUser } from "@/lib/use-current-user";

import { CandidateDossierModal } from "../dossier/CandidateDossierModal";
import { CandidateCardView, SortableCandidateCard } from "./CandidateCard";
import { DEFAULT_FILTERS, PipelineToolbar } from "./PipelineToolbar";
import { ResurfacingBanner } from "./ResurfacingBanner";

const PULSE_MS = 2400;

function Column({
  stage,
  jobTitle,
  pulsing,
  filtered,
  onOpen,
}: {
  stage: PipelineColumn;
  jobTitle: string;
  pulsing: Set<string>;
  filtered: boolean;
  onOpen: (applicationId: string) => void;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: stage.id, data: { stageId: stage.id } });
  return (
    <section
      aria-labelledby={`stage-${stage.id}`}
      className={`flex w-72 shrink-0 flex-col rounded-2xl border bg-surface-muted/60 transition-colors ${
        isOver ? "border-brand bg-[color-mix(in_srgb,var(--brand)_7%,transparent)]" : "border-border"
      }`}
    >
      <header className="flex items-center justify-between px-3 py-3">
        <h2 id={`stage-${stage.id}`} className="text-sm font-semibold uppercase tracking-wide">
          {stage.name}
        </h2>
        <span className="rounded-full bg-surface px-2 py-0.5 text-xs font-medium tabular-nums text-muted">
          {filtered ? `${stage.cards.length} of ${stage.total_count}` : stage.cards.length}
        </span>
      </header>
      <SortableContext
        items={stage.cards.map((card) => card.application_id)}
        strategy={verticalListSortingStrategy}
      >
        <ul ref={setNodeRef} className="flex min-h-24 flex-1 flex-col gap-2 px-2 pb-3">
          {stage.cards.map((card) => (
            <SortableCandidateCard
              key={card.application_id}
              card={card}
              jobTitle={jobTitle}
              pulsing={pulsing.has(card.application_id)}
              onOpen={onOpen}
            />
          ))}
          {stage.cards.length === 0 && (
            <li className="rounded-xl border border-dashed border-border px-3 py-6 text-center text-xs text-muted">
              {filtered && stage.total_count > 0 ? "No candidates match the filters" : "Drop candidates here"}
            </li>
          )}
        </ul>
      </SortableContext>
    </section>
  );
}

export function KanbanBoard({ jobId }: { jobId: string }) {
  const [board, setBoard] = useState<PipelineBoard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [pulsing, setPulsing] = useState<Set<string>>(new Set());
  const [filters, setFilters] = useState<PipelineFilters>(DEFAULT_FILTERS);
  const [openApplication, setOpenApplication] = useState<string | null>(null);
  const me = useCurrentUser();

  // Deep links from notifications: /workspace/jobs/<id>/pipeline?application=<id>
  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("application");
    if (requested) setOpenApplication(requested);
  }, []);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const reload = useCallback(async () => {
    try {
      setBoard(await fetchPipeline(jobId, filters));
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load the pipeline.");
    }
  }, [jobId, filters]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach((timer) => clearTimeout(timer));
  }, []);

  const pulse = useCallback((applicationId: string) => {
    setPulsing((current) => new Set(current).add(applicationId));
    clearTimeout(timers.current.get(applicationId));
    timers.current.set(
      applicationId,
      setTimeout(() => {
        setPulsing((current) => {
          const next = new Set(current);
          next.delete(applicationId);
          return next;
        });
      }, PULSE_MS),
    );
  }, []);

  const onStreamEvent = useCallback(
    (event: PipelineEvent) => {
      setBoard((current) => {
        if (!current) return current;
        const known = findCard(current, event.card.application_id);
        // Ignore stale echoes (e.g. our own optimistic move already applied a newer version).
        if (known && known.version > event.card.version) return current;
        return upsertCard(current, event.card);
      });
      pulse(event.card.application_id);
    },
    [pulse],
  );
  const streamStatus = usePipelineStream(jobId, onStreamEvent);

  const onDragStart = (event: DragStartEvent) => setActiveId(String(event.active.id));

  const onDragEnd = async (event: DragEndEvent) => {
    setActiveId(null);
    if (!board || !event.over) return;
    const card = findCard(board, String(event.active.id));
    const targetStageId =
      (event.over.data.current?.stageId as string | undefined) ?? String(event.over.id);
    if (!card || card.stage_id === targetStageId) return;

    const snapshot = board;
    // Optimistic update: show the card in its new column immediately.
    setBoard(
      upsertCard(board, {
        ...card,
        stage_id: targetStageId,
        stage_entered_at: new Date().toISOString(),
      }),
    );
    try {
      const saved = await moveStage(card.application_id, targetStageId, card.version);
      setBoard((current) => (current ? upsertCard(current, saved) : current));
      setNotice(null);
    } catch (caught) {
      setBoard(snapshot); // rollback
      const message = caught instanceof ApiError ? caught.message : "Move failed.";
      setNotice(message);
      if (caught instanceof ApiError && caught.status === 409) void reload();
    }
  };

  if (error) {
    return (
      <p role="alert" className="rounded-xl border border-border bg-surface p-4 text-sm text-danger">
        {error}
      </p>
    );
  }
  if (!board) {
    return <p className="text-sm text-muted">Loading pipeline…</p>;
  }

  const activeCard = activeId ? findCard(board, activeId) : undefined;
  const total = board.stages.reduce((sum, stage) => sum + stage.total_count, 0);
  const filtered =
    filters.min_score !== null || filters.missing_skill !== null || Boolean(filters.q);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{board.job.title}</h1>
          <p className="text-sm text-muted">
            {total} {total === 1 ? "applicant" : "applicants"} · Applied is ranked by deterministic
            match score
          </p>
        </div>
        <div className="flex items-center gap-4">
          {(me?.user.role === "admin" || me?.user.role === "recruiter") && (
            <Link href={`/workspace/jobs/${jobId}/stages`} className="text-sm font-medium text-brand hover:underline">
              Edit stages
            </Link>
          )}
          <span className="flex items-center gap-2 text-xs text-muted" aria-live="polite">
            <span
              aria-hidden
              className={`size-2 rounded-full ${streamStatus === "live" ? "bg-success" : "bg-border"}`}
            />
            {streamStatus === "live" ? "Live" : streamStatus === "connecting" ? "Connecting…" : "Reconnecting…"}
          </span>
        </div>
      </div>

      <ResurfacingBanner
        jobId={jobId}
        canInvite={me?.user.role === "admin" || me?.user.role === "recruiter"}
        onInvited={() => void reload()}
      />

      <PipelineToolbar
        filters={filters}
        requiredSkills={board.job.required_skills}
        onChange={setFilters}
      />

      {notice && (
        <p role="alert" className="rounded-lg border border-border bg-surface px-3 py-2 text-sm text-danger">
          {notice}
        </p>
      )}

      <DndContext
        sensors={sensors}
        collisionDetection={closestCorners}
        onDragStart={onDragStart}
        onDragEnd={onDragEnd}
        onDragCancel={() => setActiveId(null)}
      >
        <div className="flex gap-4 overflow-x-auto pb-4">
          {board.stages.map((stage) => (
            <Column
              key={stage.id}
              stage={stage}
              jobTitle={board.job.title}
              pulsing={pulsing}
              filtered={filtered}
              onOpen={setOpenApplication}
            />
          ))}
        </div>
        <DragOverlay>
          {activeCard ? <CandidateCardView card={activeCard} jobTitle={board.job.title} overlay /> : null}
        </DragOverlay>
      </DndContext>

      <CandidateDossierModal
        applicationId={openApplication}
        onClose={() => setOpenApplication(null)}
        onErased={() => void reload()}
      />
    </div>
  );
}
