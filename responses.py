"""Synthetic response pipeline.

POST /submit-form-url runs four steps for a loaded Google Form:
  A. Persona generation  - an LLM produces `num_responses` demographic profiles
  B. Response simulation - each persona answers the form's questions
  C. Submission          - answers are posted to the form's responder endpoint
  D. Analytics           - demographic splits, archetype clusters, diversity index

The LLM is reached through the OpenAI SDK (any OpenAI-compatible endpoint via
OPENAI_BASE_URL). When OPENAI_API_KEY is missing, or LLM_PROVIDER=mock, a
deterministic offline generator is used so the app still works end to end.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from typing import Any, Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Form, HTTPException, Request
from googleapiclient.errors import HttpError
from pydantic import BaseModel, Field

import forms as forms_module

load_dotenv()

logger = logging.getLogger("synthetic")
router = APIRouter(tags=["synthetic"])

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "auto").lower()  # auto | openai | mock
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
SUBMISSION_MODE = os.environ.get("SUBMISSION_MODE", "auto").lower()  # auto | live | mock
PERSONA_BATCH_SIZE = int(os.environ.get("PERSONA_BATCH_SIZE", "20"))
ANSWER_BATCH_SIZE = int(os.environ.get("ANSWER_BATCH_SIZE", "5"))
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "6"))
MAX_RESPONSES = 1000

# --- Pydantic schemas ----------------------------------------------------------

Gender = Literal["Female", "Male", "Non-binary", "Prefer not to say"]
IncomeBracket = Literal["Low", "Lower-middle", "Middle", "Upper-middle", "High"]
Education = Literal[
    "High school or less",
    "Vocational / technical",
    "Some college",
    "Bachelor's degree",
    "Master's degree",
    "Doctorate",
]
Employment = Literal[
    "Student",
    "Employed full-time",
    "Employed part-time",
    "Self-employed",
    "Unemployed",
    "Homemaker",
    "Retired",
]

GENDER_VALUES = list(Gender.__args__)
INCOME_VALUES = list(IncomeBracket.__args__)
EDUCATION_VALUES = list(Education.__args__)
EMPLOYMENT_VALUES = list(Employment.__args__)
AGE_RANGES = ["Under 18", "18-24", "25-34", "35-44", "45-54", "55-64", "65+"]


class Profile(BaseModel):
    id: int
    name: str
    age: int = Field(ge=5, le=100)
    gender: Gender
    location: str
    income_bracket: IncomeBracket
    education: Education
    employment: Employment
    archetype: str = Field(description="Short label shared by similar personas, e.g. 'Budget-conscious commuter'")
    psychographic_summary: str = Field(description="2-3 sentences: values, habits, motivations, constraints")


class ProfileBatch(BaseModel):
    profiles: list[Profile]


class FormAnswer(BaseModel):
    question_id: str
    values: list[str] = Field(description="One value, or several for CHECKBOX. Empty list = skipped.")


class PersonaResponse(BaseModel):
    profile_id: int
    answers: list[FormAnswer]


class PersonaResponseBatch(BaseModel):
    responses: list[PersonaResponse]


class GeneratedCohort(BaseModel):
    target_audience: str
    profiles: list[Profile]
    responses: list[PersonaResponse]


class DistributionEntry(BaseModel):
    label: str
    count: int
    percentage: float


class Demographics(BaseModel):
    gender: list[DistributionEntry]
    age_ranges: list[DistributionEntry]
    income_tiers: list[DistributionEntry]
    education: list[DistributionEntry]
    employment: list[DistributionEntry]


class ArchetypeCluster(BaseModel):
    name: str
    count: int
    percentage: float
    description: str


class SampleAnswer(BaseModel):
    question: str
    answer: str


class SampleProfile(Profile):
    answers: list[SampleAnswer] = []


class AnalyticsMeta(BaseModel):
    num_responses: int
    target_audience: str
    diversity_score: str
    form_id: str
    form_title: Optional[str] = None
    llm_provider: str
    model: Optional[str] = None
    duration_seconds: float


class AnalyticsPayload(BaseModel):
    status: str = "success"
    meta: AnalyticsMeta
    demographics: Demographics
    archetypes: list[ArchetypeCluster]
    sample_profiles: list[SampleProfile]
    submitted_count: int
    submission_mode: str  # live | mock | partial
    submission_errors: list[str] = []
    diversity_breakdown: dict[str, float] = {}


# --- Live progress (polled by the UI while the POST is in flight) ---------------

STAGES = ["loading_form", "generating_personas", "simulating_answers", "submitting", "computing_statistics", "done", "error"]
_jobs: dict[str, dict[str, Any]] = {}


def _set_progress(job_id: Optional[str], stage: str, message: str, **extra: Any) -> None:
    if not job_id:
        return
    _jobs[job_id] = {"stage": stage, "message": message, "updated_at": time.time(), **extra}
    # Keep the in-memory store small.
    if len(_jobs) > 500:
        for key in sorted(_jobs, key=lambda k: _jobs[k]["updated_at"])[:100]:
            _jobs.pop(key, None)


@router.get("/generation-status/{job_id}")
async def generation_status(job_id: str):
    return _jobs.get(job_id) or {"stage": "pending", "message": "Waiting to start…"}


# --- Form structure normalisation -----------------------------------------------

def extract_form_id(form_url: str) -> str:
    match = re.search(r"forms/d/(?:e/)?([a-zA-Z0-9_-]+)", form_url.strip())
    return match.group(1) if match else form_url.strip()


def normalize_question(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Flatten a Forms API questionItem into {questionId, title, type, options, ...}."""
    q_item = item.get("questionItem")
    if not q_item:
        return None
    question = q_item.get("question", {})
    base = {
        "itemId": item.get("itemId"),
        "questionId": question.get("questionId"),
        "title": item.get("title") or "(untitled question)",
        "description": item.get("description") or "",
        "required": bool(question.get("required", False)),
        "options": [],
        "allows_other": False,
    }
    if "choiceQuestion" in question:
        choice = question["choiceQuestion"]
        base["type"] = choice.get("type", "RADIO")
        base["options"] = [o["value"] for o in choice.get("options", []) if o.get("value")]
        base["allows_other"] = any(o.get("isOther") for o in choice.get("options", []))
    elif "textQuestion" in question:
        base["type"] = "PARAGRAPH" if question["textQuestion"].get("paragraph") else "TEXT"
    elif "scaleQuestion" in question:
        scale = question["scaleQuestion"]
        base["type"] = "SCALE"
        base["low"] = int(scale.get("low", 1))
        base["high"] = int(scale.get("high", 5))
        base["lowLabel"] = scale.get("lowLabel", "")
        base["highLabel"] = scale.get("highLabel", "")
    elif "dateQuestion" in question:
        base["type"] = "DATE"
        base["includeYear"] = question["dateQuestion"].get("includeYear", True)
        base["includeTime"] = question["dateQuestion"].get("includeTime", False)
    elif "timeQuestion" in question:
        base["type"] = "TIME"
    else:
        base["type"] = "UNSUPPORTED"
    return base


