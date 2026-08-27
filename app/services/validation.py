"""Trip request normalization and validation."""

from __future__ import annotations

from datetime import date, timedelta

from app.schemas import TripRequest

REQUIRED_FIELDS = (
    "origin",
    "destination",
    "departure_date",
    "travelers",
    "total_budget",
)


def normalize_request(request: TripRequest) -> TripRequest:
    values = request.model_dump()
    if request.departure_date and not request.return_date and request.duration_days:
        values["return_date"] = request.departure_date + timedelta(
            days=request.duration_days - 1
        )
    if request.departure_date and request.return_date and not request.duration_days:
        values["duration_days"] = (request.return_date - request.departure_date).days + 1
    return TripRequest.model_validate(values)


def validate_request(
    request: TripRequest,
    *,
    today: date | None = None,
) -> tuple[list[str], list[str]]:
    today = today or date.today()
    missing = [field for field in REQUIRED_FIELDS if getattr(request, field) in (None, "")]
    if request.return_date is None and request.duration_days is None:
        missing.append("return_date_or_duration")

    errors: list[str] = []
    if request.currency != "VND":
        errors.append("MVP chỉ hỗ trợ ngân sách VND.")
    if request.departure_date and request.departure_date < today:
        errors.append("Ngày khởi hành không được nằm trong quá khứ.")
    if request.return_date and request.departure_date:
        if request.return_date < request.departure_date:
            errors.append("Ngày về phải bằng hoặc sau ngày khởi hành.")
        expected_days = (request.return_date - request.departure_date).days + 1
        if request.duration_days and expected_days != request.duration_days:
            errors.append("Ngày về và số ngày chuyến đi đang mâu thuẫn.")
    if request.total_budget is not None and request.total_budget <= 0:
        errors.append("Ngân sách phải lớn hơn 0.")
    return missing, errors


def clarification_questions(missing: list[str], errors: list[str]) -> list[str]:
    labels = {
        "origin": "Bạn khởi hành từ thành phố nào?",
        "destination": "Bạn muốn đi đâu?",
        "departure_date": "Ngày khởi hành cụ thể (YYYY-MM-DD) là ngày nào?",
        "return_date_or_duration": "Bạn về ngày nào hoặc đi trong bao nhiêu ngày?",
        "travelers": "Có bao nhiêu người đi?",
        "total_budget": "Ngân sách tổng bằng VND là bao nhiêu?",
    }
    return [labels[field] for field in missing if field in labels] + errors
