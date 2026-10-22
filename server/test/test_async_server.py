import asyncio,importlib.util,json,logging,os,sys,tempfile,threading,types,unittest,time,uuid
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).parent
SOURCE=ROOT.parent
class UpstreamError(RuntimeError):
 def __init__(self,message,**kwargs):super().__init__(message);self.__dict__.update(kwargs)
sys.modules['app']=types.ModuleType('app');sys.modules['app'].__path__=[str(SOURCE/'app')]
sys.modules['app.notion_client']=types.SimpleNamespace(NotionUpstreamError=UpstreamError)
sys.modules['app.logger']=types.SimpleNamespace(logger=logging.getLogger('test'))
sys.modules['app.conversation']=types.SimpleNamespace(build_standard_transcript=lambda messages,model,account:[{'type':'config','value':{'model':model}},{'type':'user','value':[[json.dumps(messages)]]}])
sys.modules['app.model_registry']=types.SimpleNamespace(is_supported_model=lambda _:True)
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m
B=load('app.agent_bridge',SOURCE/'app/agent_bridge.py');A=load('agent_api',SOURCE/'app/api/agent.py')
class Response:
 status_code=200
 def __init__(self,data=None):self.data=data
 def json(self):return self.data
class Message:
 def __init__(self,text):self.text=text
 def model_dump(self):return {'role':'user','content':self.text}
