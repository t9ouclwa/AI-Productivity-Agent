# Contributing

## Development setup

Follow [`docs/setup.md`](docs/setup.md), then install development dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

## Before submitting a change

```bash
python -m compileall -q bot.py gmail_client.py llm.py prompts.py vault.py scripts
python -m pytest
```

Keep runtime state and personal data out of fixtures. Tests should use temporary directories, synthetic chat text, and mocked external services.

## Design expectations

- Future actions must be backed by deterministic scheduler state, not only model text.
- Proactive messages must pass local quiet, cooldown, idle, and repetition gates.
- Gmail access must remain read-only unless a separately reviewed design changes the security boundary.
- New configuration should use environment variables or documented local files rather than hard-coded personal paths.
