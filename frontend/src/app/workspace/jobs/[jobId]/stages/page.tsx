"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";

import { ApiError } from "@/lib/api/errors";
import {
  createStage,
  deleteStage,
  fetchStageActions,
  fetchStages,
  reorderStages,
  updateStage,
} from "@/lib/api/workspace";
import type { StageAction, StageConfig, StageType } from "@/lib/types";

const STAGE_TYPES: { value: StageType; label: string }[] = [
  { value: "screen", label: "Screen" },
  { value: "interview", label: "Interview" },
  { value: "offer", label: "Offer" },
  { value: "hired", label: "Hired" },
  { value: "rejected", label: "Rejected" },
];

function StageRow({
  stage,
  index,
  count,
  actions,
  busy,
  onRename,
  onType,
  onToggleAction,
  onMove,
  onDelete,
}: {
  stage: StageConfig;
  index: number;
  count: number;
  actions: StageAction[];
  busy: boolean;
  onRename: (name: string) => void;
  onType: (type: StageType) => void;
  onToggleAction: (key: string) => void;
  onMove: (direction: -1 | 1) => void;
  onDelete: () => void;
}) {
  const [name, setName] = useState(stage.name);
  const isApplied = stage.stage_type === "applied";
  const commitName = () => {
    const trimmed = name.trim();
    if (trimmed && trimmed !== stage.name) onRename(trimmed);
    else setName(stage.name);
  };

  return (
    <li className="space-y-3 rounded-2xl border border-border bg-surface p-4">
      <div className="flex flex-wrap items-center gap-3">
        <span className="w-6 text-center text-sm font-semibold text-muted tabular-nums">{index + 1}</span>
        <label className="min-w-40 flex-1">
          <span className="sr-only">Stage name</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            onBlur={commitName}
            onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
            maxLength={100}
            disabled={busy}
            className="w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm font-medium"
          />
        </label>
        {isApplied ? (
          <span className="rounded-full bg-surface-muted px-2.5 py-1 text-xs text-muted">Applied · always first</span>
        ) : (
          <label>
            <span className="sr-only">Stage type</span>
            <select
              value={stage.stage_type}
              onChange={(event) => onType(event.target.value as StageType)}
              disabled={busy}
              className="rounded-lg border border-border bg-background px-2 py-1.5 text-sm"
            >
              {STAGE_TYPES.map((type) => (
                <option key={type.value} value={type.value}>
                  {type.label}
                </option>
              ))}
            </select>
          </label>
        )}
        <span className="text-xs text-muted tabular-nums">
          {stage.applications} {stage.applications === 1 ? "candidate" : "candidates"}
        </span>
        <div className="ml-auto flex gap-1">
          <button
            type="button"
            onClick={() => onMove(-1)}
            disabled={busy || isApplied || index <= 1}
            aria-label={`Move ${stage.name} up`}
            className="rounded-md border border-border px-2 py-1 text-sm disabled:opacity-40"
          >
            ↑
          </button>
          <button
            type="button"
            onClick={() => onMove(1)}
            disabled={busy || isApplied || index >= count - 1}
            aria-label={`Move ${stage.name} down`}
            className="rounded-md border border-border px-2 py-1 text-sm disabled:opacity-40"
          >
            ↓
          </button>
          <button
            type="button"
            onClick={onDelete}
            disabled={busy || isApplied || stage.applications > 0}
            title={stage.applications > 0 ? "Move candidates out of this stage before deleting it" : undefined}
            className="rounded-md border border-border px-2 py-1 text-sm text-danger disabled:opacity-40"
          >
            Delete
          </button>
        </div>
      </div>
      {actions.length > 0 && (
        <fieldset className="flex flex-wrap gap-x-4 gap-y-1 pl-9">
          <legend className="sr-only">Automated actions when a candidate enters {stage.name}</legend>
          {actions.map((action) => (
            <label key={action.key} className="flex items-center gap-1.5 text-xs text-muted" title={action.description}>
              <input
                type="checkbox"
                checked={stage.automated_actions.includes(action.key)}
                onChange={() => onToggleAction(action.key)}
                disabled={busy}
              />
              {action.key.replace(/_/g, " ")}
            </label>
          ))}
        </fieldset>
      )}
    </li>
  );
}

