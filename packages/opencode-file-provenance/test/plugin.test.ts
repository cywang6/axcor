// Copyright (c) 2026 Workflow Controller Contributors
//
// Licensed under the MIT License (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://opensource.org/licenses/MIT
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, resolve } from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { WorkflowControllerFileProvenance } from "../src/index.ts"

const repo = resolve(dirname(fileURLToPath(import.meta.url)), "../../..")
const python = process.env.WORKFLOW_CONTROLLER_TEST_PYTHON || resolve(
  repo,
  process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python",
)
const backend = [python, "-m", "workflow_controller"]

function input(project: string): any {
  return { worktree: project, directory: project }
}

function readCapture(project: string): any {
  const script = [
    "import json,sqlite3,sys",
    "from pathlib import Path",
    "from workflow_controller.plugins.file_provenance.store import ProvenanceStore",
    "s=ProvenanceStore(Path(sys.argv[1]))",
    "c=sqlite3.connect(s.db_path)",
    "i=c.execute('SELECT capture_id FROM captures ORDER BY started_at DESC LIMIT 1').fetchone()[0]",
    "c.close()",
    "print(json.dumps(s.get_capture(i)))",
  ].join(";")
  return JSON.parse(execFileSync(python, ["-c", script, project], { encoding: "utf8" }))
}

test("captures OpenCode tool evidence through the real sidecar", async () => {
  const project = mkdtempSync(resolve(tmpdir(), "opencode-provenance-"))
  const path = resolve(project, "example.py")
  writeFileSync(path, "before\n")
  const hooks: any = await WorkflowControllerFileProvenance(input(project), {
    backendCommand: backend,
    requestTimeoutMs: 10_000,
  })
  await hooks["chat.message"](
    { sessionID: "session-1", agent: "build", model: { providerID: "p", modelID: "m" } },
    { parts: [{ type: "text", text: "update example" }] },
  )
  await hooks["chat.message"](
    { sessionID: "session-1", agent: "build", model: { providerID: "p", modelID: "m" } },
    { parts: [{ type: "text", text: "apply the change now" }] },
  )
  await hooks["tool.execute.before"](
    { tool: "write", sessionID: "session-1", callID: "call-1" },
    { args: { filePath: "example.py", content: "after" } },
  )
  writeFileSync(path, "after\n")
  await hooks["tool.execute.after"](
    { tool: "write", sessionID: "session-1", callID: "call-1", args: {} },
    { title: "write", output: "ok", metadata: {} },
  )
  await hooks.dispose()

  const capture = readCapture(project)
  assert.equal(capture.native_session_id, "session-1")
  assert.equal(capture.model, "p/m")
  assert.equal(capture.agent_name, "build")
  assert.equal(capture.attributes.initial_prompt, "update example")
  assert.equal(capture.attributes.prompt, "apply the change now")
  assert.deepEqual(capture.mutations.map((mutation: any) => mutation.path), ["example.py"])
  assert.equal(readFileSync(path, "utf8"), "after\n")
})

test("untracked tools do not require a backend", async () => {
  const project = mkdtempSync(resolve(tmpdir(), "opencode-provenance-"))
  const hooks: any = await WorkflowControllerFileProvenance(input(project), {
    backendCommand: ["definitely-missing-workflow-controller"],
  })
  await hooks["tool.execute.before"](
    { tool: "read", sessionID: "session-1", callID: "call-1" },
    { args: { file_path: "example.py" } },
  )
  await hooks.dispose()
})

test("missing backend is fail-open", async () => {
  const project = mkdtempSync(resolve(tmpdir(), "opencode-provenance-"))
  const hooks: any = await WorkflowControllerFileProvenance(input(project), {
    backendCommand: ["definitely-missing-workflow-controller"],
    requestTimeoutMs: 100,
  })
  await hooks["tool.execute.before"](
    { tool: "write", sessionID: "session-1", callID: "call-1" },
    { args: { file_path: "example.py" } },
  )
  await hooks["tool.execute.before"](
    { tool: "write", sessionID: "session-1", callID: "call-2" },
    { args: { file_path: "example.py" } },
  )
  await hooks.dispose()
})

