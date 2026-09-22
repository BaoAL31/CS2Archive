# CSDM 3.20.1 recording startup fix

`server.dll` is the locally compiled, tested CSDM plugin with independent
engine-thread initialization. Upstream: akiver/cs-demo-manager, MIT (see LICENSE),
commit `8961f5072fe4d42803dde68e8e71b3c90b216504` (v3.20.1).
Patch: `docs/bugs/hlae-startup-experiment/startup-fix-candidate.patch`.
SHA-256: `9042b9abeb1efee881440c1c96aad74858791f69c63ec8118611c6ae7b3c9f79`.

Install when CS2 and CSDM are closed:

```powershell
& "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe" scripts/misc/install_csdm_startup_fix.py install
```

The installer verifies the stock and patched DLL hashes, copies the fix under
its own name in CSDM's plugin directory, and selects it with the required
`+csdm_initialize` launch command. This applies to all renderers using this CSDM
installation, including CS2UtilArchive. It leaves Steam and the stock DLL alone.
The settings journal is `.data/csdm-startup-fix-install.json`. Reinstall is
idempotent. Installation refuses a different stock plugin version.

Undo:

```powershell
& "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe" scripts/misc/install_csdm_startup_fix.py undo
```

Undo restores only the two changed playback settings and preserves unrelated
settings. If either was edited subsequently, undo refuses to overwrite it.
The inactive custom DLL is retained. CSDM updates may remove or reset custom
plugins; do not force this binary onto a new version. Rebase the source patch,
rebuild and repeat the real capture regression before updating pinned hashes.

## Rebuild

Check out the pinned upstream commit with its recursive submodules. Apply the
patch above to the pristine source. Build
`cs2-server-plugin/cs2-server-plugin/cs2-server-plugin.vcxproj` using VS 2026
MSBuild, `Configuration=Release`, `Platform=x64`, an isolated `OutDir`, and
`PostBuildEventUseInBuild=false`. This avoids the upstream post-build installer.
Build outputs can differ by compiler; validate any rebuilt binary rather than
blindly changing the installer's expected hash. No diagnostic/test commands
are included in this clean binary. The real regression results and reproduction
instructions are in `docs/bugs/hlae-steam-online-hook.md`.