def load_form_structure(request: Request, form_id: str) -> dict[str, Any]:
    service = forms_module.get_forms_client(request)
    try:
        data = service.forms().get(formId=form_id).execute()
    except HttpError as err:
        status = err.resp.status
        detail = (
            "Access denied — the logged-in account must be an editor/owner of this form."
            if status == 403
            else "Failed to fetch form."
        )
        raise HTTPException(status_code=status, detail=detail) from err

    questions = [q for q in (normalize_question(i) for i in data.get("items", [])) if q]
    page_count = 1 + sum(1 for i in data.get("items", []) if "pageBreakItem" in i)
    return {
        "formId": data.get("formId"),
        "title": data.get("info", {}).get("title"),
        "responderUri": data.get("responderUri"),
        "pageCount": page_count,
        "questions": questions,
    }


def describe_question(q: dict[str, Any]) -> str:
    desc = f'- questionId "{q["questionId"]}" [{q["type"]}{", required" if q["required"] else ""}]: {q["title"]}'
    if q.get("description"):
        desc += f" ({q['description']})"
    if q["type"] in ("RADIO", "DROP_DOWN", "CHECKBOX"):
        desc += "\n    options: " + " | ".join(q["options"])
        if q["allows_other"]:
            desc += " | <free-text 'Other'>"
        if q["type"] == "CHECKBOX":
            desc += "\n    (pick one or more options)"
    elif q["type"] == "SCALE":
        desc += f"\n    integer from {q['low']} ({q.get('lowLabel') or 'low'}) to {q['high']} ({q.get('highLabel') or 'high'})"
    elif q["type"] == "DATE":
        desc += "\n    format YYYY-MM-DD"
    elif q["type"] == "TIME":
        desc += "\n    format HH:MM (24h)"
    return desc


# --- LLM access -----------------------------------------------------------------

def resolve_provider() -> str:
    if LLM_PROVIDER == "mock":
        return "mock"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if LLM_PROVIDER == "openai":
        raise HTTPException(status_code=500, detail="LLM_PROVIDER=openai but OPENAI_API_KEY is not set.")
    return "mock"


class LLMClient:
    def __init__(self, provider: str):
        self.provider = provider
        self._client = None
        self._sem = asyncio.Semaphore(LLM_CONCURRENCY)
        if provider == "openai":
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI()

    async def structured(self, system: str, user: str, schema: type[BaseModel], temperature: float = 0.9) -> BaseModel:
        async with self._sem:
            completion = await self._client.chat.completions.parse(
                model=LLM_MODEL,
                temperature=temperature,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format=schema,
            )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError("LLM returned no structured output (refusal or empty response).")
        return parsed


