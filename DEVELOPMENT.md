# Development

Run the local validation suite:

```bash
uv run --locked ruff check src tests scripts/corpus.py
uv run --locked pytest
scripts/smoke-wheel.sh
```

The smoke script builds the wheel, installs it into a fresh virtualenv, and
checks that the installed `vyupgrade` console script can run against a minimal
contract.

## Release

Publishing uses GitHub Actions and PyPI Trusted Publishing. Configure the PyPI
trusted publisher for repository `banteg/vyupgrade`, workflow `publish.yml`,
and environment `pypi`, then push a version tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```
