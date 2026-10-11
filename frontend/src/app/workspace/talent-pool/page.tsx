"use client";

import { useEffect, useState, type FormEvent } from "react";

import { CandidateDossierModal } from "@/components/dossier/CandidateDossierModal";
import { ApiError } from "@/lib/api/errors";
import { fetchTalentPoolStats, searchTalentPool } from "@/lib/api/workspace";
import type { TalentMatch, TalentPoolStats, TalentSearchResponse } from "@/lib/types";

const EXAMPLES = [
  "Full-stack engineer who has built robust APIs",
  "Engineers skilled in distributed systems and Go",
  "Kubernetes reliability engineer with infrastructure as code",
  "Machine learning engineer who trains NLP models",
];

/** Renders a ts_headline snippet, turning «term» markers into <mark> (no HTML injection). */
function Highlight({ snippet }: { snippet: string }) {
  const parts = snippet.split(/(«[^»]*»)/g);
  return (
    <>
      {parts.map((part, index) =>
        part.startsWith("«") ? (
          <mark key={index} className="rounded bg-[color-mix(in_srgb,var(--brand)_18%,transparent)] px-0.5 text-foreground">
            {part.slice(1, -1)}
          </mark>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </>
  );
}

function SimilarityBadge({ similarity }: { similarity: number }) {
  const percent = Math.round(similarity * 100);
  const tone =
    percent >= 80
      ? "text-[#15803d] dark:text-[#4ade80] ring-[#16a34a]/30"
      : percent >= 65
        ? "text-[#a16207] dark:text-[#facc15] ring-[#eab308]/30"
        : "text-muted ring-border";
  return (
    <span
      className={`shrink-0 rounded-lg px-2 py-1 text-sm font-bold tabular-nums ring-1 ${tone}`}
      title="Cosine similarity between your query and the candidate's profile"
    >
      {percent}% match
    </span>
  );
}

function ResultCard({ match, onOpen }: { match: TalentMatch; onOpen: (applicationId: string) => void }) {
  const matched = new Set(match.matched_skills.map((s) => s.toLowerCase()));
  const latest = match.applications[0];
  return (
    <article className="flex flex-col gap-3 rounded-2xl border border-border bg-surface p-4 shadow-sm transition hover:shadow-md">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="truncate font-semibold">{match.full_name ?? match.email}</h2>
          <p className="truncate text-sm text-muted">{match.location ?? match.email}</p>
        </div>
        <SimilarityBadge similarity={match.similarity} />
      </div>

      {match.skills.length > 0 && (
        <ul className="flex flex-wrap gap-1.5" aria-label="Skills">
          {match.skills.slice(0, 10).map((skill) => (
            <li
              key={skill}
              className={`rounded-md px-2 py-0.5 text-xs ${
                matched.has(skill.toLowerCase())
                  ? "bg-[color-mix(in_srgb,var(--brand)_16%,transparent)] font-semibold text-foreground"
                  : "bg-surface-muted text-muted"
              }`}
            >
              {skill}
            </li>
          ))}
        </ul>
      )}

      {match.highlights.length > 0 && (
        <ul className="space-y-1 text-sm text-muted">
          {match.highlights.map((snippet) => (
            <li key={snippet} className="line-clamp-2">
              …<Highlight snippet={snippet} />…
            </li>
          ))}
        </ul>
      )}

      {match.applications.length > 0 && (
        <div className="border-t border-border pt-3 text-xs text-muted">
          <p className="font-medium uppercase tracking-wider">Past applications</p>
          <ul className="mt-1 space-y-0.5">
            {match.applications.slice(0, 3).map((app) => (
              <li key={app.application_id} className="flex justify-between gap-2">
                <span className="truncate text-foreground">{app.job_title}</span>
                <span className="shrink-0 capitalize">
                  {app.status.replace(/_/g, " ")}
                  {app.deterministic_match_score != null && ` · ${Math.round(app.deterministic_match_score)}/100`}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {latest && (
        <button
          type="button"
          onClick={() => onOpen(latest.application_id)}
          className="mt-auto self-start rounded-lg px-3 py-1.5 text-sm font-medium text-brand hover:bg-surface-muted"
        >
          View profile
        </button>
      )}
    </article>
  );
}

export default function TalentPoolPage() {
  const [query, setQuery] = useState("");
  const [stats, setStats] = useState<TalentPoolStats | null>(null);
  const [response, setResponse] = useState<TalentSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openApplication, setOpenApplication] = useState<string | null>(null);

  useEffect(() => {
    fetchTalentPoolStats()
      .then(setStats)
      .catch(() => setStats(null));
  }, []);

  const run = async (text: string) => {
    const trimmed = text.trim();
    if (trimmed.length < 3) return;
    setQuery(trimmed);
    setLoading(true);
    setError(null);
    try {
      setResponse(await searchTalentPool(trimmed));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Search failed.");
    } finally {
      setLoading(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(query);
  };

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Talent pool</h1>
        <p className="text-sm text-muted">
          Search every past applicant in plain language. Ranking blends semantic similarity
          (on-device {stats?.model ?? "embeddings"}) with keyword matches.
          {stats && ` ${stats.indexed} of ${stats.candidates} profiles indexed.`}
        </p>
      </header>

      <form onSubmit={submit} role="search" className="flex flex-col gap-3 sm:flex-row">
        <label className="sr-only" htmlFor="talent-query">
          Describe who you are looking for
        </label>
        <input
          id="talent-query"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="e.g. Full-stack engineer who has built robust APIs"
          className="flex-1 rounded-xl border border-border bg-surface px-4 py-3 outline-none transition focus:border-brand focus:ring-2 focus:ring-[var(--ring)]"
        />
        <button
          type="submit"
          disabled={loading || query.trim().length < 3}
          className="rounded-xl bg-brand px-5 py-3 font-semibold text-brand-contrast transition hover:opacity-90 disabled:opacity-50"
        >
          {loading ? "Searching…" : "Search"}
        </button>
      </form>

      <ul className="flex flex-wrap gap-2" aria-label="Example searches">
        {EXAMPLES.map((example) => (
          <li key={example}>
            <button
              type="button"
              onClick={() => void run(example)}
              className="rounded-full border border-border bg-surface px-3 py-1 text-xs text-muted transition hover:border-brand hover:text-foreground"
            >
              {example}
            </button>
          </li>
        ))}
      </ul>

      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}

      {response && (
        <section aria-live="polite" className="space-y-3">
          <p className="text-sm text-muted">
            {response.results.length === 0
              ? "No matching candidates yet."
              : `${response.results.length} candidates for “${response.query}”`}
          </p>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {response.results.map((match) => (
              <ResultCard key={match.candidate_id} match={match} onOpen={setOpenApplication} />
            ))}
          </div>
        </section>
      )}

      <CandidateDossierModal applicationId={openApplication} onClose={() => setOpenApplication(null)} />
    </div>
  );
}
