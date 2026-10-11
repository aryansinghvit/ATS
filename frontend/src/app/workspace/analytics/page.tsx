"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { ApiError } from "@/lib/api/errors";
import { fetchJobs, fetchPipelineAnalytics } from "@/lib/api/workspace";
import type { JobListItem, PipelineAnalytics, StageFunnel } from "@/lib/types";

const MAX_RAMP_STAGES = 7; // the OKLab funnel ramp is validated up to 7 ordinal steps

const pct = (value: number | null, digits = 0) => (value == null ? "—" : `${(value * 100).toFixed(digits)}%`);
const days = (value: number | null) => (value == null ? "—" : `${value.toFixed(value < 10 ? 1 : 0)} d`);
const shortDate = (iso: string) =>
  new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });

function rampColor(index: number, count: number) {
  if (count > MAX_RAMP_STAGES) return "var(--chart-series)";
  const share = count <= 1 ? 0 : Math.round((index / (count - 1)) * 100);
  return `color-mix(in oklab, var(--chart-ordinal-start), var(--chart-ordinal-end) ${share}%)`;
}

/** Rounds an axis maximum up to a readable tick value. */
function niceMax(value: number) {
  if (value <= 4) return Math.max(1, value);
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((m) => m * magnitude >= value / 4)! * magnitude;
  return Math.ceil(value / step) * step;
}

