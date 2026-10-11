"use client";

/**
 * Recruiter access token.
 *
 * Development: a mock Clerk-shaped token (mint one with `scripts/mint_dev_token.py`) pasted at
 * /workspace/session and kept in sessionStorage for the tab's lifetime. When the Clerk frontend
 * SDK is wired in, `getAccessToken` becomes `await clerk.session.getToken()`; callers don't change.
 */
const STORAGE_KEY = "ats.workspace.token";

export function getAccessToken(): string | null {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setAccessToken(token: string) {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, token.trim());
  } catch {
    // Storage unavailable (private mode): the user will be asked again.
  }
}

export function clearAccessToken() {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}
