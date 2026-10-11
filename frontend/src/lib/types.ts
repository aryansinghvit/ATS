// Mirrors the FastAPI response schemas (backend/app/schemas).

export type RemotePolicy = "remote" | "hybrid" | "on_site";
export type EmploymentType = "full_time" | "part_time" | "contract" | "temporary" | "internship";

export interface TenantBranding {
  name: string;
  subdomain: string;
  logo_url: string | null;
  primary_color: string;
}

export interface PublicJobSummary {
  id: string;
  title: string;
  slug: string;
  department: string;
  location: string;
  remote_policy: RemotePolicy;
  employment_type: EmploymentType;
  seniority_level: string | null;
  salary_min: number | null;
  salary_max: number | null;
  currency: string | null;
  published_at: string | null;
}

export interface PublicJobDetail extends PublicJobSummary {
  description_html: string;
  organization: TenantBranding;
  json_ld: Record<string, unknown>;
}

export interface PublicJobBoard {
  organization: TenantBranding;
  jobs: PublicJobSummary[];
}

export interface PresignUploadResponse {
  upload_url: string;
  method: "PUT";
  headers: Record<string, string>;
  s3_key: string;
  expires_in: number;
  max_file_size_bytes: number;
}

export interface SubmitApplicationResponse {
  application_id: string;
  status: string;
  status_token: string;
  events_url: string;
}

export interface ParsedCandidate {
  full_name: string | null;
  email: string | null;
  phone: string | null;
  linkedin_url: string | null;
  github_url: string | null;
  location: string | null;
  skills: string[];
}

export type ApplicationStatusEvent =
  | { application_id: string; status: "processing" }
  | { application_id: string; status: "parsed"; confirmed: boolean; candidate: ParsedCandidate }
  | { application_id: string; status: "failed"; confirmed: boolean };

// ---------------------------------------------------------------- recruiter workspace

export type ApplicationStatus =
  | "pending_extraction"
  | "applied"
  | "screening_invited"
  | "screening_completed"
  | "interview_scheduled"
  | "interview_completed"
  | "offered"
  | "hired"
  | "rejected"
  | "withdrawn";

export interface JobListItem {
  id: string;
  title: string;
  slug: string;
  department: string;
  location: string;
  status: "draft" | "published" | "paused" | "closed" | "archived";
  published_at: string | null;
}

export interface PipelineCard {
  application_id: string;
  job_id: string;
  stage_id: string | null;
  status: ApplicationStatus;
  extraction_status: "pending" | "complete" | "failed";
  deterministic_match_score: number | null;
  match: { matched_required: string[]; matched_preferred: string[]; missing_required: string[] };
  candidate: {
    id: string;
    full_name: string | null;
    email: string;
    location: string | null;
    linkedin_url: string | null;
  };
  applied_at: string;
  stage_entered_at: string;
  version: number;
}

export interface PipelineColumn {
  id: string;
  name: string;
  stage_type: string;
  position_index: number;
  total_count: number;
  cards: PipelineCard[];
}

export type PipelineSort = "default" | "score" | "applied_desc" | "applied_asc" | "missing_skills";

export interface PipelineFilters {
  sort: PipelineSort;
  min_score: number | null;
  missing_skill: string | null;
  q: string | null;
}

export interface PipelineBoard {
  job: { id: string; title: string; slug: string; status: string; required_skills: string[] };
  filters: PipelineFilters;
  stages: PipelineColumn[];
}

export interface PipelineEvent {
  type: "card_updated" | "card_moved";
  card: PipelineCard;
}

// ------------------------------------------------------------------ candidate dossier

export interface SkillEvidence {
  skill: string;
  evidence: string | null;
}

export interface WorkExperience {
  company?: string;
  title?: string;
  location?: string;
  start_date?: string;
  end_date?: string;
  highlights?: string[];
  technologies?: string[];
}

export interface Education {
  institution?: string;
  degree?: string;
  field_of_study?: string;
  graduation_year?: number;
}

export interface Project {
  name?: string;
  description?: string;
  technologies?: string[];
  url?: string;
  highlights?: string[];
}