def completed(text='{"text":"retained result"}'):return {'session':{'status':'idle'},'session_pending':False,'patches':[{'entity':{'kind':'assistant_message','content':[{'type':'text','text':text}]}}]}
def running():return {'session':{'status':'running'},'session_pending':False,'patches':[]}
class AsyncServerTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.p=patch.object(B,'STATE_DIR',Path(self.temp.name));self.p.start();self.addCleanup(self.p.stop)
  self.client=types.SimpleNamespace(space_id='fixture-space',user_id='fixture-user',cookies={})
  pool=types.SimpleNamespace(clients=[self.client],get_client=lambda **kwargs:self.client)
  self.request=types.SimpleNamespace(headers={'x-notion-async':'1','x-deepseek-harness-session-id':'fixture-scope'},app=types.SimpleNamespace(state=types.SimpleNamespace(account_pool=pool)))
  self.body=types.SimpleNamespace(model='fixture',conversation_id=None,stream=False,messages=[Message('fixture')])
  if hasattr(A,'_tasks'):A._tasks.clear()
 async def finish(self,id):
  for _ in range(200):
   r=await A.get_agent_job(self.request,id)
   if r.status_code!=202:return r
   await asyncio.sleep(.005)
  self.fail('job did not finish')
 async def test_retained_pending_can_be_checked_after_error_was_reported(self):
  id='f'*64
  account=A._account(self.client)
  B._save(Path(self.temp.name)/(account+'.json'),{'async_job_id':id,'phase':'pending','thread_id':'original','fingerprint':'old','created_at':0,'model':'fixture'})
  async def failed():return A._error(B.AgentRequestError('AGENT_REQUIRES_ACTION','fixture',409))
  task=asyncio.create_task(failed());await task
  A._tasks[account]={'job_id':id,'task':task,'model':'fixture'}
  first=await A.get_agent_job(self.request,id);self.assertEqual(first.status_code,409)
  with patch.object(A,'_schedule') as schedule:
   second=await A.get_agent_job(self.request,id)
  self.assertEqual(second.status_code,202);self.assertEqual(schedule.call_count,1)
 async def test_nine_minute_task_survives_without_a_nine_minute_http_connection(self):
  clock=[0];creates=[]
  def post(url,**kwargs):
   if url.endswith('createAgentThread'):creates.append(1);return Response()
   return Response(completed() if clock[0]>=540 else running())
  with patch.object(B.requests,'post',side_effect=post),patch.object(B,'time',types.SimpleNamespace(monotonic=lambda:clock[0],time=time.time,sleep=lambda s:clock.__setitem__(0,clock[0]+s))):
   start=await A.handle_agent_request(self.request,self.body)
   self.assertEqual(start.status_code,202)
   result=await self.finish(json.loads(start.body)['job_id'])
   self.assertEqual(result.status_code,200);self.assertGreaterEqual(clock[0],540)
   self.assertEqual(len(creates),1)
   self.assertEqual(json.loads(result.body)['notion_job_id'],json.loads(start.body)['job_id'])
 async def test_duplicate_post_and_competing_request_do_not_create_another_task(self):
  entered=threading.Event();release=threading.Event();creates=[]
  def post(url,**kwargs):
   if url.endswith('createAgentThread'):creates.append(1);entered.set();release.wait(2);return Response()
   return Response(completed())
  with patch.object(B.requests,'post',side_effect=post):
   start=await A.handle_agent_request(self.request,self.body);id=json.loads(start.body)['job_id']
   try:
    self.assertTrue(await asyncio.to_thread(entered.wait,1))
    again=await A.handle_agent_request(self.request,self.body);self.assertEqual(json.loads(again.body)['job_id'],id)
    self.body.messages=[Message('different')]
    conflict=await A.handle_agent_request(self.request,self.body);self.assertEqual(conflict.status_code,409)
   finally:release.set()
   self.assertEqual((await self.finish(id)).status_code,200)
  self.assertEqual(len(creates),1)
 async def test_restart_recovery_queries_only_retained_thread(self):
  id='a'*64
  account=B.hashlib.sha256(f'{self.client.space_id}:{self.client.user_id}'.encode()).hexdigest()
  B._save(Path(self.temp.name)/(account+'.json'),{'async_job_id':id,'phase':'pending','thread_id':'original-thread','fingerprint':'old','created_at':0,'model':'fixture'})
  with patch.object(B.requests,'post',return_value=Response(completed())) as post:
   result=await self.finish(id);self.assertEqual(result.status_code,200)
   self.assertTrue(all(c.args[0].endswith('getThreadTranscript') for c in post.call_args_list))
   self.assertTrue(all(c.kwargs['json']['threadId']=='original-thread' for c in post.call_args_list))
 async def test_legacy_client_gets_controlled_timeout_before_proxy_limit(self):
  clock=[0];self.request.headers={}
  def post(url,**kwargs):return Response() if url.endswith('createAgentThread') else Response(running())
  with patch.object(B.requests,'post',side_effect=post),patch.object(B,'time',types.SimpleNamespace(monotonic=lambda:clock[0],time=time.time,sleep=lambda s:clock.__setitem__(0,clock[0]+s))):
   result=await A.handle_agent_request(self.request,self.body)
   self.assertEqual(result.status_code,504);self.assertEqual(json.loads(result.body)['error']['code'],'AGENT_PENDING');self.assertLess(clock[0],300)
 async def test_completed_result_survives_the_next_account_job_and_memory_reset(self):
  replies={};creates=[]
  def post(url,**kwargs):
   if url.endswith('createAgentThread'):
    creates.append(kwargs['json']['threadId'])
    replies[creates[-1]]=json.dumps({'text':'result A' if len(creates)==1 else 'result B'})
    return Response()
   return Response(completed(replies[kwargs['json']['threadId']]))
  with patch.object(B.requests,'post',side_effect=post):
   first=await A.handle_agent_request(self.request,self.body);first_id=json.loads(first.body)['job_id']
   await A._tasks[A._account(self.client)]['task']
   self.body.messages=[Message('second request')]
   second=await A.handle_agent_request(self.request,self.body);second_id=json.loads(second.body)['job_id']
   await A._tasks[A._account(self.client)]['task']
   A._tasks.clear();B._locks.clear()
   first_result=await A.get_agent_job(self.request,first_id)
   second_result=await A.get_agent_job(self.request,second_id)
  self.assertEqual(first_result.status_code,200)
  self.assertEqual(second_result.status_code,200)
  self.assertEqual(json.loads(first_result.body)['choices'][0]['message']['content'],'{"text": "result A"}')
  self.assertEqual(json.loads(second_result.body)['choices'][0]['message']['content'],'{"text": "result B"}')
  self.assertEqual(len(creates),2)
  for path in Path(self.temp.name).rglob('*.json'):
   self.assertEqual(path.stat().st_mode & 0o777,0o600)
 async def test_accepted_queued_job_recovers_before_worker_start_and_submits_only_once(self):
  with patch.object(A,'_schedule'),patch.object(B.requests,'post') as post:
   accepted=await A.handle_agent_request(self.request,self.body)
   self.assertEqual(accepted.status_code,202)
   id=json.loads(accepted.body)['job_id']
   retained=B.read_agent_job(self.client,id)
   self.assertIsNotNone(retained)
   self.assertEqual(retained['phase'],'queued')
   self.assertTrue(retained['text']);self.assertEqual(retained['model'],'fixture')
   uuid.UUID(retained['thread_id']);uuid.UUID(retained['request_id'])
   self.body.messages=[Message('competing queued request')]
   self.assertEqual((await A.handle_agent_request(self.request,self.body)).status_code,409)
   post.assert_not_called()
  A._tasks.clear();B._locks.clear();creates=[]
  def post(url,**kwargs):
   if url.endswith('createAgentThread'):creates.append(kwargs['json']);return Response()
   return Response(completed())
  self.body.messages=[Message('fixture')]
  with patch.object(B.requests,'post',side_effect=post):
   result=await self.finish(id)
   self.assertEqual(result.status_code,200)
   A._tasks.clear();B._locks.clear()
   self.assertEqual((await A.get_agent_job(self.request,id)).status_code,200)
   self.assertEqual((await A.handle_agent_request(self.request,self.body)).status_code,200)
  self.assertEqual(len(creates),1)
  self.assertEqual(creates[0]['threadId'],retained['thread_id'])
  self.assertEqual(creates[0]['clientMessageId'],retained['request_id'])
 async def test_completed_cache_expiration_does_not_remove_a_queued_task(self):
  def post(url,**kwargs):return Response() if url.endswith('createAgentThread') else Response(completed())
  with patch.object(B.requests,'post',side_effect=post):
   accepted=await A.handle_agent_request(self.request,self.body);old_id=json.loads(accepted.body)['job_id']
   self.assertEqual((await self.finish(old_id)).status_code,200)
  old=B.read_agent_job(self.client,old_id)
  with patch.object(B,'time',types.SimpleNamespace(time=lambda:old['completed_at']+86401)):
   self.assertEqual((await A.get_agent_job(self.request,old_id)).status_code,404)
  self.body.messages=[Message('new queued task')]
  with patch.object(A,'_schedule'):
   accepted=await A.handle_agent_request(self.request,self.body);queued_id=json.loads(accepted.body)['job_id']
  with patch.object(B,'time',types.SimpleNamespace(time=lambda:old['completed_at']+86401)):
   self.assertIsNone(B.read_agent_job(self.client,old_id))
   self.assertEqual(B.read_agent_job(self.client,queued_id)['phase'],'queued')
  account=B.hashlib.sha256(f'{self.client.space_id}:{self.client.user_id}'.encode()).hexdigest()
  self.assertEqual(json.loads((Path(self.temp.name)/(account+'.json')).read_text())['async_job_id'],queued_id)
 async def test_replacing_a_legacy_done_index_archives_its_original_async_id(self):
  old_id='c'*64
  account=B.hashlib.sha256(f'{self.client.space_id}:{self.client.user_id}'.encode()).hexdigest()
  B._save(Path(self.temp.name)/(account+'.json'),{'async_job_id':old_id,'phase':'done',
   'thread_id':'legacy-thread','fingerprint':'legacy-fingerprint','created_at':0,
   'model':'fixture','reply':'{"text":"legacy retained result"}'})
  with patch.object(A,'_schedule'),patch.object(B.requests,'post') as post:
   accepted=await A.handle_agent_request(self.request,self.body)
   self.assertEqual(accepted.status_code,202)
   A._tasks.clear();B._locks.clear()
   retained=await A.get_agent_job(self.request,old_id)
   self.assertEqual(retained.status_code,200)
   self.assertEqual(json.loads(retained.body)['choices'][0]['message']['content'],'{"text":"legacy retained result"}')
   self.assertEqual(B.read_agent_job(self.client,json.loads(accepted.body)['job_id'])['phase'],'queued')
   post.assert_not_called()
if __name__=='__main__':unittest.main()
