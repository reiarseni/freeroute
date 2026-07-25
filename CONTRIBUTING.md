# Contributing to FreeRoute

Thanks for considering a contribution! This is a small, opinionated project — keeping the
scope tight is part of the design. A couple of minutes reading this will save both of us
time.

## Before you write code

**Open an issue first** for anything beyond a bug fix or docs improvement. FreeRoute is
deliberately scoped (single-binary local proxy for individual developers, not a
multi-tenant gateway), and features that make sense for hosted routers often don't fit
here. A quick discussion in an issue avoids wasted work.

Bug fixes and documentation improvements don't need an upstream issue — just send the PR.

## Development setup

```bash
git clone https://github.com/<your-fork>/freeroute.git
cd freeroute
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install ruff pytest pytest-asyncio   # dev tools
```

Run the proxy:

```bash
.venv/bin/python3 main.py
# UI at http://localhost:8787
```

Verify your changes:

```bash
.venv/bin/python3 -m pytest tests/ -q --ignore=tests/smoke_test.py
.venv/bin/ruff check services/ routers/ db.py main.py
```

The frontend is Svelte + Vite. To rebuild the SPA after editing `frontend/src/`:

```bash
cd frontend && npm install && npm run build   # output goes to ../static
```

## What we look for in PRs

- **Tests.** Add or update tests in `tests/` for any behaviour change in `services/` or
  `routers/`. The suite mocks upstreams, so no network is needed — keep it that way.
  Don't add tests that hit real providers.
- **No hardcoded providers/URLs/keys.** Configuration is data-driven from SQLite via the
  `providers`, `api_instances`, `deployments` and `router_settings` tables. New providers
  go in `SEED_PROVIDERS` in `db.py` only if they're broadly useful; otherwise leave them
  to the user to add via the UI.
- **Streaming stays streaming.** No endpoint should buffer the full response. When
  decoding bytes from a stream, use `codecs.getincrementaldecoder("utf-8")`, never
  `bytes.decode()` per chunk.
- **Explicit timeouts** on every outbound network call.
- **Commit messages** in Conventional Commits form (`feat:`, `fix:`, `docs:`, `refactor:`,
  `test:`, `chore:`). Spanish or English both fine.

## Commit sign-off / licensing

By contributing you agree your changes are licensed under Apache-2.0, consistent with the
rest of the project.

## Reporting security issues

Please **don't** open a public issue for security problems. Email the maintainer directly
or open a private security advisory on GitHub.
