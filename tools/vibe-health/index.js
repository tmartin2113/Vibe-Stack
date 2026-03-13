#!/usr/bin/env node

'use strict';

const http = require('http');
const { execSync } = require('child_process');
const fs = require('fs');

const DOCKER_COMPOSE_FILE = require('path').resolve(__dirname, '..', '..', 'docker-compose.yml');
const DC_BASE = `docker compose -f ${DOCKER_COMPOSE_FILE}`;

// --- HTTP check ---

function httpGet(url, timeoutMs = 5000) {
  return new Promise((resolve) => {
    const start = Date.now();
    const req = http.get(url, (res) => {
      res.resume(); // drain
      resolve({ ok: true, responseTimeMs: Date.now() - start });
    });
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      resolve({ ok: false, responseTimeMs: Date.now() - start, error: 'timeout' });
    });
    req.on('error', (err) => {
      resolve({ ok: false, responseTimeMs: Date.now() - start, error: err.message });
    });
  });
}

// --- Docker compose exec check ---

function dockerExec(service, innerCmd, timeoutMs = 10000) {
  const start = Date.now();
  try {
    execSync(`${DC_BASE} exec -T ${service} ${innerCmd}`, {
      timeout: timeoutMs,
      stdio: 'pipe',
    });
    return { ok: true, responseTimeMs: Date.now() - start };
  } catch (err) {
    return {
      ok: false,
      responseTimeMs: Date.now() - start,
      error: (err.stderr ? err.stderr.toString().trim() : err.message) || 'command failed',
    };
  }
}

// --- Docker compose ps check ---

function dockerPs(timeoutMs = 10000) {
  const start = Date.now();
  try {
    const out = execSync(`${DC_BASE} ps --format json`, {
      timeout: timeoutMs,
      stdio: 'pipe',
    }).toString().trim();

    // Each line is a JSON object (docker compose ps --format json outputs JSONL)
    const lines = out.split('\n').filter(Boolean);
    const containers = lines.map((l) => {
      try { return JSON.parse(l); } catch { return null; }
    }).filter(Boolean);

    const allRunning = containers.length > 0 && containers.every((c) => {
      const state = (c.State || c.status || '').toLowerCase();
      return state === 'running';
    });

    return {
      ok: allRunning,
      responseTimeMs: Date.now() - start,
      error: allRunning ? undefined : `containers not all running: ${containers.map(c => `${c.Name||c.name}=${c.State||c.status}`).join(', ')}`,
    };
  } catch (err) {
    return {
      ok: false,
      responseTimeMs: Date.now() - start,
      error: (err.stderr ? err.stderr.toString().trim() : err.message) || 'command failed',
    };
  }
}

// --- Run all checks ---

async function runChecks() {
  const timestamp = new Date().toISOString();

  const [paperclip, vllm] = await Promise.all([
    httpGet('http://127.0.0.1:3100/api/health'),
    httpGet('http://127.0.0.1:8000/v1/models'),
  ]);

  const deerflowLanggraph = dockerExec('deerflow-langgraph', 'curl -s http://localhost:2024/ok');
  const deerflowGateway   = dockerExec('deerflow-gateway',   'curl -s http://localhost:8001/health');
  const postgres          = dockerExec('db',                 'pg_isready -U paperclip');
  const dockerContainers  = dockerPs();

  const results = [
    { service: 'Paperclip',           ...paperclip,          timestamp },
    { service: 'vLLM',                ...vllm,               timestamp },
    { service: 'DeerFlow LangGraph',  ...deerflowLanggraph,  timestamp },
    { service: 'DeerFlow Gateway',    ...deerflowGateway,    timestamp },
    { service: 'PostgreSQL',          ...postgres,           timestamp },
    { service: 'Docker Containers',   ...dockerContainers,   timestamp },
  ].map(({ ok, service, responseTimeMs, timestamp, error }) => ({
    service,
    status: ok ? 'UP' : 'DOWN',
    responseTimeMs,
    timestamp,
    ...(error ? { error } : {}),
  }));

  return results;
}

// --- Output helpers ---

function printTable(results) {
  const COL_SERVICE = 22;
  const COL_STATUS  = 7;
  const COL_TIME    = 18;
  const COL_TS      = 26;

  const header =
    'Service'.padEnd(COL_SERVICE) +
    'Status'.padEnd(COL_STATUS) +
    'Response Time (ms)'.padEnd(COL_TIME) +
    'Timestamp';

  const sep = '-'.repeat(COL_SERVICE + COL_STATUS + COL_TIME + COL_TS);

  console.log(header);
  console.log(sep);

  for (const r of results) {
    const statusLabel = r.status === 'UP' ? 'UP' : 'DOWN';
    console.log(
      r.service.padEnd(COL_SERVICE) +
      statusLabel.padEnd(COL_STATUS) +
      String(r.responseTimeMs).padEnd(COL_TIME) +
      r.timestamp +
      (r.error ? `  [${r.error}]` : '')
    );
  }
}

function printJson(results) {
  console.log(JSON.stringify(results, null, 2));
}

function appendLog(path, results) {
  const line = JSON.stringify({ runAt: new Date().toISOString(), results }) + '\n';
  fs.appendFileSync(path, line, 'utf8');
}

// --- Main ---

(async () => {
  const args = process.argv.slice(2);
  const jsonMode = args.includes('--json');
  const logIdx = args.indexOf('--log');
  const logPath = logIdx !== -1 ? args[logIdx + 1] : null;

  const results = await runChecks();

  if (jsonMode) {
    printJson(results);
  } else {
    printTable(results);
  }

  if (logPath) {
    appendLog(logPath, results);
  }

  const allUp = results.every((r) => r.status === 'UP');
  process.exit(allUp ? 0 : 1);
})();
