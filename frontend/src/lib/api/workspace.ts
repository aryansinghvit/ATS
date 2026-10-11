"use client";

// Authenticated recruiter-workspace API calls (browser side).
import { getAccessToken } from "../auth/session";
import { API_PREFIX, PUBLIC_API_URL } from "../config";
import type {
  AppNotification,
  CandidateLookup,
  ErasureReport,
  Interview,
  OfferLetter,
  OfferLetterInput,
  PipelineAnalytics,
  StageAction,
  StageConfig,
  StageType,
  CandidateDossier,
  CommunicationSettings,
  CurrentUser,
  EmailTemplate,
  Note,
  OutboxEmail,
  ScorecardBoard,
  ScorecardInput,
  Scorecard,
  Screening,
  TalentSuggestion,
  TeamMember,
  JobListItem,
  PipelineBoard,
  PipelineCard,
  PipelineFilters,
  TalentPoolStats,
  TalentSearchResponse,
} from "../types";
import { ApiError, errorFromResponse } from "./errors";

export function authHeaders(): Record<string, string> {
  const token = getAccessToken();
  if (!token) throw new ApiError(401, "Sign in to the workspace first.");
  return { Authorization: `Bearer ${token}` };
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${PUBLIC_API_URL}${API_PREFIX}${path}`, {
    ...init,
    headers: { ...authHeaders(), "Content-Type": "application/json", ...init.headers },
  });
  if (!response.ok) throw await errorFromResponse(response);
  return (await response.json()) as T;
}

export const fetchJobs = () => request<JobListItem[]>("/jobs");

export function fetchPipeline(jobId: string, filters?: Partial<PipelineFilters>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters ?? {})) {
    if (value !== null && value !== undefined && value !== "" && value !== "default") {
      params.set(key, String(value));
    }
  }
  const query = params.size ? `?${params}` : "";
  return request<PipelineBoard>(`/jobs/${jobId}/pipeline${query}`);
}

export const fetchDossier = (applicationId: string) =>
  request<CandidateDossier>(`/applications/${applicationId}/dossier`);

export const moveStage = (applicationId: string, toStageId: string, expectedVersion: number) =>
  request<PipelineCard>(`/applications/${applicationId}/move-stage`, {
    method: "PATCH",
    body: JSON.stringify({ to_stage_id: toStageId, expected_version: expectedVersion }),
  });

export const pipelineStreamUrl = (jobId: string) =>
  `${PUBLIC_API_URL}${API_PREFIX}/jobs/${jobId}/pipeline/stream`;

export const searchTalentPool = (query: string, limit = 20) =>
  request<TalentSearchResponse>("/talent-pool/search", {
    method: "POST",
    body: JSON.stringify({ query, limit }),
  });

export const fetchTalentPoolStats = () => request<TalentPoolStats>("/talent-pool/stats");

// ---------------------------------------------------------------- collaboration (Phase 6)
export const fetchMe = () => request<CurrentUser>("/auth/me");
export const fetchTeam = () => request<TeamMember[]>("/users");

export const fetchScorecards = (applicationId: string) =>
  request<ScorecardBoard>(`/applications/${applicationId}/scorecards`);
export const saveScorecard = (applicationId: string, input: ScorecardInput) =>
  request<Scorecard>(`/applications/${applicationId}/scorecards/me`, {
    method: "PUT",
    body: JSON.stringify(input),
  });

export const fetchNotes = (applicationId: string) =>
  request<Note[]>(`/applications/${applicationId}/notes`);
export const addNote = (applicationId: string, content: string) =>
  request<Note>(`/applications/${applicationId}/notes`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });

export const fetchNotifications = () =>
  request<{ unread: number; items: AppNotification[] }>("/notifications");
export const markNotificationRead = (id: string) =>
  request<{ updated: number }>(`/notifications/${id}/read`, { method: "POST" });
export const markAllNotificationsRead = () =>
  request<{ updated: number }>("/notifications/read-all", { method: "POST" });

export const fetchScreening = (applicationId: string) =>
  request<Screening | null>(`/applications/${applicationId}/screening`);
export const generateScreening = (applicationId: string, count: number) =>
  request<Screening>(`/applications/${applicationId}/screening-questions`, {
    method: "POST",
    body: JSON.stringify({ count }),
  });

export const inviteToScreening = (applicationId: string) =>
  request<Screening>(`/applications/${applicationId}/screening/invite`, { method: "POST" });

export const fetchApplicationEmails = (applicationId: string) =>
  request<OutboxEmail[]>(`/applications/${applicationId}/emails`);

export const fetchTemplates = () => request<EmailTemplate[]>("/communications/templates");
export const updateTemplate = (key: string, body: Pick<EmailTemplate, "subject" | "body" | "enabled">) =>
  request<EmailTemplate>(`/communications/templates/${key}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
export const resetTemplate = (key: string) =>
  request<EmailTemplate>(`/communications/templates/${key}`, { method: "DELETE" });
export const previewTemplate = (key: string, subject: string, body: string) =>
  request<{ subject: string; body: string }>(`/communications/templates/${key}/preview`, {
    method: "POST",
    body: JSON.stringify({ subject, body }),
  });
export const fetchCommunicationSettings = () =>
  request<CommunicationSettings>("/communications/settings");
export const saveCommunicationSettings = (settings: CommunicationSettings) =>
  request<CommunicationSettings>("/communications/settings", {
    method: "PUT",
    body: JSON.stringify(settings),
  });

export const fetchSuggestions = (jobId: string) =>
  request<TalentSuggestion[]>(`/talent-pool/jobs/${jobId}/suggestions`);
export const refreshSuggestions = (jobId: string) =>
  request<{ matches: number; new_matches: number }>(`/talent-pool/jobs/${jobId}/resurface`, {
    method: "POST",
  });
export async function dismissSuggestion(jobId: string, candidateId: string) {
  const response = await fetch(
    `${PUBLIC_API_URL}${API_PREFIX}/talent-pool/jobs/${jobId}/suggestions/${candidateId}/dismiss`,
    { method: "POST", headers: authHeaders() },
  );
  if (!response.ok) throw await errorFromResponse(response);
}
export const inviteToJob = (candidateId: string, jobId: string) =>
  request<{ application_id: string; email_queued: boolean }>(
    `/talent-pool/candidates/${candidateId}/invite-to-job`,
    { method: "POST", body: JSON.stringify({ job_id: jobId }) },
  );

// ------------------------------------------------------- analytics & admin (Phase 7)
export function fetchPipelineAnalytics(filters: { jobId?: string; from?: string; to?: string }) {
  const params = new URLSearchParams();
  if (filters.jobId) params.set("job_id", filters.jobId);
  if (filters.from) params.set("from", filters.from);
  if (filters.to) params.set("to", filters.to);
  const query = params.size ? `?${params}` : "";
  return request<PipelineAnalytics>(`/analytics/pipeline${query}`);
}

export const fetchStageActions = () => request<StageAction[]>("/stage-actions");
export const fetchStages = (jobId: string) => request<StageConfig[]>(`/jobs/${jobId}/stages`);
export const createStage = (
  jobId: string,
  body: { name: string; stage_type: StageType; automated_actions: string[] },
) => request<StageConfig[]>(`/jobs/${jobId}/stages`, { method: "POST", body: JSON.stringify(body) });
export const updateStage = (
  jobId: string,
  stageId: string,
  body: Partial<Pick<StageConfig, "name" | "stage_type" | "automated_actions">>,
) =>
  request<StageConfig[]>(`/jobs/${jobId}/stages/${stageId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
export const reorderStages = (jobId: string, stageIds: string[]) =>
  request<StageConfig[]>(`/jobs/${jobId}/stages/order`, {
    method: "PUT",
    body: JSON.stringify({ stage_ids: stageIds }),
  });
export const deleteStage = (jobId: string, stageId: string) =>
  request<StageConfig[]>(`/jobs/${jobId}/stages/${stageId}`, { method: "DELETE" });

export const updateTeamMember = (
  userId: string,
  body: { department?: string | null; is_active?: boolean },
) => request<TeamMember>(`/users/${userId}`, { method: "PATCH", body: JSON.stringify(body) });

export const fetchInterviews = (applicationId: string) =>
  request<Interview[]>(`/applications/${applicationId}/interviews`);
export const scheduleInterview = (
  applicationId: string,
  body: {
    title: string;
    interviewer_id: string | null;
    scheduled_start: string;
    scheduled_end: string;
    meeting_url: string | null;
  },
) =>
  request<Interview>(`/applications/${applicationId}/interviews`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const setInterviewStatus = (interviewId: string, status: Interview["status"]) =>
  request<Interview>(`/interviews/${interviewId}`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });

export const fetchOfferLetter = (applicationId: string) =>
  request<OfferLetter>(`/applications/${applicationId}/offer-letter`);
export const generateOfferLetter = (applicationId: string, body: OfferLetterInput) =>
  request<OfferLetter>(`/applications/${applicationId}/offer-letter`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export const findCandidatesByEmail = (email: string) =>
  request<CandidateLookup[]>(`/compliance/candidates?email=${encodeURIComponent(email)}`);
export const eraseCandidate = (candidateId: string) =>
  request<ErasureReport>(`/compliance/candidates/${candidateId}/forget`, { method: "DELETE" });
