# Contributing

Thanks for your interest in WirePilot. Bug reports, ideas and pull requests are welcome.

## Before you start

- For anything bigger than a small fix, open an issue first so we can agree on the approach.
- Security problems go through [SECURITY.md](SECURITY.md), not a public issue.

## Set up

Follow the development section of [INSTALL.md](INSTALL.md). In short: `docker compose up -d`, `uv sync`,
`cd frontend && npm ci`.

## The workflow

1. Branch from `master`.
2. Write the tests first for new behaviour, then the code.
3. Keep the layering: router → service → repository / `VPNProvider` → SSH or database. Routers only do HTTP,
   schemas and the permission check. VPN-specific code lives behind `VPNProvider`.
4. If you touch a module, update the `CLAUDE.md` in its folder, and add a line to `CHANGELOG.md` for anything a
   user would notice.
5. Run every check below and make sure they pass.
6. Open a pull request and describe what changed and why.

## Checks

```bash
# backend
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest                      # needs `docker compose up -d`

# frontend
cd frontend
npm run typecheck && npm run lint && npm test && npm run build
```

If you changed the API, regenerate the frontend types (see [INSTALL.md](INSTALL.md)) and commit
`frontend/openapi.json` and `frontend/src/api/schema.d.ts` with it.

## Rules that matter

- **Secrets.** SSH credentials and VPN configs are stored encrypted only. Never log a password, key, token or
  config, and never put user input into an SSH command.
- **Migrations.** Every schema change is an Alembic migration that works both up and down.
- **Real servers.** Code and tests must not touch real servers. Use the fakes in `tests/fakes`.
- **Commits.** Small and focused, with a message that says why. Conventional prefixes (`feat:`, `fix:`,
  `chore:`, `docs:`) are used in this repository.

## License

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
