export default function Home() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-2xl flex-col justify-center gap-4 px-6">
      <p className="text-sm font-semibold uppercase tracking-widest text-brand">ATS Platform</p>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
        Deterministic AI for triage. Humans for decisions.
      </h1>
      <p className="text-muted">
        Each company&apos;s career site lives at{" "}
        <code className="rounded bg-surface-muted px-1.5 py-0.5 font-mono text-sm">
          /careers/&lt;company-subdomain&gt;
        </code>
        .
      </p>
    </main>
  );
}
