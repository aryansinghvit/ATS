// Browser-side calls for the zero-friction application dropzone.
import { API_PREFIX, PUBLIC_API_URL } from "../config";
import type { ParsedCandidate, PresignUploadResponse, SubmitApplicationResponse } from "../types";
import { ApiError, errorFromResponse } from "./errors";

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${PUBLIC_API_URL}${API_PREFIX}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await errorFromResponse(response);
  return (await response.json()) as T;
}

export function requestResumeUpload(input: {
  tenantSubdomain: string;
  jobId: string;
  file: File;
  mimeType: string;
}): Promise<PresignUploadResponse> {
  return postJson("/public/applications/presign-upload", {
    tenant_subdomain: input.tenantSubdomain,
    job_id: input.jobId,
    file_name: input.file.name,
    mime_type: input.mimeType,
    file_size_bytes: input.file.size,
  });
}

/**
 * PUTs the file straight to object storage via the pre-signed URL. Uses XMLHttpRequest because
 * fetch() exposes no upload-progress events.
 */
export function uploadResume(
  upload: PresignUploadResponse,
  file: File,
  onProgress: (fraction: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(upload.method, upload.upload_url);
    for (const [name, value] of Object.entries(upload.headers)) {
      request.setRequestHeader(name, value);
    }
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) {
        onProgress(1);
        resolve();
      } else {
        reject(new ApiError(request.status, "The upload was rejected. Please try again."));
      }
    };
    request.onerror = () => reject(new ApiError(0, "Network error while uploading your resume."));
    request.send(file);
  });
}

export function submitApplication(input: {
  tenantSubdomain: string;
  jobId: string;
  s3Key: string;
  fileName: string;
  email: string;
  fullName?: string;
}): Promise<SubmitApplicationResponse> {
  return postJson("/public/applications/submit", {
    tenant_subdomain: input.tenantSubdomain,
    job_id: input.jobId,
    s3_key: input.s3Key,
    file_name: input.fileName,
    email: input.email,
    full_name: input.fullName || null,
  });
}

/**
 * Final step: the candidate verifies the fields extracted by the M3 microservice.
 * The backend endpoint ships with the extraction webhook in Phase 3.
 */
export async function confirmApplication(
  applicationId: string,
  statusToken: string,
  profile: ParsedCandidate,
): Promise<void> {
  const query = new URLSearchParams({ token: statusToken });
  const response = await fetch(
    `${PUBLIC_API_URL}${API_PREFIX}/public/applications/${applicationId}/confirm?${query}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile),
    },
  );
  if (!response.ok) throw await errorFromResponse(response);
}