export interface CandidateDossier {
  application_id: string;
  redacted: boolean;
  job: { id: string; title: string };
  status: ApplicationStatus;
  stage: { id: string; name: string; stage_type: string } | null;
  applied_at: string;
  confirmed_at: string | null;
  version: number;
  candidate: {
    id: string;
    display_name: string;
    full_name: string | null;
    email: string | null;
    phone: string | null;
    linkedin_url: string | null;
    github_url: string | null;
    portfolio_url: string | null;
    location: string | null;
  };
  resume: {
    file_name: string | null;
    kind: "pdf" | "docx";
    mime_type: string;
    url: string | null;
    url_expires_in: number | null;
  };
  match: {
    extraction_status: "pending" | "complete" | "failed";
    extraction_error: string | null;
    deterministic_match_score: number | null;
    matched_required: SkillEvidence[];
    missing_required: SkillEvidence[];
    matched_preferred: SkillEvidence[];
    missing_preferred: SkillEvidence[];
    experience_match: { candidate_years?: number; required_years?: number; meets_criteria?: boolean } | null;
  } | null;
  profile: {
    summary: string | null;
    skills: string[];
    verified_skills: string[];
    work_experience: WorkExperience[];
    education: Education[];
    projects: Project[];
  };
  stage_history: { at: string; from_stage: string | null; to_stage: string | null; actor_name: string | null }[];
}

// ------------------------------------------------------------------ semantic talent pool

export interface TalentMatch {
  candidate_id: string;
  full_name: string | null;
  email: string;
  location: string | null;
  similarity: number;
  keyword_rank: number | null;
  hybrid_score: number;
  matched_skills: string[];
  skills: string[];
  highlights: string[];
  applications: {
    application_id: string;
    job_id: string;
    job_title: string;
    status: ApplicationStatus;
    deterministic_match_score: number | null;
    applied_at: string;
  }[];
}

export interface TalentSearchResponse {
  query: string;
  model: string;
  total_indexed: number;
  results: TalentMatch[];
}

export interface TalentPoolStats {
  model: string;
  dimensions: number;
  candidates: number;
  indexed: number;
}

// ------------------------------------------------------------- collaboration (Phase 6)

export type UserRole = "admin" | "recruiter" | "hiring_manager" | "interviewer";
export type Recommendation = "strong_yes" | "yes" | "neutral" | "no" | "strong_no";

export interface CurrentUser {
  user: { id: string; tenant_id: string; email: string; full_name: string; role: UserRole };
  tenant: { id: string; name: string; subdomain: string };
}

export interface TeamMember {
  id: string;
  full_name: string;
  email: string;
  role: UserRole;
  department: string | null;
  is_active: boolean;
  created_at: string;
}

export interface Scorecard {
  id: string;
  evaluator_id: string;
  evaluator_name: string | null;
  evaluator_role: string;
  technical_competence: number | null;
  communication: number | null;
  problem_solving: number | null;
  overall_recommendation: Recommendation | null;
  notes_markdown: string | null;
  is_submitted: boolean;
  submitted_at: string | null;
  updated_at: string;
}

export interface ScorecardBoard {
  mine: Scorecard | null;
  others: Scorecard[];
  can_view_others: boolean;
  hidden_count: number;
  visibility_rule: string;
  summary: {
    submitted: number;
    averages: Record<string, number | null>;
    recommendations: Partial<Record<Recommendation, number>>;
  } | null;
}

export interface ScorecardInput {
  technical_competence: number | null;
  communication: number | null;
  problem_solving: number | null;
  overall_recommendation: Recommendation | null;
  notes_markdown: string | null;
  submit: boolean;
}

export interface Note {
  id: string;
  author_id: string;
  author_name: string | null;
  content: string;
  mentions: { user_id: string; name: string }[];
  created_at: string;
}

export interface AppNotification {
  id: string;
  type: string;
  title: string;
  body: string | null;
  link: string | null;
  read_at: string | null;
  created_at: string;
}

export type ScreeningStatus = "pending" | "opened" | "completed" | "expired";

export interface ScreeningResponse {
  index: number;
  question: string;
  targets: string;
  type: "text" | "audio" | null;
  text: string | null;
  transcript: string | null;
  transcription_engine: string | null;
  transcription_error: string | null;
  audio_url: string | null;
  updated_at: string | null;
}

export interface ScreeningEvaluation {
  source: "ai" | "heuristic";
  model: string | null;
  overall_score: number;
  category_averages: Record<string, number>;
  rubric: {
    index: number;
    technical_depth: number;
    clarity: number;
    relevance: number;
    justification: string;
  }[];
  summary: string;
  strengths: string[];
  probe_questions: string[];
  advisory: true;
  requires_human_review: true;
  note: string | null;
  evaluated_at: string;
}

