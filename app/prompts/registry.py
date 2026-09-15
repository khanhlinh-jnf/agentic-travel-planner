"""Git artifacts plus an optional Langfuse deployment registry."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import settings

VARIABLE = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")
NAMES = {"trip_intake", "itinerary_planner", "revision_router"}
CONTRACTS = {
    "trip_intake": "TripRequestPatch",
    "itinerary_planner": "PlanNarrative",
    "revision_router": "RevisionIntent",
}


@dataclass
class ManagedPrompt:
    name: str
    version: int
    messages: list[dict[str, str]]
    config: dict[str, Any]
    metadata: dict[str, Any]
    source: str = "local"
    label: str = "production"
    fallback_reason: str | None = None
    remote: Any = field(default=None, repr=False)

    def compile(self, **variables: Any) -> list[dict[str, str]]:
        required = {v for msg in self.messages for v in VARIABLE.findall(msg["content"])}
        missing = required - variables.keys()
        if missing:
            raise ValueError(f"Missing prompt variables: {', '.join(sorted(missing))}")
        # Single substitution pass: braces in untrusted evidence are never re-evaluated.
        return [
            {"role": msg["role"], "content": VARIABLE.sub(
                lambda match: str(variables[match[1]]), msg["content"]
            )}
            for msg in self.messages
        ]

    def reference(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version, "label": self.label,
            "source": self.source, "fallback_reason": self.fallback_reason,
            "artifact_version": self.config.get("artifact_version", self.version),
        }


class PromptRegistry:
    def __init__(self, root: Path | None = None):
        self.root = root or settings.project_root / "prompts"

    def local(self, name: str, version: int | None = None, label: str = "production"):
        if name not in NAMES or label not in {"production", "staging", "prod-a", "prod-b"}:
            raise ValueError("Unknown prompt name or label")
        folder = self.root / name
        if version is None:
            version = int((folder / f"{label}.txt").read_text(encoding="utf-8").strip())
        if version < 1:
            raise ValueError("Prompt version must be positive")
        data = yaml.safe_load((folder / f"v{version}.yaml").read_text(encoding="utf-8"))
        if data["name"] != name or data["version"] != version:
            raise ValueError("Prompt artifact identity mismatch")
        if data["config"]["response_schema"] != CONTRACTS[name]:
            raise ValueError("Incompatible prompt schema")
        messages = data["messages"]
        required = {v for msg in messages for v in VARIABLE.findall(msg["content"])}
        if required != set(data["variables"]):
            raise ValueError("Declared prompt variables do not match template")
        return ManagedPrompt(name, version, messages, data["config"], data, label=label)

    def get(self, name: str, version: int | None = None, label: str | None = None):
        from app.services.observability import current_run, langfuse_client

        run = current_run()
        label = label or settings.prompt_label
        if name == "itinerary_planner" and settings.prompt_ab_enabled and version is None:
            identity = run.user_id if run else "offline"
            bucket = int(hashlib.sha256(
                f"planner-v2:{identity}".encode()
            ).hexdigest(), 16) % 100
            label = "prod-b" if bucket < settings.prompt_ab_treatment_pct else "prod-a"
        key = (name, version, label)
        if run and key in run.prompts:
            return run.prompts[key]
        # Explicit version numbers belong to the selected backend's version namespace.
        if settings.prompt_backend not in {"local", "langfuse"}:
            raise ValueError("PROMPT_BACKEND must be local or langfuse")
        if settings.prompt_backend == "local":
            prompt = self.local(name, version, label)
        else:
            fallback = self.local(name, label=label)
            try:
                client = langfuse_client(for_prompts=True)
                if client is None:
                    raise RuntimeError("LangfuseNotConfigured")
                remote = client.get_prompt(
                    name, type="chat", cache_ttl_seconds=settings.prompt_cache_ttl,
                    **({"version": version} if version is not None else {"label": label}),
                )
                if remote.config.get("response_schema") != CONTRACTS[name]:
                    raise ValueError("Incompatible remote prompt schema")
                if remote.config.get("variables") != fallback.metadata["variables"]:
                    raise ValueError("Incompatible remote prompt variables")
                actual = {v for msg in remote.prompt for v in VARIABLE.findall(msg["content"])}
                if actual != set(fallback.metadata["variables"]):
                    raise ValueError("Remote template variables differ from declared contract")
                if any(msg["role"] not in {"system", "user", "assistant"} for msg in remote.prompt):
                    raise ValueError("Unsupported remote message role")
                if not 1 <= int(remote.config["max_completion_tokens"]) <= 5000:
                    raise ValueError("Invalid remote output cap")
                prompt = ManagedPrompt(
                    name, remote.version, remote.prompt, remote.config, {},
                    source="langfuse", label=label, remote=remote,
                )
            except Exception as exc:
                prompt = fallback
                prompt.fallback_reason = type(exc).__name__
        if run:
            run.prompts[key] = prompt
        return prompt

    def render(self, name: str, *, version: int | None = None, label=None, **variables):
        return self.get(name, version, label).compile(**variables)


def registry() -> PromptRegistry:
    return PromptRegistry()
