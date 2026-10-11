import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { JobMeta } from "@/components/careers/JobMeta";
import { fetchJobBoard } from "@/lib/api/public-jobs";
import { formatPostedDate } from "@/lib/format";

type Params = Promise<{ subdomain: string }>;

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const { subdomain } = await params;
  const board = await fetchJobBoard(subdomain);
  if (!board) return { title: "Careers" };
  return {
    title: `Careers at ${board.organization.name}`,
    description: `${board.jobs.length} open roles at ${board.organization.name}. Apply in seconds.`,
  };
}

export default async function JobBoardPage({ params }: { params: Params }) {
  const { subdomain } = await params;
  const board = await fetchJobBoard(subdomain);
  if (!board) notFound();
  const { organization, jobs } = board;

  return (
    <main className="mx-auto max-w-6xl px-4 py-12 sm:px-6">
      <section className="max-w-2xl">
        <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
          Build what&apos;s next at {organization.name}
        </h1>
        <p className="mt-3 text-muted">
          {jobs.length === 0
            ? "There are no open roles right now — check back soon."
            : `${jobs.length} open ${jobs.length === 1 ? "role" : "roles"}. No account needed to apply.`}
        </p>
      </section>

      <ul className="mt-10 grid gap-4 md:grid-cols-2">
        {jobs.map((job) => (
          <li key={job.id}>
            <Link
              href={`/careers/${organization.subdomain}/${job.slug}`}
              className="group block h-full rounded-2xl border border-border bg-surface p-5 transition duration-200 hover:-translate-y-0.5 hover:border-brand/60 hover:shadow-lg"
            >
              <p className="text-sm font-medium text-brand">{job.department}</p>
              <h2 className="mt-1 text-lg font-semibold tracking-tight group-hover:text-brand">
                {job.title}
              </h2>
              <div className="mt-4">
                <JobMeta job={job} />
              </div>
              {job.published_at && (
                <p className="mt-4 text-xs text-muted">Posted {formatPostedDate(job.published_at)}</p>
              )}
            </Link>
          </li>
        ))}
      </ul>
    </main>
  );
}
