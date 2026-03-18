/**
 * Tests for slack-notifier.ts and slack-reply-poller.ts
 *
 * Structural tests that validate module behavior without making real
 * Slack API calls. Run with: node --import tsx --test src/server/slack-notifier.test.ts
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  notifyClarificationViaSlack,
  type SlackNotifyOptions,
  type SlackNotifyResult,
} from "./slack-notifier.js";
import { pollForSlackReply } from "./slack-reply-poller.js";

// ── Helper: mock fetch globally ──

function installMockFetch(
  responses: Array<{
    ok: boolean;
    channel?: { id: string };
    ts?: string;
    error?: string;
    messages?: Array<{
      user?: string;
      bot_id?: string;
      text?: string;
      ts?: string;
    }>;
  }>,
) {
  let callIndex = 0;
  const calls: Array<{ url: string; body: unknown }> = [];

  const mockFetch = async (url: string | URL, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(init.body as string) : {};
    calls.push({ url: url.toString(), body });
    const response = responses[callIndex++] ?? { ok: false, error: "no mock" };
    return {
      json: async () => response,
      ok: response.ok !== false,
      status: response.ok !== false ? 200 : 400,
    } as Response;
  };

  (globalThis as unknown as { fetch: typeof fetch }).fetch =
    mockFetch as typeof fetch;
  return calls;
}

function baseOptions(
  overrides?: Partial<SlackNotifyOptions>,
): SlackNotifyOptions {
  return {
    botToken: "xoxb-test-token",
    userId: "U12345",
    questions: ["Which database?", "REST or GraphQL?"],
    issueId: "GEN-42",
    ...overrides,
  };
}

// ── Notifier Tests ──

describe("notifyClarificationViaSlack", () => {
  it("returns { ok: false } when botToken is missing", async () => {
    const logs: string[] = [];
    const result: SlackNotifyResult = await notifyClarificationViaSlack(
      baseOptions({ botToken: "" }),
      (line) => logs.push(line),
    );
    assert.equal(result.ok, false);
    assert.equal(result.channelId, undefined);
    assert.ok(logs.some((l) => l.includes("missing")));
  });

  it("returns { ok: false } when userId is missing", async () => {
    const result = await notifyClarificationViaSlack(
      baseOptions({ userId: "" }),
    );
    assert.equal(result.ok, false);
  });

  it("returns channelId and messageTs on success", async () => {
    const calls = installMockFetch([
      { ok: true, channel: { id: "D999" } },
      { ok: true, ts: "1234567890.123456" },
    ]);

    const result = await notifyClarificationViaSlack(baseOptions());
    assert.equal(result.ok, true);
    assert.equal(result.channelId, "D999");
    assert.equal(result.messageTs, "1234567890.123456");
    assert.equal(calls.length, 2);
    assert.ok(calls[0].url.includes("conversations.open"));
    assert.ok(calls[1].url.includes("chat.postMessage"));
  });

  it("returns { ok: false } when conversations.open fails", async () => {
    installMockFetch([{ ok: false, error: "user_not_found" }]);

    const logs: string[] = [];
    const result = await notifyClarificationViaSlack(
      baseOptions(),
      (line) => logs.push(line),
    );
    assert.equal(result.ok, false);
    assert.ok(logs.some((l) => l.includes("user_not_found")));
  });

  it("returns { ok: false } when chat.postMessage fails", async () => {
    installMockFetch([
      { ok: true, channel: { id: "D999" } },
      { ok: false, error: "channel_not_found" },
    ]);

    const result = await notifyClarificationViaSlack(baseOptions());
    assert.equal(result.ok, false);
  });

  it("includes agent name in fallback text", async () => {
    const calls = installMockFetch([
      { ok: true, channel: { id: "D999" } },
      { ok: true, ts: "1111.2222" },
    ]);

    await notifyClarificationViaSlack(
      baseOptions({
        issueUrl: "https://app.paperclip.dev/issues/GEN-42",
        agentName: "CodeBot",
      }),
    );

    const messageBody = calls[1].body as Record<string, unknown>;
    const text = messageBody.text as string;
    assert.ok(text.includes("CodeBot"));
    assert.ok(text.includes("GEN-42"));
  });

  it("tells user to reply in thread (not on Paperclip)", async () => {
    const calls = installMockFetch([
      { ok: true, channel: { id: "D999" } },
      { ok: true, ts: "1111.2222" },
    ]);

    await notifyClarificationViaSlack(baseOptions());

    const messageBody = calls[1].body as Record<string, unknown>;
    const blocks = messageBody.blocks as Array<Record<string, unknown>>;
    const contextBlock = blocks.find((b) => b.type === "context");
    assert.ok(contextBlock, "Should have a context block");
    const elements = contextBlock!.elements as Array<
      Record<string, unknown>
    >;
    const text = elements[0].text as string;
    assert.ok(
      text.includes("Reply in this thread"),
      "Should tell user to reply in thread",
    );
    assert.ok(
      !text.includes("Paperclip issue"),
      "Should NOT mention Paperclip issue",
    );
  });

  it("handles fetch exceptions gracefully", async () => {
    (globalThis as unknown as { fetch: typeof fetch }).fetch = (() => {
      throw new Error("network down");
    }) as unknown as typeof fetch;

    const logs: string[] = [];
    const result = await notifyClarificationViaSlack(
      baseOptions(),
      (line) => logs.push(line),
    );
    assert.equal(result.ok, false);
    assert.ok(logs.some((l) => l.includes("network down")));
  });

  it("formats multiple questions as numbered list", async () => {
    const calls = installMockFetch([
      { ok: true, channel: { id: "D999" } },
      { ok: true, ts: "1111.2222" },
    ]);

    await notifyClarificationViaSlack(
      baseOptions({
        questions: ["Q1?", "Q2?", "Q3?"],
      }),
    );

    const messageBody = calls[1].body as Record<string, unknown>;
    const blocks = messageBody.blocks as Array<Record<string, unknown>>;
    const questionBlock = blocks.find(
      (b) =>
        b.type === "section" &&
        typeof (b.text as Record<string, unknown>)?.text === "string" &&
        ((b.text as Record<string, unknown>).text as string).includes("1."),
    );
    assert.ok(questionBlock, "Should have a numbered question block");
    const text = (questionBlock!.text as Record<string, unknown>)
      .text as string;
    assert.ok(text.includes("1. Q1?"));
    assert.ok(text.includes("2. Q2?"));
    assert.ok(text.includes("3. Q3?"));
  });
});

// ── Reply Poller Tests ──

describe("pollForSlackReply", () => {
  it("returns reply when human responds in thread", async () => {
    const parentTs = "1000.0001";

    installMockFetch([
      {
        ok: true,
        messages: [
          { ts: parentTs, user: "UBOT", text: "Clarification Needed" },
          { ts: "1000.0050", user: "UHUMAN", text: "Use PostgreSQL please" },
        ],
      },
    ]);

    const result = await pollForSlackReply({
      botToken: "xoxb-test",
      channelId: "D999",
      messageTs: parentTs,
      timeoutSeconds: 1,
      pollIntervalSeconds: 0.1,
    });

    assert.equal(result.replied, true);
    assert.equal(result.replyText, "Use PostgreSQL please");
    assert.equal(result.timedOut, false);
  });

  it("filters out bot messages by bot_id", async () => {
    const parentTs = "1000.0001";

    installMockFetch([
      {
        ok: true,
        messages: [
          { ts: parentTs, user: "UBOT", text: "Questions" },
          {
            ts: "1000.0020",
            user: "UBOT",
            bot_id: "B123",
            text: "Bot auto-reply",
          },
          { ts: "1000.0050", user: "UHUMAN", text: "Real answer" },
        ],
      },
    ]);

    const result = await pollForSlackReply({
      botToken: "xoxb-test",
      channelId: "D999",
      messageTs: parentTs,
      timeoutSeconds: 1,
      pollIntervalSeconds: 0.1,
    });

    assert.equal(result.replied, true);
    assert.equal(result.replyText, "Real answer");
  });

  it("filters out bot messages by botUserId", async () => {
    const parentTs = "1000.0001";

    installMockFetch([
      {
        ok: true,
        messages: [
          { ts: parentTs, user: "UBOT", text: "Questions" },
          { ts: "1000.0020", user: "UBOT", text: "Bot followup" },
          { ts: "1000.0050", user: "UHUMAN", text: "Human answer" },
        ],
      },
    ]);

    const result = await pollForSlackReply({
      botToken: "xoxb-test",
      channelId: "D999",
      messageTs: parentTs,
      botUserId: "UBOT",
      timeoutSeconds: 1,
      pollIntervalSeconds: 0.1,
    });

    assert.equal(result.replied, true);
    assert.equal(result.replyText, "Human answer");
  });

  it("concatenates multiple human replies", async () => {
    const parentTs = "1000.0001";

    installMockFetch([
      {
        ok: true,
        messages: [
          { ts: parentTs, user: "UBOT", text: "Questions" },
          { ts: "1000.0050", user: "UHUMAN", text: "First part" },
          { ts: "1000.0060", user: "UHUMAN", text: "Second part" },
        ],
      },
    ]);

    const result = await pollForSlackReply({
      botToken: "xoxb-test",
      channelId: "D999",
      messageTs: parentTs,
      timeoutSeconds: 1,
      pollIntervalSeconds: 0.1,
    });

    assert.equal(result.replied, true);
    assert.equal(result.replyText, "First part\n\nSecond part");
  });

  it("times out when no reply arrives", async () => {
    const parentTs = "1000.0001";

    // Return only the parent message (no replies) on every poll
    installMockFetch(
      Array(20).fill({
        ok: true,
        messages: [
          { ts: parentTs, user: "UBOT", text: "Questions" },
        ],
      }),
    );

    const logs: string[] = [];
    const result = await pollForSlackReply(
      {
        botToken: "xoxb-test",
        channelId: "D999",
        messageTs: parentTs,
        timeoutSeconds: 0.3,
        pollIntervalSeconds: 0.1,
      },
      (line) => logs.push(line),
    );

    assert.equal(result.replied, false);
    assert.equal(result.timedOut, true);
    assert.ok(logs.some((l) => l.includes("Timed out")));
  });

  it("retries on API errors", async () => {
    const parentTs = "1000.0001";

    installMockFetch([
      { ok: false, error: "internal_error" },
      {
        ok: true,
        messages: [
          { ts: parentTs, user: "UBOT", text: "Questions" },
          { ts: "1000.0050", user: "UHUMAN", text: "Got it" },
        ],
      },
    ]);

    const logs: string[] = [];
    const result = await pollForSlackReply(
      {
        botToken: "xoxb-test",
        channelId: "D999",
        messageTs: parentTs,
        timeoutSeconds: 5,
        pollIntervalSeconds: 0.1,
      },
      (line) => logs.push(line),
    );

    assert.equal(result.replied, true);
    assert.equal(result.replyText, "Got it");
    assert.ok(logs.some((l) => l.includes("will retry")));
  });

  it("handles fetch exceptions without crashing", async () => {
    let callCount = 0;
    (globalThis as unknown as { fetch: typeof fetch }).fetch = (async () => {
      callCount++;
      if (callCount === 1) throw new Error("network down");
      return {
        json: async () => ({
          ok: true,
          messages: [
            { ts: "1000.0001", user: "UBOT", text: "Q" },
            { ts: "1000.0050", user: "UHUMAN", text: "Answer" },
          ],
        }),
      } as Response;
    }) as unknown as typeof fetch;

    const result = await pollForSlackReply({
      botToken: "xoxb-test",
      channelId: "D999",
      messageTs: "1000.0001",
      timeoutSeconds: 5,
      pollIntervalSeconds: 0.1,
    });

    assert.equal(result.replied, true);
    assert.equal(result.replyText, "Answer");
  });

  it("skips empty/whitespace-only replies", async () => {
    const parentTs = "1000.0001";

    installMockFetch([
      {
        ok: true,
        messages: [
          { ts: parentTs, user: "UBOT", text: "Questions" },
          { ts: "1000.0020", user: "UHUMAN", text: "   " },
          { ts: "1000.0030", user: "UHUMAN", text: "" },
        ],
      },
      {
        ok: true,
        messages: [
          { ts: parentTs, user: "UBOT", text: "Questions" },
          { ts: "1000.0020", user: "UHUMAN", text: "   " },
          { ts: "1000.0030", user: "UHUMAN", text: "" },
          { ts: "1000.0040", user: "UHUMAN", text: "Real answer" },
        ],
      },
    ]);

    const result = await pollForSlackReply({
      botToken: "xoxb-test",
      channelId: "D999",
      messageTs: parentTs,
      timeoutSeconds: 5,
      pollIntervalSeconds: 0.1,
    });

    assert.equal(result.replied, true);
    assert.equal(result.replyText, "Real answer");
  });
});
