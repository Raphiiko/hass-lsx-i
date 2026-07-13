# Release process

The initial version is `0.1.0b1`. It is a prerelease and must not be described as
hardware-verified or stable.

## Versioning

Use semantic versioning:

- `0.1.0bN` while fake-server/integration coverage is complete but real hardware
  behavior is still being validated;
- patch prereleases for compatible bug fixes;
- minor versions for new verified features;
- a stable `1.0.0` only after the supported hardware/behavior contract is proven
  and documented.

Keep `pyproject.toml` and `custom_components/kef_lsx/manifest.json` versions in
sync. HACS derives an installed release version from a GitHub release, not a bare
tag.

## Required automated checks

From a clean Python 3.14.2+ environment:

```console
uv sync --python 3.14.2 --extra test --extra dev
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
uv run --frozen pytest
```

CI must also pass:

- the deterministic TCP suite on Linux and Windows;
- Home Assistant config flow/setup/service/unload/diagnostics tests;
- HACS integration validation with no ignored checks;
- hassfest validation;
- import/package validation against Home Assistant 2026.7.2;
- a sensitive-data scan and clean working-tree check.

Record exact commands and actual output in the PR/release notes. Do not report a
check as passing if it was unavailable or skipped.

## Hardware gate before stable release

With explicit production-system approval, stop the built-in `kef` owner, back up
configuration, and test on a real first-generation LSX:

- direct `Opt` wake with no pre-read;
- direct wake immediately after a transient/failed poll;
- off, every advertised source, absolute and step volume, maximum-volume clamp,
  mute/unmute;
- availability hysteresis and immediate recovery;
- repeated polling without concurrent connections or control-server degradation;
- unload/reload and rollback;
- Home Assistant logs/history and both known PC automations.

Document firmware, observations, and any unsupported behavior without storing
private addresses or configuration. Play/pause, track navigation, DSP, LS50W, and
KEF Connect devices remain unsupported until separately validated.

## Publishing

1. Update changelog/release notes and all tested-versus-unverified statements.
2. Confirm third-party notices and licence files still accompany the component.
3. Merge only after required reviews and CI pass.
4. Prepare a GitHub **prerelease** for beta versions. A draft is appropriate
   before hardware validation.
5. Do not mark a release stable, create a stable tag, or claim hardware support
   before the hardware gate is complete.
6. Include migration and immediate rollback links in every prerelease note.
