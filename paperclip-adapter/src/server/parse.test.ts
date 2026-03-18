/**
 * Tests for parseVibeOutput — the stdout JSON parser.
 *
 * Validates that the parser correctly extracts structured results from
 * Vibe heartbeat stdout, which may contain log lines before the JSON.
 *
 * Run with: node --import tsx --test src/server/parse.test.ts
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { parseVibeOutput, type VibeResult } from "./parse.js";

// ── Helper: build a HeartbeatResult JSON ──

function heartbeatJson(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    status: "success",
    issue_id: "GEN-42",
    summary: "Done successfully",
    usage: { input_tokens: 1000, output_tokens: 500 },
    cost_cents: 0,
    provider: "ollama",
    model: "codellama",
    exit_code: 0,
    clarification: null,
    retry_after_seconds: null,
    ...overrides,
  });
}

// ── Basic Parsing ──

describe("parseVibeOutput", () => {
  it("parses clean JSON output", () => {
    const result = parseVibeOutput(heartbeatJson());
    assert.ok(result.resultJson);
    assert.equal(result.resultJson!.status, "success");
    assert.equal(result.resultJson!.issue_id, "GEN-42");
    assert.equal(result.summary, "Done successfully");
    assert.equal(result.provider, "ollama");
    assert.equal(result.model, "codellama");
    assert.equal(result.costCents, 0);
  });

  it("extracts usage tokens", () => {
    const result = parseVibeOutput(heartbeatJson());
    assert.ok(result.usage);
    assert.equal(result.usage!.inputTokens, 1000);
    assert.equal(result.usage!.outputTokens, 500);
  });

  it("returns fallback for empty stdout", () => {
    const result = parseVibeOutput("");
    assert.equal(result.resultJson, null);
    assert.equal(result.usage, null);
    assert.equal(result.summary, "");
    assert.equal(result.clarification, null);
  });

  it("returns fallback for whitespace-only stdout", () => {
    const result = parseVibeOutput("   \n\n  ");
    assert.equal(result.resultJson, null);
  });

  it("returns fallback for non-JSON stdout", () => {
    const result = parseVibeOutput("just some logs\nno JSON here");
    assert.equal(result.resultJson, null);
  });

  // ── JSON extraction from mixed output ──

  it("finds JSON after log lines", () => {
    const stdout = [
      "2024-01-15 10:00:00 INFO Starting heartbeat...",
      "2024-01-15 10:00:01 INFO Fetching assignments...",
      "2024-01-15 10:00:02 INFO Running workflow...",
      heartbeatJson(),
    ].join("\n");

    const result = parseVibeOutput(stdout);
    assert.ok(result.resultJson);
    assert.equal(result.resultJson!.status, "success");
  });

  it("finds last JSON when multiple JSON objects present", () => {
    const stdout = [
      '{"debug": true, "step": "warmup"}',
      "some log line",
      heartbeatJson({ status: "success", summary: "Final result" }),
    ].join("\n");

    const result = parseVibeOutput(stdout);
    assert.ok(result.resultJson);
    assert.equal(result.summary, "Final result");
  });

  it("handles multi-line JSON", () => {
    const json = JSON.stringify(
      {
        status: "success",
        issue_id: "GEN-42",
        summary: "Done",
        usage: { input_tokens: 100, output_tokens: 50 },
        cost_cents: 0,
        provider: "ollama",
        model: "codellama",
      },
      null,
      2,
    );
    const stdout = `INFO: Starting\n${json}\n`;

    const result = parseVibeOutput(stdout);
    assert.ok(result.resultJson);
    assert.equal(result.resultJson!.status, "success");
  });

  it("handles JSON with escaped quotes in strings", () => {
    const result = parseVibeOutput(
      heartbeatJson({ summary: 'Used "PostgreSQL" for storage' }),
    );
    assert.ok(result.resultJson);
    assert.equal(result.summary, 'Used "PostgreSQL" for storage');
  });

  it("handles JSON with nested braces in strings", () => {
    const result = parseVibeOutput(
      heartbeatJson({ summary: "Output: {key: value}" }),
    );
    assert.ok(result.resultJson);
    assert.ok(result.summary.includes("{key: value}"));
  });

  // ── Clarification extraction ──

  it("extracts clarification request", () => {
    const result = parseVibeOutput(
      heartbeatJson({
        status: "clarification_needed",
        clarification: {
          questions: ["Which DB engine?", "REST or GraphQL?"],
          blocking_node: "vibe",
          context_summary: "Building API backend",
        },
      }),
    );

    assert.ok(result.clarification);
    assert.deepEqual(result.clarification!.questions, [
      "Which DB engine?",
      "REST or GraphQL?",
    ]);
    assert.equal(result.clarification!.blockingNode, "vibe");
    assert.equal(result.clarification!.contextSummary, "Building API backend");
  });

  it("returns null clarification when not present", () => {
    const result = parseVibeOutput(heartbeatJson());
    assert.equal(result.clarification, null);
  });

  it("handles clarification with empty questions array", () => {
    const result = parseVibeOutput(
      heartbeatJson({
        clarification: {
          questions: [],
          blocking_node: "",
          context_summary: "",
        },
      }),
    );

    assert.ok(result.clarification);
    assert.deepEqual(result.clarification!.questions, []);
  });

  it("handles clarification missing optional fields", () => {
    const result = parseVibeOutput(
      heartbeatJson({
        clarification: {
          questions: ["Q1?"],
        },
      }),
    );

    assert.ok(result.clarification);
    assert.equal(result.clarification!.blockingNode, "");
    assert.equal(result.clarification!.contextSummary, "");
  });

  // ── Edge cases ──

  it("handles missing usage field", () => {
    const json = JSON.stringify({
      status: "idle",
      summary: "No tasks",
    });
    const result = parseVibeOutput(json);
    assert.equal(result.usage, null);
  });

  it("handles missing summary field", () => {
    const json = JSON.stringify({ status: "idle" });
    const result = parseVibeOutput(json);
    assert.equal(result.summary, "");
  });

  it("handles missing provider and model", () => {
    const json = JSON.stringify({ status: "idle" });
    const result = parseVibeOutput(json);
    assert.equal(result.provider, "");
    assert.equal(result.model, "");
  });

  it("handles malformed JSON gracefully", () => {
    const result = parseVibeOutput('{"status": "success", broken}');
    // Should return fallback since JSON.parse fails
    assert.equal(result.resultJson, null);
  });

  it("handles stdout with trailing newlines after JSON", () => {
    const stdout = heartbeatJson() + "\n\n\n";
    const result = parseVibeOutput(stdout);
    assert.ok(result.resultJson);
    assert.equal(result.resultJson!.status, "success");
  });

  // ── Status variants ──

  it("parses idle status", () => {
    const result = parseVibeOutput(
      heartbeatJson({ status: "idle", summary: "No tasks assigned" }),
    );
    assert.equal(result.resultJson!.status, "idle");
    assert.equal(result.summary, "No tasks assigned");
  });

  it("parses blocked status", () => {
    const result = parseVibeOutput(
      heartbeatJson({
        status: "blocked",
        summary: "Quality below threshold",
        exit_code: 1,
      }),
    );
    assert.equal(result.resultJson!.status, "blocked");
  });

  it("parses failed status", () => {
    const result = parseVibeOutput(
      heartbeatJson({
        status: "failed",
        summary: "LLM backend crashed",
        exit_code: 1,
      }),
    );
    assert.equal(result.resultJson!.status, "failed");
    assert.equal(result.summary, "LLM backend crashed");
  });

  it("extracts cost_cents", () => {
    const result = parseVibeOutput(heartbeatJson({ cost_cents: 42 }));
    assert.equal(result.costCents, 42);
  });
});
