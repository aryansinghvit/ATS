"""AI job-description generator and taxonomy extractor (project.md Module 2).

The local LLM only produces structured *content*. Everything with legal or scoring weight is
deterministic Python: the markdown layout, the EEO / accommodation statement, skill-name
canonicalisation, de-duplication, weights and the M3 request payload.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.models.job import EmploymentType, Job, RemotePolicy
from app.schemas.job import (
    JobDescriptionRequest,
    JobDraft,
    JobTaxonomy,
    PreferredSkill,
    RequiredSkill,
)
from app.services.llm_client import LLMClient, LLMOutputError

REQUIRED_SKILL_WEIGHT = 1.0
PREFERRED_SKILL_WEIGHT = 0.5
MAX_SKILLS_PER_LIST = 25

EEO_STATEMENT = (
    "{company} is an equal opportunity employer. We welcome applicants of every background and "
    "do not discriminate on the basis of race, color, religion, sex, sexual orientation, gender "
    "identity or expression, national origin, age, disability, veteran status, or any other "
    "legally protected characteristic. If you need a reasonable accommodation at any point in "
    "the application or interview process, please let us know and we will work with you."
)

# Lower-cased alias -> canonical skill name (keeps M3 matching consistent across job posts).
SKILL_ALIASES: dict[str, str] = {
    "go": "Go", "golang": "Go",
    "k8s": "Kubernetes", "kubernetes": "Kubernetes",
    "postgres": "PostgreSQL", "postgresql": "PostgreSQL", "psql": "PostgreSQL",
    "js": "JavaScript", "javascript": "JavaScript",
    "ts": "TypeScript", "typescript": "TypeScript",
    "node": "Node.js", "nodejs": "Node.js", "node.js": "Node.js",
    "react": "React", "reactjs": "React", "react.js": "React",
    "next": "Next.js", "nextjs": "Next.js", "next.js": "Next.js",
    "vue": "Vue.js", "vuejs": "Vue.js", "vue.js": "Vue.js", "vue 3": "Vue.js",
    "python": "Python", "python3": "Python",
    "fastapi": "FastAPI", "django": "Django",
    "aws": "AWS", "amazon web services": "AWS",
    "gcp": "GCP", "google cloud": "GCP", "google cloud platform": "GCP",
    "azure": "Azure", "microsoft azure": "Azure",
    "docker": "Docker", "terraform": "Terraform", "helm": "Helm",
    "kafka": "Kafka", "apache kafka": "Kafka",
    "grpc": "gRPC", "graphql": "GraphQL",
    "redis": "Redis", "mysql": "MySQL", "mongodb": "MongoDB", "mongo": "MongoDB",
    "c#": "C#", "csharp": "C#", "c++": "C++", "cpp": "C++",
    "java": "Java", "kotlin": "Kotlin", "rust": "Rust", "scala": "Scala",
    "sql": "SQL", "tailwind": "Tailwind CSS", "tailwindcss": "Tailwind CSS",
    "ci/cd": "CI/CD", "cicd": "CI/CD",
    "ml": "Machine Learning", "machine learning": "Machine Learning",
}  # fmt: skip


# ------------------------------------------------------------- structured LLM output models
def _coerce_choice(value: Any) -> Any:
    """Accept "Full-time", "On site", ... for enum fields produced by small local models."""
    if isinstance(value, str):
        return re.sub(r"[\s\-]+", "_", value.strip().lower())
    return value


class LLMSkill(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    min_years: float = Field(default=0, ge=0, le=40)


class LLMNamedSkill(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class LLMCompensation(BaseModel):
    min: float | None = Field(default=None, ge=0)
    max: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    equity: bool = False


class GeneratedJobContent(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    department: str = Field(min_length=1, max_length=100)
    seniority_level: str | None = Field(default=None, max_length=50)
    employment_type: EmploymentType = EmploymentType.FULL_TIME
    location: str = Field(min_length=1, max_length=100)
    remote_policy: RemotePolicy = RemotePolicy.REMOTE
    compensation: LLMCompensation = Field(default_factory=LLMCompensation)
    about_team: str = Field(min_length=10, max_length=2000)
    responsibilities: list[str] = Field(min_length=1, max_length=15)
    required_qualifications: list[str] = Field(min_length=1, max_length=15)
    preferred_qualifications: list[str] = Field(default_factory=list, max_length=15)
    required_skills: list[LLMSkill] = Field(min_length=1, max_length=MAX_SKILLS_PER_LIST)
    preferred_skills: list[LLMNamedSkill] = Field(
        default_factory=list, max_length=MAX_SKILLS_PER_LIST
    )
    min_total_experience_years: float = Field(default=0, ge=0, le=50)
    education_level: str | None = Field(default=None, max_length=200)

    @field_validator("employment_type", "remote_policy", mode="before")
    @classmethod
    def _coerce_enums(cls, value: Any) -> Any:
        return _coerce_choice(value)


class ExtractedTaxonomy(BaseModel):
    required_skills: list[LLMSkill] = Field(default_factory=list, max_length=MAX_SKILLS_PER_LIST)
    preferred_skills: list[LLMNamedSkill] = Field(
        default_factory=list, max_length=MAX_SKILLS_PER_LIST
    )
    min_total_experience_years: float = Field(default=0, ge=0, le=50)
    education_level: str | None = Field(default=None, max_length=200)


GENERATOR_SYSTEM_PROMPT = """You are an expert technical recruiter writing job requisitions.
Turn the recruiter's rough notes into structured job content.
Rules:
- Use inclusive, gender-neutral language. Never mention age, "digital natives", "rockstar",
  "ninja", culture-fit clichés, or physical requirements unless the notes state them.
