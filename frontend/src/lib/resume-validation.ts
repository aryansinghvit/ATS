import { MAX_RESUME_BYTES } from "./config";

export type ResumeKind = "pdf" | "docx";

export const RESUME_MIME_TYPES: Record<ResumeKind, string> = {
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
};

export const RESUME_ACCEPT = [".pdf", ".docx", ...Object.values(RESUME_MIME_TYPES)].join(",");

// Magic numbers: PDF starts with "%PDF-", DOCX is a ZIP container ("PK\x03\x04").
const SIGNATURES: Record<ResumeKind, readonly number[]> = {
  pdf: [0x25, 0x50, 0x44, 0x46, 0x2d],
  docx: [0x50, 0x4b, 0x03, 0x04],
};

export type ResumeValidation =
  | { ok: true; kind: ResumeKind; mimeType: string }
  | { ok: false; error: string };

/**
 * Client-side pre-validation (the backend re-checks everything). The browser-reported MIME type
 * is ignored because it is often empty for DOCX and trivially spoofed; the file header decides.
 */
export async function validateResume(file: File): Promise<ResumeValidation> {
  const extension = file.name.split(".").pop()?.toLowerCase();
  if (extension !== "pdf" && extension !== "docx") {
    return { ok: false, error: "Please upload a PDF or Word (.docx) file." };
  }
  if (file.size === 0) {
    return { ok: false, error: "That file is empty." };
  }
  if (file.size > MAX_RESUME_BYTES) {
    return { ok: false, error: "Resumes must be 15 MB or smaller." };
  }

  const signature = SIGNATURES[extension];
  const header = new Uint8Array(await file.slice(0, signature.length).arrayBuffer());
  if (!signature.every((byte, index) => header[index] === byte)) {
    return {
      ok: false,
      error: `This file doesn't look like a valid ${extension.toUpperCase()} document.`,
    };
  }
  return { ok: true, kind: extension, mimeType: RESUME_MIME_TYPES[extension] };
}