function Card({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  return (
    <section className="rounded-2xl border border-border bg-surface p-5">
      <h2 className="font-semibold tracking-tight">{title}</h2>
      {subtitle && <p className="text-sm text-muted">{subtitle}</p>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

function StatTile({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="rounded-2xl border border-border bg-surface p-4">
      <p className="text-sm text-muted">{label}</p>
      <p className="mt-1 text-3xl font-semibold tracking-tight">{value}</p>
      {detail && <p className="mt-1 text-xs text-muted">{detail}</p>}
    </div>
  );
}

function Tooltip({ rows }: { rows: [string, string][] }) {
  return (
    <div
      role="tooltip"
      className="pointer-events-none absolute z-20 min-w-48 rounded-lg border border-border bg-surface p-2.5 text-xs shadow-lg"
    >
      <dl className="grid grid-cols-[auto_auto] gap-x-4 gap-y-0.5">
        {rows.map(([key, value], index) => (
          <div key={key} className="contents">
            <dt className={index === 0 ? "col-span-2 pb-1 text-sm font-semibold text-foreground" : "text-muted"}>{key}</dt>
            {index > 0 && <dd className="text-right tabular-nums text-foreground">{value}</dd>}
          </div>
        ))}
      </dl>
    </div>
  );
}

function stageTooltip(stage: StageFunnel): [string, string][] {
  return [
    [stage.name, ""],
    ["Reached", String(stage.reached)],
    ["Currently here", String(stage.current)],
    ["Conversion from previous", pct(stage.conversion_from_previous, 1)],
    ["Drop-off to next stage", pct(stage.drop_off_rate, 1)],
    ["Rejected at this stage", String(stage.rejected_here)],
    ["Avg. time in stage", days(stage.avg_days_in_stage)],
  ];
}

function FunnelChart({ funnel }: { funnel: StageFunnel[] }) {
  const [active, setActive] = useState<number | null>(null);
  const max = Math.max(1, ...funnel.map((stage) => stage.reached));
  return (
    <div className="space-y-0.5" onMouseLeave={() => setActive(null)}>
      {funnel.map((stage, index) => {
        const width = (stage.reached / max) * 100;
        return (
          <div
            key={stage.name}
            tabIndex={0}
            onMouseEnter={() => setActive(index)}
            onFocus={() => setActive(index)}
            onBlur={() => setActive(null)}
            aria-label={`${stage.name}: ${stage.reached} reached, ${pct(stage.conversion_from_previous, 1)} from previous stage`}
            className="relative grid grid-cols-[7rem_1fr_auto] items-center gap-3 rounded-md py-1.5 outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] sm:grid-cols-[10rem_1fr_9rem]"
          >
            <span className="truncate text-sm">{stage.name}</span>
            <div className="h-5">
              <div
                className="h-full rounded-r-[4px] transition-[width,opacity]"
                style={{
                  width: `max(${width}%, 2px)`,
                  background: rampColor(index, funnel.length),
                  opacity: active === null || active === index ? 1 : 0.55,
                }}
              />
            </div>
            <span className="text-right text-sm tabular-nums">
              {stage.reached}
              {index > 0 && (
                <span className="ml-2 text-xs text-muted">{pct(stage.conversion_from_previous)} of prev.</span>
              )}
            </span>
            {active === index && (
              <div className="absolute left-[7.75rem] top-full sm:left-[10.75rem]">
                <Tooltip rows={stageTooltip(stage)} />
              </div>
            )}
          </div>
        );
      })}
      <p className="pt-2 text-xs text-muted">
        Cumulative: an application counts toward every stage up to the furthest one it reached. Rejected applications
        count at the stage they left.
      </p>
    </div>
  );
}

function WeeklyChart({ weeks }: { weeks: PipelineAnalytics["weekly_applications"] }) {
  const [active, setActive] = useState<number | null>(null);
  const top = niceMax(Math.max(0, ...weeks.map((week) => week.applications)));
  const ticks = [top, top / 2, 0];
  return (
    <div className="grid grid-cols-[2rem_1fr] gap-2">
      <div className="relative h-40 text-right text-xs tabular-nums text-muted">
        {ticks.map((tick, index) => (
          <span key={tick} className="absolute right-0 -translate-y-1/2" style={{ top: `${(index / 2) * 100}%` }}>
            {Number.isInteger(tick) ? tick : tick.toFixed(1)}
          </span>
        ))}
      </div>
      <div>
        <div className="relative h-40" onMouseLeave={() => setActive(null)}>
          {ticks.map((tick, index) => (
            <div
              key={tick}
              aria-hidden
              className="absolute inset-x-0 h-px bg-[var(--chart-grid)]"
              style={{ top: `${(index / 2) * 100}%` }}
            />
          ))}
          <div className="absolute inset-0 flex items-end gap-[2px]">
            {weeks.map((week, index) => (
              <div
                key={week.week_start}
                tabIndex={0}
                onMouseEnter={() => setActive(index)}
                onFocus={() => setActive(index)}
                onBlur={() => setActive(null)}
                aria-label={`Week of ${shortDate(week.week_start)}: ${week.applications} applications`}
                className="relative flex h-full flex-1 items-end justify-center outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
              >
                <div
                  className="w-full max-w-10 rounded-t-[4px]"
                  style={{
                    height: week.applications ? `${(week.applications / top) * 100}%` : 0,
                    background: "var(--chart-series)",
                    opacity: active === null || active === index ? 1 : 0.55,
                  }}
                />
                {active === index && (
                  <div className={`absolute bottom-full mb-2 ${index > weeks.length / 2 ? "right-0" : "left-0"}`}>
                    <Tooltip
                      rows={[
                        [`Week of ${shortDate(week.week_start)}`, ""],
                        ["Applications", String(week.applications)],
                      ]}
                    />
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
        <div className="mt-1.5 flex gap-[2px] text-xs text-muted">
          {weeks.map((week, index) => (
            <span key={week.week_start} className="flex-1 truncate text-center">
              {index % 2 === weeks.length % 2 ? shortDate(week.week_start) : ""}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

function DataTable({ headers, rows }: { headers: string[]; rows: (string | number)[][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-muted">
          <tr>
            {headers.map((header, index) => (
              <th key={header} scope="col" className={`py-2 pr-4 font-medium ${index > 0 ? "text-right" : ""}`}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={`${row[0]}-${rowIndex}`} className="border-t border-border">
              {row.map((cell, index) => (
                <td key={index} className={`py-2 pr-4 ${index > 0 ? "text-right tabular-nums" : ""}`}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AnalyticsPage() {
  const [jobs, setJobs] = useState<JobListItem[]>([]);
  const [jobId, setJobId] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [view, setView] = useState<"chart" | "table">("chart");
  const [data, setData] = useState<PipelineAnalytics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchJobs().then(setJobs).catch(() => setJobs([]));
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchPipelineAnalytics({ jobId, from, to })
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof ApiError ? caught.message : "Could not load analytics.");
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [jobId, from, to]);

  const funnelRows = useMemo(
    () =>
      data?.funnel.map((stage) => [
        stage.name,
        stage.reached,
        stage.current,
        pct(stage.conversion_from_previous, 1),
        pct(stage.drop_off_rate, 1),
        stage.rejected_here,
        days(stage.avg_days_in_stage),
      ]) ?? [],
    [data],
  );

  const control = "rounded-lg border border-border bg-surface px-3 py-1.5 text-sm";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Pipeline analytics</h1>
        <p className="text-sm text-muted">Conversion and drop-off by stage, and how long hiring takes.</p>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <label className="space-y-1 text-xs text-muted">
          <span className="block">Job</span>
          <select value={jobId} onChange={(event) => setJobId(event.target.value)} className={`${control} text-foreground`}>
            <option value="">All jobs</option>
            {jobs.map((job) => (
              <option key={job.id} value={job.id}>
                {job.title}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1 text-xs text-muted">
          <span className="block">Applied from</span>
          <input type="date" value={from} max={to || undefined} onChange={(event) => setFrom(event.target.value)} className={`${control} text-foreground`} />
        </label>
        <label className="space-y-1 text-xs text-muted">
          <span className="block">to</span>
          <input type="date" value={to} min={from || undefined} onChange={(event) => setTo(event.target.value)} className={`${control} text-foreground`} />
        </label>
        {(jobId || from || to) && (
          <button
            type="button"
            onClick={() => {
              setJobId("");
              setFrom("");
              setTo("");
            }}
            className="py-1.5 text-sm text-brand hover:underline"
          >
            Clear filters
          </button>
        )}
        <div role="radiogroup" aria-label="Display" className="ml-auto inline-flex rounded-lg border border-border bg-surface p-0.5">
          {(["chart", "table"] as const).map((option) => (
            <button
              key={option}
              type="button"
              role="radio"
              aria-checked={view === option}
              onClick={() => setView(option)}
              className={`rounded-md px-3 py-1 text-sm capitalize ${view === option ? "bg-surface-muted font-medium" : "text-muted"}`}
            >
              {option}
            </button>
          ))}
        </div>
      </div>

      {error && !data ? (
        <div className="space-y-2">
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
          <Link href="/workspace/session" className="text-sm font-medium text-brand hover:underline">
            Set up a workspace session
          </Link>
        </div>
      ) : !data ? (
        <p className="text-sm text-muted">Loading…</p>
      ) : (
        <div className={`space-y-6 transition-opacity ${loading ? "opacity-60" : ""}`} aria-busy={loading}>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile
              label="Applications"
              value={data.totals.applications.toLocaleString()}
              detail={`${data.totals.withdrawn} withdrawn`}
            />
            <StatTile label="In progress" value={data.totals.in_progress.toLocaleString()} detail="Not yet hired or rejected" />
            <StatTile
              label="Hire rate"
              value={pct(data.totals.hire_rate, 1)}
              detail={`${data.totals.hired} hired · ${data.totals.rejected} rejected`}
            />
            <StatTile
              label="Median time to hire"
              value={days(data.time_to_hire.median_days)}
              detail={
                data.time_to_hire.hired
                  ? `Average ${days(data.time_to_hire.average_days)} · 90th percentile ${days(data.time_to_hire.p90_days)}`
                  : "No hires in this range yet"
              }
            />
          </div>

          <div className="grid gap-6 xl:grid-cols-[3fr_2fr]">
            <Card
              title="Stage funnel"
              subtitle={data.job_id ? "Stages of the selected job" : "All jobs, grouped by stage name"}
            >
              {data.funnel.length === 0 || data.totals.applications === 0 ? (
                <p className="text-sm text-muted">No applications in this range.</p>
              ) : view === "chart" ? (
                <FunnelChart funnel={data.funnel} />
              ) : (
                <DataTable
                  headers={["Stage", "Reached", "Current", "Conversion", "Drop-off", "Rejected here", "Avg. time"]}
                  rows={funnelRows}
                />
              )}
            </Card>
            <Card title="Applications per week" subtitle="Last 12 weeks">
              {view === "chart" ? (
                <WeeklyChart weeks={data.weekly_applications} />
              ) : (
                <DataTable
                  headers={["Week of", "Applications"]}
                  rows={data.weekly_applications.map((week) => [shortDate(week.week_start), week.applications])}
                />
              )}
            </Card>
          </div>

          <Card title="By job">
            {data.jobs.length === 0 ? (
              <p className="text-sm text-muted">No jobs.</p>
            ) : (
              <DataTable
                headers={["Job", "Status", "Applications", "In progress", "Hired", "Rejected", "Avg. match", "Median time to hire"]}
                rows={data.jobs.map((job) => [
                  job.title,
                  job.status,
                  job.applications,
                  job.in_progress,
                  job.hired,
                  job.rejected,
                  job.average_match_score == null ? "—" : Math.round(job.average_match_score),
                  days(job.median_days_to_hire),
                ])}
              />
            )}
          </Card>
          <p className="text-xs text-muted">
            Generated {new Date(data.generated_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
          </p>
        </div>
      )}
    </div>
  );
}
