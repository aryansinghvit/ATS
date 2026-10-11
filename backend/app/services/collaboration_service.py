"""Interview scorecards (blind rules) and the internal team notes feed (project.md Module 7).

Scorecard visibility follows the Module 1 RBAC matrix ("View Other Interviewers' Feedback"):
* Admin / Recruiter — every colleague's *submitted* scorecard;
* Hiring Manager    — colleagues' submitted scorecards only after submitting their own;
* Blind Interviewer — never (their own scorecard only).
Drafts are private to their author. A submitted scorecard is locked.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models.audit import AuditLog
from app.models.candidate import JobApplication
from app.models.collaboration import CandidateNote, Recommendation, Scorecard
from app.models.job import Job
from app.models.user import User, UserRole
from app.schemas.collaboration import (
    Mention,
    NoteCreate,
    NoteRead,
    ScorecardBoard,
    ScorecardRead,
    ScorecardSummary,
    ScorecardUpsert,
)
from app.services.notification_service import notify

RATING_FIELDS = ("technical_competence", "communication", "problem_solving")
MENTION_PATTERN = re.compile(r"@\[(?P<name>[^\]\n]{1,100})\]\((?P<id>[0-9a-fA-F-]{36})\)")
VISIBILITY_RULES = {
    UserRole.ADMIN: "Admins see every submitted scorecard.",
    UserRole.RECRUITER: "Recruiters see every submitted scorecard.",
    UserRole.HIRING_MANAGER: (
        "Hiring managers see colleagues' scorecards after submitting their own."
    ),
    UserRole.INTERVIEWER: "Blind review: interviewers never see colleagues' scorecards.",
}


def _audit(actor: User, action: str, application_id: uuid.UUID, **metadata: object) -> AuditLog:
    return AuditLog(
        tenant_id=actor.tenant_id,
        actor_id=actor.id,
        action=action,
        entity_type="job_application",
        entity_id=application_id,
        metadata_json=metadata,
    )


class CollaborationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _application(self, application_id: uuid.UUID) -> JobApplication:
        application = await self._session.get(JobApplication, application_id)
        if application is None:
            raise NotFoundError("Application not found")
        return application

    # ---------------------------------------------------------------------- scorecards
    @staticmethod
    def _read(card: Scorecard, evaluator: User | None) -> ScorecardRead:
        return ScorecardRead(
            id=card.id,
            evaluator_id=card.evaluator_id,
            evaluator_name=evaluator.full_name if evaluator else None,
            evaluator_role=evaluator.role.value if evaluator else "unknown",
            overall_recommendation=card.overall_recommendation,
            notes_markdown=card.notes_markdown,
            is_submitted=card.is_submitted,
            submitted_at=card.submitted_at,
            updated_at=card.updated_at,
            **{field: card.scores_json.get(field) for field in RATING_FIELDS},
        )

    async def scorecards(self, application_id: uuid.UUID, *, viewer: User) -> ScorecardBoard:
        await self._application(application_id)
        rows = (
            await self._session.execute(
                select(Scorecard, User)
                .join(User, User.id == Scorecard.evaluator_id)
                .where(Scorecard.application_id == application_id)
                .order_by(Scorecard.submitted_at.nulls_last(), Scorecard.created_at)
            )
        ).all()
        mine = next((card for card, _ in rows if card.evaluator_id == viewer.id), None)
        submitted_others = [
            (card, user)
            for card, user in rows
            if card.evaluator_id != viewer.id and card.is_submitted
        ]
        if viewer.role in (UserRole.ADMIN, UserRole.RECRUITER):
            can_view = True
        elif viewer.role is UserRole.HIRING_MANAGER:
            can_view = bool(mine and mine.is_submitted)
        else:
            can_view = False

        visible = submitted_others if can_view else []
        summary = None
        if can_view:
            pool = [card for card, _ in visible] + ([mine] if mine and mine.is_submitted else [])
            averages: dict[str, float | None] = {}
            for field in RATING_FIELDS:
                values = [c.scores_json[field] for c in pool if c.scores_json.get(field)]
                averages[field] = round(sum(values) / len(values), 2) if values else None
            summary = ScorecardSummary(
                submitted=len(pool),
                averages=averages,
                recommendations=dict(
                    Counter(c.overall_recommendation for c in pool if c.overall_recommendation)
                ),
            )
        return ScorecardBoard(
            mine=self._read(mine, viewer) if mine else None,
            others=[self._read(card, user) for card, user in visible],
            can_view_others=can_view,
            hidden_count=0 if can_view else len(submitted_others),
            visibility_rule=VISIBILITY_RULES[viewer.role],
            summary=summary,
        )

    async def save_scorecard(
        self, application_id: uuid.UUID, payload: ScorecardUpsert, *, evaluator: User
    ) -> ScorecardRead:
        application = await self._application(application_id)
        card = (
            await self._session.execute(
                select(Scorecard).where(
                    Scorecard.application_id == application_id,
                    Scorecard.evaluator_id == evaluator.id,
                )
            )
        ).scalar_one_or_none()
        if card is not None and card.is_submitted:
            raise ConflictError("This scorecard was submitted and is locked")
        if card is None:
            card = Scorecard(
                tenant_id=application.tenant_id,
                application_id=application_id,
                evaluator_id=evaluator.id,
            )
            self._session.add(card)

        card.scores_json = {
            field: getattr(payload, field)
            for field in RATING_FIELDS
            if getattr(payload, field) is not None
        }
        card.overall_recommendation = (
            Recommendation(payload.overall_recommendation).value
            if payload.overall_recommendation
            else None
        )
        card.notes_markdown = payload.notes_markdown
        if payload.submit:
            card.is_submitted = True
            card.submitted_at = datetime.now(UTC)
            self._session.add(
                _audit(
                    evaluator,
                    "scorecard_submitted",
                    application_id,
                    recommendation=card.overall_recommendation,
                    blind=evaluator.role is UserRole.INTERVIEWER,
                )
            )
        await self._session.commit()
        return self._read(card, evaluator)

    # --------------------------------------------------------------------------- notes
    async def notes(self, application_id: uuid.UUID) -> list[NoteRead]:
        await self._application(application_id)
        rows = (
            await self._session.execute(
                select(CandidateNote, User.full_name)
                .outerjoin(User, User.id == CandidateNote.author_id)
                .where(CandidateNote.application_id == application_id)
                .order_by(CandidateNote.created_at)
            )
        ).all()
        return [
            NoteRead(
                id=note.id,
                author_id=note.author_id,
                author_name=author_name,
                content=note.content_markdown,
                mentions=[Mention.model_validate(m) for m in note.mentions_json],
                created_at=note.created_at,
            )
            for note, author_name in rows
        ]

    async def add_note(
        self, application_id: uuid.UUID, payload: NoteCreate, *, author: User
    ) -> NoteRead:
        application = await self._application(application_id)
        mention_ids = list(
            dict.fromkeys(
                uuid.UUID(match["id"]) for match in MENTION_PATTERN.finditer(payload.content)
            )
        )
        mentioned = (
            list(
                (
                    await self._session.execute(select(User).where(User.id.in_(mention_ids)))
                ).scalars()
            )
            if mention_ids
            else []
        )  # RLS: users of other tenants are simply not found
        if len(mentioned) != len(mention_ids):
            raise UnprocessableError("A mentioned teammate does not exist in this workspace")
        blind = [user.full_name for user in mentioned if user.role is UserRole.INTERVIEWER]
        if blind:
            raise UnprocessableError(
                f"Blind interviewers cannot be mentioned on team notes: {', '.join(blind)}"
            )

        note = CandidateNote(
            tenant_id=application.tenant_id,
            application_id=application_id,
            author_id=author.id,
            content_markdown=payload.content,
            mentions_json=[{"user_id": str(u.id), "name": u.full_name} for u in mentioned],
        )
        self._session.add(note)
        job_title = await self._session.scalar(
            select(Job.title).where(Job.id == application.job_id)
        )
        notify(
            self._session,
            tenant_id=application.tenant_id,
            user_ids=[u.id for u in mentioned if u.id != author.id],
            type="mention",
            title=f"{author.full_name} mentioned you on a candidate for {job_title}",
            body=MENTION_PATTERN.sub(lambda m: f"@{m['name']}", payload.content)[:500],
            link=f"/workspace/jobs/{application.job_id}/pipeline?application={application_id}",
            entity_type="job_application",
            entity_id=application_id,
        )
        self._session.add(
            _audit(author, "note_added", application_id, mentions=[str(u.id) for u in mentioned])
        )
        await self._session.commit()
        return NoteRead(
            id=note.id,
            author_id=author.id,
            author_name=author.full_name,
            content=note.content_markdown,
            mentions=[Mention(user_id=u.id, name=u.full_name) for u in mentioned],
            created_at=note.created_at,
        )
