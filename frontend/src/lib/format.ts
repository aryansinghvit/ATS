import type { EmploymentType, PublicJobSummary, RemotePolicy } from "./types";

export const REMOTE_LABELS: Record<RemotePolicy, string> = {
  remote: "Remote",
  hybrid: "Hybrid",
  on_site: "On-site",
};

export const EMPLOYMENT_LABELS: Record<EmploymentType, string> = {
  full_time: "Full-time",
  part_time: "Part-time",
  contract: "Contract",
  temporary: "Temporary",
  internship: "Internship",
};

export function formatSalary(job: Pick<PublicJobSummary, "salary_min" | "salary_max" | "currency">) {
  const { salary_min: min, salary_max: max } = job;
  if (min == null && max == null) return null;
  const money = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: job.currency ?? "USD",
    maximumFractionDigits: 0,
  });
  if (min != null && max != null) return `${money.format(min)} – ${money.format(max)}`;
  return money.format((min ?? max) as number);
}

export function formatPostedDate(iso: string | null) {
  if (!iso) return null;
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(
    new Date(iso),
  );
}

/** Black or white text, whichever is more legible on the given hex background. */
export function readableTextColor(hex: string) {
  const value = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((offset) => {
    const channel = parseInt(value.slice(offset, offset + 2), 16) / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return luminance > 0.4 ? "#0b0d12" : "#ffffff";
}

export function stripHtml(html: string) {
  return html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}