test("stale captures are closed as incomplete", async () => {
  const project = mkdtempSync(resolve(tmpdir(), "opencode-provenance-"))
  const hooks: any = await WorkflowControllerFileProvenance(input(project), {
    backendCommand: backend,
    requestTimeoutMs: 10_000,
    staleCaptureMs: 20,
  })
  await hooks["tool.execute.before"](
    { tool: "bash", sessionID: "session-1", callID: "call-stale" },
    { args: { command: "sleep forever" } },
  )
  await new Promise((resolve) => setTimeout(resolve, 100))
  await hooks.dispose()

  const capture = readCapture(project)
  assert.equal(capture.incomplete, true)
  assert.equal(capture.conflicted, true)
})

test("non-zero bash exits are recorded as errors", async () => {
  const project = mkdtempSync(resolve(tmpdir(), "opencode-provenance-"))
  const hooks: any = await WorkflowControllerFileProvenance(input(project), {
    backendCommand: backend,
    requestTimeoutMs: 10_000,
  })
  await hooks["tool.execute.before"](
    { tool: "bash", sessionID: "session-1", callID: "call-failed" },
    { args: { command: "false" } },
  )
  await hooks["tool.execute.after"](
    { tool: "bash", sessionID: "session-1", callID: "call-failed", args: {} },
    { title: "bash", output: "", metadata: { exit: 1 } },
  )
  await hooks.dispose()

  const capture = readCapture(project)
  assert.equal(capture.succeeded, false)
  assert.equal(capture.is_error, true)
  assert.equal(capture.incomplete, false)
})

test("tool failure events close captures immediately", async () => {
  const project = mkdtempSync(resolve(tmpdir(), "opencode-provenance-"))
  const hooks: any = await WorkflowControllerFileProvenance(input(project), {
    backendCommand: backend,
    requestTimeoutMs: 10_000,
    staleCaptureMs: 10_000,
  })
  await hooks["tool.execute.before"](
    { tool: "edit", sessionID: "session-1", callID: "call-aborted" },
    { args: { filePath: "example.py" } },
  )
  await hooks.event({
    event: {
      type: "session.tool.failed",
      properties: { sessionID: "session-1", callID: "call-aborted", error: "aborted" },
    },
  })
  const capture = readCapture(project)
  assert.equal(capture.succeeded, false)
  assert.equal(capture.is_error, true)
  assert.equal(capture.incomplete, false)
  await hooks.dispose()
})

test("protocol serialization failure is fail-open and the sidecar restarts", async () => {
  const project = mkdtempSync(resolve(tmpdir(), "opencode-provenance-"))
  const hooks: any = await WorkflowControllerFileProvenance(input(project), {
    backendCommand: backend,
    requestTimeoutMs: 10_000,
  })
  const cyclic: Record<string, unknown> = {}
  cyclic.self = cyclic
  await hooks["tool.execute.before"](
    { tool: "write", sessionID: "session-1", callID: "call-cyclic" },
    { args: cyclic },
  )
  await hooks["tool.execute.before"](
    { tool: "write", sessionID: "session-1", callID: "call-valid" },
    { args: { file_path: "example.py" } },
  )
  writeFileSync(resolve(project, "example.py"), "created\n")
  await hooks["tool.execute.after"](
    { tool: "write", sessionID: "session-1", callID: "call-valid", args: {} },
    { title: "write", output: "ok", metadata: {} },
  )
  await hooks.dispose()

  assert.equal(readCapture(project).tool_use_id, "call-valid")
})

test("Workflow Controller-managed sessions disable standalone capture", async () => {
  const previousURL = process.env.WORKFLOW_CONTROLLER_HOOK_URL
  const previousID = process.env.WORKFLOW_CONTROLLER_SESSION_ID
  process.env.WORKFLOW_CONTROLLER_HOOK_URL = "http://127.0.0.1:18765"
  process.env.WORKFLOW_CONTROLLER_SESSION_ID = "managed"
  try {
    const hooks = await WorkflowControllerFileProvenance(input(process.cwd()), {})
    assert.deepEqual(hooks, {})
  } finally {
    if (previousURL === undefined) delete process.env.WORKFLOW_CONTROLLER_HOOK_URL
    else process.env.WORKFLOW_CONTROLLER_HOOK_URL = previousURL
    if (previousID === undefined) delete process.env.WORKFLOW_CONTROLLER_SESSION_ID
    else process.env.WORKFLOW_CONTROLLER_SESSION_ID = previousID
  }
})
