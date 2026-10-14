# Releasing

Releases are built and published by `.github/workflows/release.yml`.

## One-time setup (maintainer)

1. On [PyPI](https://pypi.org/manage/account/publishing/) and
   [TestPyPI](https://test.pypi.org/manage/account/publishing/), add a **trusted publisher**
   for this repository: workflow `release.yml`, environments `pypi` and `testpypi`.
2. In GitHub → Settings → Environments, create `pypi` and `testpypi`, and require a reviewer
   (you) on `pypi`.

No API token is stored anywhere: the workflow gets a short-lived one through OIDC.

## Dry run

Actions → release → *Run workflow*. It builds the wheel and sdist, checks the wheel's
contents, installs it with `pipx` on Linux arm64, macOS arm64 and Linux x86_64, runs
`openvigie --version` and `openvigie scan --json`, then publishes to **TestPyPI**.

## Release

1. Set `version` in `pyproject.toml`, and turn `## [X.Y.Z] - unreleased` in `CHANGELOG.md`
   into today's date. Merge that.
2. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The workflow checks that the tag, `pyproject.toml` and `CHANGELOG.md` agree, builds,
   runs the smoke tests, and waits for your approval of the `pypi` environment before
   publishing.