- Only list requirements implied by the notes; do not invent certifications or degrees.
- required_skills are concrete technologies or competencies with realistic min_years.
- Leave compensation fields null unless the notes state compensation.
- Do not write an equal-opportunity statement; it is added automatically.
Return only JSON matching the provided schema."""

TAXONOMY_SYSTEM_PROMPT = """You extract a hiring skill taxonomy from a job description.
- required_skills: technologies/competencies the description says are required, with the
  minimum years stated (0 if not stated).
- preferred_skills: nice-to-have technologies.
- min_total_experience_years: overall years of experience required (0 if not stated).
Use short canonical skill names (e.g. "Go", "Kubernetes", "PostgreSQL").
Return only JSON matching the provided schema."""

ModelT = TypeVar("ModelT", bound=BaseModel)


async def generate_validated(
    llm: LLMClient, model_cls: type[ModelT], *, system: str, prompt: str
) -> ModelT:
    """One generation plus a single auto-repair attempt that feeds back the validation error."""
    schema = model_cls.model_json_schema()
    feedback = ""
    for _attempt in range(2):
        try:
            raw = await llm.generate_json(system=system, prompt=prompt + feedback, schema=schema)
            return model_cls.model_validate(raw)
        except (ValidationError, LLMOutputError) as exc:
            detail = (
                "; ".join(
                    f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                    for err in exc.errors()[:8]
                )
                if isinstance(exc, ValidationError)
                else exc.detail
            )
            feedback = (
                f"\n\nYour previous answer was invalid ({detail}). "
                "Return corrected JSON that satisfies the schema."
            )
    raise LLMOutputError("The AI output did not match the required structure; please retry")


# ------------------------------------------------------------------ deterministic helpers
def canonical_skill_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name).strip().strip(".,;:")
    return SKILL_ALIASES.get(cleaned.lower(), cleaned)


def normalize_taxonomy(taxonomy: JobTaxonomy) -> JobTaxonomy:
    """Canonicalise names, de-duplicate (required wins over preferred), fill default weights."""
    required: dict[str, RequiredSkill] = {}
    for skill in taxonomy.required_skills:
        name = canonical_skill_name(skill.name)
        if not name:
            continue
        key = name.lower()
        existing = required.get(key)
        if existing is None:
            required[key] = RequiredSkill(
                name=name, min_years=round(skill.min_years, 1), weight=skill.weight
            )
        else:
            existing.min_years = max(existing.min_years, round(skill.min_years, 1))
            existing.weight = max(existing.weight, skill.weight)

    preferred: dict[str, PreferredSkill] = {}
    for skill in taxonomy.preferred_skills:
        name = canonical_skill_name(skill.name)
        key = name.lower()
        if not name or key in required:
            continue
        if key not in preferred or skill.weight > preferred[key].weight:
            preferred[key] = PreferredSkill(name=name, weight=skill.weight)

    return JobTaxonomy(
        required_skills=list(required.values())[:MAX_SKILLS_PER_LIST],
        preferred_skills=list(preferred.values())[:MAX_SKILLS_PER_LIST],
        min_total_experience_years=round(taxonomy.min_total_experience_years, 1),
        education_level=taxonomy.education_level,
    )


def _taxonomy_from_llm(
    required: list[LLMSkill],
    preferred: list[LLMNamedSkill],
    min_total_experience_years: float,
    education_level: str | None,
) -> JobTaxonomy:
    return normalize_taxonomy(
        JobTaxonomy(
            required_skills=[
                RequiredSkill(name=s.name, min_years=s.min_years, weight=REQUIRED_SKILL_WEIGHT)
                for s in required
            ],
            preferred_skills=[
                PreferredSkill(name=s.name, weight=PREFERRED_SKILL_WEIGHT) for s in preferred
            ],
            min_total_experience_years=min_total_experience_years,
            education_level=education_level,
        )
    )


_EMPLOYMENT_LABELS = {
    EmploymentType.FULL_TIME: "Full-time",
    EmploymentType.PART_TIME: "Part-time",
    EmploymentType.CONTRACT: "Contract",
    EmploymentType.TEMPORARY: "Temporary",
    EmploymentType.INTERNSHIP: "Internship",
}
_REMOTE_LABELS = {
    RemotePolicy.REMOTE: "Remote",
    RemotePolicy.HYBRID: "Hybrid",
    RemotePolicy.ON_SITE: "On-site",
}


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item.strip().lstrip('-•* ').strip()}" for item in items if item.strip())


def render_description_markdown(
    content: GeneratedJobContent,
    *,
    company_name: str,
    location: str,
    remote_policy: RemotePolicy,
    employment_type: EmploymentType,
    seniority_level: str | None,
    salary_min: Decimal | None,
    salary_max: Decimal | None,
    currency: str,
) -> str:
    facts = [
        f"**Location:** {location} ({_REMOTE_LABELS[remote_policy]})",
        f"**Employment type:** {_EMPLOYMENT_LABELS[employment_type]}",
    ]
    if seniority_level:
        facts.append(f"**Level:** {seniority_level}")

    sections = [
        " · ".join(facts),
        f"## About the Team\n\n{content.about_team.strip()}",
        f"## What You'll Do\n\n{_bullets(content.responsibilities)}",
        f"## What You'll Bring\n\n{_bullets(content.required_qualifications)}",
    ]
    if content.preferred_qualifications:
        sections.append(f"## Nice to Have\n\n{_bullets(content.preferred_qualifications)}")
    if salary_min is not None or salary_max is not None:
        if salary_min is not None and salary_max is not None:
            band = f"{currency} {salary_min:,.0f} – {salary_max:,.0f} per year"
        else:
            band = f"{currency} {(salary_min or salary_max):,.0f} per year"
        if content.compensation.equity:
            band += ", plus equity"
        sections.append(f"## Compensation\n\n{band}")
    sections.append(f"## Equal Opportunity\n\n{EEO_STATEMENT.format(company=company_name)}")
    return "\n\n".join(sections) + "\n"


def build_m3_job_description(job: Job) -> dict[str, Any]:
    """The ``job_description`` part of ``POST /api/v1/resumes/extract`` (project.md §6.2)."""
    taxonomy = JobTaxonomy.model_validate(job.taxonomy_json)
    return {
        "job_id": str(job.id),
        "title": job.title,
        "required_skills": [skill.name for skill in taxonomy.required_skills],
        "preferred_skills": [skill.name for skill in taxonomy.preferred_skills],
        "min_years_experience": taxonomy.min_total_experience_years,
    }


# ------------------------------------------------------------------------------- services
class JobDescriptionGenerator:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def generate(self, request: JobDescriptionRequest, *, company_name: str) -> JobDraft:
        facts = {
            key: value
            for key, value in request.model_dump(exclude={"prompt"}, mode="json").items()
            if value is not None
        }
        prompt = f"Company: {company_name}\nRecruiter notes:\n{request.prompt}"
        if facts:
            prompt += "\nFacts that must be respected exactly:\n" + "\n".join(
                f"- {key}: {value}" for key, value in facts.items()
            )
        content = await generate_validated(
            self._llm, GeneratedJobContent, system=GENERATOR_SYSTEM_PROMPT, prompt=prompt
        )

        # Recruiter-supplied facts always win over model output.
        title = request.title or content.title
        department = request.department or content.department
        location = request.location or content.location
        remote_policy = request.remote_policy or content.remote_policy
        employment_type = request.employment_type or content.employment_type
        seniority_level = request.seniority_level or content.seniority_level
        currency = (request.currency or content.compensation.currency or "USD").upper()
        salary_min, salary_max = request.salary_min, request.salary_max
        if salary_min is None and salary_max is None:
            comp = content.compensation
            if comp.min is not None and comp.max is not None and comp.min <= comp.max:
                salary_min, salary_max = Decimal(str(comp.min)), Decimal(str(comp.max))

        taxonomy = _taxonomy_from_llm(
            content.required_skills,
            content.preferred_skills,
            content.min_total_experience_years,
            content.education_level,
        )
        markdown = render_description_markdown(
            content,
            company_name=company_name,
            location=location,
            remote_policy=remote_policy,
            employment_type=employment_type,
            seniority_level=seniority_level,
            salary_min=salary_min,
            salary_max=salary_max,
            currency=currency,
        )
        return JobDraft(
            title=title,
            department=department,
            location=location,
            remote_policy=remote_policy,
            employment_type=employment_type,
            seniority_level=seniority_level,
            salary_min=salary_min,
            salary_max=salary_max,
            currency=currency,
            description_markdown=markdown,
            taxonomy=taxonomy,
            model=self._llm.model,
        )


class TaxonomyExtractor:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def extract(self, *, title: str, description_markdown: str) -> JobTaxonomy:
        extracted = await generate_validated(
            self._llm,
            ExtractedTaxonomy,
            system=TAXONOMY_SYSTEM_PROMPT,
            prompt=f"Job title: {title}\n\nJob description:\n{description_markdown}",
        )
        return _taxonomy_from_llm(
            extracted.required_skills,
            extracted.preferred_skills,
            extracted.min_total_experience_years,
            extracted.education_level,
        )
