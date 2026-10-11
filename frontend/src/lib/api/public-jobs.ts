// Server-side data access for the public career portal (used by server components).
import { API_PREFIX, SERVER_API_URL } from "../config";
import type { PublicJobBoard, PublicJobDetail } from "../types";
import { ApiError, errorFromResponse } from "./errors";

const REVALIDATE_SECONDS = 60;

async function getOrNull<T>(path: string): Promise<T | null> {
  const response = await fetch(`${SERVER_API_URL}${API_PREFIX}${path}`, {
    next: { revalidate: REVALIDATE_SECONDS },
  });
  if (response.status === 404) return null;
  if (!response.ok) throw await errorFromResponse(response);
  return (await response.json()) as T;
}

export function fetchJobBoard(subdomain: string): Promise<PublicJobBoard | null> {
  return getOrNull(`/public/jobs/${encodeURIComponent(subdomain)}`);
}

export function fetchJob(subdomain: string, slug: string): Promise<PublicJobDetail | null> {
  return getOrNull(`/public/jobs/${encodeURIComponent(subdomain)}/${encodeURIComponent(slug)}`);
}

export { ApiError };
