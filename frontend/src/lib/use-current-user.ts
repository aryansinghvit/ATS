"use client";

import { useEffect, useState } from "react";

import { fetchMe } from "./api/workspace";
import type { CurrentUser } from "./types";

let cached: Promise<CurrentUser> | null = null;

/** The signed-in workspace user (fetched once per page load). */
export function useCurrentUser(): CurrentUser | null {
  const [me, setMe] = useState<CurrentUser | null>(null);
  useEffect(() => {
    cached ??= fetchMe();
    cached.then(setMe).catch(() => {
      cached = null;
    });
  }, []);
  return me;
}
