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

import type { Plugin } from "@opencode-ai/plugin"

const tracked = new Set(["edit", "write", "apply_patch", "bash"])
const hookURL = process.env.WORKFLOW_CONTROLLER_HOOK_URL || ""

const post = async (path: string, body: unknown): Promise<void> => {
  if (!hookURL) return
  try {
    await fetch(`${hookURL}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(5000),
    })
  } catch {
    // Provenance is fail-open: a controller outage must not fail the tool.
  }
}

export const WorkflowControllerFileChanges: Plugin = async ({ worktree, directory }) => {
  const pending = new Map<string, { body: Record<string, unknown>, createdAt: number }>()
  const cwd = worktree || directory || process.cwd()
  if (!hookURL) console.warn("Workflow Controller file provenance disabled: WORKFLOW_CONTROLLER_HOOK_URL is missing")
  return {
    "tool.execute.before": async (input: any, output?: any) => {
      const tool = String(input.tool || "")
      if (!tracked.has(tool.toLowerCase())) return
      const callID = String(input.callID || "")
      if (!callID) return
      const now = Date.now()
      for (const [key, value] of pending) {
        if (now - value.createdAt > 10 * 60 * 1000) pending.delete(key)
      }
      if (pending.size >= 1024) pending.delete(pending.keys().next().value as string)
      const body = {
        session_id: process.env.WORKFLOW_CONTROLLER_SESSION_ID || input.sessionID || "unknown",
        native_session_id: input.sessionID || "",
        parent_session_id: input.parentSessionID || "",
        parent_tool_use_id: input.parentCallID || "",
        agent_id: input.agentID || input.agent_id || "",
        agent_type: input.agent || input.agentName || input.agent_type || "",
        tool_use_id: callID,
        tool_name: tool,
        tool_input: output?.args ?? input.args ?? {},
        cwd,
      }
      pending.set(callID, { body, createdAt: now })
      await post("/hooks/file-changes/pre", body)
    },
    "tool.execute.after": async (input: any, output?: any) => {
      const callID = String(input.callID || "")
      const entry = pending.get(callID)
      pending.delete(callID)
      if (!entry) return
      await post("/hooks/file-changes/post", {
        ...entry.body,
        tool_response: output?.output ?? output?.metadata ?? null,
        is_error: Boolean(output?.error),
      })
    },
  }
}

export default WorkflowControllerFileChanges
