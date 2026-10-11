/** Base URL of the ATS FastAPI backend as seen from the browser. */
export const PUBLIC_API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8001").replace(
  /\/$/,
  "",
);

/** Base URL used by server components (may be an internal address). */
export const SERVER_API_URL = (process.env.API_URL ?? PUBLIC_API_URL).replace(/\/$/, "");

export const API_PREFIX = "/api/v1";

/** Must match the backend's MAX_RESUME_SIZE_BYTES. */
export const MAX_RESUME_BYTES = 15 * 1024 * 1024;
