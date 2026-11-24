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

The portable Compose example uses the exact release tag `tgharbor:0.2.3`. Container
tags are aliases and do not replace a recorded image digest. The release workflow
treats the exact version and commit tags as immutable identities and only the minor
and `latest` tags as forward-only mutable aliases. The Dockerfile provides safe local
defaults: version `0.2.3`, revision `uncommitted`, source `local`, and the Unix epoch
created timestamp. Those revision, source, and created defaults identify a non-release
build and are forbidden for a release image.

### Registry tag policy

The exact release version (`0.2.3`) and the exact commit identity
(`sha-<first 12 characters of the commit SHA>`) are immutable. The workflow fails
closed before registry login, build, or push if either target tag already exists.
Never overwrite or reuse an immutable version or `sha-*` tag.

The derived minor tag (`0.2`) and `latest` are mutable aliases. Each may be absent. If
present, an alias may move only forward from a verified older release in the same
repository. Before registry login, the workflow resolves the complete paginated Docker
Hub tag catalog and each alias digest. For every present alias it requires that:

- the alias digest is shared by at least one published stable `MAJOR.MINOR.PATCH` tag
  lower than the candidate version and by at least one immutable `sha-*` tag;
- the minor alias points to the highest published stable version in the same major and
  minor series, and the candidate patch is higher;
- `latest` points to the highest published stable version overall, and the candidate
  version is higher; and
- aliases agree on one digest when more than one is present.

The candidate version must also be higher than the highest published stable semantic
version across the repository, even when both mutable aliases are absent. This global
monotonic rule prevents an older immutable version from being backfilled after aliases
have been removed.

A shared digest for a stable tag and a `sha-*` tag is only a naming convention. Before
login, the workflow therefore anonymously resolves each present alias through the
registry API and validates its actual OCI identity. Both supported platform images
(`linux/amd64` and `linux/arm64`) must consistently report the canonical project source,
the version equal to the highest stable tag sharing the index digest, and one full
40-character revision whose first 12 characters exactly match a `sha-*` tag sharing that
digest. Missing or duplicate platforms, divergent platform labels, missing or malformed
labels or media types, platform-manifest or config digest/body mismatches, oversized
responses, unsupported index shapes, and registry, authentication, or network errors
fail closed. A fabricated stable plus `sha-*` naming pair is not accepted without the
matching image identity.

If several stable tags share the alias digest, the highest matching version is used,
but the alias must still equal the highest published stable version for its scope. The
workflow rejects an alias that points to a digest with no stable identity, a digest
with no `sha-*` identity, an equal or newer version, a non-highest or rollback version,
a wrong major/minor target for the minor alias, divergent aliases, an unknown or
malformed tag, duplicate metadata, truncated pagination, or inconsistent counts. Any
Docker Hub API or registry error is fatal, and the pre-login checks send no
credentials. The alias policy never writes a stable version or `sha-*` tag.

The pre-login catalog read is not an atomic compare-and-swap. A concurrent registry
change after validation cannot be prevented by the workflow. The workflow rechecks
the aliases immediately before registry login, and post-push verification fails closed
unless all four target tags (`0.2.3`, `0.2`, `latest`, and `sha-<first 12>`) resolve
to the one new OCI index digest with the required OCI labels, provenance, SBOM, and
runtime smoke result. A failed post-push check blocks the release and requires
investigation or a new patch release; it never authorizes moving an immutable tag or
a mutable alias backward.

### Verification-only recovery for an already published image

If publication pushed an immutable image but post-push verification failed, the
registry may already hold the index digest and all four tags. Correct the verifier first,
then verify that existing image without publishing again. Do not move, delete, or reuse
an immutable tag, and do not re-run a workflow that would push a second image.

The tracked `Docker image verification` workflow
(`.github/workflows/docker-verify.yml`) is manually dispatchable for a stable tag. It has
only `contents: read` repository permission, no Docker Hub secrets or environment, and no
registry login. It passes the ephemeral read-only GitHub token to the two metadata steps
only, through the environment, so tag and default-branch resolution works against a
private repository without anonymous REST failures. The token is used only for GitHub
metadata reads through the canonical GitHub REST API and is never sent to Docker Hub, a
registry, a Git remote, command arguments, summaries, or the image runtime. The workflow
resolves the annotated tag and peeled commit, proves reachability from the protected
default branch, checks out the verifier source from that exact default-branch commit, and
checks out the exact tagged commit for version metadata. It then discovers the immutable
index digest anonymously from the version tag and requires the derived minor, `latest`,
and `sha-*` aliases to converge to the same digest. An optional expected digest input pins
the exact existing identity and fails closed on a mismatch.

After discovery, the workflow runs only existing-image verification: OCI labels,
structural provenance, SBOM, layer and static checks, and the hardened Docker runtime
smoke test. It never logs in, builds, pushes, retags, deletes, or mutates a registry or
GitHub ref. Concurrency prevents duplicate verification but does not cancel a running
check. A successful verification run establishes only that the already published image
matches the corrected checks. It does not retroactively turn a failed publication into a
successful release, replace a production restore-point backup, or authorize deployment.
The `verify-existing` mode in the release contract provides the same discovery and
verification path locally. It reads GitHub metadata anonymously unless a read-only
GitHub token is present in the environment, and it always discovers and verifies the
registry image anonymously with no registry credentials.

