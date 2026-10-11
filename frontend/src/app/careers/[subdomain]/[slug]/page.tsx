import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { JobMeta } from "@/components/careers/JobMeta";
import { ResumeDropzone } from "@/components/careers/ResumeDropzone";
import { fetchJob } from "@/lib/api/public-jobs";
import { formatPostedDate, stripHtml } from "@/lib/format";

type Params = Promise<{ subdomain: string; slug: string }>;

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const { subdomain, slug } = await params;
  const job = await fetchJob(subdomain, slug);
  if (!job) return { title: "Role not found" };
  const title = `${job.title} at ${job.organization.name}`;
  const description = stripHtml(job.description_html).slice(0, 160);
  return { title, description, openGraph: { title, description, type: "website" } };
}

export default async function JobDetailPage({ params }: { params: Params }) {
  const { subdomain, slug } = await params;
  const job = await fetchJob(subdomain, slug);
  if (!job) notFound();

  return (
    <main className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
      {/* Google for Jobs structured data; "<" is escaped so the JSON cannot close the tag. */}
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(job.json_ld).replace(/</g, "\\u003c") }}
      />

      <Link
        href={`/careers/${subdomain}`}
        className="text-sm text-muted transition hover:text-foreground"
      >
        ← All roles at {job.organization.name}
      </Link>

      <div className="mt-6 grid gap-10 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <article>
          <p className="text-sm font-medium text-brand">{job.department}</p>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight sm:text-4xl">{job.title}</h1>
          <div className="mt-4">
            <JobMeta job={job} />
          </div>
          {job.published_at && (
            <p className="mt-3 text-xs text-muted">Posted {formatPostedDate(job.published_at)}</p>
          )}
          {/* Rendered server-side from markdown with raw HTML disabled (safe to inject). */}
          <div
            className="job-description mt-8"
            dangerouslySetInnerHTML={{ __html: job.description_html }}
          />
        </article>

        <aside className="lg:sticky lg:top-24 lg:self-start">
          <div className="rounded-2xl border border-border bg-surface p-6 shadow-sm">
            <h2 className="text-lg font-semibold tracking-tight">Apply for this role</h2>
            <p className="mt-1 text-sm text-muted">
              Drop your resume — we&apos;ll fill in the rest for you to confirm.
            </p>
            <div className="mt-5">
              <ResumeDropzone tenantSubdomain={subdomain} jobId={job.id} jobTitle={job.title} />
            </div>
          </div>
        </aside>
      </div>
    </main>
  );
}
