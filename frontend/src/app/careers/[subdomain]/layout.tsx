import Link from "next/link";
import { notFound } from "next/navigation";
import type { CSSProperties, ReactNode } from "react";

import { fetchJobBoard } from "@/lib/api/public-jobs";
import { readableTextColor } from "@/lib/format";

export default async function CareersLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ subdomain: string }>;
}) {
  const { subdomain } = await params;
  const board = await fetchJobBoard(subdomain);
  if (!board) notFound();
  const { organization } = board;

  const brandStyle = {
    "--brand": organization.primary_color,
    "--brand-contrast": readableTextColor(organization.primary_color),
  } as CSSProperties;

  return (
    <div style={brandStyle} className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-30 border-b border-border bg-surface/85 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-6xl items-center gap-3 px-4 sm:px-6">
          <Link href={`/careers/${organization.subdomain}`} className="flex items-center gap-3">
            {organization.logo_url ? (
              // Tenant logos are arbitrary external URLs, so next/image domain allow-listing
              // does not apply.
              // eslint-disable-next-line @next/next/no-img-element
              <img src={organization.logo_url} alt="" className="size-8 rounded-md object-contain" />
            ) : (
              <span
                aria-hidden
                className="grid size-8 place-items-center rounded-md bg-brand text-sm font-bold text-brand-contrast"
              >
                {organization.name.charAt(0).toUpperCase()}
              </span>
            )}
            <span className="font-semibold tracking-tight">{organization.name}</span>
          </Link>
          <span className="ml-auto text-sm text-muted">Careers</span>
        </div>
      </header>
      <div className="flex-1">{children}</div>
      <footer className="border-t border-border py-6 text-center text-xs text-muted">
        {organization.name} is an equal opportunity employer.
      </footer>
    </div>
  );
}
