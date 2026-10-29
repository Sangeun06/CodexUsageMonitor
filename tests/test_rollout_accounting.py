"""Regression cases derived from ccusage's documented Codex log semantics."""
import json
import tempfile
import unittest
from pathlib import Path
from collector import _read_rollout_usage_buckets


def usage(total, cached=0):
    return dict(input_tokens=total-10, cached_input_tokens=cached, output_tokens=10,
                reasoning_output_tokens=4, total_tokens=total)


def event(last=None, cumulative=None, timestamp='2026-10-28T00:00:00Z'):
    info = {}
    if last is not None: info['last_token_usage'] = last
    if cumulative is not None: info['total_token_usage'] = cumulative
    return dict(type='event_msg', timestamp=timestamp, payload=dict(type='token_count', info=info))


class RolloutAccountingTest(unittest.TestCase):
    def parse(self, records, chunk=False):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'rollout.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in records))
            context, details, offset, total = {}, [], 0, None
            while offset < path.stat().st_size:
                _, next_offset, total = _read_rollout_usage_buckets(path, 0, offset, total, details, context,
                                                                  max_bytes=1 if chunk else None)
                self.assertGreater(next_offset, offset)
                offset = next_offset
            return details, context

    def test_cumulative_only_components_and_repeated_snapshot_across_chunks(self):
        records = [event(cumulative=usage(100, 50)), event(cumulative=usage(100, 50)),
                   event(cumulative=dict(input_tokens=180, cached_input_tokens=100,
                                         output_tokens=20, reasoning_output_tokens=8, total_tokens=200))]
        rows, _ = self.parse(records, chunk=True)
        self.assertEqual([r['tokens'] for r in rows], [100, 100])
        self.assertEqual(rows[1]['input_tokens'], 90)
        self.assertEqual(rows[1]['cached_input_tokens'], 50)
        self.assertEqual(rows[1]['reasoning_output_tokens'], 4)

    def test_speed_changes_persist_across_turns_and_omissions_but_unknown_clears(self):
        turn = dict(type='turn_context', payload=dict(model='small', effort='low', secret='private'))
        settings = lambda value: dict(type='event_msg', payload=dict(type='thread_settings_applied',thread_settings=value))
        rows, state = self.parse([turn, settings({'service_tier':'priority'}),event(usage(100)),
                                 turn,settings({'model':'large','reasoning_effort':'high'}),event(usage(110)),
                                 settings({'service_tier':'unsupported'}),event(usage(120))], chunk=True)
        self.assertEqual([r['service_tier'] for r in rows], ['fast','fast','unknown'])
        self.assertEqual(rows[1]['model'], 'large')
        self.assertEqual(rows[1]['effort'], 'high')
        self.assertNotIn('private', str(state))

    def test_subagent_replayed_parent_prefix_is_excluded_with_both_boundaries(self):
        for marker in [dict(type='event_msg',payload=dict(type='task_started')),
                       dict(type='inter_agent_communication_metadata',payload=dict(trigger_turn=True)),
                       dict(type='inter_agent_communication',payload=dict(trigger_turn=True))]:
            rows, _ = self.parse([event(usage(1000),usage(1000)), marker,
                                  event(usage(100),usage(1100))], chunk=True)
            self.assertEqual([r['tokens'] for r in rows], [100])

    def test_false_trigger_does_not_hide_real_usage(self):
        rows, _ = self.parse([dict(type='inter_agent_communication_metadata',payload=dict(trigger_turn=False)),event(usage(100))])
        self.assertEqual(sum(r['tokens'] for r in rows),100)

    def test_cumulative_reset_and_missing_total_legacy(self):
        records=[event(cumulative=usage(100)),event(cumulative=usage(50)),
                 event(cumulative=usage(150)),event(dict(input_tokens=20,output_tokens=5))]
        rows,_=self.parse(records)
        self.assertEqual([r['tokens'] for r in rows],[100,100,25])

    def test_request_duplicates_without_cumulative_and_partial_tail(self):
        item=event(usage(100))
        rows,_=self.parse([item,item],chunk=True)
        self.assertEqual(len(rows),1)
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'log';path.write_text(json.dumps(item))
            rows=[];buckets,offset,_=_read_rollout_usage_buckets(path,0,0,details=rows)
            self.assertEqual((buckets,offset,rows),([],0,[]))

    def test_compaction_pair_both_orders_remote_and_local(self):
        record=dict(type='token_usage_record',timestamp='2026-10-28T00:00:00Z',
                    payload=dict(response_id='r',usage=usage(100)))
        marker=dict(type='compacted',payload=dict(compaction_response_id='r'))
        for records in ([record,marker],[marker,record],[record,event(usage(100),usage(100)),marker]):
            for chunk in (False,True):
                rows,_=self.parse(records,chunk)
                self.assertEqual(sum(r['tokens'] for r in rows),100)
        rows,_=self.parse([record])
        self.assertEqual(rows,[])

    def test_compaction_uses_recorded_settings_and_nested_cache(self):
        record=dict(type='token_usage_record',timestamp='2026-10-28T00:00:00Z',
                    payload=dict(response_id='r',model='large',service_tier='priority',
                                 usage=dict(input_tokens=90,input_tokens_details=dict(cached_tokens=50),
                                            output_tokens=10,output_tokens_details=dict(reasoning_tokens=4))))
        rows,_=self.parse([record,dict(type='compacted',payload=dict(compaction_response_id='r'))])
        self.assertEqual(rows[0]['tokens'],100)
        self.assertEqual(rows[0]['model'],'large')
        self.assertEqual(rows[0]['service_tier'],'fast')
        self.assertEqual(rows[0]['cached_input_tokens'],50)
        self.assertEqual(rows[0]['reasoning_output_tokens'],4)

    def test_legacy_fork_matches_parent_prefix_and_excludes_parent_future(self):
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)/'sessions';folder.mkdir()
            parent=folder/'rollout-parent.jsonl';child=folder/'rollout-child.jsonl'
            parent.write_text(''.join(json.dumps(r)+'\n' for r in [event(usage(100),usage(100)),event(usage(50),usage(150),'2026-10-28T00:10:00Z')]))
            metadata=dict(type='session_meta',timestamp='2026-10-28T00:05:00Z',payload=dict(forked_from_id='parent'))
            child.write_text(''.join(json.dumps(r)+'\n' for r in [metadata,event(usage(100),usage(100)),event(usage(50),usage(150),'2026-10-28T00:06:00Z')]))
            rows=[]
            _read_rollout_usage_buckets(child,0,0,details=rows)
            self.assertEqual([r['tokens'] for r in rows],[50])

    def test_late_compaction_snapshot_after_incremental_eof_is_not_added_again(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'rollout.jsonl'
            record=dict(type='token_usage_record',timestamp='2026-10-28T00:00:00Z',payload=dict(response_id='r',usage=usage(100)))
            marker=dict(type='compacted',payload=dict(compaction_response_id='r'))
            path.write_text(json.dumps(record)+'\n'+json.dumps(marker)+'\n')
            rows=[];context={}
            _,offset,total=_read_rollout_usage_buckets(path,0,0,details=rows,context=context)
            with path.open('a') as stream:stream.write(json.dumps(event(usage(100),usage(100)))+'\n')
            _read_rollout_usage_buckets(path,0,offset,total,rows,context)
            self.assertEqual(sum(r['tokens'] for r in rows),100)
