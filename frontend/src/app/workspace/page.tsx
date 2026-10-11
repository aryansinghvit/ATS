"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiError } from "@/lib/api/errors";
import { fetchJobs } from "@/lib/api/workspace";
import type { JobListItem } from "@/lib/types";

export default function WorkspaceJobsPage() {
  const [jobs, setJobs] = useState<JobListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchJobs()
      .then(setJobs)
      .catch((caught: unknown) =>
        setError(caught instanceof ApiError ? caught.message : "Could not load jobs."),
      );
  }, []);

  if (error) {
    return (
      <div className="space-y-2">
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
        <Link href="/workspace/session" className="text-sm font-medium text-brand hover:underline">
          Set up a workspace session
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Job requisitions</h1>
      {!jobs ? (
        <p className="text-sm text-muted">Loading…</p>
      ) : jobs.length === 0 ? (
        <p className="text-sm text-muted">No jobs yet.</p>
      ) : (
        <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {jobs.map((job) => (
            <li key={job.id}>
              <Link
                href={`/workspace/jobs/${job.id}/pipeline`}
                className="block rounded-2xl border border-border bg-surface p-4 transition hover:-translate-y-0.5 hover:shadow-md"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm text-muted">{job.department}</span>
                  <span className="rounded-full bg-surface-muted px-2 py-0.5 text-xs capitalize">
                    {job.status}
                  </span>
                </div>
                <h2 className="mt-1 font-semibold">{job.title}</h2>
                <p className="mt-1 text-sm text-muted">{job.location}</p>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
