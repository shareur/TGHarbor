# 302 — Release security

[简体中文](../zh-CN/3-operations/302-release-security.md)

Treat Telegram Bot tokens, API hashes, phone numbers, session files, local
configuration, databases, logs, downloaded content, message summaries, and private
deployment identifiers as non-public data.

## Before the first public release

1. Inspect the candidate tracked paths, working tree, index, ignored files, every local
   and remote Git ref, and the candidate `git archive` with a dedicated secret scanner.
2. Compare known local credential values against Git objects without printing the
   values. A match requires credential rotation even if the file was later deleted.
3. Confirm remote URLs contain no embedded credentials.
4. From the exact candidate commit, build `git archive`, the sdist, and the wheel.
   Confirm the candidate archive contains only intended tracked paths, then unpack and
   scan each payload against the artifact contract below.
5. Review the Docker build-context allowlist, then build the container and inspect its
   final filesystem, configuration/history, and every layer for material outside the
   published project tree, untracked material, configuration, sessions, databases,
   downloaded content, and known credential values.
6. Review generated Compose output without publishing expanded environment values.
7. Validate public documentation links against the candidate archive and fail if a link
   targets material outside the published project tree or untracked material. Run Ruff,
   formatting, tests, dependency audit, documentation-link validation, and Gitleaks.
   Resolve findings or document narrowly scoped false-positive allowlists.
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
   known bad names. Reject `.env`, real or example config and Compose files, material
   outside the published project tree or untracked material, tests, full documentation,
   sessions, databases, logs, download data, duplicate members, absolute or traversing
   paths, and symbolic- or hard-link members.
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

The portable Compose example pulls the published Docker Hub image
`shareur/tgharbor:0.2.1`; it does not build locally. The `0.2.1` publication is recorded
in the [Docker Hub repository](https://hub.docker.com/r/shareur/tgharbor) with aliases
`0.2.1`, `0.2`, `latest`, and `sha-13c5bc7645b9` for `linux/amd64` and `linux/arm64`.
The published `v0.2.1` release record identifies the multi-platform image index as
`sha256:b40996ef4938f8545e5eaee86c91fc57b3d27f9b087353d02a97ae7548c5f560`, built
from source revision `13c5bc7645b9ba27ea9837be3697fa1388ffb44d` and source
`https://github.com/shareur/TGHarbor`. Tags remain mutable aliases and do not replace a
recorded image digest. Stable aliases are published only after the exact tag-push `CI`
run succeeds, with Buildx provenance and an SBOM recorded by the release workflow.

The Dockerfile provides safe local defaults: version `0.2.1`, revision `uncommitted`,
source `local`, and the Unix epoch created timestamp.
Those revision, source, and created defaults identify a non-release build and are
forbidden for a release image.

Verify a pulled publication by comparing its resolved digest and OCI labels with the
release record:

```bash
docker image inspect shareur/tgharbor:0.2.1 \
  --format '{{index .RepoDigests 0}}'
docker image inspect shareur/tgharbor:0.2.1 \
  --format '{{json .Config.Labels}}'
```

The `RepoDigests` entry must resolve to the recorded index digest. Require
`org.opencontainers.image.version=0.2.1`,
`org.opencontainers.image.revision=13c5bc7645b9ba27ea9837be3697fa1388ffb44d`, and
`org.opencontainers.image.source=https://github.com/shareur/TGHarbor`. A tag alias
alone is not provenance.

Build a release image from the clean immutable candidate. Set
`TGHARBOR_IMAGE_SOURCE` to the verified project source URL without hardcoding an
alternate source URL, and set `TGHARBOR_IMAGE_CREATED` once to the candidate build time
in RFC 3339 UTC form. The full candidate commit is mandatory:

```bash
CANDIDATE_COMMIT="$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"
test -n "$TGHARBOR_IMAGE_SOURCE"
test -n "$TGHARBOR_IMAGE_CREATED"
docker build --pull \
  --build-arg TGHARBOR_IMAGE_VERSION=0.2.1 \
  --build-arg TGHARBOR_IMAGE_REVISION="$CANDIDATE_COMMIT" \
  --build-arg TGHARBOR_IMAGE_SOURCE="$TGHARBOR_IMAGE_SOURCE" \
  --build-arg TGHARBOR_IMAGE_CREATED="$TGHARBOR_IMAGE_CREATED" \
  --tag tgharbor:0.2.1 .
```

Inspect the resulting OCI labels and require title `TGHarbor`, version `0.2.1`, the
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

## Source-only 0.2.2 maintenance release

`v0.2.2` is a formatting-only source release. It applies Ruff formatting to the release
validation scripts and release metadata tests so the repository-wide formatting check
passes on the published source snapshot, and it aligns the project and runtime version
metadata with `0.2.2`. It changes no runtime behavior, configuration, interface, or
documented behavior.

No container image is published for `0.2.2`. The recorded `0.2.1` image and its `0.2`,
`latest`, and `sha-13c5bc7645b9` aliases remain the current image release, the index
digest above stays unchanged, and Compose instructions and image examples continue to
reference the published `0.2.1` image. Do not describe a `0.2.2` image as published, do
not retag an existing image as `0.2.2`, and do not treat the `v0.2.2` tag as container
provenance.

The Docker release workflow re-validates the remote tag, the checkout version metadata,
and the absence of every target Docker Hub tag before registry login. `0.2` and `latest`
are already occupied by the recorded `0.2.1` image, so a `v0.2.2` tag run refuses to
overwrite or move them and fails pre-push validation instead. That refusal is the
expected outcome for a source-only release: it is neither a successful image publication
nor a failure to repair, and the immutable `0.2.1` image tags must not be moved to make
it pass.

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

CI must not upload a broad workspace, build-context, or history archive as an artifact.
Publish only the exact release artifacts identified and verified for the candidate.

Never paste secrets, session files, message contents, private links, database rows,
or raw diagnostic objects into issues or pull requests. Security reports should use
the confidential reporting process in `SECURITY.md` once a maintainer contact is
published.

## Release record

Record scanner versions and their pinned archive checksums, ref count, object count,
image digest, candidate commit, artifact names, sizes, SHA-256 hashes, versions,
commands, outcomes, false-positive decisions, and any rotations in the release record.
Do not record the credential values, private hostnames, account IDs, or user-content
paths.
