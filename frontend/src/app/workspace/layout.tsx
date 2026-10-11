import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { NotificationBell } from "@/components/workspace/NotificationBell";

export const metadata: Metadata = { title: "Recruiter workspace", robots: { index: false } };

export default function WorkspaceLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex h-14 max-w-[110rem] items-center gap-6 px-4 sm:px-6">
          <Link href="/workspace" className="font-semibold tracking-tight">
            ATS Workspace
          </Link>
          <nav className="flex gap-4 overflow-x-auto whitespace-nowrap text-sm text-muted">
            <Link href="/workspace" className="hover:text-foreground">
              Jobs
            </Link>
            <Link href="/workspace/talent-pool" className="hover:text-foreground">
              Talent pool
            </Link>
            <Link href="/workspace/analytics" className="hover:text-foreground">
              Analytics
            </Link>
            <Link href="/workspace/team" className="hover:text-foreground">
              Team
            </Link>
            <Link href="/workspace/settings/communications" className="hover:text-foreground">
              Emails
            </Link>
            <Link href="/workspace/session" className="hover:text-foreground">
              Session
            </Link>
          </nav>
          <div className="ml-auto">
            <NotificationBell />
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-[110rem] flex-1 px-4 py-8 sm:px-6">{children}</main>
    </div>
  );
}
