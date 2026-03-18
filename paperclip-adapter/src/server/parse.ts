/**
 * Parse Genesia heartbeat JSON output from stdout.
 *
 * Genesia may emit log lines before the JSON result. This parser
 * finds the last valid JSON object in stdout.
 */

export interface ClarificationRequest {
  questions: string[];
  blockingNode: string;
  contextSummary: string;
}

export interface GenesiaResult {
  resultJson: Record<string, unknown> | null;
  usage: { inputTokens: number; outputTokens: number } | null;
  summary: string;
  costCents: number;
  provider: string;
  model: string;
  clarification: ClarificationRequest | null;
}

export function parseGenesiaOutput(stdout: string): GenesiaResult {
  const fallback: GenesiaResult = {
    resultJson: null,
    usage: null,
    summary: "",
    costCents: 0,
    provider: "",
    model: "",
    clarification: null,
  };

  if (!stdout.trim()) return fallback;

  // Find the last JSON object in stdout (Genesia prints logs before the result)
  const lines = stdout.split("\n");
  let jsonStr = "";

  // Try to find a complete JSON block (may span multiple lines)
  let braceDepth = 0;
  let jsonStart = -1;
  let lastJsonEnd = -1;
  let lastJsonStart = -1;

  let inString = false;
  let escaped = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    for (let j = 0; j < line.length; j++) {
      const ch = line[j];

      if (escaped) {
        escaped = false;
        continue;
      }

      if (ch === "\\" && inString) {
        escaped = true;
        continue;
      }

      if (ch === '"') {
        inString = !inString;
        continue;
      }

      if (inString) continue;

      if (ch === "{") {
        if (braceDepth === 0) jsonStart = i;
        braceDepth++;
      } else if (ch === "}") {
        braceDepth--;
        if (braceDepth === 0 && jsonStart >= 0) {
          lastJsonStart = jsonStart;
          lastJsonEnd = i;
          jsonStart = -1;
        }
      }
    }
  }

  if (lastJsonStart >= 0 && lastJsonEnd >= 0) {
    jsonStr = lines.slice(lastJsonStart, lastJsonEnd + 1).join("\n");
  }

  if (!jsonStr) return fallback;

  try {
    const parsed = JSON.parse(jsonStr) as Record<string, unknown>;
    const usage = parsed.usage as Record<string, number> | undefined;

    // Extract clarification request if present
    const rawClarification = parsed.clarification as
      | Record<string, unknown>
      | undefined;
    const clarification: ClarificationRequest | null = rawClarification
      ? {
          questions: Array.isArray(rawClarification.questions)
            ? (rawClarification.questions as string[])
            : [],
          blockingNode: String(rawClarification.blocking_node ?? ""),
          contextSummary: String(rawClarification.context_summary ?? ""),
        }
      : null;

    return {
      resultJson: parsed,
      usage: usage
        ? {
            inputTokens: usage.input_tokens ?? 0,
            outputTokens: usage.output_tokens ?? 0,
          }
        : null,
      summary: String(parsed.summary ?? ""),
      costCents: Number(parsed.cost_cents ?? 0),
      provider: String(parsed.provider ?? ""),
      model: String(parsed.model ?? ""),
      clarification,
    };
  } catch {
    return fallback;
  }
}
