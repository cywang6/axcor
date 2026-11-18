# @workflow-controller/file-provenance

File provenance capture for OpenCode. Install the `workflow-controller` Python package
first, then add the plugin to `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": [["@workflow-controller/file-provenance", { "backendCommand": ["workflow-controller"] }]]
}
```

Evidence is stored in `.workflow-controller/file-provenance/`. The plugin records source
snapshots, tool input/output, session/model/agent metadata, and text prompts. It
does not provide a UI or write Git notes. Additional options are `ignore`,
`maxFileBytes`, `requestTimeoutMs`, and `staleCaptureMs`.

This PR includes the publish-ready package but does not publish it to the npm
registry. Until publication, run `npm pack` in this directory and install the
generated tarball in the OpenCode project:

```bash
cd packages/opencode-file-provenance
npm pack
npm install /path/to/generated-package.tgz
```

After publication, install it with `npm install @workflow-controller/file-provenance`. In
both cases, the `workflow-controller` Python package must already be installed and the
`workflow-controller` command must be available on `PATH`.

After a session completes, create a Sift-compatible offline audit package without
starting Workflow Controller Web or calling `opencode export`:

```bash
workflow-controller provenance export-audit-evidence \
  --project /path/to/worktree \
  --session ses_xxx \
  --output workflow-controller-audit-ses_xxx.json
```

The exporter reads the OpenCode SQLite database in read-only mode, recursively
includes child sessions, and joins captures by native session ID plus tool call ID.
