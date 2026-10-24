# Private CLI with a PostHog opt-out

The official Freebuff 0.2.34 bundle hardcodes the PostHog key and host. Setting
`NEXT_PUBLIC_POSTHOG_API_KEY` or `NEXT_PUBLIC_POSTHOG_HOST_URL` in the worker
environment does not override those values. `CODEBUFF_SHIP_LOGS=false` only
controls `/api/logs`.

This local build adds `CODEBUFF_DISABLE_POSTHOG=1` (also accepts `true`). It reads
the switch once at process startup and returns a no-op client from the shared
PostHog factory, before SDK construction. This covers CLI and agent SDK events,
identify/alias, explicit exception capture, automatic exception hooks, and
flush. Later direnv changes cannot reverse the startup opt-out. Without the
switch, the original factory behavior is retained.

The gateway forces both switches after custom environment overrides and after
login shell profiles. It verifies the build before worker startup, then
launches the private executable directly. The npm launcher is bypassed: it has
its own telemetry and can install official updates that remove this patch.

## Verification model

A receipt alone proves nothing (anyone can write JSON next to a binary), so
`app/cli_build.py` also requires that the binary itself **embeds the patched
PostHog factory** (`if (gatewayPostHogDisabled()) return
gatewayDisabledPostHogClient();`, whitespace-tolerant because Bun reprints the
bundle). Those guard function names exist only in our patched bundle, so an
official binary is rejected even when someone generates a receipt for it —
including the container entrypoint, which fails closed the same way. The
receipt additionally pins the source JavaScript digest to the per-target values
in `cli/freebuff-0.2.34-binary-checksums.json`, which also records the SHA-256
of every official 0.2.34 release archive (mirrored from the npm package's
`binaryChecksums`, a trust root separate from this repository).

## Build

Requires Python 3.11+ and Bun on `PATH` (`npm install -g bun@1.3`). Targets:
`darwin-arm64`, `linux-x64`, `linux-arm64` (must build on the target platform).

```sh
# Recommended: extract and rebuild the pinned official release archive.
python3 scripts/build_private_cli.py --fetch

# Already downloaded the archive yourself:
python3 scripts/build_private_cli.py --archive /path/to/freebuff-<target>.tar.gz

# macOS arm64 development convenience: reuse the audited extracted_sources/ tree.
python3 scripts/build_private_cli.py
```

The build never executes the CLI and never installs dependencies. It downloads
only the pinned release archive from `https://codebuff.com`, verifies its
SHA-256 against `cli/freebuff-0.2.34-binary-checksums.json`, extracts it with
the committed `unbuned/unbuned.py` (in-process, no subprocess), verifies the
extracted JavaScript digest and every embedded asset against the per-target
registry in `cli/assets-0.2.34-*.json`, locates the unique PostHog client
factory structurally (minifier names differ per target), and refuses to build
if that coverage changed. Output is `.freebuff/private-cli/freebuff` with an
adjacent `freebuff.privacy.json` receipt; `--output` selects another location.
Copy the receipt alongside the executable when moving it. The official
installed executable is untouched. Bun autoload of `.env`, bunfig, tsconfig,
and package.json is disabled for this build.

**Container images build this automatically.** The Dockerfile's first stage
performs exactly the pinned-archive build for the image's platform
(`linux-x64`/`linux-arm64`) and installs it at
`/opt/freebuff/private-cli/freebuff`; the entrypoint verifies it before the
gateway starts. Bare-metal Linux installs do the same via
`scripts/setup_linux.sh`.

## Verify

```sh
node --test tests/test_posthog_opt_out.mjs
FREEBUFF_HARDWARE_PRIVACY=off python3 -m unittest discover -s tests -p 'test_cli_build.py' -v
FREEBUFF_HARDWARE_PRIVACY=off python3 -m unittest discover -s tests -p 'test_worker_lifecycle.py' -v
```

The JavaScript tests execute the actual recovered analytics functions using a
recording SDK substitute and compile a small executable to check runtime flag
behavior. Python tests verify receipt rejection (including receipts forged
against official binaries), environment precedence, and delivery through a
real worker/shell to an offline fake CLI. The lifecycle tests turn off the
separate hardware probe because their CLI is a Python fixture. None of these
tests authenticate or submit an upstream prompt.

Restart the intended gateway to use new code and its private executable. This
build process does not restart services or modify running workers. Disabling
PostHog does not remove identifiers from normal login or inference requests.
