# Contributing to Memory Map

Thanks for your interest in contributing!

## Getting started

1. Fork the repo and clone your fork
2. Create a virtual environment and install dependencies:

```bash
python -m venv venv
venv\Scripts\activate       # Windows
source venv/bin/activate    # Mac/Linux
pip install -r requirements.txt
```

3. Create a branch for your change:

```bash
git checkout -b your-feature-name
```

## Making changes

- All server logic lives in `server.py`
- Tests are in the `tests/` directory — run them with `pytest`
- Keep changes focused; one fix or feature per PR

## Submitting a pull request

1. Push your branch and open a PR against `main`
2. Describe what the change does and why
3. If it fixes a bug, include steps to reproduce the original issue

## Reporting issues

Open a GitHub issue with:
- What you expected to happen
- What actually happened
- Your OS, Python version, and Claude Code version
