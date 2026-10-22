import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createAdapter, encodeRequest } from '../adapter.mjs';

const tools = [{ name:'read', description:'Read a local text file', parameters:{ type:'object' } }];
const input = { model:'fixture', sessionId:'independent-session', tools,
  messages:[{ role:'user', content:[{ type:'text', text:'Read the independent input' }] }] };

test('the local protocol preserves authoritative roles and tool-result identity', () => {
  const result = encodeRequest({ ...input, messages:[...input.messages,
    { role:'assistant', content:[{ type:'tool-call', id:'call_read', name:'read', arguments:'{"file_path":"input.txt"}' }] },
    { role:'tool', toolCallId:'call_read', isError:false, content:[{ type:'text', text:'UNSEEN-雪🐳-4821' }] },
  ] });
  const serialized = result.messages[0].content.split('DSH_REQUEST_JSON:\n')[1].split('\n\nRespond with')[0];
  const request = JSON.parse(serialized);
  assert.equal(request.protocol, 'dsh-local-tools-v2');
  assert.deepEqual(request.conversation.map(m => m.role), ['user','assistant','tool']);
  assert.equal(request.conversation[1].content[0].id, request.conversation[2].callId);
  assert.equal(request.conversation[2].content[0].text, 'UNSEEN-雪🐳-4821');
  assert.deepEqual(request.capabilities.inputModalities, ['text']);
});

test('a cached tool decision has stable IDs while another session or request stays distinct', async t => {
  const seen = [];
  const server = createServer(async (req,res) => {
    let raw='';for await (const piece of req) raw+=piece;
    seen.push({ body:JSON.parse(raw), protocol:req.headers['x-notion-local-protocol'] });
    res.writeHead(200,{'content-type':'application/json'});
    res.end(JSON.stringify({ choices:[{ message:{ content:JSON.stringify({ tool_calls:[
      { name:'read', arguments:{ file_path:'input.txt' } },
      { name:'read', arguments:{ file_path:'second.txt' } },
    ] }) } }] }));
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');
  t.after(()=>{server.closeAllConnections();server.close();});
  const adapter = createAdapter({ credentials:{resolve:async()=> 'fixture-key'} },
    { baseURL:`http://127.0.0.1:${server.address().port}/v1` });
  const run=async options=>(await Array.fromAsync(adapter.stream(options)))
    .filter(c=>c.type==='block-end').map(c=>c.block.id);
  const first=await run(input),same=await run(input);
  const other=await run({...input,sessionId:'another-session'});
  const next=await run({...input,messages:[...input.messages,{role:'user',content:[{type:'text',text:'Now read again'}]}]});
  assert.deepEqual(first,same);
  assert.equal(new Set(first).size,2);
  assert.ok(first.every(id=>/^call_[a-f0-9]{32}$/.test(id)));
  assert.notDeepEqual(first,other);assert.notDeepEqual(first,next);
  assert.ok(seen.every(r=>r.protocol==='dsh-local-tools-v2'));
});

test('an accepted job cannot return another job identity as a local decision', async t => {
  let posts=0,gets=0;
  const server=createServer(async(req,res)=>{
    for await(const _ of req){}
    res.setHeader('content-type','application/json');
    if(req.method==='POST'){
      posts++;res.writeHead(202);res.end(JSON.stringify({object:'notion.agent.job',job_id:'a'.repeat(64),status:'running',poll_after_ms:250}));
    }else{
      gets++;res.end(JSON.stringify({notion_job_id:'b'.repeat(64),choices:[{message:{content:'{"tool_calls":[{"name":"read","arguments":{}}]}'}}]}));
    }
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');
  t.after(()=>{server.closeAllConnections();server.close();});
  const adapter=createAdapter({credentials:{resolve:async()=> 'fixture-key'}},{baseURL:`http://127.0.0.1:${server.address().port}/v1`});
  await assert.rejects(Array.fromAsync(adapter.stream(input)),/job identity/);
  assert.equal(posts,1);assert.equal(gets,1);
});

test('retained job IDs namespace calls independently of JSON rendering', async t=>{
  let n=0;
  const server=createServer(async(req,res)=>{
    for await(const _ of req){}
    const argumentsValue=n++?{b:2,a:1}:{a:1,b:2};
    res.setHeader('content-type','application/json');
    res.end(JSON.stringify({notion_job_id:'c'.repeat(64),choices:[{message:{content:JSON.stringify({tool_calls:[{name:'read',arguments:argumentsValue}]})}}]}));
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');t.after(()=>{server.closeAllConnections();server.close();});
  const adapter=createAdapter({credentials:{resolve:async()=> 'fixture-key'}},{baseURL:`http://127.0.0.1:${server.address().port}/v1`});
  const id=async()=> (await Array.fromAsync(adapter.stream(input))).find(c=>c.type==='block-end').block.id;
  assert.equal(await id(),await id());
});
