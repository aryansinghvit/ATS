"""Schema.org ``JobPosting`` JSON-LD for Google for Jobs (project.md Module 2)."""

from __future__ import annotations

from typing import Any

from app.models.job import EmploymentType, Job, RemotePolicy
from app.models.tenant import Tenant
from app.schemas.job import JobTaxonomy

_EMPLOYMENT_TYPES = {
    EmploymentType.FULL_TIME: "FULL_TIME",
    EmploymentType.PART_TIME: "PART_TIME",
    EmploymentType.CONTRACT: "CONTRACTOR",
    EmploymentType.TEMPORARY: "TEMPORARY",
    EmploymentType.INTERNSHIP: "INTERN",
}


def build_job_posting_json_ld(job: Job, tenant: Tenant, *, job_url: str) -> dict[str, Any]:
    organization: dict[str, Any] = {"@type": "Organization", "name": tenant.name}
    if tenant.logo_url:
        organization["logo"] = tenant.logo_url

    data: dict[str, Any] = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": job.title,
        "description": job.description_html,
        "identifier": {"@type": "PropertyValue", "name": tenant.name, "value": str(job.id)},
        "employmentType": _EMPLOYMENT_TYPES[EmploymentType(job.employment_type)],
        "hiringOrganization": organization,
        "directApply": True,
        "url": job_url,
    }
    if job.published_at is not None:
        data["datePosted"] = job.published_at.date().isoformat()

    if RemotePolicy(job.remote_policy) is RemotePolicy.REMOTE:
        data["jobLocationType"] = "TELECOMMUTE"
        data["applicantLocationRequirements"] = {"@type": "Country", "name": job.location}
    else:
        data["jobLocation"] = {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressLocality": job.location},
        }

    if job.salary_min is not None or job.salary_max is not None:
        value: dict[str, Any] = {"@type": "QuantitativeValue", "unitText": "YEAR"}
        if job.salary_min is not None and job.salary_max is not None:
            value.update(minValue=float(job.salary_min), maxValue=float(job.salary_max))
        else:
            value["value"] = float(job.salary_min if job.salary_min is not None else job.salary_max)
        data["baseSalary"] = {
            "@type": "MonetaryAmount",
            "currency": job.currency or "USD",
            "value": value,
        }

    years = JobTaxonomy.model_validate(job.taxonomy_json).min_total_experience_years
    if years > 0:
        data["experienceRequirements"] = {
            "@type": "OccupationalExperienceRequirements",
            "monthsOfExperience": int(years * 12),
        }
    return data
