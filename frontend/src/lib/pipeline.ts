import type { PipelineBoard, PipelineCard, PipelineFilters, PipelineSort } from "./types";

export type ScoreTone = "high" | "medium" | "low" | "pending";

/** Badge colour bands from task.md Prompt 3.3: green >= 85, yellow 70–84, red < 70. */
export function scoreTone(score: number | null): ScoreTone {
  if (score == null) return "pending";
  if (score >= 85) return "high";
  if (score >= 70) return "medium";
  return "low";
}

export const SORT_LABELS: Record<PipelineSort, string> = {
  default: "Pipeline order",
  score: "Match score (high → low)",
  applied_desc: "Newest applications",
  applied_asc: "Oldest applications",
  missing_skills: "Fewest missing skills",
};

function byScore(a: PipelineCard, b: PipelineCard) {
  const sa = a.deterministic_match_score;
  const sb = b.deterministic_match_score;
  if (sa === sb) return 0;
  if (sa == null) return 1;
  if (sb == null) return -1;
  return sb - sa;
}

/**
 * Mirrors backend `sort_cards` so optimistic moves and live SSE updates land where a refetch
 * would put them. "default": Applied by score (DESC, NULLS LAST, oldest first), later stages FIFO.
 */
export function sortCards(stageType: string, cards: PipelineCard[], mode: PipelineSort = "default") {
  const sorted = [...cards];
  if (mode === "score" || (mode === "default" && stageType === "applied")) {
    sorted.sort((a, b) => byScore(a, b) || a.applied_at.localeCompare(b.applied_at));
  } else if (mode === "applied_desc") {
    sorted.sort((a, b) => b.applied_at.localeCompare(a.applied_at));
  } else if (mode === "applied_asc") {
    sorted.sort((a, b) => a.applied_at.localeCompare(b.applied_at));
  } else if (mode === "missing_skills") {
    sorted.sort(
      (a, b) =>
        Number(a.extraction_status !== "complete") - Number(b.extraction_status !== "complete") ||
        a.match.missing_required.length - b.match.missing_required.length ||
        byScore(a, b),
    );
  } else {
    sorted.sort((a, b) => a.stage_entered_at.localeCompare(b.stage_entered_at));
  }
  return sorted;
}

/** Mirrors backend `matches_filters`. */
export function matchesFilters(card: PipelineCard, filters: PipelineFilters) {
  if (filters.min_score != null) {
    const score = card.deterministic_match_score;
    if (score == null || score < filters.min_score) return false;
  }
  if (filters.missing_skill) {
    const wanted = filters.missing_skill.toLowerCase();
    if (!card.match.missing_required.some((skill) => skill.toLowerCase() === wanted)) return false;
  }
  if (filters.q) {
    const haystack = `${card.candidate.full_name ?? ""} ${card.candidate.email}`.toLowerCase();
    if (!haystack.includes(filters.q.toLowerCase())) return false;
  }
  return true;
}

/** Insert/replace a card in the column named by `card.stage_id`, honouring the active view. */
export function upsertCard(board: PipelineBoard, card: PipelineCard): PipelineBoard {
  return {
    ...board,
    stages: board.stages.map((stage) => {
      const existed = stage.cards.some((c) => c.application_id === card.application_id);
      const others = stage.cards.filter((c) => c.application_id !== card.application_id);
      const entering = stage.id === card.stage_id;
      const cards = entering && matchesFilters(card, board.filters) ? [...others, card] : others;
      const total = stage.total_count + (entering && !existed ? 1 : 0) - (!entering && existed ? 1 : 0);
      return { ...stage, total_count: Math.max(total, cards.length), cards: sortCards(stage.stage_type, cards, board.filters.sort) };
    }),
  };
}

export function findCard(board: PipelineBoard, applicationId: string): PipelineCard | undefined {
  for (const stage of board.stages) {
    const card = stage.cards.find((c) => c.application_id === applicationId);
    if (card) return card;
  }
  return undefined;
}
