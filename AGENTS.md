# Repository Guidance

This file keeps the default working assumptions for maintainers and automated agents.

## Safety First

- Use `data/testing.db` for test runs by default.
- Do not run multiple entry points at the same time if they rebuild `testing.db`.
- Keep secrets, tokens, and private environment values out of version control.

## Change Priorities

1. Stability
2. Test coverage
3. Data compatibility

## When Changing Behavior

- Prefer small, targeted fixes over broad rewrites.
- Reuse existing business logic before introducing new paths.
- Add or update regression coverage when behavior changes.
- If a change may affect data structures or persisted data, explain the impact clearly before expanding the scope.

## Documentation

- Keep the top-level README public-facing.
- Keep operational notes and private workflows out of the public release.
