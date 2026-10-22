import importlib.util
import logging
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

class UpstreamError(RuntimeError):
    def __init__(self, message, **kwargs):
        super().__init__(message)
        self.__dict__.update(kwargs)

sys.modules['app'] = types.ModuleType('app');sys.modules['app'].__path__=[str(Path(__file__).resolve().parents[1]/'app')]
sys.modules['app.notion_client'] = types.SimpleNamespace(NotionUpstreamError=UpstreamError)
sys.modules['app.logger'] = types.SimpleNamespace(logger=logging.getLogger('test'))
spec = importlib.util.spec_from_file_location('bridge', Path(__file__).resolve().parents[1]/'app/agent_bridge.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

class Response:
    status_code = 200
    def __init__(self, data=None): self.data = data
    def json(self): return self.data

def transcript(text='fixture'):
    return [{'type':'user','value':[[text]]}]

def reply(status='idle', text='FINAL', pending=False):
    return {'session': {'status': status}, 'session_pending': pending,
            'patches': [{'op':'put','entity':{'kind':'assistant_message', 'content':[{'type':'text','text':text}]}}]}

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = patch.object(b, 'STATE_DIR', Path(self.temp.name))
        self.state.start();self.addCleanup(self.state.stop)
        self.client = types.SimpleNamespace(space_id='space',user_id='user',cookies={},token_v2='fixture')

    def test_waits_beyond_old_30_seconds_and_ignores_intermediate_text(self):
        clock = [0]
        polls = []
        def post(url, **kwargs):
            if url.endswith('createAgentThread'): return Response()
            polls.append(clock[0])
            return Response(reply('running','PARTIAL') if clock[0] < 45 else reply())
        with patch.object(b.requests,'post',side_effect=post), patch.object(b.time,'monotonic',side_effect=lambda:clock[0]), patch.object(b.time,'sleep',side_effect=lambda s:clock.__setitem__(0,clock[0]+s)):
            self.assertEqual(b.complete_agent(self.client,transcript()),'FINAL')
        self.assertGreaterEqual(polls[-1],45)

    def test_retry_recovers_same_thread_even_after_lock_map_reset(self):
        creates=[]
        def post(url,**kwargs):
            if url.endswith('createAgentThread'): creates.append(kwargs['json']['threadId']); return Response()
            return Response(reply('running'))
        with patch.object(b.requests,'post',side_effect=post),patch.object(b,'WAIT_SECONDS',0.001),patch.object(b,'POLL_SECONDS',0.001):
            with self.assertRaises(b.AgentRequestError) as error:b.complete_agent(self.client,transcript())
            self.assertEqual(error.exception.code,'AGENT_PENDING')
        b._locks.clear()
        with patch.object(b.requests,'post',return_value=Response(reply())) as post:
            self.assertEqual(b.complete_agent(self.client,transcript()),'FINAL')
            self.assertTrue(all(c.args[0].endswith('getThreadTranscript') for c in post.call_args_list))
        with patch.object(b.requests,'post') as post:
            self.assertEqual(b.complete_agent(self.client,transcript()),'FINAL');post.assert_not_called()
        self.assertEqual(len(creates),1)

    def test_different_input_blocked_while_previous_run_is_unknown(self):
        with patch.object(b.requests,'post',return_value=Response(reply('running'))),patch.object(b,'WAIT_SECONDS',0):
            with self.assertRaises(b.AgentRequestError):b.complete_agent(self.client,transcript())
        with patch.object(b.requests,'post',return_value=Response(reply('running'))) as post:
            with self.assertRaises(b.AgentRequestError) as error:b.complete_agent(self.client,transcript('different'))
            self.assertEqual(error.exception.code,'AGENT_PENDING')
            self.assertEqual(post.call_count,1)
            self.assertTrue(post.call_args.args[0].endswith('getThreadTranscript'))

    def test_ambiguous_create_timeout_is_polled_without_replay(self):
        creates=[]
        def post(url,**kwargs):
            if url.endswith('createAgentThread'):
                creates.append(1);raise b.requests.Timeout()
            return Response(reply())
        with patch.object(b.requests,'post',side_effect=post):
            self.assertEqual(b.complete_agent(self.client,transcript()),'FINAL')
        self.assertEqual(len(creates),1)

    def test_ambiguous_http_408_retains_original_task(self):
        creates=[]
        def post(url,**kwargs):
            if url.endswith('createAgentThread'):
                creates.append(kwargs['json']['threadId'])
                response=Response();response.status_code=408;return response
            return Response(reply())
        with patch.object(b.requests,'post',side_effect=post):
            self.assertEqual(b.complete_agent(self.client,transcript()),'FINAL')
        self.assertEqual(len(creates),1)

    def test_parallel_request_does_not_create_second_task(self):
        entered=threading.Event();release=threading.Event();creates=[]
        def post(url,**kwargs):
            if url.endswith('createAgentThread'):
                creates.append(1);entered.set();release.wait(3)
            return Response(reply())
        with patch.object(b.requests,'post',side_effect=post):
            t=threading.Thread(target=b.complete_agent,args=(self.client,transcript()))
            t.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(b.AgentRequestError) as error:b.complete_agent(self.client,transcript('other'))
                self.assertEqual(error.exception.code,'AGENT_BUSY')
            finally:release.set();t.join(3)
        self.assertEqual(len(creates),1)

    def test_idle_pending_input_is_not_completed(self):
        self.assertFalse(b._completed(reply(pending=True)))
        self.assertFalse(b._completed(None))
        self.assertFalse(b._completed({'session': None}))
        self.assertFalse(b._completed({'session': {'status': 'idle'}}))

if __name__=='__main__':unittest.main()