# --- Step A: persona generation -------------------------------------------------

PERSONA_SYSTEM = """You are a market-research demographer who designs realistic synthetic survey panels.
You produce diverse, believable personas that all fit a target audience description but differ
from each other the way real people do: spread ages across the allowed range, vary gender, income,
education, employment, neighbourhood/city, household situation, values and habits. Avoid stereotypes,
duplicate names and copy-paste summaries. Use culturally appropriate names for the location.
Assign each persona an `archetype`: a short label (2-4 words) describing its behavioural segment.
Across the whole panel use only 3 to 5 distinct archetype labels, reusing them for similar personas."""


async def generate_personas(llm: LLMClient, target_audience: str, n: int, progress_cb) -> list[Profile]:
    if llm.provider == "mock":
        return mock_personas(target_audience, n)

    batches: list[tuple[int, int]] = []
    start = 1
    while start <= n:
        size = min(PERSONA_BATCH_SIZE, n - start + 1)
        batches.append((start, size))
        start += size

    done = 0

    async def run(start_id: int, size: int) -> list[Profile]:
        nonlocal done
        user = (
            f"Target audience: {target_audience}\n\n"
            f"Generate exactly {size} personas with ids {start_id} to {start_id + size - 1} (inclusive). "
            f"This is batch {batches.index((start_id, size)) + 1} of {len(batches)} for a panel of {n}; "
            "inject natural human variance while staying inside the audience boundary."
        )
        result: ProfileBatch = await llm.structured(PERSONA_SYSTEM, user, ProfileBatch)
        done += size
        progress_cb(done)
        return result.profiles

    results = await asyncio.gather(*(run(s, size) for s, size in batches))
    profiles = [p for batch in results for p in batch]
    seen: set[int] = set()
    for idx, profile in enumerate(profiles, start=1):
        if profile.id in seen or profile.id < 1:
            profile.id = idx
        seen.add(profile.id)
    return profiles[:n]


# --- Step B: response simulation ------------------------------------------------

ANSWER_SYSTEM = """You role-play survey respondents. For each persona provided you fill in the given
Google Form exactly as that person would, staying consistent with their age, gender, location,
income, education, employment, archetype and psychographic summary.
Rules:
- Use the exact questionId for every answer.
- RADIO / DROP_DOWN: `values` has exactly one option, copied verbatim from the options list
  (or a short free-text if the question allows 'Other').
- CHECKBOX: `values` has one or more options copied verbatim.
- SCALE: a single integer within the given range, as a string.
- TEXT: a short phrase or sentence. PARAGRAPH: 2-4 sentences in the persona's own voice.
- DATE: YYYY-MM-DD. TIME: HH:MM.
- Answer every required question. Optional questions may occasionally be skipped (empty list) as real people do.
- Different personas must not give identical free-text answers."""


async def simulate_responses(llm: LLMClient, profiles: list[Profile], questions: list[dict[str, Any]], progress_cb) -> list[PersonaResponse]:
    if llm.provider == "mock":
        return [mock_answers(p, questions) for p in profiles]

    q_text = "\n".join(describe_question(q) for q in questions if q["type"] != "UNSUPPORTED")
    batches = [profiles[i : i + ANSWER_BATCH_SIZE] for i in range(0, len(profiles), ANSWER_BATCH_SIZE)]
    done = 0

    async def run(batch: list[Profile]) -> list[PersonaResponse]:
        nonlocal done
        personas = "\n\n".join(
            f"Persona {p.id}: {p.name}, {p.age}, {p.gender}, {p.location}; income {p.income_bracket}; "
            f"{p.education}; {p.employment}; archetype '{p.archetype}'.\n{p.psychographic_summary}"
            for p in batch
        )
        user = f"FORM QUESTIONS:\n{q_text}\n\nPERSONAS:\n{personas}\n\nReturn one response object per persona, with profile_id set to the persona id."
        result: PersonaResponseBatch = await llm.structured(ANSWER_SYSTEM, user, PersonaResponseBatch)
        done += len(batch)
        progress_cb(done)
        return result.responses

    results = await asyncio.gather(*(run(b) for b in batches))
    by_id = {r.profile_id: r for batch in results for r in batch}
    return [by_id.get(p.id) or PersonaResponse(profile_id=p.id, answers=[]) for p in profiles]


