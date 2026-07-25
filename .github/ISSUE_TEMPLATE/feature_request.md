---
name: Feature request
about: Suggest something FreeRoute should do
title: "[feat] "
labels: enhancement
---

## The problem

<!-- What are you trying to do that FreeRoute doesn't let you do today? -->

## Proposed solution

<!-- Describe the change you'd like. A concrete example (config, CLI, API shape) helps
a lot. -->

## Alternatives considered

<!-- Workarounds you've tried, or other ways to solve the same problem. -->

## Scope check

FreeRoute is intentionally a single-binary local proxy for individual developers, not a
hosted multi-tenant gateway. Does this feature fit that scope?

- [ ] Works without external services (Redis, Postgres, etc.)
- [ ] Doesn't add multi-user / multi-tenant concerns
- [ ] Keeps config data-driven (SQLite + UI), not hardcoded