export interface Screening {
  id: string;
  application_id: string;
  status: ScreeningStatus;
  source: "ai" | "fallback";
  source_detail: string | null;
  model: string | null;
  skill_gaps: string[];
  questions: { question: string; targets: string; rationale: string }[];
  expires_at: string;
  created_at: string;
  invited_at: string | null;
  opened_at: string | null;
  submitted_at: string | null;
  candidate_link: string | null;
  responses: ScreeningResponse[];
  evaluation_status: "not_started" | "pending" | "completed";
  evaluation: ScreeningEvaluation | null;
}

export interface OutboxEmail {
  id: string;
  template_key: string;
  trigger: string;
  to_email: string;
  status: "queued" | "sent" | "cancelled" | "failed";
  scheduled_for: string;
  sent_at: string | null;
  attempts: number;
  last_error: string | null;
  subject: string | null;
  body: string | null;
}

export interface EmailTemplate {
  key: string;
  name: string;
  description: string;
  trigger: string;
  subject: string;
  body: string;
  enabled: boolean;
  customized: boolean;
  variables: string[];
}

export interface CommunicationSettings {
  scheduling_url: string | null;
  rejection_delay_hours: number;
  timezone: string;
  reply_to: string | null;
}

export interface TalentSuggestion {
  candidate_id: string;
  full_name: string | null;
  email: string;
  location: string | null;
  similarity: number;
  status: "suggested" | "invited" | "dismissed";
  skills: string[];
  applications: TalentMatch["applications"];
}

// --------------------------------------------------- candidate portal (Phase 7, public)

export interface PortalAnswer {
  index: number;
  type: "text" | "audio";
  text: string | null;
  has_audio: boolean;
  audio_bytes: number | null;
  updated_at: string;
}

export interface PortalView {
  status: ScreeningStatus;
  company_name: string;
  logo_url: string | null;
  primary_color: string;
  job_title: string;
  candidate_first_name: string | null;
  expires_at: string;
  questions: { index: number; question: string }[];
  answers: PortalAnswer[];
  limits: { max_audio_seconds: number; max_audio_bytes: number; max_text_chars: number };
}

// ------------------------------------------------------- analytics & admin (Phase 7)

export interface StageFunnel {
  name: string;
  stage_type: string;
  position: number;
  reached: number;
  current: number;
  conversion_from_previous: number | null;
  drop_off_rate: number | null;
  rejected_here: number;
  avg_days_in_stage: number | null;
}

export interface PipelineAnalytics {
  job_id: string | null;
  date_from: string | null;
  date_to: string | null;
  generated_at: string;
  totals: {
    applications: number;
    in_progress: number;
    hired: number;
    rejected: number;
    withdrawn: number;
    hire_rate: number | null;
  };
  funnel: StageFunnel[];
  time_to_hire: {
    hired: number;
    average_days: number | null;
    median_days: number | null;
    p90_days: number | null;
  };
  weekly_applications: { week_start: string; applications: number }[];
  jobs: {
    job_id: string;
    title: string;
    status: string;
    applications: number;
    in_progress: number;
    hired: number;
    rejected: number;
    average_match_score: number | null;
    median_days_to_hire: number | null;
  }[];
}

export type StageType = "applied" | "screen" | "interview" | "offer" | "hired" | "rejected";

export interface StageConfig {
  id: string;
  name: string;
  stage_type: StageType;
  position_index: number;
  automated_actions: string[];
  applications: number;
}

export interface StageAction {
  key: string;
  description: string;
}

export interface Interview {
  id: string;
  application_id: string;
  interviewer_id: string | null;
  interviewer_name: string | null;
  title: string;
  scheduled_start: string;
  scheduled_end: string;
  meeting_url: string | null;
  source: string;
  status: "scheduled" | "cancelled" | "completed";
  created_at: string;
}

export interface OfferLetterInput {
  base_salary: string;
  currency: string;
  start_date: string;
  job_title: string | null;
  equity: string | null;
  respond_by: string | null;
  additional_terms: string | null;
}

export interface OfferLetter {
  application_id: string;
  details: Record<string, string | null>;
  generated_at: string;
  download_url: string;
  expires_in: number;
}

export interface CandidateLookup {
  candidate_id: string;
  email: string;
  full_name: string | null;
  applications: number;
  created_at: string;
}

export interface ErasureReport {
  candidate_id: string;
  applications_deleted: number;
  files_deleted: number;
  file_errors: number;
  notifications_deleted: number;
  erased_at: string;
  status: "erased";
}
