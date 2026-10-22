"""Bridge integration regressions with synthetic account/network data."""
import importlib.util
import json
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'app'
class UpstreamError(RuntimeError):
    def __init__(self, message, **kw):
        super().__init__(message); self.__dict__.update(kw)
app = types.ModuleType('app'); app.__path__ = [str(ROOT)]
sys.modules['app'] = app
sys.modules['app.notion_client'] = types.SimpleNamespace(NotionUpstreamError=UpstreamError)
sys.modules['app.logger'] = types.SimpleNamespace(logger=logging.getLogger('test'))
spec = importlib.util.spec_from_file_location('bridge_v2', ROOT/'agent_bridge.py')
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)

def request(messages, system='rules'):
    data = dict(protocol='dsh-local-tools-v2', system=system,
                available_tools=[dict(name='read', parameters={'type':'object'})],
                conversation=messages)
    text = 'DSH_REQUEST_JSON:\n'+json.dumps(data)+'\n\nRespond with the next protocol JSON object only.'
    return [{'type':'user', 'value':[[text]]}]

def snapshot(request_id, sequence, text='{"text":"ok"}', stop='end_turn'):
    entities = [dict(id=f'u{sequence}', kind='user_message', client_message_id=request_id, sequence=sequence-2),
                dict(id=f'a{sequence}', kind='assistant_message', turn_id=f't{sequence}', sequence=sequence-1,
                     content=[dict(type='text', text=text)]),
                dict(id=f'd{sequence}', kind='turn_completed', turn_id=f't{sequence}', sequence=sequence, stop_reason=stop)]
    return dict(patches=[dict(op='put', entity=dict(e, source='committed')) for e in entities]+[dict(op='committed', sequence=sequence)],
                session=dict(status='idle', sequence=sequence, pending_input=None), session_pending=False,
                has_more_forward=False, has_more_backward=False)

class Response:
    status_code = 200
    def __init__(self, data=None): self.data=data
    def json(self): return self.data

