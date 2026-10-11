import { EMPLOYMENT_LABELS, formatSalary, REMOTE_LABELS } from "@/lib/format";
import type { PublicJobSummary } from "@/lib/types";

export function JobMeta({ job }: { job: PublicJobSummary }) {
  const salary = formatSalary(job);
  const items = [
    job.location,
    REMOTE_LABELS[job.remote_policy],
    EMPLOYMENT_LABELS[job.employment_type],
    job.seniority_level,
    salary,
  ].filter(Boolean);
  return (
    <ul className="flex flex-wrap gap-2 text-sm text-muted">
      {items.map((item) => (
        <li key={item} className="rounded-full border border-border bg-surface px-3 py-1">
          {item}
        </li>
      ))}
    </ul>
  );
}
