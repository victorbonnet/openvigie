# Fixture: Claude Code capabilities (synthetic)

Hand-written config files in the documented shapes, with fake secret values that tests check
never appear in the output. `{HOME}` in `.claude.json` is replaced with the test's home.

- `settings.json` + `settings.local.json`: permissions (the local file sets
  `bypassPermissions` and adds a deny rule), env with one secret-looking name, a Bash hook.
- `.claude.json`: account data (must be dropped), a user MCP server run with unpinned `npx`,
  a project MCP server with a pinned `uvx` package, and a project outside the home (ignored).
- `src/app/.mcp.json`: an HTTP MCP server with an auth header.
- `skills/pdf/SKILL.md`; `skills/no-skill-md/` is not a skill.
