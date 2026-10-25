# Pinned official CLI with proxy-level telemetry blocking

The official Freebuff 0.2.34 bundle hardcodes the PostHog key and host. Setting
`NEXT_PUBLIC_POSTHOG_API_KEY` or `NEXT_PUBLIC_POSTHOG_HOST_URL` in the worker
environment does not override those values. `CODEBUFF_SHIP_LOGS=false` only
controls `/api/logs`. Rebuilding the binary (Bun recompilation or byte
splicing) breaks the CLI's interactive startup — the TUI paints blank frames —
so the private-build pipeline was removed entirely.

**The gateway now runs the official executable byte-for-byte.** Nothing is
unpacked, rebuilt, repacked or re-signed: the vendor's native signature,
embedded assets and startup ordering stay intact, and a new release is adopted
by downloading it and pinning its digests.

## Enforcement model

Telemetry is blocked where it must leave the machine — the gateway proxy
(`app/proxy_manager.py`):

- Every enabled account egresses through a gateway-managed sing-box inbound:
  subscription accounts through their matched node, explicit `proxy://…`
  endpoints are chained behind the inbound (the operator's endpoint stays the
  exit), and proxy-less accounts get a `direct` outbound.
- The first route rule in every generated config refuses all `*.posthog.com`
  requests for every inbound, in the legacy outbound-field dialect so the
  container-pinned sing-box 1.10 and newer releases accept it alike.
- A failed or missing daemon fails the account closed (`proxy_error`): without
  the gateway inbound there is no telemetry block, so workers never degrade
  into direct CLI egress.

What still runs in-process: the CLI constructs its PostHog client and batches
events locally, but every delivery attempt is refused by the proxy before it
leaves the machine. `/api/logs` stays off through the upstream-supported
`CODEBUFF_SHIP_LOGS=false`.

## Verification model

Workers and the container entrypoint call `app/cli_build.verify_official_cli`
before touching any account credential: `FREEBUFF_BIN` must hash (SHA-256) to
the `binary_sha256` section of `cli/freebuff-0.2.34-binary-checksums.json`.
The registry also pins every official release archive digest (mirrored from
the npm package's `binaryChecksums`, a trust root separate from this
repository). A tampered binary, an unaudited new release, or a damaged
registry is rejected — there is no fallback to the npm launcher or any other
executable.

## Install

Requires Python 3.11+ and sing-box on `PATH`. Targets: `darwin-arm64`,
`linux-x64`, `linux-arm64`.

```sh
# Recommended: download and verify the pinned official release.
python3 scripts/fetch_official_cli.py --fetch

# Already downloaded the archive yourself:
python3 scripts/fetch_official_cli.py --archive /path/to/freebuff-<target>.tar.gz
```

Output is `.freebuff/official-cli/freebuff` (override with `--output`; the
file must be named `freebuff`). The script downloads only from
`https://codebuff.com` (redirects confined to the publisher's hosts), verifies
the archive SHA-256, then verifies the extracted executable's SHA-256 before
installing atomically. It never executes the CLI and never installs
dependencies.

**Upgrading to a new release**: download it, audit it, then extend the
`sha256`/`binary_sha256`/`bundle_sha256` sections of
`cli/freebuff-0.2.34-binary-checksums.json` (renaming the file to the new
version and updating `FREEBUFF_VERSION` in `scripts/fetch_official_cli.py` and
`app/cli_build.py`). Until the digests are pinned, workers refuse the new
binary. `scripts/render_smoke_check.py` renders the first TUI frame under a
pty as a release sanity gate (copy the candidate to
`.freebuff/render-smoke/freebuff` first).

**Container images install this automatically.** The Dockerfile's first stage
fetches and verifies the pinned release for the image's platform and installs
it at `/opt/freebuff/official-cli/freebuff`; the entrypoint re-verifies the
digest before the gateway starts. Bare-metal Linux installs do the same via
`scripts/setup_linux.sh`.

## Verify

```sh
FREEBUFF_HARDWARE_PRIVACY=off python3 -m unittest discover -s tests -p 'test_cli_build.py' -v
FREEBUFF_HARDWARE_PRIVACY=off python3 -m unittest discover -s tests -p 'test_telemetry_blocklist.py' -v
FREEBUFF_HARDWARE_PRIVACY=off python3 -m unittest discover -s tests -p 'test_worker_lifecycle.py' -v
```

The Python tests verify digest rejection (unknown, tampered and non-executable
binaries), the telemetry block rule's position and coverage, explicit-proxy
chaining, fail-closed daemon behavior, environment precedence, and delivery
through a real worker/shell to an offline fake CLI. The lifecycle tests turn
off the separate hardware probe because their CLI is a Python fixture. None of
these tests authenticate or submit an upstream prompt.

Restart the intended gateway to use a newly installed executable. Installing a
release does not restart services or modify running workers. Blocking PostHog
egress does not remove identifiers from normal login or inference requests.
