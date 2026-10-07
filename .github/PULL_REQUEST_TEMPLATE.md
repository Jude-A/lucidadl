<!-- PR title: use fix:/feat:/perf:/revert: for Python package changes;
     docs:/ci:/test:/build:/chore: otherwise (build: for Nix packaging).
     See https://github.com/Jude-A/lucidadl/blob/main/CONTRIBUTING.md#pull-requests -->

## What & why

<!-- Explain the purpose and resulting behavior. Include only project-relevant information.
     Link any related issue (e.g. Closes #12). -->

## How it was tested

- [ ] `python selftest.py` passes ("ALL OFFLINE TESTS PASSED")
- [ ] Manually tested the live flow if touched (e.g. `lucida setup` + a download, or `lucida ui`) — describe below:

<!-- Manual test notes -->

## Checklist

- [ ] Comments and user-facing strings are in English
- [ ] `README.md` / `CHANGELOG.md` updated if behavior or options changed
- [ ] No new silently-swallowed errors (failures stay visible to the user)
- [ ] Added/updated a `selftest.py` assertion if pure logic changed