def coerce_answers(profile: Profile, response: PersonaResponse, questions: list[dict[str, Any]], rng: random.Random) -> PersonaResponse:
    """Force every answer into a valid shape for its question; fill required gaps."""
    given = {a.question_id: [v.strip() for v in a.values if v and v.strip()] for a in response.answers}
    fixed: list[FormAnswer] = []
    for q in questions:
        if q["type"] == "UNSUPPORTED":
            continue
        values = given.get(q["questionId"], [])
        qtype = q["type"]
        if qtype in ("RADIO", "DROP_DOWN", "CHECKBOX"):
            lookup = {o.lower(): o for o in q["options"]}
            valid = [lookup[v.lower()] for v in values if v.lower() in lookup]
            other = [v for v in values if v.lower() not in lookup]
            if qtype == "CHECKBOX":
                values = list(dict.fromkeys(valid))
                if q["allows_other"] and other:
                    values.append(other[0])
            else:
                if valid:
                    values = [valid[0]]
                elif q["allows_other"] and other:
                    values = [other[0][:200]]
                elif values or q["required"]:
                    values = [rng.choice(q["options"])] if q["options"] else []
                else:
                    values = []
            if not values and q["required"] and q["options"]:
                values = [rng.choice(q["options"])]
        elif qtype == "SCALE":
            low, high = q["low"], q["high"]
            num = None
            if values:
                m = re.search(r"-?\d+", values[0])
                if m:
                    num = max(low, min(high, int(m.group())))
            if num is None and (values or q["required"]):
                num = rng.randint(low, high)
            values = [str(num)] if num is not None else []
        elif qtype in ("TEXT", "PARAGRAPH"):
            limit = 2000 if qtype == "PARAGRAPH" else 300
            values = [values[0][:limit]] if values else ([mock_text(profile, q, rng)] if q["required"] else [])
        elif qtype == "DATE":
            m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", values[0]) if values else None
            if m:
                values = [f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"]
            elif values or q["required"]:
                values = [f"{rng.randint(2015, 2025)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"]
            else:
                values = []
        elif qtype == "TIME":
            m = re.search(r"(\d{1,2}):(\d{2})", values[0]) if values else None
            if m:
                values = [f"{min(23, int(m.group(1))):02d}:{min(59, int(m.group(2))):02d}"]
            elif values or q["required"]:
                values = [f"{rng.randint(6, 22):02d}:{rng.choice([0, 15, 30, 45]):02d}"]
            else:
                values = []
        fixed.append(FormAnswer(question_id=q["questionId"], values=values))
    return PersonaResponse(profile_id=profile.id, answers=fixed)


# --- Step C: submission ----------------------------------------------------------

def entry_key(question_id: str) -> str:
    """Forms API questionIds are the hex form of the `entry.<n>` ids used by the responder page."""
    return f"entry.{int(question_id, 16)}"


def build_submission_fields(response: PersonaResponse, questions: list[dict[str, Any]], page_count: int) -> dict[str, list[str]]:
    """Form-encoded body for the responder endpoint; repeated keys (CHECKBOX) are lists."""
    by_id = {q["questionId"]: q for q in questions}
    fields: dict[str, list[str]] = defaultdict(list)
    for ans in response.answers:
        q = by_id.get(ans.question_id)
        if not q or not ans.values:
            continue
        key = entry_key(q["questionId"])
        if q["type"] in ("RADIO", "DROP_DOWN", "CHECKBOX"):
            for v in ans.values:
                if v in q["options"]:
                    fields[key].append(v)
                elif q["allows_other"]:
                    fields[key].append("__other_option__")
                    fields[f"{key}.other_option_response"].append(v)
        elif q["type"] == "SCALE" or q["type"] in ("TEXT", "PARAGRAPH"):
            fields[key].append(ans.values[0])
        elif q["type"] == "DATE":
            year, month, day = ans.values[0].split("-")
            if q.get("includeYear", True):
                fields[f"{key}_year"].append(year)
            fields[f"{key}_month"].append(str(int(month)))
            fields[f"{key}_day"].append(str(int(day)))
        elif q["type"] == "TIME":
            hour, minute = ans.values[0].split(":")
            fields[f"{key}_hour"].append(str(int(hour)))
            fields[f"{key}_minute"].append(str(int(minute)))
    fields["fvv"].append("1")
    fields["pageHistory"].append(",".join(str(i) for i in range(page_count)))
    return dict(fields)


async def submit_responses(form: dict[str, Any], responses: list[PersonaResponse], progress_cb) -> tuple[int, str, list[str]]:
    """Returns (submitted_count, mode, errors). mode: live | mock | partial."""
    total = len(responses)
    if SUBMISSION_MODE == "mock":
        progress_cb(total)
        return total, "mock", []

    responder_uri = form.get("responderUri")
    if not responder_uri:
        progress_cb(total)
        return total, "mock", ["Form has no responderUri (not accepting responses?) — mock submission used."]

    endpoint = responder_uri.replace("/viewform", "/formResponse")
    errors: list[str] = []
    submitted = 0
    sem = asyncio.Semaphore(4)
    done = 0

    async with httpx.AsyncClient(timeout=30, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0 SyntheticAgentForm/1.0"}) as client:

        async def send(resp: PersonaResponse) -> bool:
            nonlocal done
            fields = build_submission_fields(resp, form["questions"], form.get("pageCount", 1))
            async with sem:
                try:
                    r = await client.post(endpoint, data=fields)
                except (httpx.HTTPError, RuntimeError) as exc:
                    errors.append(f"persona {resp.profile_id}: network error {exc}")
                    return False
                finally:
                    done += 1
                    progress_cb(done)
            if r.status_code in (301, 302, 303, 307) and "accounts.google.com" in r.headers.get("location", ""):
                errors.append(f"persona {resp.profile_id}: form requires Google sign-in to respond")
                return False
            if r.status_code >= 400:
                errors.append(f"persona {resp.profile_id}: HTTP {r.status_code}")
                return False
            if 'name="entry.' in r.text:  # Google re-rendered the form => validation failure
                errors.append(f"persona {resp.profile_id}: form rejected the answers (validation)")
                return False
            return True

        results = await asyncio.gather(*(send(r) for r in responses))
    submitted = sum(1 for ok in results if ok)

    if submitted == total:
        return submitted, "live", []
    if SUBMISSION_MODE == "auto" and submitted == 0:
        errors.insert(0, "Live submission failed for every response — fell back to mock submission.")
        return total, "mock", errors[:6]
    return submitted, "partial" if submitted else "failed", errors[:6]


# --- Step D: analytics ------------------------------------------------------------

def age_range(age: int) -> str:
    if age < 18:
        return "Under 18"
    if age < 25:
        return "18-24"
    if age < 35:
        return "25-34"
    if age < 45:
        return "35-44"
    if age < 55:
        return "45-54"
    if age < 65:
        return "55-64"
    return "65+"


def distribution(values: list[str], order: Optional[list[str]] = None) -> list[DistributionEntry]:
    counts = Counter(values)
    total = max(1, len(values))
    labels = [l for l in (order or []) if l in counts] + sorted((l for l in counts if l not in (order or [])), key=lambda l: -counts[l])
    return [DistributionEntry(label=l, count=counts[l], percentage=round(100 * counts[l] / total, 1)) for l in labels]


def trait_diversity(values: list[str], category_count: int) -> float:
    """1 - std/std_max of category shares; 1.0 = perfectly even spread, 0.0 = everyone identical."""
    n = len(values)
    if n < 2:
        return 0.0
    k = max(2, min(category_count, n))
    counts = Counter(values)
    shares = [counts.get(c, 0) / n for c in counts] + [0.0] * max(0, k - len(counts))
    std = statistics.pstdev(shares[:k] if len(shares) > k else shares)
    std_max = math.sqrt(k - 1) / k
    return max(0.0, min(1.0, 1 - std / std_max)) if std_max else 0.0


def age_diversity(ages: list[int]) -> float:
    """Spread of ages relative to a uniform spread over the cohort's own min..max."""
    if len(ages) < 2 or max(ages) == min(ages):
        return 0.0
    std_max = (max(ages) - min(ages)) / math.sqrt(12)
    return max(0.0, min(1.0, statistics.pstdev(ages) / std_max))


def build_archetypes(profiles: list[Profile]) -> list[ArchetypeCluster]:
    groups: dict[str, list[Profile]] = defaultdict(list)
    for p in profiles:
        groups[p.archetype.strip().title() or "Unlabelled"].append(p)
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    if len(ranked) > 5:
        head, tail = ranked[:4], ranked[4:]
        other = [p for _, members in tail for p in members]
        ranked = head + [("Other segments", other)]
    total = max(1, len(profiles))
    clusters = []
    for name, members in ranked:
        ages = Counter(age_range(p.age) for p in members).most_common(1)[0][0]
        employment = Counter(p.employment for p in members).most_common(1)[0][0]
        income = Counter(p.income_bracket for p in members).most_common(1)[0][0]
        clusters.append(
            ArchetypeCluster(
                name=name,
                count=len(members),
                percentage=round(100 * len(members) / total, 1),
                description=f"Mostly {ages} · {employment} · {income} income",
            )
        )
    return clusters


def build_analytics(cohort: GeneratedCohort, questions: list[dict[str, Any]]) -> tuple[Demographics, list[ArchetypeCluster], float, dict[str, float], list[SampleProfile]]:
    profiles = cohort.profiles
    demographics = Demographics(
        gender=distribution([p.gender for p in profiles], GENDER_VALUES),
        age_ranges=distribution([age_range(p.age) for p in profiles], AGE_RANGES),
        income_tiers=distribution([p.income_bracket for p in profiles], INCOME_VALUES),
        education=distribution([p.education for p in profiles], EDUCATION_VALUES),
        employment=distribution([p.employment for p in profiles], EMPLOYMENT_VALUES),
    )
    archetypes = build_archetypes(profiles)
    breakdown = {
        "gender": trait_diversity([p.gender for p in profiles], len(GENDER_VALUES)),
        "age": age_diversity([p.age for p in profiles]),
        "income": trait_diversity([p.income_bracket for p in profiles], len(INCOME_VALUES)),
        "education": trait_diversity([p.education for p in profiles], len(EDUCATION_VALUES)),
        "employment": trait_diversity([p.employment for p in profiles], len(EMPLOYMENT_VALUES)),
        "archetype": trait_diversity([p.archetype.strip().title() for p in profiles], 5),
        "location": trait_diversity([p.location.strip().lower() for p in profiles], 6),
    }
    diversity = round(100 * sum(breakdown.values()) / len(breakdown), 1) if profiles else 0.0
    breakdown = {k: round(100 * v, 1) for k, v in breakdown.items()}

    q_by_id = {q["questionId"]: q for q in questions}
    resp_by_id = {r.profile_id: r for r in cohort.responses}
    samples: list[SampleProfile] = []
    seen_archetypes: set[str] = set()
    ordered = sorted(profiles, key=lambda p: (p.archetype.strip().title() in seen_archetypes or seen_archetypes.add(p.archetype.strip().title()) is not None, p.id))
    for p in ordered[:6]:
        answers = []
        for a in (resp_by_id.get(p.id).answers if resp_by_id.get(p.id) else []):
            q = q_by_id.get(a.question_id)
            if q and a.values:
                answers.append(SampleAnswer(question=q["title"], answer=", ".join(a.values)))
            if len(answers) == 3:
                break
        samples.append(SampleProfile(**p.model_dump(), answers=answers))
    return demographics, archetypes, diversity, breakdown, samples


# --- Mock provider (no API key) ------------------------------------------------------

FIRST_NAMES = {
    "Female": ["Sofía", "Valentina", "Camila", "Ana", "Lucía", "Mariana", "Emma", "Olivia", "Aisha", "Priya", "Mei", "Fatima", "Chloe", "Nora", "Isabela"],
    "Male": ["Santiago", "Mateo", "Diego", "Luis", "Andrés", "Liam", "Noah", "Omar", "Arjun", "Kenji", "Daniel", "Gabriel", "Ethan", "Tomás", "Samuel"],
    "Non-binary": ["Alex", "Sam", "Ari", "Kai", "Robin", "Noa", "Dani", "Jules"],
    "Prefer not to say": ["J.", "M.", "R.", "A.", "S."],
}
LAST_NAMES = ["García", "Hernández", "López", "Martínez", "Rodríguez", "Pérez", "Smith", "Johnson", "Khan", "Patel", "Nguyen", "Kim", "Silva", "Rossi", "Müller", "Okafor"]
ARCHETYPES = [
    ("Pragmatic Planner", "values stability, researches before buying and budgets carefully"),
    ("Social Explorer", "is trend-aware and heavily influenced by friends and social media"),
    ("Busy Juggler", "is time-poor and prioritises convenience and speed over price"),
    ("Value Seeker", "is price-sensitive, compares options and hunts for discounts"),
    ("Mindful Idealist", "cares about sustainability, health and brand ethics"),
]
EMPLOYMENT_BY_AGE = {
    "Under 18": ["Student"],
    "18-24": ["Student", "Student", "Employed part-time", "Employed full-time", "Unemployed"],
    "25-34": ["Employed full-time", "Employed full-time", "Self-employed", "Employed part-time", "Student"],
    "35-44": ["Employed full-time", "Self-employed", "Homemaker", "Employed part-time"],
    "45-54": ["Employed full-time", "Self-employed", "Homemaker", "Unemployed"],
    "55-64": ["Employed full-time", "Self-employed", "Retired", "Employed part-time"],
    "65+": ["Retired", "Retired", "Self-employed"],
}


def _rng_for(*parts: Any) -> random.Random:
    seed = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(seed[:16], 16))


def _audience_age_bounds(audience: str) -> tuple[int, int]:
    m = re.search(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})", audience)
    if m:
        lo, hi = sorted((int(m.group(1)), int(m.group(2))))
        return max(5, lo), min(100, hi)
    m = re.search(r"(?:over|above|\+)\s*(\d{2})|(\d{2})\s*\+", audience)
    if m:
        lo = int(m.group(1) or m.group(2))
        return lo, min(100, lo + 25)
    a = audience.lower()
    if "student" in a or "teen" in a:
        return 17, 26
    if "retire" in a or "senior" in a:
        return 60, 85
    return 18, 65


