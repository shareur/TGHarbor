# 302 — Release security

[简体中文](../zh-CN/3-operations/302-release-security.md)

Treat Telegram Bot tokens, API hashes, phone numbers, session files, local
configuration, databases, logs, downloaded content, message summaries, and private
deployment identifiers as non-public data.

## Before the first public release

1. Inspect the working tree, index, ignored files, every local and remote Git ref,
   and all reachable blobs with a dedicated secret scanner.
2. Compare known local credential values against Git objects without printing the
   values. A match requires credential rotation even if the file was later deleted.
3. Confirm remote URLs contain no embedded credentials.
4. From the exact candidate commit, build `git archive`, the sdist, and the wheel;
   unpack and scan each payload against the artifact contract below.
5. Build the container and inspect its file list and layers for configuration,
   sessions, databases, downloaded content, and known credential values.
6. Review generated Compose output without publishing expanded environment values.
7. Run Ruff, formatting, tests, dependency audit, documentation-link validation, and
   Gitleaks. Resolve findings or document narrowly scoped false-positive allowlists.
8. Enable repository secret scanning, push protection, dependency alerts, and branch
   protection before accepting external contributions.

## Distribution artifacts

Package selection is a two-layer security boundary. The Hatch selectors in
`pyproject.toml` narrow runtime-package inputs to Python source paths under
`src/tgharbor/**/*.py`, but selectors do not know whether a Python module has been
reviewed. The independent exact artifact-member gate enforces equality with the
explicit reviewed runtime manifest and rejects an unknown Python module; both layers
are required. Arbitrary text, JSON, YAML, archives, and other package data are not
runtime payload. The sdist additionally allows only `pyproject.toml`, `README.md`,
`LICENSE`, `CHANGELOG.md`, the public `hatch_build.py` build helper, and
builder-generated `PKG-INFO`. The helper removes Hatchling's forced VCS ignore
metadata and is itself packaged so an unpacked sdist can rebuild both distributions.
The wheel maps `src/tgharbor/**` to `tgharbor/**` and permits only the explicit
reviewed Python manifest plus `.dist-info` metadata and licenses.

Do not disable required Hatch build hooks for a release build. A modified environment
can disable or alter a hook, so backend configuration alone is not the enforcement
point: the exact artifact-member gate must reject `.gitignore`, an unknown Python
module, duplicate members, or any other extra path. Every intentional runtime module
addition requires review and an explicit manifest-test update.

For the exact candidate commit:

1. Build `git archive`, the sdist, and the wheel into a temporary directory. Do not
   build from a dirty tree or from a different commit.
2. Unpack each artifact and assert its complete member list, not only the absence of
   known bad names. Reject `.env`, real or example config and Compose files,
   `AGENTS.md`, handoff files, `docs-local/`, `archive/`, `reference/`, tests, full
   documentation, sessions, databases, logs, download data, duplicate members,
   absolute or traversing paths, and symbolic- or hard-link members.
3. Validate distribution name, version, `Requires-Python`, and that the wheel installs
   and imports the expected version in a fresh temporary environment.
4. Record artifact filenames, sizes, SHA-256 hashes, and versions in the release
   record. Rebuild from the same immutable candidate and compare hashes when anyone
   needs a fresh artifact.
5. Keep `tests/test_distribution_artifacts.py` passing; it builds direct artifacts,
   rebuilds both artifact types from the unpacked sdist, installs both wheel paths,
   and exercises neutral-file, unknown-Python-file, local-root-payload, disabled-hook,
   duplicate-member, path, and link adversarial cases.

Do not describe an artifact or gate as passed until the command has actually run
against the exact candidate. Git ignore and Docker ignore files are not package
security controls.

## Container identity and provenance

The portable Compose example uses the exact release tag `tgharbor:0.2.0`. Tags remain
mutable and do not replace a recorded image digest. The Dockerfile provides safe local
defaults: version `0.2.0`, revision `uncommitted`, source `local`, and the Unix epoch
created timestamp. Those revision, source, and created defaults identify a non-release
build and are forbidden for a release image.

Build the release image from the clean immutable candidate. Set
`TGHARBOR_IMAGE_SOURCE` to the verified canonical public source URL without hardcoding
a non-public source URL, and set `TGHARBOR_IMAGE_CREATED` once to the candidate build
time in RFC 3339 UTC form. The full candidate commit is mandatory:

```bash
CANDIDATE_COMMIT="$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"
test -n "$TGHARBOR_IMAGE_SOURCE"
test -n "$TGHARBOR_IMAGE_CREATED"
docker build --pull \
  --build-arg TGHARBOR_IMAGE_VERSION=0.2.0 \
  --build-arg TGHARBOR_IMAGE_REVISION="$CANDIDATE_COMMIT" \
  --build-arg TGHARBOR_IMAGE_SOURCE="$TGHARBOR_IMAGE_SOURCE" \
  --build-arg TGHARBOR_IMAGE_CREATED="$TGHARBOR_IMAGE_CREATED" \
  --tag tgharbor:0.2.0 .
```

Inspect the resulting OCI labels and require title `TGHarbor`, version `0.2.0`, the
exact candidate commit as revision, the verified public source, license `Apache-2.0`,
and the supplied created timestamp. Record the image digest with the label values,
then run the final-filesystem, configuration/history, and per-layer scans. Rebuild the
release image from the immutable candidate rather than relying on the mutable tag. An
actual container build remains unpassed when Docker is unavailable; static Dockerfile
tests do not replace this candidate gate. Completing this source/container release
gate does not authorize a production rollout, which requires its own recovery and
deployment acceptance.

If a credential or session may have been exposed, revoke or rotate it first. History
rewriting does not make a still-valid credential safe. Coordinate history rewriting
with all repository users and rescan every ref afterward.

## Continuous controls

CI has read-only repository permissions, checks out complete history, pins Actions to
immutable commit SHAs, runs dependency auditing, and scans secrets. Dependabot opens
bounded weekly updates for Python, Actions, and Docker dependencies.

The secret scan installs the pinned Gitleaks CLI release `8.30.1` into
`$RUNNER_TEMP` instead of running a marketplace Action, verifies the official
`linux_x64` archive SHA-256
`551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb` before
extraction, and passes no token or license secret to the scanner. It scans the
complete reachable history of the checked-out repository rather than a push,
pull-request, or tag event range, so an initial branch push, a normal push, a pull
request, and a tag push all use the same full-history gate. It refuses to report a
clean result when no commit is checked out or when the checkout is shallow. Findings
are printed with 100% redaction, no banner or color, and an exit code of `2`; a finding
fails the job as a secret incident, while any other non-zero exit is reported as an
operational scanner failure and must never be described as a clean scan.

Never paste secrets, session files, message contents, private links, database rows,
or raw diagnostic objects into issues or pull requests. Security reports should use
the private process in `SECURITY.md` once a maintainer contact is published.

## Release record

Record scanner versions and their pinned archive checksums, ref count, object count,
image digest, candidate commit,
artifact names, sizes, SHA-256 hashes, versions, commands, outcomes, false-positive
decisions, and any rotations in the local handoff. Do not record the credential
values, private hostnames, account IDs, or user-content paths.
