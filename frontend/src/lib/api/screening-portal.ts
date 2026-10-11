// Public candidate screening portal API (no login: the signed token in the URL is the credential).
import { API_PREFIX, PUBLIC_API_URL } from "../config";
import type { PortalAnswer, PortalView, ScreeningStatus } from "../types";
import { errorFromResponse } from "./errors";

const base = (token: string) =>
  `${PUBLIC_API_URL}${API_PREFIX}/public/screenings/${encodeURIComponent(token)}`;

async function send<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, { cache: "no-store", ...init });
  if (!response.ok) throw await errorFromResponse(response);
  return (await response.json()) as T;
}

export const openScreening = (token: string) => send<PortalView>(base(token));

export const saveTextAnswer = (token: string, index: number, text: string, keepalive = false) =>
  send<PortalAnswer>(`${base(token)}/answers/${index}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    keepalive,
  });

export const saveAudioAnswer = (token: string, index: number, audio: Blob) =>
  send<PortalAnswer>(`${base(token)}/answers/${index}/audio`, {
    method: "PUT",
    headers: { "Content-Type": audio.type || "audio/webm" },
    body: audio,
  });

export const submitScreening = (token: string) =>
  send<{ status: ScreeningStatus; submitted_at: string }>(`${base(token)}/submit`, {
    method: "POST",
  });
