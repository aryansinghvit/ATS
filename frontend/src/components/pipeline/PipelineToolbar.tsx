"use client";

import { useEffect, useState } from "react";

import { SORT_LABELS } from "@/lib/pipeline";
import type { PipelineFilters, PipelineSort } from "@/lib/types";

export const DEFAULT_FILTERS: PipelineFilters = {
  sort: "default",
  min_score: null,
  missing_skill: null,
  q: null,
};

const SCORE_FLOORS = [null, 70, 85] as const;

interface PipelineToolbarProps {
  filters: PipelineFilters;
  requiredSkills: string[];
  onChange: (filters: PipelineFilters) => void;
}

/** Deterministic triage controls; the server applies them so results are identical everywhere. */
export function PipelineToolbar({ filters, requiredSkills, onChange }: PipelineToolbarProps) {
  const [search, setSearch] = useState(filters.q ?? "");

  useEffect(() => {
    const timer = setTimeout(() => {
      const q = search.trim() || null;
      if (q !== filters.q) onChange({ ...filters, q });
    }, 300);
    return () => clearTimeout(timer);
  }, [search, filters, onChange]);

  const selectClass =
    "rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm outline-none focus:border-brand";
  const active =
    filters.sort !== "default" || filters.min_score !== null || filters.missing_skill !== null || filters.q;

  return (
    <div className="flex flex-wrap items-end gap-3 rounded-xl border border-border bg-surface p-3" role="search">
      <label className="flex min-w-48 flex-1 flex-col gap-1 text-xs font-medium text-muted">
        Search
        <input
          type="search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Name or email"
          className={selectClass}
        />
      </label>
      <label className="flex flex-col gap-1 text-xs font-medium text-muted">
        Sort
        <select
          value={filters.sort}
          onChange={(event) => onChange({ ...filters, sort: event.target.value as PipelineSort })}
          className={selectClass}
        >
          {Object.entries(SORT_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-xs font-medium text-muted">
        Match score
        <select
          value={filters.min_score ?? ""}
          onChange={(event) =>
            onChange({ ...filters, min_score: event.target.value ? Number(event.target.value) : null })
          }
          className={selectClass}
        >
          {SCORE_FLOORS.map((floor) => (
            <option key={floor ?? "any"} value={floor ?? ""}>
              {floor === null ? "Any score" : `${floor}+`}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-xs font-medium text-muted">
        Missing skill
        <select
          value={filters.missing_skill ?? ""}
          onChange={(event) => onChange({ ...filters, missing_skill: event.target.value || null })}
          className={selectClass}
        >
          <option value="">Any</option>
          {requiredSkills.map((skill) => (
            <option key={skill} value={skill}>
              Missing {skill}
            </option>
          ))}
        </select>
      </label>
      {active && (
        <button
          type="button"
          onClick={() => {
            setSearch("");
            onChange(DEFAULT_FILTERS);
          }}
          className="rounded-lg px-3 py-1.5 text-sm text-muted hover:bg-surface-muted"
        >
          Reset
        </button>
      )}
    </div>
  );
}
