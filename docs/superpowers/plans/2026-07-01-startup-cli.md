# Initial Setup CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build issue #118: an explicit initial setup CLI that prepares the root `.env` for Docker Compose local execution.

**Architecture:** Put the tested core in `backend/scripts/setup_env.py` and expose it through `scripts/setup_env.sh`. Keep the CLI responsible only for `.env` creation/update; reuse existing setup and run scripts for dependency installation and stack startup.

**Tech Stack:** Python 3.13, pytest, Bash wrapper, root `.env.example`.

---

## File Structure

- Create `backend/scripts/setup_env.py`: parse `.env`-style files, classify placeholder values, prompt for grouped settings, validate local formats, write normalized `.env`, create backups, mask summaries.
- Create `backend/tests/test_setup_env.py`: unit tests for parsing, rendering, validation, backup behavior, real-order confirmation, and wrapper contract.
- Create `scripts/setup_env.sh`: user-facing command that runs the Python script through the backend uv project.
- Modify `scripts/check_env.sh`: point missing root `.env` users to `bash scripts/setup_env.sh`.
- Modify `README.md`: prefer the setup command before dependency installation and stack startup.
- Keep `CONTEXT.md`: glossary for Local Operator and Initial Setup Flow.

## Task 1: File Preparation Core

- [ ] Write failing tests in `backend/tests/test_setup_env.py` for:
  - missing `.env` rendered from `.env.example`
  - existing real values preserved
  - custom keys preserved under a user-added section
  - backup file created before overwrite
- [ ] Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_setup_env.py -q
```

Expected: tests fail because `backend.scripts.setup_env` does not exist.

- [ ] Implement the minimal parsing, merge, backup, and render functions in `backend/scripts/setup_env.py`.
- [ ] Re-run the focused test command and confirm it passes.
- [ ] Commit:

```bash
git add CONTEXT.md docs/superpowers/specs/2026-07-01-startup-cli-design.md docs/superpowers/plans/2026-07-01-startup-cli.md backend/scripts/setup_env.py backend/tests/test_setup_env.py
git commit -m "feat: 초기 설정 env 생성 기반 추가"
```

## Task 2: Validation and Prompt Decisions

- [ ] Add failing tests for:
  - requiring one non-placeholder LLM API key
  - URL validation
  - `KIS_ORDER_ENV` validation
  - real-order enablement requiring the exact confirmation phrase
  - secret masking in summaries
- [ ] Run the focused test command and confirm the new tests fail for missing behavior.
- [ ] Implement the minimal validation, masking, and prompt-decision helpers.
- [ ] Re-run the focused test command and confirm it passes.
- [ ] Commit:

```bash
git add backend/scripts/setup_env.py backend/tests/test_setup_env.py
git commit -m "feat: 초기 설정 값 검증 추가"
```

## Task 3: Interactive CLI and Wrapper

- [ ] Add failing tests that simulate user input for grouped prompts and verify the final env mapping.
- [ ] Add a test that `scripts/setup_env.sh` calls `backend/scripts/setup_env.py` through `uv run --project backend`.
- [ ] Run the focused test command and confirm the new tests fail for missing behavior or wrapper.
- [ ] Implement the interactive `main()` flow and create `scripts/setup_env.sh`.
- [ ] Re-run the focused test command and confirm it passes.
- [ ] Commit:

```bash
git add backend/scripts/setup_env.py backend/tests/test_setup_env.py scripts/setup_env.sh
git commit -m "feat: 초기 설정 CLI 추가"
```

## Task 4: Documentation and Guidance

- [ ] Add failing or text-check tests for `scripts/check_env.sh` guidance when feasible, otherwise verify through shell output.
- [ ] Update `README.md` and `scripts/check_env.sh` to guide Local Operators to `bash scripts/setup_env.sh`.
- [ ] Run focused backend tests and `bash scripts/check_env.sh` in an environment-safe way.
- [ ] Commit:

```bash
git add README.md scripts/check_env.sh backend/tests/test_setup_env.py
git commit -m "docs: 초기 설정 실행 안내 추가"
```

## Final Verification

- [ ] Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_setup_env.py -q
```

- [ ] Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_config.py backend/tests/test_setup_env.py -q
```

- [ ] Run:

```bash
bash scripts/setup_env.sh --help
```

- [ ] Inspect:

```bash
git log main..HEAD --oneline
git diff main..HEAD --stat
```

- [ ] Push the branch and create a PR against `main` using `.github/PULL_REQUEST_TEMPLATE.md`.
