import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createAdapter } from '../adapter.mjs';

const id = 'a'.repeat(64);
const options = { model: 'fixture', messages: [{ role: 'user', content: [{ type: 'text', text: 'independent fixture' }] }] };

test('a long-running job uses one POST and polls the same authenticated job with GET', async t => {
  const calls = []; let polls = 0;
  const server = createServer(async (req, res) => {
    for await (const part of req) {}
    calls.push({ method: req.method, path: req.url, auth: req.headers.authorization, async: req.headers['x-notion-async'] });
    if (req.method === 'POST' || ++polls < 3) {
      res.writeHead(202, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ object: 'notion.agent.job', job_id: id, status: 'running', poll_after_ms: 1 }));
    } else {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ choices: [{ message: { content: '{"text":"long job complete"}' } }] }));
    }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  const adapter = createAdapter({ credentials: { resolve: async () => 'fixture-key' } }, {
    baseURL: `http://127.0.0.1:${server.address().port}/v1`, timeoutMs: 10000,
  });
  const chunks = await Array.fromAsync(adapter.stream(options));
  assert.equal(chunks.find(x => x.type === 'text-delta').text, 'long job complete');
  assert.equal(calls.filter(c => c.method === 'POST').length, 1);
  assert.equal(calls.filter(c => c.method === 'GET').length, 3);
  assert.equal(calls[0].async, '1');
  assert.ok(calls.slice(1).every(c => c.path === `/v1/agent/jobs/${id}` && c.auth === 'Bearer fixture-key'));
});

test('an untrusted async job ID is rejected without sending credentials to another URL', async t => {
  let count = 0;
  const server = createServer((req, res) => {
    req.resume();count++;
    res.writeHead(202, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ job_id: 'https://attacker.example/private', status: 'running' }));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  const adapter = createAdapter({ credentials: { resolve: async () => 'fixture-key' } }, { baseURL: `http://127.0.0.1:${server.address().port}/v1` });
  await assert.rejects(Array.fromAsync(adapter.stream(options)), e => e.failure?.code === 'MALFORMED_RESPONSE');
  assert.equal(count, 1);
});

for (const truncatedStatus of [202, 200]) {
  test(`a disconnected ${truncatedStatus} GET body is safely retried without another POST`, async t => {
    let posts = 0, gets = 0;
    const server = createServer((req, res) => {
      req.resume();
      if (req.method === 'POST') {
        posts++; res.writeHead(202, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ object: 'notion.agent.job', job_id: id, status: 'running', poll_after_ms: 1 }));
      } else if (++gets === 1) {
        res.writeHead(truncatedStatus, { 'content-type': 'application/json' });
        res.write('{"incomplete":'); setTimeout(() => res.destroy(), 10);
      } else {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ choices: [{ message: { content: '{"text":"recovered"}' } }] }));
      }
    });
    server.listen(0, '127.0.0.1'); await once(server, 'listening');
    t.after(() => { server.closeAllConnections(); server.close(); });
    const adapter = createAdapter({ credentials: { resolve: async () => 'fixture-key' } }, { baseURL: `http://127.0.0.1:${server.address().port}/v1` });
    const chunks = await Array.fromAsync(adapter.stream(options));
    assert.equal(chunks.find(x => x.type === 'text-delta').text, 'recovered');
    assert.equal(posts, 1); assert.equal(gets, 2);
  });
}

test('cancelling local waiting does not resubmit or send an upstream cancellation', async t => {
  const controller = new AbortController(); const methods = [];
  const server = createServer((req, res) => {
    req.resume(); methods.push(req.method);
    res.writeHead(202, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ object: 'notion.agent.job', job_id: id, status: 'running', poll_after_ms: 1000 }));
    setTimeout(() => controller.abort(), 20);
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  const adapter = createAdapter({ credentials: { resolve: async () => 'fixture-key' } }, { baseURL: `http://127.0.0.1:${server.address().port}/v1` });
  await assert.rejects(Array.fromAsync(adapter.stream({ ...options, signal: controller.signal })), e => e.failure?.code === 'ABORTED');
  assert.deepEqual(methods, ['POST']);
});