def _audience_location(audience: str) -> str:
    m = re.search(r"\b(?:in|from|living in|based in)\s+([A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*){0,3})", audience)
    return m.group(1).rstrip(".,") if m else "Unspecified"


def mock_personas(target_audience: str, n: int) -> list[Profile]:
    rng = _rng_for("personas", target_audience, n)
    lo, hi = _audience_age_bounds(target_audience)
    city = _audience_location(target_audience)
    districts = ["Centro", "Norte", "Sur", "Oeste", "Este", "suburbs", "old town", "university district"]
    a = target_audience.lower()
    gender_weights = [0.48, 0.47, 0.03, 0.02]
    if re.search(r"\bwomen\b|\bfemale\b|\bmothers?\b", a):
        gender_weights = [0.94, 0.0, 0.04, 0.02]
    elif re.search(r"\bmen\b|\bmale\b|\bfathers?\b", a):
        gender_weights = [0.0, 0.94, 0.04, 0.02]
    archetype_pool = ARCHETYPES[: rng.randint(3, 5)]
    profiles = []
    for i in range(1, n + 1):
        gender = rng.choices(GENDER_VALUES, weights=gender_weights)[0]
        age = int(round(min(hi, max(lo, rng.gauss((lo + hi) / 2, max(1.5, (hi - lo) / 4))))))
        band = age_range(age)
        employment = rng.choice(EMPLOYMENT_BY_AGE[band])
        if "student" in a and rng.random() < 0.7:
            employment = "Student"
        if age < 22:
            education = rng.choice(["High school or less", "Some college", "Some college"])
        elif age < 30:
            education = rng.choice(["Some college", "Bachelor's degree", "Bachelor's degree", "Vocational / technical", "Master's degree"])
        else:
            education = rng.choice(EDUCATION_VALUES)
        income = rng.choices(INCOME_VALUES, weights=[0.2, 0.3, 0.3, 0.15, 0.05] if employment in ("Student", "Unemployed") else [0.1, 0.25, 0.35, 0.2, 0.1])[0]
        archetype, trait = rng.choice(archetype_pool)
        first = rng.choice(FIRST_NAMES[gender])
        name = f"{first} {rng.choice(LAST_NAMES)}"
        location = f"{rng.choice(districts)}, {city}" if city != "Unspecified" else rng.choice(["Mexico City", "Madrid", "Austin", "Toronto", "Berlin", "São Paulo"])
        summary = (
            f"{first} is a {age}-year-old {employment.lower()} living in {location}. "
            f"As a {archetype.lower()}, {first} {trait}. "
            f"{rng.choice(['Spends most free time with friends and family.', 'Commutes daily and listens to podcasts on the way.', 'Cooks at home to save money.', 'Follows local news and sports closely.', 'Is active in a community or hobby group.', 'Shops online late at night.'])}"
        )
        profiles.append(
            Profile(
                id=i, name=name, age=age, gender=gender, location=location, income_bracket=income,
                education=education, employment=employment, archetype=archetype, psychographic_summary=summary,
            )
        )
    return profiles


