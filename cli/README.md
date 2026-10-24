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
login shell profiles. It verifies a SHA-256 build receipt before worker startup,
then launches the private executable directly. The npm launcher is bypassed:
it has its own telemetry and can install official updates that remove this patch.
The receipt identifies a local build; it is not a publisher signature and does
not protect against an attacker able to replace both files.

## Build

The audited input is the existing local `extracted_sources/freebuff.js` and all
22 assets listed by `extracted_sources/assets/assets.json`. The build script
pins the full bundle hash, factory shape, asset hashes, version, and platform.
It refuses unknown input rather than making an unchecked patch. Extracted
upstream code and generated binaries stay outside Git.

Requires Python 3.10+ and Bun 1.4.4 with `--asset` support, on macOS arm64:

```sh
python3 scripts/build_private_cli.py
```

Output is `.freebuff/private-cli/freebuff` with an adjacent
`freebuff.privacy.json` receipt. This is now the gateway's default executable.
Existing output is preserved; use `--output /absolute/path/to/new/freebuff` for
another build and export `FREEBUFF_BIN` to select it. Copy the receipt alongside
the executable when moving it. The official installed executable is untouched.

The build does not download packages or execute the CLI. It preserves embedded
asset names and uses `--env=disable` so the new switch is read at runtime. Bun
autoload of `.env`, bunfig, tsconfig, and package.json is disabled for this build.

**Linux/Docker is not built by this recipe.** The recovered bundle contains
macOS arm64 native libraries and platform constants. Do not cross-compile it
and call it a Linux build. Linux deployment needs its own audited input and
receipt; gateway startup fails closed until that exists.

## Verify

```sh
node --test tests/test_posthog_opt_out.mjs
FREEBUFF_HARDWARE_PRIVACY=off python3 -m unittest discover -s tests -p 'test_cli_build.py' -v
FREEBUFF_HARDWARE_PRIVACY=off python3 -m unittest discover -s tests -p 'test_worker_lifecycle.py' -v
```

The JavaScript tests execute the actual recovered analytics functions using a
recording SDK substitute and compile a small executable to check runtime flag
behavior. Python tests verify receipt rejection, environment precedence and
delivery through a real worker/shell to an offline fake CLI. The lifecycle
tests turn off the separate hardware probe because their CLI is a Python fixture.
None of these tests authenticate or submit an upstream prompt.

Restart the intended gateway to use new code and its private executable. This
build process does not restart services or modify running workers. Disabling
PostHog does not remove identifiers from normal login or inference requests.
