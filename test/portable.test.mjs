import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createAdapter, normalizeBaseURL } from '../adapter.mjs';
import { Config } from '../index.mjs';

test('endpoint must be supplied locally and cannot contain credentials or query secrets', () => {
  assert.throws(() => normalizeBaseURL(undefined), /configure|address|baseURL/i);
  assert.throws(() => normalizeBaseURL('https://name:secret@example.com/v1'), /credentials|baseURL/i);
  assert.throws(() => normalizeBaseURL('https://example.com/v1?key=secret'), /query|baseURL/i);
  assert.throws(() => normalizeBaseURL('file:///tmp/server'), /HTTP|baseURL/i);
  assert.equal(normalizeBaseURL('https://example.com/v1/chat/completions/'), 'https://example.com/v1');
  assert.equal(normalizeBaseURL('http://127.0.0.1:9999/v1/'), 'http://127.0.0.1:9999/v1');
});

test('catalog supports the objects used by the native DSH Models form', async () => {
  const adapter = createAdapter({}, {
    models: [{ id: 'claude-sonnet5.5', name: 'Sonnet 5.5' }, { id: 'claude-opus5.5', name: 'Opus 5.5' }],
    displayName: 'Notion local tools',
  });
  assert.deepEqual((await adapter.listModels('notion-local-tools')).map(x => [x.id, x.name]), [
    ['claude-sonnet5.5', 'Sonnet 5.5'], ['claude-opus5.5', 'Opus 5.5'],
  ]);
  assert.equal(adapter.providerInfo('notion-local-tools').name, 'Notion local tools');
});

test('an unconfigured installation fails before resolving or sending credentials', async () => {
  const adapter = createAdapter({ credentials: { resolve() { throw new Error('credential lookup must not happen'); } } });
  await assert.rejects(async () => Array.fromAsync(adapter.stream({ model: 'claude-sonnet5.5', messages: [] })),
    e => e.failure?.code === 'MISSING_ENDPOINT');
});

test('body-phase timeout and cancellation remain transport and aborted failures', async (t) => {
  let headersSent;
  const server = createServer((req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.write('{"choices":');
    headersSent?.();
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  const adapter = createAdapter({ credentials: { resolve: async () => 'fixture-key' } }, {
    baseURL: `http://127.0.0.1:${server.address().port}/v1`, timeoutMs: 150,
  });
  await assert.rejects(Array.fromAsync(adapter.stream({ model: 'fixture', messages: [] })),
    error => error.failure?.code === 'TRANSPORT' && /still be running/.test(error.message)
      && /Do not automatically resubmit/.test(error.message));
  const controller = new AbortController();
  const ready = new Promise(resolve => { headersSent = resolve; });
  const result = Array.fromAsync(adapter.stream({ model: 'fixture', messages: [], signal: controller.signal }));
  const assertion = assert.rejects(result, error => error.failure?.code === 'ABORTED');
  await ready;
  controller.abort();
  await assertion;
});

test('a prepared call keeps its endpoint, credential reference and model configuration generation', async () => {
  let current = { baseURL: 'https://first.example/v1', apiKeyEnv: 'FIRST', models: [{ id: 'fixture', contextWindow: 100 }] };
  const refs = [];
  const adapter = createAdapter({ credentials: { resolve: async ref => { refs.push(ref); return undefined; } } }, () => current);
  const prepared = await adapter.prepareCall('notion-local-tools', 'fixture');
  current.models[0].contextWindow = 200;
  current = { baseURL: 'https://second.example/v1', apiKeyEnv: 'SECOND', models: [{ id: 'fixture', contextWindow: 300 }] };
  assert.equal(prepared.model.context.contextWindow, 100);
  await assert.rejects(Array.fromAsync(prepared.stream({ model: 'fixture', messages: [] })), error => error.failure?.code === 'AUTH');
  assert.deepEqual(refs, ['FIRST']);
  assert.equal((await adapter.resolveModel('notion-local-tools', 'fixture')).context.contextWindow, 300);
});

test('provider policy prevents DSH from automatically replaying ambiguous upstream failures', () => {
  const policy = createAdapter({}).providerRetryPolicy();
  assert.equal(policy?.mode, 'normal');
  assert.equal(policy?.maxRetries, 0);
  assert.ok(policy.retryableCodes.includes('SERVER'));
  assert.ok(policy.retryableCodes.includes('TRANSPORT'));
  assert.ok(policy.initialDelayMs > 0 && policy.maxDelayMs >= policy.initialDelayMs);
  assert.ok(policy.jitterRatio >= 0 && policy.jitterRatio <= 1);
});

test('requests allow the server budget by default but honor a shorter configured timeout', async (t) => {
  const server = createServer((req, res) => {
    req.resume();
    setTimeout(() => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ choices: [{ message: { content: '{"text":"fixture complete"}' } }] }));
    }, 50);
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  const durations = [];
  const originalTimeout = AbortSignal.timeout;
  t.mock.method(AbortSignal, 'timeout', ms => { durations.push(ms); return originalTimeout(ms); });
  const ctx = { credentials: { resolve: async () => 'fixture-key' } };
  const config = { baseURL: `http://127.0.0.1:${server.address().port}/v1` };
  const input = { model: 'fixture', messages: [] };
  const completed = await Array.fromAsync(createAdapter(ctx, config).stream(input));
  assert.equal(completed.at(-1).reason.kind, 'stop');
  assert.deepEqual(durations, [330000]);
  await assert.rejects(Array.fromAsync(createAdapter(ctx, { ...config, timeoutMs: 15 }).stream(input)),
    error => error.failure?.code === 'TRANSPORT' && /still be running/.test(error.message)
      && /Do not automatically resubmit/.test(error.message));
  assert.deepEqual(durations, [330000, 15]);
});

test('native timeout configuration uses the extended budget and accepts operator overrides', () => {
  assert.equal(Config({}).timeoutMs.get(), 330000);
  assert.equal(Config({ timeoutMs: 120000 }).timeoutMs.get(), 120000);
  assert.equal(Config({ timeoutMs: 600000 }).timeoutMs.get(), 600000);
  assert.throws(() => Config({ timeoutMs: 600001 }));
});
