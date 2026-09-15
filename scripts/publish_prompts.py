"""Validate/diff local artifacts; explicitly publish immutable versions and aliases."""

import argparse

import yaml

from app.prompts.registry import NAMES, PromptRegistry
from app.services.observability import langfuse_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Publish to configured Langfuse project"
    )
    parser.add_argument("--promote", action="store_true", help="Also sync local alias pointers")
    args = parser.parse_args()
    registry = PromptRegistry()
    client = langfuse_client(for_prompts=True) if args.apply else None
    if args.apply and client is None:
        raise SystemExit("Configure LANGFUSE_PUBLIC_KEY, SECRET_KEY and BASE_URL in .env first.")
    for name in sorted(NAMES):
        folder = registry.root / name
        for path in sorted(folder.glob("v*.yaml")):
            artifact = yaml.safe_load(path.read_text(encoding="utf-8"))
            prompt = registry.local(name, artifact["version"])
            config = {
                **prompt.config, "variables": artifact["variables"],
                "owner": artifact["owner"], "description": artifact["description"],
                "changelog": artifact["changelog"],
            }
            labels = [
                path.stem for path in folder.glob("*.txt")
                if int(path.read_text().strip()) == prompt.version
            ]
            print(f"{name} artifact v{prompt.version}: valid; aliases={labels}")
            if client is None:
                continue
            # Stable artifact label prevents duplicate versions on a second publish.
            artifact_label = f"artifact-v{prompt.version}"
            try:
                remote = client.get_prompt(name, label=artifact_label, type="chat",
                                           cache_ttl_seconds=0, max_retries=0)
            except Exception as exc:
                from langfuse.api import NotFoundError

                if not isinstance(exc, NotFoundError):
                    raise
                remote = None
            if remote is not None:
                if remote.prompt != prompt.messages or remote.config != config:
                    raise SystemExit(
                        f"{name} v{prompt.version} changed: create a NEW artifact version."
                    )
                print(f"  unchanged; Langfuse version={remote.version}")
            else:
                remote = client.create_prompt(
                    name=name, type="chat", prompt=prompt.messages, config=config,
                    labels=[artifact_label], commit_message=artifact["changelog"],
                )
                print(f"  published; Langfuse version={remote.version}")
            if args.promote:
                client.update_prompt(
                    name=name, version=remote.version, new_labels=[artifact_label, *labels]
                )
                print(f"  labels promoted: {labels}")
    if not args.apply:
        print("Dry run only. Publish: --apply; deploy local aliases: --apply --promote")


if __name__ == "__main__":
    main()