def mock_text(profile: Profile, q: dict[str, Any], rng: random.Random) -> str:
    openers = ["Honestly,", "For me,", "I'd say", "In my experience,", "To be fair,"]
    topic = re.sub(r"[?¿!.]+$", "", q["title"]).strip().lower()
    if q["type"] == "PARAGRAPH":
        return (
            f"{rng.choice(openers)} {topic} depends a lot on my routine as a {profile.employment.lower()} in {profile.location}. "
            f"I tend to {rng.choice(['look for value', 'follow recommendations from friends', 'go with what is convenient', 'research carefully first'])}, "
            f"which fits my {profile.income_bracket.lower()} budget. {rng.choice(['It matters to me.', 'I could be convinced otherwise.', 'That has changed over the years.'])}"
        )
    return f"{rng.choice(openers)} {rng.choice(['it is fine', 'it could be better', 'I like it', 'not sure yet', 'it works for me'])}"


def mock_answers(profile: Profile, questions: list[dict[str, Any]]) -> PersonaResponse:
    rng = _rng_for("answers", profile.id, profile.name)
    answers = []
    for q in questions:
        if q["type"] == "UNSUPPORTED":
            continue
        if not q["required"] and rng.random() < 0.08:
            answers.append(FormAnswer(question_id=q["questionId"], values=[]))
            continue
        if q["type"] in ("RADIO", "DROP_DOWN"):
            values = [rng.choice(q["options"])] if q["options"] else []
        elif q["type"] == "CHECKBOX":
            k = rng.randint(1, max(1, min(3, len(q["options"]))))
            values = rng.sample(q["options"], k) if q["options"] else []
        elif q["type"] == "SCALE":
            bias = {"Low": -1, "Lower-middle": -0.5, "Middle": 0, "Upper-middle": 0.5, "High": 1}[profile.income_bracket]
            mid = (q["low"] + q["high"]) / 2
            values = [str(int(round(max(q["low"], min(q["high"], rng.gauss(mid + bias, 1.2))))))]
        elif q["type"] in ("TEXT", "PARAGRAPH"):
            values = [mock_text(profile, q, rng)]
        elif q["type"] == "DATE":
            values = [f"{rng.randint(2018, 2025)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"]
        elif q["type"] == "TIME":
            values = [f"{rng.randint(6, 22):02d}:{rng.choice([0, 15, 30, 45]):02d}"]
        else:
            values = []
        answers.append(FormAnswer(question_id=q["questionId"], values=values))
    return PersonaResponse(profile_id=profile.id, answers=answers)


