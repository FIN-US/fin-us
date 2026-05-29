## Summary

<!-- 이 PR이 해결하는 이슈·목적을 2~3문장으로 -->

## Test plan

- [ ] `uv run --project backend pytest backend/tests/`
- [ ] `uv run --project finus_nat pytest finus_nat/tests/`
- [ ] `cd mcp-trading && npm test`
- [ ] `cd frontend-react && npx tsc --noEmit`

## Merge note

머지 시 **Squash and merge** 를 권장합니다. 중간 머지·revert 커밋이 섞이면 `main` 히스토리가 읽기 어려워집니다.
