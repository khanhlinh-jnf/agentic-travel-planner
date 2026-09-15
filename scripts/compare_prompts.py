"""Compare local templates offline, or explicitly run two planner generations on frozen evidence."""

import argparse
import asyncio
import difflib
import json
import sys
from dataclasses import replace
from datetime import date
from unittest.mock import patch

from app.config import settings
from app.prompts.registry import PromptRegistry
from app.schemas import PlanNarrative, TripRequest
from app.services.llm import _managed_parse
from app.services.observability import run_scope


async def evaluate(prompt, variables):
    with run_scope(f"eval-v{prompt.version}", "offline-evaluator", "eval") as run:
        # Pin local artifact and register it in the same request-scoped telemetry.
        pinned = replace(prompt)
        run.prompts[(prompt.name, prompt.version, "eval")] = pinned
        with patch.object(PromptRegistry, "get", return_value=pinned):
            result = await asyncio.to_thread(
                _managed_parse, "itinerary_planner", PlanNarrative, variables
            )
        run.status = "completed"
    metrics = run.summary()
    print(json.dumps({k: metrics[k] for k in (
        "elapsed_ms", "input_tokens", "output_tokens", "cost_usd", "llm_calls"
    )}))
    return result


def main():
    # Windows terminals and redirected output may default to cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="At most two LLM calls; no search APIs")
    args = parser.parse_args()
    registry = PromptRegistry()
    versions = [registry.local("itinerary_planner", version=v) for v in (1, 2)]
    case = json.loads((settings.project_root / "evals/planner_case.json").read_text("utf-8"))
    messages = [p.compile(**case["variables"]) for p in versions]
    for line in difflib.unified_diff(
        json.dumps(messages[0], ensure_ascii=False, indent=2).splitlines(),
        json.dumps(messages[1], ensure_ascii=False, indent=2).splitlines(),
        fromfile="v1", tofile="v2", lineterm="",
    ):
        print(line)
    if not args.live:
        print("Offline diff only: no LLM calls. --live compares one case with two paid calls.")
        return
    request = TripRequest.model_validate(json.loads(case["variables"]["trip_request"]))
    for prompt in versions:
        # Pin local version; no Langfuse prompt fetch or provider search in this experiment.
        result = asyncio.run(evaluate(prompt, case["variables"]))
        if result is None:
            print(f"v{prompt.version}: parse failed")
            continue
        days_ok = len(result.itinerary) == request.duration_days
        start_ok = bool(result.itinerary) and (
            result.itinerary[0].date == date.fromisoformat(case["expected_start"])
        )
        print(f"v{prompt.version}: days_ok={days_ok}, start_date_ok={start_ok}")
        print(result.model_dump_json(indent=2))
    print("Small comparison only; review feasibility and evidence manually.")


if __name__ == "__main__":
    main()
