// Included verbatim before the recovered CLI bundle by build_private_cli.py.
// Read once at process startup. direnv or later application code must not
// re-enable telemetry. Bun must not inline this variable at build time.
const gatewayPostHogOptOut = ["1", "true"].includes(
  process.env.CODEBUFF_DISABLE_POSTHOG,
);

function gatewayPostHogDisabled() {
  return gatewayPostHogOptOut;
}

function gatewayDisabledPostHogClient() {
  // Do not construct PostHog: its constructor can install exception handlers
  // and start background work before the first explicit capture call.
  return Object.freeze({
    capture() {},
    identify() {},
    alias() {},
    captureException() {},
    async flush() {},
  });
}
