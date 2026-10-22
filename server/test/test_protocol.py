"""Offline protocol regressions. Synthetic fixtures contain no account information."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / 'app/agent_protocol.py'
protocol = None
if MODULE_PATH.is_file():
    spec = importlib.util.spec_from_file_location('bridge_protocol_v2', MODULE_PATH)
    protocol = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(protocol)


def request(conversation=None, **changes):
    data = dict(protocol='dsh-local-tools-v2', system='operator rules',
                available_tools=[dict(name='local_echo', description='Echo locally',
                                      parameters={'type': 'object'})],
                capabilities={'text_tools': True}, purpose='chat',
                conversation=conversation if conversation is not None else [
                    {'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}])
    data.update(changes)
    return 'Local tool protocol instructions\nDSH_REQUEST_JSON:\n' + json.dumps(data) + '\n\nRespond with the next protocol JSON object only.'


def entity(identifier, kind, sequence, **fields):
    return dict(id=identifier, kind=kind, source='committed', sequence=sequence, **fields)


def snapshot():
    # Mirrors the observed committed put/session/committed structure; IDs/text synthetic.
    entities = [
        entity('old-user', 'user_message', 1, client_message_id='old-request'),
        entity('old-answer', 'assistant_message', 2, turn_id='old-turn', content=[{'type': 'text', 'text': 'OLD'}]),
        entity('old-done', 'turn_completed', 3, turn_id='old-turn', stop_reason='end_turn'),
        entity('current-user', 'user_message', 10, client_message_id='request-current'),
        entity('first-answer', 'assistant_message', 12, turn_id='turn-current', content=[{'type': 'text', 'text': 'EARLY'}]),
        entity('answer', 'assistant_message', 13, turn_id='turn-current', content=[{'type': 'text', 'text': '{"text":"fresh"}'}]),
        entity('done', 'turn_completed', 14, turn_id='turn-current', stop_reason='end_turn'),
    ]
    return dict(patches=[{'op': 'put', 'entity': e} for e in entities] + [{'op': 'committed', 'sequence': 14}],
                session={'status': 'idle', 'sequence': 14, 'pending_input': None},
                session_pending=False, has_more_forward=False, has_more_backward=False)


class ProtocolV2Tests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(protocol, 'new pure protocol helper has not been implemented')

    def classify(self, data, **changes):
        return protocol.classify_snapshot(data, request_id='request-current', baseline_sequence=9, **changes)

    def test_only_explicit_v2_marker_activates_request_parser(self):
        for text in ['plain user text', '{"protocol":"dsh-local-tools-v2"}',
                     request(protocol='text-tools-v1'), request(protocol=None),
                     'DSH_REQUEST_JSON:\n{"not_protocol":"dsh-local-tools-v2"}']:
            self.assertIsNone(protocol.parse_bridge_request(text))
        result = protocol.parse_bridge_request(request())
        self.assertEqual(result['tool_names'], ['local_echo'])
        self.assertEqual(result['raw_text'], request())

    def test_empty_registry_supports_text_without_allowing_any_tool(self):
        result = protocol.parse_bridge_request(request(available_tools=[]))
        self.assertEqual(result['tool_names'], [])
        self.assertTrue(protocol.validate_local_reply('{"text":"hello"}', []))
        self.assertFalse(protocol.validate_local_reply('{"tool_calls":[{"name":"local_echo","arguments":{}}]}', []))

    def test_v2_preserves_role_order_callids_and_error_results(self):
        messages = [dict(role='developer', content=[{'type': 'text', 'text': 'rule'}]),
                    dict(role='assistant', content=[dict(type='tool-call', id='call-a', name='local_echo', arguments='{}')]),
                    dict(role='tool', source={'callId': 'call-a'}, callId='call-a', isError=True,
                         content=[dict(type='text', text='failed')])]
        self.assertEqual(protocol.parse_bridge_request(request(messages))['conversation'], messages)

    def test_manifest_is_key_order_invariant_and_message_hash_is_content_sensitive(self):
        a = protocol.parse_bridge_request(request())
        data = json.loads(request().split('DSH_REQUEST_JSON:\n')[1].split('\n\nRespond')[0])
        reordered = 'DSH_REQUEST_JSON:\n' + json.dumps(dict(reversed(list(data.items())))) + '\n\nRespond with the next protocol JSON object only.'
        b = protocol.parse_bridge_request(reordered)
        self.assertEqual(a['manifest_hash'], b['manifest_hash'])
        self.assertEqual(a['message_hashes'], b['message_hashes'])
        changed = protocol.parse_bridge_request(request([dict(role='user', content='different')]))
        self.assertNotEqual(a['message_hashes'], changed['message_hashes'])
        self.assertEqual(len(a['manifest_hash']), 64)

    def test_explicit_malformed_v2_fails_instead_of_downgrading(self):
        bad = [request(conversation='wrong'), request(conversation=[{'role': 'alien'}]),
               request(available_tools=[{'name': ''}]), request(available_tools=[{'name': 'a'}, {'name': 'a'}]),
               request().replace('"purpose": "chat"', '"purpose": NaN'),
               request().replace('"purpose": "chat"', '"purpose": "chat", "purpose": "other"'),
               request().replace('"purpose": "chat"', '"purpose": 1e999'),
               request() + ' extra prose',
               'DSH_REQUEST_JSON:\n{"protocol":"dsh-local-tools-v2", BROKEN']
        for text in bad:
            with self.subTest(text=text[:50]), self.assertRaises(ValueError):
                protocol.parse_bridge_request(text)

    def test_duplicate_protocol_key_cannot_downgrade_v2_to_legacy(self):
        text = request().replace('"protocol": "dsh-local-tools-v2"',
                                 '"protocol":"dsh-local-tools-v2","protocol":"legacy"')
        with self.assertRaises(ValueError):
            protocol.parse_bridge_request(text)
        nested = request(protocol='legacy', capabilities={'protocol': 'dsh-local-tools-v2'})
        self.assertIsNone(protocol.parse_bridge_request(nested))

    def test_continuation_contains_only_strict_prefix_extension(self):
        old_messages = [{'role': 'user', 'content': 'secret-old-history'}]
        new_messages = old_messages + [{'role': 'assistant', 'content': 'old-answer'},
                                       {'role': 'tool', 'callId': 'call-z', 'isError': True, 'content': 'new-result'}]
        before = protocol.parse_bridge_request(request(old_messages))
        metadata = {key: before[key] for key in ('manifest_hash', 'message_hashes')}
        after = protocol.parse_bridge_request(request(new_messages))
        text = protocol.continuation_text(after, metadata)
        self.assertIn('DSH_CONTINUATION_JSON:', text)
        self.assertNotIn('secret-old-history', text)
        payload = json.JSONDecoder().raw_decode(text.split('DSH_CONTINUATION_JSON:\n')[1])[0]
        self.assertEqual(payload['conversation'], new_messages[1:])
        self.assertEqual(payload['protocol'], 'dsh-local-tools-v2')
        self.assertNotIn('conversation', metadata)

    def test_continuation_rejects_equal_shorter_or_branched_history(self):
        before = protocol.parse_bridge_request(request([{'role': 'user', 'content': 'first'}]))
        for messages in [[], [{'role': 'user', 'content': 'first'}],
                         [{'role': 'user', 'content': 'edited'}, {'role': 'user', 'content': 'next'}]]:
            self.assertIsNone(protocol.continuation_text(protocol.parse_bridge_request(request(messages)), before))
        self.assertIsNone(protocol.continuation_text(before, {}))

    def test_continuation_rejects_mutated_in_memory_history_with_stale_hashes(self):
        old = protocol.parse_bridge_request(request())
        new = protocol.parse_bridge_request(request(old['conversation'] + [{'role': 'user', 'content': 'next'}]))
        new['conversation'][0]['content'] = 'edited after parsing'
        self.assertIsNone(protocol.continuation_text(new, old))

    def test_changed_manifest_never_continues_old_thread(self):
        before = protocol.parse_bridge_request(request())
        messages = before['conversation'] + [{'role': 'user', 'content': 'next'}]
        for changes in [dict(system='different'), dict(available_tools=[]),
                        dict(capabilities={'text_tools': False}), dict(purpose='compact')]:
            self.assertIsNone(protocol.continuation_text(protocol.parse_bridge_request(request(messages, **changes)), before))

    def test_current_committed_message_and_final_same_turn_text_are_required(self):
        data = snapshot()
        data['patches'][:7] = reversed(data['patches'][:7])
        before = copy.deepcopy(data)
        result = self.classify(data)
        self.assertEqual(result, dict(status='completed', last_sequence=14, reply='{"text":"fresh"}', turn_id='turn-current'))
        self.assertEqual(data, before, 'classification must not mutate caller-owned snapshots')

    def test_old_idle_does_not_complete_unsubmitted_request(self):
        data = snapshot(); data['patches'] = data['patches'][:3]
        data['session']['sequence'] = 3
        self.assertEqual(self.classify(data)['status'], 'awaiting_message')

    def test_wrong_id_baseline_provisional_or_queued_user_cannot_bind(self):
        for changes in [dict(client_message_id='other'), dict(sequence=9),
                        dict(source='provisional'), dict(queue_sequence=1)]:
            data = snapshot(); data['patches'][3]['entity'].update(changes)
            self.assertEqual(self.classify(data)['status'], 'awaiting_message')

    def test_pagination_flags_are_mandatory_and_require_assembly(self):
        for flag in ['has_more_forward', 'has_more_backward']:
            data = snapshot(); data[flag] = True
            self.assertEqual(self.classify(data)['status'], 'needs_pages')
            self.assertIn('last_sequence', self.classify(data))
            del data[flag]
            with self.assertRaises(ValueError): self.classify(data)

    def test_session_state_never_substitutes_for_turn_completion(self):
        for changes, want in [({'session_pending': True}, 'pending'), ({'session': None}, 'pending'),
                              ({'session': {'status': 'running', 'sequence': 14}}, 'running'),
                              ({'session': {'status': 'deleted', 'sequence': 14}}, 'deleted'),
                              ({'session': {'status': 'idle', 'sequence': 14, 'pending_input': {'request_sequence': 14}}}, 'requires_action')]:
            data = snapshot(); data.update(changes)
            self.assertEqual(self.classify(data)['status'], want)
        data = snapshot(); data['patches'].pop(6)
        self.assertNotEqual(self.classify(data)['status'], 'completed')

    def test_failure_and_unknown_stop_reasons_never_become_success(self):
        for reason, want in [('requires_action', 'requires_action'), ('interrupted', 'interrupted'),
                             ('runtime_error', 'error'), ('retries_exhausted', 'error'),
                             ('new_reason', 'unknown_stop_reason'), (None, 'unknown_stop_reason')]:
            data = snapshot(); data['patches'][6]['entity']['stop_reason'] = reason
            self.assertEqual(self.classify(data)['status'], want)

    def test_later_user_supersedes_but_queued_user_does_not(self):
        data = snapshot()
        later = entity('later', 'user_message', 11, client_message_id='other')
        data['patches'].append({'op': 'put', 'entity': later})
        self.assertEqual(self.classify(data)['status'], 'superseded')
        later['queue_sequence'] = 1
        self.assertEqual(self.classify(data)['status'], 'completed')

    def test_provisional_and_foreign_turn_answers_are_never_returned(self):
        for changes in [dict(source='provisional'), dict(turn_id='another-turn'), dict(turn_id=None)]:
            data = snapshot(); data['patches'][5]['entity'].update(changes)
            self.assertNotEqual(self.classify(data)['status'], 'completed')
        data = snapshot(); data['patches'][6]['entity']['turn_id'] = 'another-turn'
        self.assertEqual(self.classify(data)['status'], 'ambiguous')

    def test_stale_or_ahead_watermarks_and_late_work_are_not_success(self):
        for session_sequence, commit_sequence in [(13, 14), (15, 14), (14, 16), (16, 16)]:
            data = snapshot(); data['session']['sequence'] = session_sequence
            data['patches'][-1]['sequence'] = commit_sequence
            self.assertNotEqual(self.classify(data)['status'], 'completed')
        data = snapshot(); data['patches'].append({'op': 'put', 'entity': entity('late', 'thinking', 15)})
        data['session']['sequence'] = 15
        self.assertNotEqual(self.classify(data)['status'], 'completed')

    def test_error_and_interruption_after_request_override_answer(self):
        for e, want in [(entity('error', 'error', 13), 'error'),
                        (entity('interrupt', 'status', 13, status='interrupted'), 'interrupted')]:
            data = snapshot(); data['patches'].append({'op': 'put', 'entity': e})
            self.assertEqual(self.classify(data)['status'], want)

    def test_committed_remove_cannot_resurrect_removed_final_answer(self):
        data = snapshot(); data['patches'] += [{'op': 'remove', 'id': 'answer'}, {'op': 'remove', 'id': 'first-answer'}]
        self.assertNotEqual(self.classify(data)['status'], 'completed')

    def test_tool_field_patch_is_supported_without_changing_assistant_text(self):
        data = snapshot()
        tool = entity('tool', 'tool', 11, result=None, interaction_status='pending')
        data['patches'] += [{'op': 'put', 'entity': tool}, {'op': 'patch', 'id': 'tool', 'ops': [
            {'op': 'replace', 'path': '/result', 'value': {'type': 'success'}},
            {'op': 'replace', 'path': '/interaction_status', 'value': 'submitted'},
            {'op': 'add', 'path': '/review_rejection', 'value': None},
            {'op': 'add', 'path': '/icon_name', 'value': 'tool'}]}]
        before = copy.deepcopy(data)
        self.assertEqual(self.classify(data)['status'], 'completed')
        self.assertEqual(data, before)

    def test_unsupported_patch_rewind_conflicting_put_and_unknown_kind_are_ambiguous(self):
        cases = [{'op': 'rewind', 'rewind_to_sequence': 2, 'event_sequence': 9},
                 {'op': 'patch', 'id': 'answer', 'ops': [{'op': 'append', 'path': '/content/0/text', 'value': 'EXTRA'}]},
                 {'op': 'patch', 'id': 'missing', 'ops': [{'op': 'replace', 'path': '/result', 'value': None}]},
                 {'op': 'put', 'entity': entity('answer', 'assistant_message', 13, turn_id='turn-current', content=[])},
                 {'op': 'put', 'entity': entity('future', 'new_kind', 11)}]
        for patch in cases:
            data = snapshot(); data['patches'].append(patch)
            self.assertEqual(self.classify(data)['status'], 'ambiguous')

    def test_old_session_patch_cannot_override_newer_running_session(self):
        data = snapshot()
        data['patches'].append({'op': 'session', 'session': {'status': 'running', 'sequence': 15, 'pending_input': None}})
        self.assertEqual(self.classify(data)['status'], 'running')

    def test_equal_sequence_conflicting_session_does_not_choose_idle(self):
        for state in [dict(status='running', sequence=14),
                      dict(status='idle', sequence=14, pending_input={'request_sequence': 14})]:
            data = snapshot(); data['patches'].append({'op': 'session', 'session': state})
            self.assertNotEqual(self.classify(data)['status'], 'completed')

    def test_malformed_patch_identity_is_controlled_ambiguity(self):
        data = snapshot(); data['patches'].append({'op': 'patch', 'id': [], 'ops': []})
        self.assertEqual(self.classify(data)['status'], 'ambiguous')

    def test_malformed_tool_patch_path_is_controlled_ambiguity(self):
        data = snapshot()
        data['patches'] += [{'op': 'put', 'entity': entity('tool', 'tool', 11, result=None)},
                            {'op': 'patch', 'id': 'tool', 'ops': [{'op': 'replace', 'path': [], 'value': None}]}]
        self.assertEqual(self.classify(data)['status'], 'ambiguous')

    def test_removed_later_committed_work_still_prevents_stale_completion(self):
        data = snapshot()
        data['patches'] += [{'op': 'put', 'entity': entity('later', 'thinking', 15)}, {'op': 'remove', 'id': 'later'}]
        self.assertNotEqual(self.classify(data)['status'], 'completed')

    def test_duplicate_current_request_identity_is_ambiguous(self):
        data = snapshot(); data['patches'].append({'op': 'put', 'entity': entity('duplicate', 'user_message', 11, client_message_id='request-current')})
        self.assertEqual(self.classify(data)['status'], 'ambiguous')

    def test_invalid_baseline_and_snapshot_flags_are_rejected(self):
        for baseline in [-1, True, '9']:
            with self.assertRaises(ValueError):
                protocol.classify_snapshot(snapshot(), request_id='request-current', baseline_sequence=baseline)
        data = snapshot(); data['session_pending'] = 'false'
        with self.assertRaises(ValueError): self.classify(data)

    def test_legacy_reply_envelopes_and_single_complete_fences_are_valid(self):
        for reply in ['{"text":"done"}', '```json\n{"text":"done"}\n```',
                      '{"tool_calls":[{"name":"local_echo","arguments":{"value":"ok"}}]}',
                      '{"tool_calls":[{"name":"local_echo","arguments":"{\\"value\\":\\"ok\\"}"}]}']:
            self.assertTrue(protocol.validate_local_reply(reply, ['local_echo']))

    def test_mixed_unknown_tool_empty_oversized_and_nonobject_arguments_are_invalid(self):
        calls = [{'name': 'local_echo', 'arguments': {}}]
        for data in [dict(text='hi', tool_calls=calls), dict(tool_calls=[]), dict(tool_calls=calls * 9),
                     dict(tool_calls=[dict(name='cloud_tool', arguments={})]),
                     dict(tool_calls=[dict(name='local_echo', arguments=[])]),
                     dict(tool_calls=[dict(name='local_echo', arguments='[]')]), dict(text='')]:
            self.assertFalse(protocol.validate_local_reply(json.dumps(data), ['local_echo']))

    def test_partial_json_prose_duplicate_keys_and_nonfinite_numbers_are_invalid(self):
        for reply in ['prefix {"text":"ok"}', '{"text":"ok"} trailing', '```json\n{"text":"ok"}\n``` more',
                      '{"text":"first","text":"second"}', '{"tool_calls":[{"name":"local_echo","arguments":{"x":1,"x":2}}]}',
                      '{"tool_calls":[{"name":"local_echo","arguments":"{\\"x\\":1,\\"x\\":2}"}]}',
                      '{"tool_calls":[{"name":"local_echo","arguments":{"x":NaN}}]}',
                      '{"tool_calls":[{"name":"local_echo","arguments":{"x":1e999}}]}', '{"text":']:
            self.assertFalse(protocol.validate_local_reply(reply, ['local_echo']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
