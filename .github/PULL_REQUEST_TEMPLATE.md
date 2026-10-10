## What and why

<!-- What does this change, and why is it needed? Link the issue if there is one. -->

## Checklist

- [ ] Tests added or updated
- [ ] `ruff`, `mypy`, `pytest` pass (backend)
- [ ] `typecheck`, `lint`, `test`, `build` pass (frontend)
- [ ] If the API changed: `frontend/openapi.json` and `frontend/src/api/schema.d.ts` are regenerated
- [ ] If the schema changed: an Alembic migration that works up and down
- [ ] `CHANGELOG.md` has a line for anything a user would notice
- [ ] No secrets, keys or real server addresses in the code, logs or tests
