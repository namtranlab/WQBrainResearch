from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import math

METRICS = ("sharpe", "fitness", "returns", "turnover", "drawdown", "margin",
           "longCount", "shortCount")
TEXT_SETTINGS = ("region", "universe", "instrumentType", "neutralization",
                 "pasteurization", "unitHandling", "nanHandling", "language")
SETTINGS = set(TEXT_SETTINGS) | {"delay", "decay", "truncation", "visualization"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def payload(expression: str, settings: dict) -> dict:
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("Expression must be a nonempty string.")
    validate_settings(settings)
    return {"type": "REGULAR", "regular": expression, "settings": dict(settings)}


def validate_settings(settings: dict) -> None:
    if not isinstance(settings, dict) or set(settings) != SETTINGS:
        raise ValueError("Settings must contain exactly: " + ", ".join(sorted(SETTINGS)))
    for key in TEXT_SETTINGS:
        if not isinstance(settings[key], str) or not settings[key].strip():
            raise ValueError(f"{key} must be a nonempty string.")
    for key in ("delay", "decay"):
        if type(settings[key]) is not int or settings[key] < 0:
            raise ValueError(f"{key} must be a nonnegative integer.")
    number = settings["truncation"]
    if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError("truncation must be a finite number between 0 and 1.")
    if type(settings["visualization"]) is not bool:
        raise ValueError("visualization must be a boolean.")


def validate_batch(expressions: list, settings: dict, limit: int) -> None:
    if type(limit) is not int or limit < 1:
        raise ValueError("Batch limit must be a positive integer.")
    if not isinstance(expressions, list) or not 0 < len(expressions) <= limit:
        raise ValueError(f"Batch must be a nonempty JSON list of at most {limit} expressions.")
    for expression in expressions:
        payload(expression, settings)


def validate_description(description: dict) -> None:
    if not isinstance(description, dict) or set(description) != {"hypothesis", "implementation"}:
        raise ValueError("Description must contain hypothesis and implementation.")
    if any(not isinstance(value, str) or not value.strip() for value in description.values()):
        raise ValueError("Hypothesis and implementation must be nonempty strings.")


def validate_alpha(alpha: dict, settings: dict) -> None:
    if not isinstance(alpha, dict) or set(alpha) != {"expression", "description"}:
        raise ValueError("Alpha file must contain one object with expression and description.")
    payload(alpha["expression"], settings)
    validate_description(alpha["description"])


@dataclass
class Job:
    id: str
    fingerprint: str
    expression: str
    settings: dict
    origin: str
    account: str
    schema_version: int = 1
    state: str = "queued"
    stage: str = "queued"
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    attempted_at: str | None = None
    submitted_at: str | None = None
    completed_at: str | None = None
    progress_url: str | None = None
    alpha_id: str | None = None
    next_poll_at: float = 0
    error: str | None = None
    metrics: dict = field(default_factory=lambda: dict.fromkeys(METRICS))
    checks: list = field(default_factory=list)
    grade: str | None = None
    raw_progress: dict = field(default_factory=dict)
    raw_result: dict = field(default_factory=dict)
    description: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
