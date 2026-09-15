"""Read-only connection check. Does not invoke OpenAI or travel providers."""

from app.config import settings
from app.services.observability import langfuse_client


def main():
    print(f"Host: {settings.langfuse_base_url}")
    print(f"Tracing enabled: {settings.langfuse_enabled}")
    print(f"Prompt backend: {settings.prompt_backend}")
    client = langfuse_client(for_prompts=True)
    if client is None:
        raise SystemExit("Missing Langfuse configuration; put keys in .env, not source code.")
    try:
        if not client.auth_check():
            raise SystemExit("Authentication failed: check project keys and matching region.")
        print("Langfuse authentication OK.")
    except Exception as exc:
        raise SystemExit(f"Connection failed: {type(exc).__name__}") from None


if __name__ == "__main__":
    main()
