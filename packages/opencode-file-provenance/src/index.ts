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

import { randomUUID } from "node:crypto"
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process"
import { isAbsolute, resolve } from "node:path"
import { createInterface } from "node:readline"
import type { Hooks, Plugin } from "@opencode-ai/plugin"

const PROTOCOL_VERSION = 1
const TRACKED_TOOLS = new Set(["edit", "write", "apply_patch", "bash"])

export interface FileProvenanceOptions {
  backendCommand?: string[]
  ignore?: string[]
  maxFileBytes?: number
  requestTimeoutMs?: number
  staleCaptureMs?: number
}

interface SessionEvidence {
  initialPrompt: string
  prompt: string
  model?: string
  agent?: string
}

interface PendingCapture {
  context: Record<string, unknown>
  timer: NodeJS.Timeout
}

interface PendingRequest {
  resolve: (value: unknown) => void
  reject: (error: Error) => void
  timer: NodeJS.Timeout
}

class SidecarClient {
  private readonly project: string
  private readonly options: Required<FileProvenanceOptions>
  private child?: ChildProcessWithoutNullStreams
  private starting?: Promise<void>
  private nextID = 1
  private pending = new Map<number, PendingRequest>()
  private warned = false

  constructor(project: string, options: Required<FileProvenanceOptions>) {
    this.project = project
    this.options = options
  }

  async request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    await this.ensureStarted()
    try {
      return await this.send(method, params)
    } catch (error) {
      this.reset(error instanceof Error ? error : new Error(message(error)))
      throw error
    }
  }

  async close(): Promise<void> {
    if (!this.child) return
    try {
      await this.send("shutdown", {})
    } catch {
      // Shutdown is best-effort; the process is terminated below.
    }
    this.reset(new Error("file provenance sidecar closed"))
  }

  warn(error: unknown): void {
    if (this.warned) return
    this.warned = true
    console.warn(`OpenCode file provenance disabled for this operation: ${message(error)}`)
  }

  private async ensureStarted(): Promise<void> {
    if (this.child) return
    if (!this.starting) {
      this.starting = this.start().finally(() => { this.starting = undefined })
    }
    await this.starting
  }

  private async start(): Promise<void> {
    const [command, ...prefix] = this.options.backendCommand
    const args = [
      ...prefix,
      "provenance", "serve", "--project", this.project,
      "--max-file-bytes", String(this.options.maxFileBytes),
      ...this.options.ignore.flatMap((pattern) => ["--ignore", pattern]),
    ]
    const child = spawn(command, args, { cwd: this.project, stdio: "pipe", env: process.env })
    this.child = child
    createInterface({ input: child.stdout }).on("line", (line) => this.onLine(child, line))
    createInterface({ input: child.stderr }).on("line", (line) => {
      if (line.trim()) console.warn(`workflow-controller provenance: ${line}`)
    })
    child.once("error", (error) => this.reset(error, child))
    child.once("exit", (code) => {
      this.reset(new Error(`sidecar exited with code ${code}`), child)
    })
    const value = await this.send("ping", {}) as { protocolVersion?: number }
    if (value.protocolVersion !== PROTOCOL_VERSION) {
      this.reset(new Error(`unsupported sidecar protocol: ${value.protocolVersion}`))
      throw new Error(`unsupported sidecar protocol: ${value.protocolVersion}`)
    }
    this.warned = false
  }

  private send(method: string, params: Record<string, unknown>): Promise<unknown> {
    const child = this.child
    if (!child) return Promise.reject(new Error("file provenance sidecar is unavailable"))
    const id = this.nextID++
    let line: string
    try {
      line = `${JSON.stringify({ id, method, params })}\n`
    } catch (error) {
      return Promise.reject(error)
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        const error = new Error(`sidecar request timed out: ${method}`)
        reject(error)
        this.reset(error, child)
      }, this.options.requestTimeoutMs)
      this.pending.set(id, { resolve, reject, timer })
      child.stdin.write(line, (error) => {
        if (error) this.reset(error, child)
      })
    })
  }

  private onLine(child: ChildProcessWithoutNullStreams, line: string): void {
    if (this.child !== child) return
    try {
      const value = JSON.parse(line) as { id?: number, ok?: boolean, result?: unknown, error?: string }
      if (typeof value.id !== "number") throw new Error("response is missing a numeric id")
      const request = this.pending.get(value.id)
      if (!request) return
      this.pending.delete(value.id)
      clearTimeout(request.timer)
      if (value.ok) request.resolve(value.result)
      else request.reject(new Error(value.error || "sidecar request failed"))
    } catch (error) {
      this.reset(error instanceof Error ? error : new Error(message(error)))
    }
  }

  private reset(error: Error, source?: ChildProcessWithoutNullStreams): void {
    if (source && this.child !== source) return
    const child = this.child
    this.child = undefined
    if (child && child.exitCode === null) child.kill()
    for (const request of this.pending.values()) {
      clearTimeout(request.timer)
      request.reject(error)
    }
    this.pending.clear()
  }
}