# --- Orchestration -------------------------------------------------------------------

async def run_pipeline(form: dict[str, Any], target_audience: str, num_responses: int, job_id: Optional[str]) -> AnalyticsPayload:
    started = time.time()
    provider = resolve_provider()
    llm = LLMClient(provider)
    questions = [q for q in form["questions"] if q["type"] != "UNSUPPORTED"]
    if not questions:
        raise HTTPException(status_code=400, detail="This form has no supported questions to answer.")

    _set_progress(job_id, "generating_personas", f"Generating personas… 0/{num_responses}", done=0, total=num_responses)
    profiles = await generate_personas(
        llm, target_audience, num_responses,
        lambda done: _set_progress(job_id, "generating_personas", f"Generating personas… {done}/{num_responses}", done=done, total=num_responses),
    )
    if len(profiles) < num_responses:
        raise HTTPException(status_code=502, detail=f"LLM returned {len(profiles)} personas, expected {num_responses}.")

    _set_progress(job_id, "simulating_answers", f"Simulating answers… 0/{num_responses}", done=0, total=num_responses)
    raw_responses = await simulate_responses(
        llm, profiles, questions,
        lambda done: _set_progress(job_id, "simulating_answers", f"Simulating answers… {done}/{num_responses}", done=done, total=num_responses),
    )
    rng = _rng_for("coerce", target_audience, num_responses)
    responses = [coerce_answers(p, r, questions, rng) for p, r in zip(profiles, raw_responses)]
    cohort = GeneratedCohort(target_audience=target_audience, profiles=profiles, responses=responses)

    _set_progress(job_id, "submitting", f"Submitting to Google Forms… 0/{num_responses}", done=0, total=num_responses)
    submitted, mode, errors = await submit_responses(
        form, responses,
        lambda done: _set_progress(job_id, "submitting", f"Submitting to Google Forms… {done}/{num_responses}", done=done, total=num_responses),
    )

    _set_progress(job_id, "computing_statistics", "Computing statistics…")
    demographics, archetypes, diversity, breakdown, samples = build_analytics(cohort, questions)

    payload = AnalyticsPayload(
        meta=AnalyticsMeta(
            num_responses=num_responses,
            target_audience=target_audience,
            diversity_score=f"{diversity:.0f}%",
            form_id=form.get("formId") or "",
            form_title=form.get("title"),
            llm_provider=provider,
            model=LLM_MODEL if provider == "openai" else None,
            duration_seconds=round(time.time() - started, 1),
        ),
        demographics=demographics,
        archetypes=archetypes,
        sample_profiles=samples,
        submitted_count=submitted,
        submission_mode=mode,
        submission_errors=errors,
        diversity_breakdown=breakdown,
    )
    _set_progress(job_id, "done", "Done", done=num_responses, total=num_responses)
    return payload


