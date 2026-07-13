# Troubleshooting

## Config flow cannot connect

- Confirm the speaker is first-generation LSX, not LSX II.
- Confirm Home Assistant can route to the speaker and TCP port 50001 is reachable.
- Confirm the built-in `kef` platform and other KEF-control applications are
  stopped. Do not retry with two owners running.
- Confirm the host/port is not already configured in another `kef_lsx` entry.

The config flow performs only a source GET. It does not wake or alter the speaker.
Connection refused, connect timeout, response timeout, malformed response, and
command rejection are intentionally different diagnostic categories.

## Entity is degraded but still available

This is expected after a short failed exchange. The integration retains the last
known state and continues accepting controls. `stale` means the core state is at
least 45 seconds old. The entity becomes unavailable only after at least four
primary failures and 120 seconds without channel success. One successful exchange
restores availability immediately.

Enable the disabled diagnostic sensors for last successful communication,
failure count, and last command result if needed. Disable them again afterward if
their recorder churn is not useful.

## Turn-on was attempted but verification failed

Direct wake records write attempted, acknowledgement, and later verification
separately. A dropped acknowledgement or verification response does not prove the
speaker ignored the SET. Check the last-command diagnostic and subsequent source
poll before repeating a non-idempotent action.

For the primary setup, preferred wake source should be `Opt`. The existing
automation's later `select_source: Opt` is safe but normally redundant.

## Volume step or mute is temporarily unavailable

Mute and volume-step must preserve the current absolute volume. If no valid volume
is cached, the worker first schedules a prioritized volume read instead of
guessing. A communication failure can therefore prevent that operation without
sending an unsafe value. Absolute volume set remains capped by maximum volume.

## Frequent timeouts or poll-overrun messages

- Verify there is exactly one integration/application using port 50001.
- Do not lower polling intervals or add automation retries.
- Check speaker Wi-Fi/routing separately; association alone does not prove the
  TCP control server is replying.
- Download redacted diagnostics and note the typed error category, primary
  failure count, queue state, and last success times.
- If the speaker's control server remains wedged, follow KEF's normal recovery
  guidance. Do not power-cycle production hardware during an unapproved test.

## Diagnostics and issue reports

Diagnostics should never contain the speaker host, config-entry/device/entity
identifiers, credentials, or raw protocol payloads. Before attaching a file,
inspect it anyway and remove private addresses, tokens, webhook IDs, and unrelated
configuration.

Include:

- integration and Home Assistant versions;
- speaker model/firmware as read from the device or label;
- action attempted and approximate time;
- whether degraded/unavailable was shown;
- redacted diagnostics and the smallest relevant log excerpt;
- whether the old built-in integration was fully stopped.

Do not enable debug logging indefinitely; the speaker and recorder are both
sensitive to unnecessary traffic/noise.