Build the release image from the clean immutable candidate. The workflow build context
is the already validated full commit SHA, not the mutable release tag ref; tag
reachability and the exact CI run are still verified before the build. Set
`TGHARBOR_IMAGE_SOURCE` to the verified canonical public source URL without hardcoding
a non-public source URL, and set `TGHARBOR_IMAGE_CREATED` once to the candidate build
time in RFC 3339 UTC form. The full candidate commit is mandatory:

```bash
CANDIDATE_COMMIT="$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"
test -n "$TGHARBOR_IMAGE_SOURCE"
test -n "$TGHARBOR_IMAGE_CREATED"
docker build --pull \
  --build-arg TGHARBOR_IMAGE_VERSION=0.2.3 \
  --build-arg TGHARBOR_IMAGE_REVISION="$CANDIDATE_COMMIT" \
  --build-arg TGHARBOR_IMAGE_SOURCE="$TGHARBOR_IMAGE_SOURCE" \
  --build-arg TGHARBOR_IMAGE_CREATED="$TGHARBOR_IMAGE_CREATED" \
  --tag tgharbor:0.2.3 .
```

Inspect the resulting OCI labels and require title `TGHarbor`, version `0.2.3`, the
exact candidate commit as revision, the verified public source, license `Apache-2.0`,
and the supplied created timestamp. The real BuildKit SLSA v1 shape carries the Git
source under `predicate.buildDefinition.externalParameters.configSource` and repeats
the source and revision binding in
`predicate.buildDefinition.resolvedDependencies`. The verifier requires one canonical
source URI, requires `configSource.digest.sha1` to equal the release revision, binds an
optional `configSource.ref` to the release tag or commit, binds the resolved Git
dependency primarily through `digest.gitCommit`, accepts `digest.sha1` only as a
compatible alias when `gitCommit` is absent, requires both fields to agree when both are
present, and rejects missing, conflicting, or
multiple source entries, wrong URIs or revisions, malformed structures, mixed legacy and
`configSource` paths, and source or revision strings that appear only in build arguments
or internal parameters. Record the image digest with the label values, then run the
final-filesystem, configuration/history, and per-layer scans. The history scan rejects
secrets: sensitive values, credential-bearing URLs, and sensitive `ENV`/`ARG` names,
assignments, and unsafe runtime defaults. It also rejects payload paths only where the
instruction semantics carry reliable intent, meaning exactly parsed `COPY`/`ADD` source
and destination operands and `ENV`/`ARG` values. A runtime mount default such as
`TGHARBOR_CONFIG=/config/config.yaml` is not a copied payload, while a copied `.env`,
configuration file, test, Git, session, database, or log artifact remains forbidden.
Opaque shell command text in Docker-generated history is not treated as payload
evidence, because Docker records no reliable argument boundary for a base image's `RUN`
commands; ordinary words such as `test`, `tests`, or `config.yaml`, and shell, linker, or
printf escape syntax such as `-Wl,-rpath='\$\$ORIGIN/../lib'` or `printf "*%s\n"`, are
not payloads. The per-layer archive scan is the authoritative forbidden-payload
control: it inspects every layer independently, so a forbidden file that a later layer
deletes, whitelists, or replaces, including an `.env` removed by `RUN rm`, still fails.
The layer scan accepts only the canonical
tar root directory marker `.`, requires directory metadata, rejects unsafe link or
device metadata, and applies normalization, forbidden-name, traversal, absolute,
backslash, link-target, and duplicate checks to every other member.
Rebuild the release image from the immutable candidate rather than relying on the
mutable tag. An
actual container build remains unpassed when Docker is unavailable; static Dockerfile
tests do not replace this candidate gate. Completing this source/container release
gate does not authorize a production rollout, which requires its own recovery and
deployment acceptance.

If a credential or session may have been exposed, revoke or rotate it first. History
rewriting does not make a still-valid credential safe. Coordinate history rewriting
with all repository users and rescan every ref afterward.

## Continuous controls

CI has read-only repository permissions, checks out complete history, pins Actions to
immutable commit SHAs, runs dependency auditing, and scans secrets. The credential-free
validation job may start from the protected default branch, but it resolves and outputs
that branch's exact commit SHA; the credential-scoped publish job checks out its
workflow source at that exact SHA rather than mutable `master`. CI also runs
`.github/scripts/verify_publication_boundary.py`, which fails the build on an unsafe
tracked or reachable-history path, a public link that targets restricted or untracked
material, a symbolic link in a public document or workflow, an artifact upload without
an exact-member allowlist, or a build context that is not a deny-all allowlist.
Dependabot opens bounded weekly updates for Python, Actions, and Docker dependencies.

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
decisions, and any rotations in the release record. Do not record the credential
values, private hostnames, account IDs, or user-content paths.