class BridgeV2Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        state = patch.object(b,'STATE_DIR',Path(temp.name));state.start();self.addCleanup(state.stop)
        self.client = types.SimpleNamespace(space_id='space',user_id='user',cookies={},token_v2='fixture')
        self.messages = [dict(role='user',content='read fixture')]
        self.posts=[];self.sequence=10;self.latest=None;self.old_first=False;self.text='{"text":"ok"}';self.stop='end_turn'
        self.polls=0;self.clock=0
        network=patch.object(b.requests,'post',side_effect=self.post);network.start();self.addCleanup(network.stop)
        sleep=patch.object(b.time,'sleep',side_effect=lambda seconds:setattr(self,'clock',self.clock+seconds));sleep.start();self.addCleanup(sleep.stop)
        timer=patch.object(b.time,'monotonic',side_effect=lambda:self.clock);timer.start();self.addCleanup(timer.stop)

    def post(self,url,**kwargs):
        payload=kwargs['json']
        if url.endswith('createAgentThread') or url.endswith('sendEventToAgentThread'):
            self.posts.append((url,payload))
            self.previous=self.latest
            rid=payload.get('clientMessageId') or payload['clientEventId']
            self.sequence+=10;self.latest=snapshot(rid,self.sequence,self.text,self.stop)
            return Response({})
        self.polls+=1
        if self.old_first and self.previous is not None:
            self.old_first=False;return Response(self.previous)
        return Response(self.latest)

    def run_request(self,messages=None,scope='session',system='rules',job_id=None):
        return b.complete_agent(self.client,request(messages or self.messages,system),scope=scope,
                                job_id=job_id,wait_seconds=60,local_protocol=True)

    def test_three_steps_use_one_thread_and_delta_only(self):
        self.run_request()
        second=self.messages+[dict(role='assistant',content='decision'),dict(role='tool',callId='call-1',content='nonce')]
        self.run_request(second)
        self.run_request(second+[dict(role='assistant',content='done'),dict(role='user',content='verify')])
        self.assertEqual([url.rsplit('/',1)[1] for url,_ in self.posts],['createAgentThread','sendEventToAgentThread','sendEventToAgentThread'])
        self.assertEqual(len({data['threadId'] for _,data in self.posts}),1)
        delta=self.posts[1][1]['event']['content'][0]['text'][0][0]
        self.assertIn('DSH_CONTINUATION_JSON',delta);self.assertNotIn('read fixture',delta)
        self.assertEqual(self.posts[1][1]['surface'],'full_page_chat')

    def test_old_idle_does_not_finish_next_turn(self):
        self.run_request();self.old_first=True;self.text='{"text":"new"}'
        before=self.polls
        self.assertEqual(self.run_request(self.messages+[dict(role='user',content='next')]),self.text)
        self.assertEqual(self.polls-before,2)

    def test_scope_manifest_history_and_model_boundaries_create_fresh_threads(self):
        self.run_request()
        self.run_request(self.messages+[dict(role='user',content='next')],scope='another')
        self.run_request(self.messages+[dict(role='user',content='next')],system='changed')
        self.run_request([dict(role='user',content='rewritten')])
        self.assertTrue(all(url.endswith('createAgentThread') for url,_ in self.posts))

    def test_pending_recovery_does_not_resubmit_continuation(self):
        self.run_request()
        newer=self.messages+[dict(role='user',content='next')]
        job_id='a'*64
        job=b.prepare_agent_job(self.client,request(newer),'session',job_id,local_protocol=True)
        path=b.STATE_DIR/f'{b._account(self.client)}.json'
        b._submit_queued(self.client,path,job)
        count=len(self.posts);b._locks.clear()
        self.assertEqual(b.resume_agent_job(self.client,job_id),'{"text":"ok"}')
        self.assertEqual(len(self.posts),count)

    def test_terminal_invalid_json_cached_without_permanent_account_block(self):
        self.text='I wrote an imaginary file'
        with self.assertRaises(b.AgentRequestError) as error:self.run_request(job_id='b'*64)
        self.assertEqual(error.exception.code,'AGENT_PROTOCOL')
        self.assertEqual(b.read_agent_job(self.client,'b'*64)['terminal_error']['code'],'AGENT_PROTOCOL')
        self.text='{"text":"recovered"}'
        self.assertEqual(self.run_request(self.messages+[dict(role='user',content='different')]),self.text)
        self.assertEqual(len({data['threadId'] for _,data in self.posts}),2)

    def test_malformed_optin_fails_before_any_submission(self):
        bad=request(self.messages);bad[0]['value'][0][0]=bad[0]['value'][0][0].replace('"conversation": [','"conversation": "bad", "unused": [')
        with self.assertRaises(b.AgentRequestError) as error:
            b.complete_agent(self.client,bad,scope='session',local_protocol=True)
        self.assertEqual(error.exception.code,'AGENT_PROTOCOL');self.assertEqual(self.posts,[])

    def test_explicit_cloud_action_retains_pending_and_never_resubmits(self):
        self.stop='requires_action'
        with self.assertRaises(b.AgentRequestError) as error:self.run_request(job_id='c'*64)
        self.assertEqual(error.exception.code,'AGENT_REQUIRES_ACTION')
        self.assertEqual(b.read_agent_job(self.client,'c'*64)['phase'],'pending')
        with self.assertRaises(b.AgentRequestError):b.resume_agent_job(self.client,'c'*64)
        self.assertEqual(len(self.posts),1)

    def test_queued_claim_contains_only_hashes_after_submission_and_context(self):
        self.run_request(job_id='d'*64)
        paths=list(b.STATE_DIR.rglob('*.json'))
        for path in paths:
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertNotIn('read fixture',path.read_text())

    def test_paginated_snapshot_is_assembled_without_generation_posts(self):
        self.run_request();current=self.latest
        newer=dict(current,patches=current['patches'][1:],has_more_backward=True,backward_cursor='opaque-page')
        older=dict(current,patches=current['patches'][:1],backward_cursor=None)
        with patch.object(b.requests,'post',side_effect=[Response(newer),Response(older)]) as post:
            data=b._poll_v2(self.client,'thread',timeout=20)
        self.assertEqual(post.call_args_list[1].kwargs['json']['cursor'],'opaque-page')
        self.assertEqual(b.classify_snapshot(data,request_id=current['patches'][0]['entity']['client_message_id'],baseline_sequence=0)['status'],'completed')

    def test_forward_snapshot_pages_are_consumed(self):
        self.run_request();current=self.latest
        first=dict(current,patches=current['patches'][:1],has_more_forward=True,forward_cursor='newer-page')
        second=dict(current,patches=current['patches'][1:],has_more_backward=True)
        with patch.object(b.requests,'post',side_effect=[Response(first),Response(second)]) as post:
            data=b._poll_v2(self.client,'thread',timeout=20)
        self.assertEqual(post.call_count,2)
        self.assertEqual(post.call_args_list[1].kwargs['json']['direction'],'forward')
        self.assertFalse(data['has_more_forward']);self.assertFalse(data['has_more_backward'])

    def test_deleted_thread_is_terminal_instead_of_permanently_pending(self):
        real_post=self.post
        def deleted(url,**kw):
            response=real_post(url,**kw)
            if url.endswith('getThreadTranscript'):response.data['session']['status']='deleted'
            return response
        with patch.object(b.requests,'post',side_effect=deleted):
            with self.assertRaises(b.AgentRequestError) as error:self.run_request(job_id='e'*64)
        self.assertEqual(error.exception.code,'AGENT_UPSTREAM_FAILED')
        self.assertEqual(b.read_agent_job(self.client,'e'*64)['phase'],'done')

    def test_legacy_cached_text_cannot_bypass_v2_validation(self):
        self.text='legacy cloud prose'
        b.complete_agent(self.client,request(self.messages),scope='session',wait_seconds=60)
        self.text='{"text":"new v2 decision"}'
        self.assertEqual(self.run_request(),self.text)
        self.assertEqual(len(self.posts),2)

if __name__=='__main__':unittest.main()
