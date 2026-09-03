# Contributing to lucidadl

Thanks for your interest! Bug reports, feature ideas, and pull requests are all welcome.

> **Scope reminder.** lucidadl is a personal-use tool in the spirit of `yt-dlp`. Please
> keep contributions aligned with that purpose and with the disclaimer in the
> [README](README.md).

## Development setup

```bash
git clone https://github.com/Jude-A/lucidadl
cd lucidadl
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Unix:     source .venv/bin/activate
pip install -e ".[dev]"      # editable install + build/twine
python -m playwright install chromium  # one-time for the live browser flow
```

`pip install -e .` makes the `lucida` / `lucidadl` commands point at your working copy,
so edits take effect immediately.

## Running the tests

The self-tests are **offline** — no browser, no network — and must pass before a PR:

```bash
python selftest.py        # prints "ALL OFFLINE TESTS PASSED"
```

CI runs the same self-tests on Linux and Windows for Python 3.10–3.12, plus a packaging
check (`python -m build` + `twine check`).

For playlist changes, also run `--dry-run` with the affected public source; use `--check`
when the matching path changed. Neither command writes audio files.

### What can't be tested automatically

The live flow — solving Cloudflare, downloading, and the interactive `ui` — needs a
real desktop session and a terminal, so it can't run in CI. If your change touches that
path, please test it manually and say so in the PR (e.g. `lucida setup` then
`lucida track "Artist - Title"`, or `lucida ui`).

## Style

- Match the surrounding code: same naming, comment density, and idioms. Comments and
  user-facing strings are in English.
- Keep user-facing failures **visible** — avoid swallowing errors into silent
  degradation (see the warnings around mutagen / `state.json` / `config.json`).
- Add or update a `selftest.py` assertion when you change pure logic (parsing, matching,
  organization, dedup, transcode argument building).

## Pull requests

1. Branch off `main`.
2. Keep the change focused; update `README.md` / `CHANGELOG.md` (under `## [Unreleased]`)
   when behavior or options change.
3. Make sure `python selftest.py` passes.
4. Open the PR with a clear description of what changed and how you tested it.