export const WorkflowControllerFileProvenance: Plugin = async (
  { worktree, directory },
  rawOptions = {},
) => {
  if (process.env.WORKFLOW_CONTROLLER_HOOK_URL && process.env.WORKFLOW_CONTROLLER_SESSION_ID) return {}

  const project = resolve(worktree || directory || process.cwd())
  const options = normalizeOptions(rawOptions)
  const client = new SidecarClient(project, options)
  const sessions = new Map<string, SessionEvidence>()
  const captures = new Map<string, PendingCapture>()

  const finish = async (
    callID: string,
    output: unknown,
    isError: boolean,
    incomplete = false,
  ): Promise<void> => {
    const pending = captures.get(callID)
    if (!pending) return
    captures.delete(callID)
    clearTimeout(pending.timer)
    const captureID = String(pending.context.capture_id)
    try {
      await client.request("after", {
        capture_id: captureID,
        outcome: {
          succeeded: !isError && !incomplete,
          is_error: isError,
          tool_output: output,
          path_hints: pathHints(pending.context.tool_input, project),
          finished_at: new Date().toISOString(),
          incomplete,
        },
      })
    } catch (error) {
      client.warn(error)
    }
  }

  const hooks: Hooks = {
    "chat.message": async (input, output) => {
      const prompt = output.parts
        .filter((part): part is typeof part & { text: string } => part.type === "text" && typeof part.text === "string")
        .map((part) => part.text)
        .join("\n")
      const existing = sessions.get(input.sessionID)
      sessions.set(input.sessionID, {
        initialPrompt: existing?.initialPrompt || prompt,
        prompt: prompt || existing?.prompt || "",
        model: input.model ? `${input.model.providerID}/${input.model.modelID}` : existing?.model,
        agent: input.agent || existing?.agent,
      })
    },
    "tool.execute.before": async (input, output) => {
      if (!TRACKED_TOOLS.has(input.tool.toLowerCase())) return
      const captureID = randomUUID()
      const evidence = sessions.get(input.sessionID)
      const context: Record<string, unknown> = {
        capture_id: captureID,
        actor_kind: "agent",
        workflow_controller_session_id: null,
        native_session_id: input.sessionID,
        parent_native_session_id: null,
        runtime: "opencode",
        model: evidence?.model ?? null,
        agent_name: evidence?.agent ?? null,
        native_agent_id: evidence?.agent ?? null,
        tool_name: input.tool,
        tool_use_id: input.callID,
        cwd: project,
        tool_input: output.args ?? {},
        started_at: new Date().toISOString(),
        attributes: {
          source: "opencode-plugin",
          initial_prompt: evidence?.initialPrompt ?? "",
          prompt: evidence?.prompt ?? "",
        },
      }
      try {
        await client.request("before", { context })
      } catch (error) {
        client.warn(error)
        return
      }
      const timer = setTimeout(() => {
        void finish(captureKey(input.sessionID, input.callID), {
          reason: "tool completion hook timed out",
        }, true, true)
      }, options.staleCaptureMs)
      timer.unref?.()
      captures.set(captureKey(input.sessionID, input.callID), { context, timer })
    },
    "tool.execute.after": async (input, output) => {
      const key = captureKey(input.sessionID, input.callID)
      const pending = captures.get(key)
      if (!pending) return
      const metadata = output.metadata as { error?: unknown; exit?: unknown } | undefined
      const failedExit = typeof metadata?.exit === "number" && metadata.exit !== 0
      await finish(key, output, metadata?.error === true || failedExit)
    },
    event: async ({ event }) => {
      const failure = failedToolEvent(event)
      if (!failure) return
      await finish(
        captureKey(failure.sessionID, failure.callID),
        { error: failure.error },
        true,
      )
    },
    dispose: async () => {
      await Promise.all([...captures.keys()].map((id) =>
        finish(id, { reason: "OpenCode plugin unloaded" }, true, true),
      ))
      await client.close()
    },
  }
  return hooks
}

function normalizeOptions(value: Record<string, unknown>): Required<FileProvenanceOptions> {
  const command = Array.isArray(value.backendCommand)
    ? value.backendCommand.filter((item): item is string => typeof item === "string" && item.length > 0)
    : []
  return {
    backendCommand: command.length ? command : ["workflow-controller"],
    ignore: Array.isArray(value.ignore)
      ? value.ignore.filter((item): item is string => typeof item === "string")
      : [],
    maxFileBytes: positive(value.maxFileBytes, 2 * 1024 * 1024),
    requestTimeoutMs: positive(value.requestTimeoutMs, 60_000),
    staleCaptureMs: positive(value.staleCaptureMs, 10 * 60_000),
  }
}

function positive(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : fallback
}

function pathHints(value: unknown, project: string): string[] {
  if (!value || typeof value !== "object") return []
  const input = value as Record<string, unknown>
  return ["file_path", "filePath", "filepath", "path", "notebook_path"]
    .map((key) => input[key])
    .filter((item): item is string => typeof item === "string" && item.length > 0)
    .map((item) => isAbsolute(item) ? item : resolve(project, item))
}

function failedToolEvent(value: unknown): { sessionID: string; callID: string; error: unknown } | undefined {
  if (!value || typeof value !== "object") return undefined
  const event = value as { type?: unknown; properties?: unknown }
  if (event.type !== "session.tool.failed") return undefined
  if (!event.properties || typeof event.properties !== "object") return undefined
  const properties = event.properties as Record<string, unknown>
  const sessionID = typeof properties.sessionID === "string"
    ? properties.sessionID
    : typeof properties.session_id === "string" ? properties.session_id : undefined
  const callID = typeof properties.callID === "string"
    ? properties.callID
    : typeof properties.call_id === "string" ? properties.call_id : undefined
  if (!sessionID || !callID) return undefined
  return { sessionID, callID, error: properties.error ?? "tool failed" }
}

function captureKey(sessionID: string, callID: string): string {
  return `${sessionID}\0${callID}`
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export default WorkflowControllerFileProvenance
