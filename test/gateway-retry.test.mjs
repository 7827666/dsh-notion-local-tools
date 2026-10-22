import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const source = process.env.ADAPTER_SOURCE
  ?? new URL('../adapter.mjs', import.meta.url).href;
const { createAdapter } = await import(source.startsWith('file:') ? source : pathToFileURL(resolve(source)).href);
const jobId = 'a'.repeat(64);
const options = {
  model: 'fixture',
  messages: [{ role: 'user', content: [{ type: 'text', text: 'local gateway regression fixture' }] }],
};

function json(res, status, value) {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(value));
}

function accepted(res, id = jobId) {
  json(res, 202, { object: 'notion.agent.job', job_id: id, status: 'running', poll_after_ms: 1 });
}

function completed(res) {
  json(res, 200, { choices: [{ message: { content: '{"text":"fixture recovered"}' } }] });
}

function gateway(res, status) {
  res.writeHead(status, { 'content-type': 'text/html' });
  res.end('synthetic temporary gateway response');
}

async function fixture(t, handle) {
  const calls = [];
  const server = createServer((req, res) => {
    req.resume();
    calls.push({ method: req.method, path: req.url, auth: req.headers.authorization });
    handle(req, res, calls.filter(call => call.method === 'GET').length);
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => {
    t.diagnostic(`fixture calls: POST=${calls.filter(call => call.method === 'POST').length} GET=${calls.filter(call => call.method === 'GET').length}`);
    server.closeAllConnections();
    return new Promise(resolveClose => server.close(resolveClose));
  });
  const adapter = createAdapter({ credentials: { resolve: async () => 'fixture-key' } }, {
    baseURL: `http://127.0.0.1:${server.address().port}/v1`, timeoutMs: 8000,
  });
  return { adapter, calls, baseURL: `http://127.0.0.1:${server.address().port}/v1` };
}

function assertOnlyOriginalJob(calls, gets) {
  assert.equal(calls.filter(call => call.method === 'POST').length, 1);
  const reads = calls.filter(call => call.method === 'GET');
  if (gets !== undefined) assert.equal(reads.length, gets);
  assert.ok(calls.every(call => call.method === 'POST' || call.method === 'GET'));
  assert.ok(reads.every(call => call.path === `/v1/agent/jobs/${jobId}`));
  assert.ok(calls.every(call => call.auth === 'Bearer fixture-key'));
}

for (const status of [502, 503, 504]) {
  test(`an accepted job survives a generic GET ${status} without another POST`, { timeout: 10000 }, async t => {
    const { adapter, calls } = await fixture(t, (req, res, gets) => {
      if (req.method === 'POST') accepted(res);
      else if (gets === 1) gateway(res, status);
      else completed(res);
    });
    const chunks = await Array.fromAsync(adapter.stream(options));
    assert.equal(chunks.find(chunk => chunk.type === 'text-delta')?.text, 'fixture recovered');
    assertOnlyOriginalJob(calls, 2);
  });
}

for (const failureKind of ['gateway', 'transport']) {
  test(`persistent ${failureKind} GET failures wait for the overall deadline instead of exhausting three retries`, { timeout: 9000 }, async t => {
    const { calls, baseURL } = await fixture(t, (req, res) => {
      if (req.method === 'POST') accepted(res);
      else if (failureKind === 'gateway') gateway(res, 502);
      else req.socket.destroy();
    });
    const timeoutMs = 5000;
    const adapter = createAdapter({ credentials: { resolve: async () => 'fixture-key' } }, { baseURL, timeoutMs });
    const started = performance.now();
    await assert.rejects(Array.fromAsync(adapter.stream(options)), error => {
      assert.ok(['TRANSPORT', 'TIMEOUT'].includes(error.failure?.code));
      assert.match(error.message, /timed out/i);
      assert.match(error.message, /retained|may still arrive|may still be running/i);
      return true;
    });
    assert.ok(performance.now() - started >= timeoutMs - 500, 'must use the overall wait budget');
    assert.ok(performance.now() - started < timeoutMs + 3000, 'must stop at the overall deadline');
    assertOnlyOriginalJob(calls);
    assert.ok(calls.some(call => call.method === 'GET'));
  });
}

test('cancellation during gateway recovery stops waiting without another submission', { timeout: 5000 }, async t => {
  const controller = new AbortController();
  let cancelTimer;
  t.after(() => clearTimeout(cancelTimer));
  const { adapter, calls } = await fixture(t, (req, res) => {
    if (req.method === 'POST') accepted(res);
    else {
      gateway(res, 503);
      cancelTimer = setTimeout(() => controller.abort(), 20);
    }
  });
  await assert.rejects(Array.fromAsync(adapter.stream({ ...options, signal: controller.signal })),
    error => error.failure?.code === 'ABORTED');
  assertOnlyOriginalJob(calls, 1);
});

for (const code of ['AGENT_PENDING', 'AGENT_UNKNOWN', 'AGENT_AUTH', 'AGENT_NOT_FOUND']) {
  test(`a structured ${code} response remains terminal even with HTTP 504`, { timeout: 5000 }, async t => {
    const { adapter, calls } = await fixture(t, (req, res) => {
      if (req.method === 'POST') accepted(res);
      else json(res, 504, { error: { code, message: 'sensitive-fixture-message', detail: 'sensitive-fixture-detail' } });
    });
    await assert.rejects(Array.fromAsync(adapter.stream(options)), error => {
      assert.equal(error.failure?.status, 504);
      assert.doesNotMatch(error.message, /sensitive-fixture/);
      return true;
    });
    assertOnlyOriginalJob(calls, 1);
  });
}

for (const status of [401, 403, 404, 409]) {
  test(`GET HTTP ${status} is not retried`, { timeout: 5000 }, async t => {
    const { adapter, calls } = await fixture(t, (req, res) => {
      if (req.method === 'POST') accepted(res);
      else json(res, status, { error: { code: 'TERMINAL_FIXTURE' } });
    });
    await assert.rejects(Array.fromAsync(adapter.stream(options)), error => error.failure?.status === status);
    assertOnlyOriginalJob(calls, 1);
  });
}

test('a changed GET job identity fails closed without another GET or POST', { timeout: 5000 }, async t => {
  const { adapter, calls } = await fixture(t, (req, res) => {
    if (req.method === 'POST') accepted(res);
    else accepted(res, 'b'.repeat(64));
  });
  await assert.rejects(Array.fromAsync(adapter.stream(options)), error => error.failure?.code === 'MALFORMED_RESPONSE');
  assertOnlyOriginalJob(calls, 1);
});

for (const status of [202, 200]) {
  test(`a complete but malformed GET HTTP ${status} body is not treated as a transient gateway failure`, { timeout: 5000 }, async t => {
    const { adapter, calls } = await fixture(t, (req, res) => {
      if (req.method === 'POST') accepted(res);
      else { res.writeHead(status, { 'content-type': 'application/json' }); res.end('malformed-complete-fixture'); }
    });
    await assert.rejects(Array.fromAsync(adapter.stream(options)), error => error.failure?.code === 'MALFORMED_RESPONSE');
    assertOnlyOriginalJob(calls, 1);
  });
}

test('an initial POST HTTP 502 is never automatically resubmitted', { timeout: 5000 }, async t => {
  const { adapter, calls } = await fixture(t, (_req, res) => gateway(res, 502));
  await assert.rejects(Array.fromAsync(adapter.stream(options)), error => error.failure?.status === 502);
  assertOnlyOriginalJob(calls, 0);
});
