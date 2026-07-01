# Initial Setup CLI Design

## Goal

Issue #118 adds an explicit initial setup command for a Local Operator who wants to run Fin-Us locally without manually editing the root `.env` file.

## Scope

The first version prepares the root `.env` file only. It does not install dependencies, build Docker images, run Docker Compose, or call external APIs to validate credentials. After setup, it prints the existing follow-up commands:

```bash
bash scripts/setup_deps.sh
bash scripts/run_stack.sh
```

## User Flow

The Local Operator runs:

```bash
bash scripts/setup_env.sh
```

The wrapper starts a Python script in the backend project. The script reads `.env.example`, reads an existing `.env` when present, asks only for missing or placeholder values by default, validates entered values, creates a timestamped backup before overwriting an existing `.env`, and writes a normalized `.env`.

## Configuration Groups

The prompts are grouped by capability rather than raw file order:

1. Basic execution: `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`
2. Market data: `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`, `DART_API_KEY`
3. Account and trading: `KIS_API_KEY`, `KIS_API_SECRET`, `KIS_ACCOUNT_NO`, `KIS_URL`, `KIS_ORDER_ENV`, `KIS_REAL_ORDER_ENABLED`
4. Alerts and visualization: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `VISUALIZATION_URL`
5. Local model: `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_API_KEY`

## Safety Rules

- Existing real values are preserved unless the operator chooses to change them.
- Secret values are masked in summaries.
- `.env.example` comments and key order define the normalized output.
- Keys not present in `.env.example` are preserved under a user-added settings section.
- Real-account order execution is disabled by default.
- `KIS_REAL_ORDER_ENABLED=true` is saved only after the operator types the required Korean confirmation phrase.

## Validation

The first version performs local validation only:

- At least one non-placeholder LLM API key is required.
- `KIS_ORDER_ENV` accepts only `demo` or `real`.
- Boolean values accept explicit true/false style values.
- URL fields must start with `http://` or `https://` when non-empty.
- `KIS_ACCOUNT_NO` must be non-empty when account setup is enabled.

External provider calls are out of scope for this issue and can be added later as a separate diagnostics mode.

## Success Criteria

- A missing `.env` can be created from `.env.example`.
- An existing `.env` keeps real values and user-added keys.
- Placeholder and empty values are prompted.
- A backup is created before overwriting an existing `.env`.
- Real-account order enablement requires the explicit confirmation phrase.
- Focused tests cover the file preparation, validation, masking, and wrapper contract.
- README and environment-check guidance point Local Operators to the setup command.