@router.post("/submit-form-url", response_model=AnalyticsPayload)
async def submit_form_url(
    request: Request,
    form_url: str = Form(...),
    num_responses: int = Form(..., gt=0, le=MAX_RESPONSES),
    target_audience: str = Form(..., min_length=3),
    job_id: str = Form(None),
    user_id: str = Form(None),
):
    form_id = extract_form_id(form_url)
    if not form_id:
        raise HTTPException(status_code=400, detail="Couldn't find a form ID in that URL.")

    _set_progress(job_id, "loading_form", "Loading form structure…")
    try:
        form = await asyncio.to_thread(load_form_structure, request, form_id)
        payload = await run_pipeline(form, target_audience.strip(), num_responses, job_id)
    except HTTPException as exc:
        _set_progress(job_id, "error", str(exc.detail))
        raise
    except Exception as exc:  # noqa: BLE001 - surface LLM/network failures to the UI
        logger.exception("Synthetic generation failed")
        _set_progress(job_id, "error", f"Generation failed: {exc}")
        raise HTTPException(status_code=502, detail=f"Generation failed: {exc}") from exc

    logger.info(
        "[Synthetic] form=%s n=%d provider=%s submitted=%d mode=%s user=%s",
        form_id, num_responses, payload.meta.llm_provider, payload.submitted_count, payload.submission_mode,
        (request.session.get("user") or {}).get("email"),
    )
    return payload
