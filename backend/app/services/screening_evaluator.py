"""Screening response processing: transcription + advisory rubric scorecard (project.md Module 6).

* Audio answers are transcribed on-device with faster-whisper (``base.en``, CPU int8). If the model
  or library is unavailable the answer is flagged for manual listening — never dropped.
* The rubric (Technical depth / Clarity / Relevance, 1–5 per answer) comes from the local LLM under
  a strict schema; when the model is unavailable a transparent keyword/length heuristic is used
  and labelled as such.
* Anti-bias guardrail: the result is advisory only. This module never changes the candidate's
  stage or status — it only attaches the scorecard and flags it for human review.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from statistics import mean
from typing import Any, Protocol

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.models.audit import AuditLog
from app.models.candidate import Candidate, JobApplication
from app.models.collaboration import Screening, ScreeningStatus
from app.models.job import Job
from app.schemas.job import JobTaxonomy
from app.schemas.screening import QuestionRubric, ScreeningEvaluation
from app.services.events import EventBroker
from app.services.job_generator import generate_validated
from app.services.llm_client import LLMClient, LLMOutputError, LLMUnavailableError
from app.services.notification_service import notify
from app.services.storage import StorageService

logger = logging.getLogger(__name__)
CATEGORIES = ("technical_depth", "clarity", "relevance")


# ----------------------------------------------------------------------- transcription
class TranscriptionUnavailableError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    engine: str


class Transcriber(Protocol):
    engine: str

    def transcribe(self, path: str) -> Transcript: ...


class FasterWhisperTranscriber:
    """Lazily loads the CTranslate2 Whisper model once per worker process."""

    def __init__(self, model: str, *, cache_dir: str, model_path: str | None = None) -> None:
        self.engine = f"faster-whisper:{model}"
        self._model_name = model_path or model
        self._cache_dir = cache_dir
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel

                    self._model = WhisperModel(
                        self._model_name,
                        device="cpu",
                        compute_type="int8",
                        download_root=self._cache_dir,
                    )
                except Exception as exc:  # ImportError, download or model errors
                    raise TranscriptionUnavailableError(
                        f"Speech-to-text model unavailable ({exc.__class__.__name__})"
                    ) from exc
        return self._model

    def transcribe(self, path: str) -> Transcript:
        model = self._load()
        segments, _info = model.transcribe(
            decode_audio(path), beam_size=1, vad_filter=True, language="en"
        )
        return Transcript(
            text=" ".join(s.text.strip() for s in segments).strip(), engine=self.engine
        )


def decode_audio(path: str, sample_rate: int = 16_000) -> Any:
    """Any browser/OS audio format -> mono float32 PCM at 16 kHz (what Whisper expects).

    Decoded here with PyAV's current API instead of faster-whisper's helper, which passes an
    argument newer PyAV releases removed.
    """
    import av
    import numpy as np

    chunks = []
    resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            chunks.extend(out.to_ndarray() for out in resampler.resample(frame))
    chunks.extend(out.to_ndarray() for out in resampler.resample(None))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks, axis=1).flatten().astype(np.float32) / 32768.0


@lru_cache
def get_transcriber() -> Transcriber:
    settings = get_settings()
    cache = Path(settings.WHISPER_CACHE_DIR)
    if not cache.is_absolute():
        from app.core.config import REPO_ROOT

        cache = REPO_ROOT / cache
    return FasterWhisperTranscriber(
        settings.WHISPER_MODEL, cache_dir=str(cache), model_path=settings.WHISPER_MODEL_PATH
    )


async def transcribe_audio(
    transcriber: Transcriber, storage: StorageService, key: str, suffix: str
) -> Transcript:
    data = await storage.backend.read_bytes(key)

    def _run() -> Transcript:
        with tempfile.NamedTemporaryFile(suffix=f".{suffix}") as handle:
            handle.write(data)
            handle.flush()
            return transcriber.transcribe(handle.name)

    return await asyncio.to_thread(_run)


# --------------------------------------------------------------------------- rubric
RUBRIC_SYSTEM_PROMPT = """You evaluate candidates' written or transcribed answers to pre-screening
questions for a hiring team. Score each answer 1-5 on:
- technical_depth: concrete, correct technical substance for the skill targeted;
- clarity: structured, easy to follow (ignore accents, grammar slips and transcription errors);
- relevance: actually answers the question asked.
Judge only the content of the answers. Never consider or infer age, gender, ethnicity, religion,
nationality, disability or any other protected characteristic. Your output is advisory: list
strengths and follow-up questions a human interviewer should probe. Return JSON per the schema."""


class _LLMQuestionScore(BaseModel):
    index: int = Field(ge=0)
    technical_depth: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    relevance: int = Field(ge=1, le=5)
    justification: str = Field(default="", max_length=500)


class _LLMRubric(BaseModel):
    scores: list[_LLMQuestionScore] = Field(min_length=1, max_length=5)
    summary: str = Field(min_length=10, max_length=1200)
    strengths: list[str] = Field(default_factory=list, max_length=5)
    probe_questions: list[str] = Field(default_factory=list, max_length=5)


_WORD = re.compile(r"[a-z0-9+#.]+")


def _clamp(value: float) -> int:
    return max(1, min(5, round(value)))


def heuristic_rubric(
    questions: list[dict[str, Any]], answers: list[str | None], job_skills: list[str]
) -> list[QuestionRubric]:
    """Transparent fallback: length/structure and on-topic technical vocabulary."""
    skills = {s.lower() for s in job_skills}
    rubric = []
    for index, (question, answer) in enumerate(zip(questions, answers, strict=True)):
        text = (answer or "").lower()
        words = _WORD.findall(text)
        target_terms = set(_WORD.findall(str(question.get("targets", "")).lower()))
        technical_hits = len({w for w in words if w in skills or w in target_terms})
        count = len(words)
        clarity = (
            1 if count < 10 else 2 if count < 25 else 3 if count < 60 else 4 if count <= 250 else 3
        )
        depth = 1 + min(3, technical_hits) + (1 if count >= 60 else 0)
        relevance = 1 + (2 if target_terms & set(words) else 0) + min(2, technical_hits)
        rubric.append(
            QuestionRubric(
                index=index,
                technical_depth=_clamp(depth),
                clarity=_clamp(clarity),
                relevance=_clamp(relevance),
                justification=(
                    f"Heuristic: {count} words, {technical_hits} on-topic technical term(s)."
                    if answer
                    else "No usable answer (audio could not be transcribed)."
                ),
            )
        )
    return rubric


class ScreeningEvaluator:
    def __init__(
        self,
        session: AsyncSession,
        storage: StorageService,
        llm: LLMClient | None,
        transcriber: Transcriber | None,
        broker: EventBroker | None = None,
    ) -> None:
        self._session = session
        self._storage = storage
        self._llm = llm
        self._transcriber = transcriber
        self._broker = broker

    async def evaluate(self, screening_id: uuid.UUID) -> ScreeningEvaluation:
        screening = await self._session.get(Screening, screening_id, with_for_update=True)
        if screening is None:
            raise NotFoundError("Screening not found")
        if screening.status is not ScreeningStatus.COMPLETED:
            raise NotFoundError("Screening has not been submitted")
        application = await self._session.get(JobApplication, screening.application_id)
        job = await self._session.get(Job, application.job_id) if application else None
        if application is None or job is None:  # pragma: no cover - foreign keys
            raise NotFoundError("Screening not found")

        responses = {int(r["index"]): dict(r) for r in screening.responses_json}
        for entry in responses.values():
            if (
                entry.get("type") == "audio"
                and entry.get("audio_key")
                and not entry.get("transcript")
            ):
                await self._transcribe(entry)
        screening.responses_json = [responses[i] for i in sorted(responses)]

        questions = screening.questions_json
        answers = [
            (responses.get(i, {}).get("text") or responses.get(i, {}).get("transcript"))
            for i in range(len(questions))
        ]
        taxonomy = JobTaxonomy.model_validate(job.taxonomy_json)
        job_skills = [s.name for s in [*taxonomy.required_skills, *taxonomy.preferred_skills]]
        evaluation = await self._rubric(job, questions, answers, job_skills)
        if any(e.get("transcription_error") for e in responses.values()):
            evaluation.note = (
                evaluation.note + " " if evaluation.note else ""
            ) + "Some audio could not be transcribed — please listen to the recordings."

        screening.ai_evaluation_json = {
            **screening.ai_evaluation_json,
            "evaluation_status": "completed",
            "evaluation": evaluation.model_dump(mode="json"),
        }
        self._session.add(
            AuditLog(
                tenant_id=screening.tenant_id,
                actor_id=None,
                action="screening_evaluated",
                entity_type="job_application",
                entity_id=application.id,
                metadata_json={
                    "source": evaluation.source,
                    "overall_score": evaluation.overall_score,
                    "advisory": True,
                },
            )
        )
        candidate = await self._session.get(Candidate, application.candidate_id)
        name = (
            " ".join(p for p in (candidate.first_name, candidate.last_name) if p)
            if candidate
            else ""
        )
        if job.creator_id:
            notify(
                self._session,
                tenant_id=screening.tenant_id,
                user_ids=[job.creator_id],
                type="screening",
                title=(
                    f"Screening completed{' for ' + name if name else ''}: "
                    f"{evaluation.overall_score:.1f}/5 (advisory)"
                ),
                body="Review the answers and rubric before deciding on next steps.",
                link=f"/workspace/jobs/{job.id}/pipeline?application={application.id}",
                entity_type="job_application",
                entity_id=application.id,
            )
        await self._session.commit()
        if self._broker is not None:
            from app.services.pipeline_service import PipelineService

            pipeline = PipelineService(self._session, self._broker)
            await pipeline.publish(await pipeline.card(application.id), "card_updated")
        return evaluation

    async def _transcribe(self, entry: dict[str, Any]) -> None:
        if self._transcriber is None:
            entry["transcription_error"] = "No speech-to-text engine configured"
            return
        suffix = entry["audio_key"].rsplit(".", 1)[-1]
        try:
            transcript = await transcribe_audio(
                self._transcriber, self._storage, entry["audio_key"], suffix
            )
        except TranscriptionUnavailableError as exc:
            entry["transcription_error"] = str(exc)
            return
        except Exception as exc:  # noqa: BLE001 - corrupt audio etc. must not abort the scorecard
            logger.exception("Transcription failed for %s", entry["audio_key"])
            entry["transcription_error"] = f"Transcription failed ({exc.__class__.__name__})"
            return
        entry["transcript"] = transcript.text
        entry["transcription_engine"] = transcript.engine
        entry.pop("transcription_error", None)

    async def _rubric(
        self,
        job: Job,
        questions: list[dict[str, Any]],
        answers: list[str | None],
        job_skills: list[str],
    ) -> ScreeningEvaluation:
        source, model, note = "heuristic", None, None
        summary, strengths, probes = "", [], []
        rubric: list[QuestionRubric] | None = None
        if self._llm is not None and any(answers):
            prompt = (
                f"Role: {job.title}\nRelevant skills: {', '.join(job_skills) or 'n/a'}\n\n"
                + "\n\n".join(
                    f"Question {i} (targets {q.get('targets', 'general')}): {q['question']}\n"
                    f"Answer {i}: {a or '[no answer / audio not transcribed]'}"
                    for i, (q, a) in enumerate(zip(questions, answers, strict=True))
                )
            )
            try:
                result = await generate_validated(
                    self._llm, _LLMRubric, system=RUBRIC_SYSTEM_PROMPT, prompt=prompt
                )
                by_index = {s.index: s for s in result.scores}
                if set(by_index) >= set(range(len(questions))):
                    rubric = [
                        QuestionRubric(**by_index[i].model_dump()) for i in range(len(questions))
                    ]
                    source, model = "ai", self._llm.model
                    summary, strengths, probes = (
                        result.summary,
                        result.strengths,
                        result.probe_questions,
                    )
            except (LLMUnavailableError, LLMOutputError) as exc:
                note = f"Local LLM unavailable ({exc.detail}); heuristic estimate shown."
        if rubric is None:
            rubric = heuristic_rubric(questions, answers, job_skills)
            note = note or "Heuristic estimate — review the answers directly."
            weakest = min(rubric, key=lambda r: r.technical_depth + r.relevance)
            summary = (
                f"Automatic estimate across {len(rubric)} answers. Weakest area: "
                f"{questions[weakest.index].get('targets', 'general')}."
            )
            probes = [
                "Ask for a concrete example involving "
                f"{questions[r.index].get('targets', 'this area')}."
                for r in rubric
                if r.technical_depth <= 2
            ][:3]
            strengths = [
                f"Clear, on-topic answer about {questions[r.index].get('targets', 'this area')}."
                for r in rubric
                if r.technical_depth >= 4 and r.relevance >= 4
            ][:3]
        averages = {c: round(mean(getattr(r, c) for r in rubric), 2) for c in CATEGORIES}
        overall = round(mean(averages.values()), 1)
        return ScreeningEvaluation(
            source=source,  # type: ignore[arg-type]
            model=model,
            overall_score=overall,
            category_averages=averages,
            rubric=rubric,
            summary=summary,
            strengths=strengths,
            probe_questions=probes,
            note=note,
            evaluated_at=datetime.now(UTC),
        )
