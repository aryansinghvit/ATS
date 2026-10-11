export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Builds an ApiError from a FastAPI error body ({"detail": "..."} or validation errors). */
export async function errorFromResponse(response: Response): Promise<ApiError> {
  let message = response.statusText || "Request failed";
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") {
      message = body.detail;
    } else if (Array.isArray(body.detail) && body.detail[0]?.msg) {
      message = String(body.detail[0].msg);
    }
  } catch {
    // Non-JSON error body: keep the status text.
  }
  return new ApiError(response.status, message);
}
