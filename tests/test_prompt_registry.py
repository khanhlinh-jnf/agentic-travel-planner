import asyncio
from types import SimpleNamespace

import pytest

from app.config import settings
from app.prompts.registry import NAMES, PromptRegistry
from app.services import observability as obs


def test_all_artifacts_render_and_missing_variables_fail():
    registry = PromptRegistry()
    for name in NAMES:
        for path in (registry.root / name).glob("v*.yaml"):
            prompt = registry.local(name, int(path.stem[1:]))
            values = {key: "test {{untrusted}}" for key in prompt.metadata["variables"]}
            messages = prompt.compile(**values)
            assert any("{{untrusted}}" in msg["content"] for msg in messages)
            values.pop(next(iter(values)))
            with pytest.raises(ValueError, match="Missing prompt variables"):
                prompt.compile(**values)
    with pytest.raises(ValueError):
        registry.local("../../.env")


@pytest.mark.asyncio
async def test_remote_outage_fallback_and_per_run_pinning(monkeypatch):
    monkeypatch.setattr(settings, "prompt_backend", "langfuse")
    calls = []

    def failed(*args, **kwargs):
        calls.append(1)
        raise TimeoutError()

    monkeypatch.setattr(obs, "langfuse_client",
                        lambda **kwargs: SimpleNamespace(get_prompt=failed)
                        if kwargs.get("for_prompts") else None)
    with obs.run_scope("fallback-test", "user", "start") as run:
        first = PromptRegistry().get("trip_intake")
        second = PromptRegistry().get("trip_intake")
        assert first is second
        assert first.source == "local"
        assert first.fallback_reason == "TimeoutError"
        assert len(calls) == 1
        assert run.summary()["prompts"][0]["version"] == 1


@pytest.mark.asyncio
async def test_sticky_ab_assignment(monkeypatch):
    monkeypatch.setattr(settings, "prompt_ab_enabled", True)
    monkeypatch.setattr(settings, "prompt_ab_treatment_pct", 50)
    async def assigned(user):
        with obs.run_scope(f"ab-{user}", user, "start"):
            return PromptRegistry().get("itinerary_planner").version
    first = [await assigned(f"user-{i}") for i in range(20)]
    second = [await assigned(f"user-{i}") for i in range(20)]
    assert first == second
    assert set(first) == {1, 2}
    await asyncio.sleep(0)


def test_publish_is_idempotent_and_rejects_mutated_artifact(monkeypatch):
    from langfuse.api import NotFoundError

    from scripts import publish_prompts

    class FakeCloud:
        def __init__(self):
            self.prompts = []

        def get_prompt(self, name, *, label, **kwargs):
            for prompt in self.prompts:
                if prompt.name == name and label in prompt.labels:
                    return prompt
            raise NotFoundError(body={})

        def create_prompt(self, *, name, prompt, config, labels, **kwargs):
            result = SimpleNamespace(name=name, prompt=prompt, config=config,
                                     labels=labels, version=len(self.prompts) + 1)
            self.prompts.append(result)
            return result

        def update_prompt(self, *, name, version, new_labels):
            for prompt in self.prompts:
                if prompt.name == name and prompt.version == version:
                    prompt.labels = new_labels

    cloud = FakeCloud()
    monkeypatch.setattr(publish_prompts, "langfuse_client", lambda **kwargs: cloud)
    monkeypatch.setattr("sys.argv", ["publish_prompts", "--apply", "--promote"])
    publish_prompts.main()
    publish_prompts.main()
    assert len(cloud.prompts) == 4
    assert cloud.get_prompt("itinerary_planner", label="production").config["artifact_version"] == 1
    assert cloud.get_prompt("itinerary_planner", label="staging").config["artifact_version"] == 2
    cloud.prompts[0].config = {"changed": True}
    with pytest.raises(SystemExit, match="NEW artifact version"):
        publish_prompts.main()


def test_offline_comparison_handles_windows_console_encoding(monkeypatch):
    import subprocess
    import sys

    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    result = subprocess.run(
        [sys.executable, "-m", "scripts.compare_prompts"], cwd=settings.project_root,
        capture_output=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "Offline diff only: no LLM calls" in result.stdout
