import type { Metadata } from "next";

import { ScreeningPortal } from "@/components/screening/ScreeningPortal";

export const metadata: Metadata = {
  title: "Screening questions",
  robots: { index: false, follow: false },
  referrer: "no-referrer", // the URL is the candidate's credential
};

export default async function ScreeningPortalPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return <ScreeningPortal token={decodeURIComponent(token)} />;
}