export default function StageEditorPage({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId } = use(params);
  const [stages, setStages] = useState<StageConfig[] | null>(null);
  const [actions, setActions] = useState<StageAction[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newType, setNewType] = useState<StageType>("interview");

  useEffect(() => {
    fetchStages(jobId)
      .then(setStages)
      .catch((caught: unknown) => setError(caught instanceof ApiError ? caught.message : "Could not load stages."));
    fetchStageActions().then(setActions).catch(() => setActions([]));
  }, [jobId]);

  const mutate = async (operation: () => Promise<StageConfig[]>) => {
    setBusy(true);
    setError(null);
    try {
      setStages(await operation());
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The change could not be saved.");
      setStages(await fetchStages(jobId).catch(() => stages));
    } finally {
      setBusy(false);
    }
  };

  const move = (index: number, direction: -1 | 1) => {
    if (!stages) return;
    const order = stages.map((stage) => stage.id);
    [order[index], order[index + direction]] = [order[index + direction], order[index]];
    void mutate(() => reorderStages(jobId, order));
  };

  const add = (event: React.FormEvent) => {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    void mutate(async () => {
      const result = await createStage(jobId, { name, stage_type: newType, automated_actions: [] });
      setNewName("");
      return result;
    });
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div className="space-y-1">
        <Link href={`/workspace/jobs/${jobId}/pipeline`} className="text-sm text-brand hover:underline">
          ← Back to pipeline
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight">Pipeline stages</h1>
        <p className="text-sm text-muted">
          Rename, retype, reorder or remove stages. Applied stays first; stages with candidates can&apos;t be deleted.
          Ticked actions run when a candidate is moved into the stage.
        </p>
      </div>
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      {!stages ? (
        !error && <p className="text-sm text-muted">Loading…</p>
      ) : (
        <>
          <ol className="space-y-3">
            {stages.map((stage, index) => (
              <StageRow
                key={`${stage.id}-${stage.name}`}
                stage={stage}
                index={index}
                count={stages.length}
                actions={actions}
                busy={busy}
                onRename={(name) => void mutate(() => updateStage(jobId, stage.id, { name }))}
                onType={(stage_type) => void mutate(() => updateStage(jobId, stage.id, { stage_type }))}
                onToggleAction={(key) =>
                  void mutate(() =>
                    updateStage(jobId, stage.id, {
                      automated_actions: stage.automated_actions.includes(key)
                        ? stage.automated_actions.filter((item) => item !== key)
                        : [...stage.automated_actions, key],
                    }),
                  )
                }
                onMove={(direction) => move(index, direction)}
                onDelete={() => {
                  if (window.confirm(`Delete the “${stage.name}” stage?`)) {
                    void mutate(() => deleteStage(jobId, stage.id));
                  }
                }}
              />
            ))}
          </ol>
          <form onSubmit={add} className="flex flex-wrap items-end gap-3 rounded-2xl border border-dashed border-border p-4">
            <label className="min-w-48 flex-1 space-y-1 text-xs text-muted">
              <span className="block">New stage</span>
              <input
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                maxLength={100}
                placeholder="e.g. Take-home assignment"
                className="w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm text-foreground"
              />
            </label>
            <label className="space-y-1 text-xs text-muted">
              <span className="block">Type</span>
              <select
                value={newType}
                onChange={(event) => setNewType(event.target.value as StageType)}
                className="rounded-lg border border-border bg-background px-2 py-1.5 text-sm text-foreground"
              >
                {STAGE_TYPES.map((type) => (
                  <option key={type.value} value={type.value}>
                    {type.label}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="submit"
              disabled={busy || !newName.trim()}
              className="rounded-lg bg-brand px-4 py-1.5 text-sm font-semibold text-brand-contrast disabled:opacity-60"
            >
              Add stage
            </button>
            <p className="w-full text-xs text-muted">New stages are inserted before Hired and Rejected.</p>
          </form>
        </>
      )}
    </div>
  );
}
