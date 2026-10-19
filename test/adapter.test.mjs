import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { encodeRequest, decodeReply, createAdapter } from '../adapter.mjs';

const tools = [{ name: 'read', description: 'Read a Mac file', parameters: { type: 'object', required: ['path'], properties: { path: { type: 'string' } } } }];
const input = {
  provider: 'notion-local-tools', model: 'claude-sonnet5.5', tools,
  messages: [
    { role: 'user', content: [{ type: 'text', text: 'Read input.txt' }] },
    { role: 'assistant', content: [{ type: 'tool-call', id: 'call_previous', name: 'read', arguments: '{"path":"input.txt"}' }] },
    { role: 'tool', source: { callId: 'call_previous' }, isError: false, content: [{ type: 'text', text: 'UNSEEN-4821' }] },
  ],
};

test('notion wire preserves local tool definitions, call identity and returned file content in one user message', () => {
  const body = encodeRequest(input);
  assert.equal(body.stream, false);
  assert.equal(body.messages.length, 1);
  assert.equal(body.messages[0].role, 'user');
  assert.ok(body.messages[0].content.includes('UNSEEN-4821'));
  assert.ok(body.messages[0].content.includes('call_previous'));
  assert.ok(body.messages[0].content.includes('Read a Mac file'));
  assert.ok(!('tools' in body));
  assert.throws(() => encodeRequest({ ...input, messages: [{ role: 'user', content: [{ type: 'image' }] }] }), /text|image/i);
});

test('only complete structured output can request an advertised local tool', () => {
  assert.deepEqual(decodeReply('```json\n{"tool_calls":[{"name":"read","arguments":{"path":"input.txt"}}]}\n```', tools), {
    toolCalls: [{ name: 'read', arguments: '{"path":"input.txt"}' }],
  });
  assert.throws(() => decodeReply('{"tool_calls":[{"name":"bash","arguments":{"cmd":"rm -rf /"}}]}', tools), /advertised|unknown/i);
  assert.throws(() => decodeReply('example: {"tool_calls":[{"name":"read","arguments":{}}]}', tools), /JSON|protocol/i);
  assert.throws(() => decodeReply('{"tool_calls":[{"name":"read","arguments":null}]}', tools), /arguments/i);
  assert.deepEqual(decodeReply('{"text":"Read and verified"}', tools), { text: 'Read and verified' });
});

test('real HTTP transport produces DSH tool-call chunks and sanitizes upstream failures', async (t) => {
  let status = 200;
  let upstreamCode;
  const requests = [];
  const server = createServer(async (req, res) => {
    let raw = '';
    for await (const part of req) raw += part;
    requests.push({ headers: req.headers, body: JSON.parse(raw) });
    res.writeHead(status, { 'content-type': 'application/json' });
    res.end(JSON.stringify(status === 200 ? {
      choices: [{ message: { content: '{"tool_calls":[{"name":"read","arguments":{"path":"input.txt"}}]}' } }],
    } : { error: { code: upstreamCode, message: 'sensitive-upstream-body' } }));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const adapter = createAdapter({ credentials: { resolve: async () => ({ value: 'fixture-key' }) } }, {
    baseURL: `http://127.0.0.1:${server.address().port}/v1`, apiKeyEnv: 'TEST_KEY',
  });
  const chunks = await Array.fromAsync(adapter.stream(input));
  const block = chunks.find(c => c.type === 'block-end').block;
  assert.equal(block.type, 'tool-call');
  assert.equal(block.name, 'read');
  assert.deepEqual(JSON.parse(block.arguments), { path: 'input.txt' });
  assert.match(block.id, /^call_/);
  assert.deepEqual(chunks.at(-1), { type: 'finish', reason: { kind: 'tool-calls' } });
  assert.equal(requests[0].headers.authorization, 'Bearer fixture-key');
  assert.match(requests[0].headers['user-agent'], /deepseek-harness/);
  assert.ok(requests[0].body.messages[0].content.includes('UNSEEN-4821'));
  status = 401;
  await assert.rejects(async () => Array.fromAsync(adapter.stream(input)), e => e.code === 'AUTH' && !e.message.includes('sensitive-upstream-body'));
  status = 502;
  await assert.rejects(async () => Array.fromAsync(adapter.stream(input)), e => e.failure?.code === 'SERVER' && e.failure?.status === 502 && !e.message.includes('sensitive-upstream-body'));
  status = 409; upstreamCode = 'AGENT_BUSY';
  await assert.rejects(async () => Array.fromAsync(adapter.stream(input)), e => e.failure?.code === 'INVALID_REQUEST' && /no new task/.test(e.message) && !e.message.includes('sensitive-upstream-body'));
  status = 504; upstreamCode = 'AGENT_UNKNOWN';
  await assert.rejects(async () => Array.fromAsync(adapter.stream(input)), e => e.failure?.code === 'TIMEOUT' && /retained/.test(e.message) && !e.message.includes('sensitive-upstream-body'));
});

test('DSH session title is generated locally without credentials or an upstream request', async () => {
  const adapter = createAdapter({ credentials: { resolve() { throw new Error('title must stay local'); } } });
  const chunks = await Array.fromAsync(adapter.stream({
    purpose: 'session-title',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'Generate the session title from this JSON array of human messages:\n' + JSON.stringify([{ seq: 1, text: '读取独立测试文件并保存结果' }]) }] }],
  }));
  assert.equal(chunks.find(c => c.type === 'text-delta').text, '读取独立测试文件并保存结果');
  assert.equal(chunks.at(-1).reason.kind, 'stop');
});
