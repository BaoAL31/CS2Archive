# CSDM startup causal experiment

Diagnostic code only; no production installation. The patch is against CSDM
v3.20.1, commit `8961f5072fe4d42803dde68e8e71b3c90b216504`, with its pinned
`cs2-server-plugin` submodules. Apply `causal-experiment.patch` to that checkout
and build its x64 Release C++ project (VS 2026/v145 was used here), disabling the
post-build installation event with `PostBuildEventUseInBuild=false` and choosing
an isolated output directory. The retained local binary is
`.cache/hlae-diagnostic/build-causal/server.dll`.

The patch deliberately exposes test-only commands:

- `csdm_diagnostic_probe`: log client interface availability and frame-hook state.
- `csdm_diagnostic_start_early`: execute playdemo before the background connection.
- `csdm_diagnostic_init`: install the original frame hook on the engine command
  thread without waiting for a server connection.

Run the same short CSDM capture JSON and same DLL for each arm. Ensure no CS2
is running. The existing test config is `renders/swift-baseline/smoke-csdm.json`
(local FACEIT demo, ticks 6727–8007). Use the diagnostic helper with `--console`,
`--seconds 90`, `--plugin <isolated-server.dll>` and:

```text
control:      --launch-extra "+csdm_diagnostic_probe +csdm_diagnostic_start_early"
intervention: --launch-extra "+csdm_diagnostic_probe +csdm_diagnostic_init +csdm_diagnostic_start_early"
normal path:  --launch-extra "+csdm_diagnostic_probe +csdm_diagnostic_init"
```

The test passes only when `plugin.log` shows executed record commands and a valid
finished MP4 exists. For the causal intervention, additionally verify there is
no `ClientFullyConnect: playerSlot=` entry: recording must work without that event.
The control must show demo playback in `engine-console.log` but no frame hook or
recorded MP4. Do not infer recording from HLAE module presence alone.

The helper temporarily selects a uniquely named plugin copy and restores CSDM
settings on normal completion/errors. Do not forcibly kill the helper. Its
settings restoration refuses to overwrite concurrent settings changes. Use a
shell with real process visibility; the restricted agent shell can miss Steam
and CS2 processes. Never restart/toggle Steam for this experiment.

Logs, capture configs, module observations and summaries are retained under the
timestamped `renders/hlae-diagnostic-*` directories referenced in the parent bug
document. The experimental patch is intentionally preserved here so the causal
test survives cache cleanup; it must not become a default render plugin.

## Clean candidate

Apply `startup-fix-candidate.patch` instead of the experiment patch to the same
pristine source revision. Build x64 Release as above. Use
`--plugin .cache/hlae-diagnostic/build-candidate/server.dll --launch-extra "+csdm_initialize"`
with the capture harness for an ordinary-startup check. The initialization must
be invoked through the engine console; do not move it into the WebSocket thread.

For the regression test, add only the `csdm_diagnostic_start_early` command body
from the experiment patch to the candidate source, compile a separate test DLL,
and use `--launch-extra "+csdm_initialize +csdm_diagnostic_start_early"`. Verify
recording without a `ClientFullyConnect` log entry. The clean release DLL should
exclude this command. Both ordinary and regression paths passed on the tested
CS2 build; the parent document records hashes and evidence folders.
