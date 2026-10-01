"""Offline output-gate tests; runtime cases use real Agent, queue and SQLite stores."""
import copy
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.adapters.mic_adapter import MICAdapter
from agent_trade_intel.agent import IntelligenceCollectorAgent, _tool_message_action
from agent_trade_intel.config import AgentModelConfig, CollectorConfig, RuntimeConfig, ToolConfig
from agent_trade_intel.quality import QualityGate, mic_output_summary


def tool_result(report):
    return ToolResult(tool_name='market_intelligence_collector', operation='collect_intelligence',
                      request={}, status='success', result=copy.deepcopy(report)).finish()


def empty_report():
    return {'search_run_id': 'offline_empty', 'summary': {'search_hits': 20, 'links_read': 0,
            'model_calls': 1, 'batch_triage_calls': 1},
            'structured_outputs': {'events': 0, 'facts': 0, 'metrics': 0}, 'all_events': [], 'top_events': []}


def new_agent(root):
    root.mkdir(parents=True, exist_ok=True)
    config = CollectorConfig(raw={'capability_verification': {'run_on_startup': False}, 'quality': {}},
        path=root/'offline-config.yaml', runtime=RuntimeConfig(
            agent_id='offline_gate_test', agent_group='intelligence_collector',
            state_sqlite_path=root/'state.db', bus_sqlite_path=root/'bus.db', data_sqlite_path=root/'data.db',
            workspace_root=root, log_dir=root/'logs', reports_dir=root/'reports'),
        model=AgentModelConfig(primary='offline/no_model_call', fallbacks=[], require_registered=False),
        tools=ToolConfig(mic_enabled=True, stock_enabled=False, mic_config_dir=None,
                        stock_config_dir=None, python_executable='python', stock_working_dir=None))
    return IntelligenceCollectorAgent(config)


def dispatch(agent, suffix):
    task = {'task_id': 'offline-task', 'task_type': 'mic_deep_collect', 'idempotency_key': 'same-offline-task',
            'target': {'target_id': 'company_300750', 'company_name': '宁德时代', 'ticker': '300750.SZ'}}
    ticket_id = agent.tickets.create_ticket(ticket_type='COLLECTION_TASK_TICKET', source_agent='offline-test',
        target_agent_id=agent.config.runtime.agent_id, priority='normal', payload=task)
    message_id = agent.queue.publish('intelligence.collection', {'ticket_id': ticket_id},
        target_agent_id=agent.config.runtime.agent_id, idempotency_key='offline-message-' + suffix)
    outcome = agent.run_once(topics=['intelligence.collection'])
    return ticket_id, message_id, outcome


def exercise_agent(report, root, corrupt_cached=False):
    agent = new_agent(root)
    original = copy.deepcopy(report)
    with patch.object(agent.mic, '_collect_with_timeout', return_value=copy.deepcopy(report)) as replay:
        first_ticket, first_message, first = dispatch(agent, 'first')
        # Reproduce an old run's unconditional usable=true before testing reuse.
        with agent.data_store.session() as con:
            con.execute('UPDATE collection_runs SET quality_json=?', (json.dumps({'usable': True}),))
            if corrupt_cached:
                con.execute('UPDATE collection_runs SET result_json=?', ('{invalid',))
        second_ticket, second_message, second = dispatch(agent, 'duplicate')
        calls = replay.call_count
    with agent.bus_store.session() as con:
        messages = [dict(r) for r in con.execute('SELECT message_id,status,payload_json FROM messages WHERE topic=?', ('collection.result',))]
        delivery = [dict(r) for r in con.execute('SELECT message_id,status,attempts FROM messages WHERE message_id IN (?,?)', (first_message,second_message))]
    with agent.data_store.session() as con:
        runs = [dict(r) for r in con.execute('SELECT run_id,status,quality_json FROM collection_runs')]
        issue_count = con.execute("SELECT COUNT(*) FROM data_quality_issues WHERE issue_type='mic_quality'").fetchone()[0]
    result = {'first': first, 'duplicate': second, 'saved_report_deliveries': calls, 'network_model_calls': 0,
              'result_messages': [json.loads(row['payload_json']) for row in messages], 'deliveries': delivery,
              'runs': [{**row, 'quality_json': json.loads(row['quality_json'])} for row in runs],
              'ticket_statuses': [agent.tickets.get(t)['status'] for t in (first_ticket, second_ticket)],
              'quality_issue_count': issue_count, 'input_unchanged': report == original}
    return result


class OutputGateTests(unittest.TestCase):
    def evaluate(self, report, **context):
        return QualityGate({'quality': {}}).evaluate(tool_result(report), context=context)

    def test_triage_call_is_not_structured_output(self):
        q=self.evaluate(empty_report())
        self.assertFalse(q['usable']); self.assertEqual(q['output_status'], 'no_structured_output')
        self.assertEqual(q['execution_status'], 'completed'); self.assertEqual(q['severity'], 'P2')

    def test_no_calls_and_no_outputs(self):
        report=empty_report();report['summary']['model_calls']=0
        q=self.evaluate(report)
        self.assertFalse(q['usable'])
        self.assertIn('no_links_or_model_calls', [i['issue_type'] for i in q['issues']])

    def test_link_only_gaps_and_brief_are_not_outputs(self):
        report=empty_report();report['structured_outputs'].update(coverage_gaps=37, briefs=1, analyst_questions=3)
        self.assertFalse(self.evaluate(report)['usable'])

    def test_facts_only_can_be_usable(self):
        report=empty_report();report['structured_outputs']['facts']=2
        self.assertTrue(self.evaluate(report)['usable'])

    def test_metrics_only_can_be_usable(self):
        report=empty_report();report['structured_outputs']['metrics']=1
        self.assertTrue(self.evaluate(report)['usable'])

    def test_legacy_event_list_is_supported(self):
        report={'summary':{'links_read':1,'model_calls':1}, 'top_events':[{'summary':'订单',
            'source': {'url':'https://example.test/notice', 'source_type':'official'}}]}
        self.assertTrue(self.evaluate(report)['usable'])

    def test_cached_output_needs_no_fresh_calls(self):
        report={'summary':{'links_read':0,'model_calls':0,'cached_or_reused_results':1},'structured_outputs':{'facts':2}}
        q=self.evaluate(report)
        self.assertTrue(q['usable'])
        self.assertNotIn('no_links_or_model_calls',[i['issue_type'] for i in q['issues']])

    def test_cache_count_alone_is_not_output(self):
        report=empty_report();report['summary']['cached_or_reused_results']=1
        self.assertFalse(self.evaluate(report)['usable'])

    def test_high_priority_empty_retains_specific_issue(self):
        q=self.evaluate(empty_report(),priority='high')
        self.assertFalse(q['usable'])
        self.assertIn('high_priority_zero_events',[i['issue_type'] for i in q['issues']])

    def test_disabled_optional_research_checks_do_not_make_empty_usable(self):
        gate=QualityGate({'quality':{'mic':{'flag_high_priority_zero_events':False}}})
        self.assertFalse(gate.evaluate(tool_result(empty_report()))['usable'])

    def test_bad_counts_do_not_create_output(self):
        self.assertEqual(mic_output_summary({'structured_outputs':{'facts':'bad','events':-2,'metrics':True}})['structured_output_count'],0)

    def test_tool_failure_still_retries(self):
        result=tool_result(empty_report());result.status='failed'
        result.errors=[{'error_code':'MIC_TOOL_FAILED','retryable':True}]
        q=QualityGate({}).evaluate(result)
        self.assertFalse(q['usable']);self.assertEqual(q['execution_status'],'failed')
        self.assertEqual(_tool_message_action(q,result.errors)[0],'retry')

    def test_empty_completed_result_is_not_retried(self):
        self.assertEqual(_tool_message_action(self.evaluate(empty_report()),[])[0],'ack')

    def test_stock_gate_is_unchanged(self):
        r=ToolResult(tool_name='stock_data_collector',operation='bars',request={},status='success',quality={'usable':True,'data_quality':.9})
        self.assertTrue(QualityGate({}).evaluate(r)['usable'])

    def test_adapter_reports_empty_unusable(self):
        adapter=MICAdapter('/test/config')
        with patch.object(adapter,'_collect_with_timeout',return_value=empty_report()):
            result=adapter.collect(target_id='test',task_profile={})
        self.assertEqual(result.status,'success');self.assertFalse(result.quality['usable'])

    def test_adapter_preserves_explicit_config(self):
        api=types.ModuleType('mic.api');cfg=types.ModuleType('mic.config');package=types.ModuleType('mic')
        marker=object();cfg.load_config=Mock(return_value=marker);api.AnalystAPI=Mock()
        with patch.dict('sys.modules',{'mic':package,'mic.api':api,'mic.config':cfg}):
            MICAdapter('/test/config')._api()
        cfg.load_config.assert_called_once_with('/test/config')
        api.AnalystAPI.assert_called_once_with(config=marker)

    def test_empty_query_result_is_unusable(self):
        adapter=MICAdapter();api=Mock();api.get_recent_events.return_value=[]
        with patch.object(adapter,'_api',return_value=api):
            result=adapter.get_recent_events('test')
        self.assertFalse(result.quality['usable'])

    def test_query_with_event_has_output(self):
        self.assertGreater(mic_output_summary({'events':[{'summary':'订单'}]})['structured_output_count'],0)

    def run_runtime(self, report=None, corrupt_cached=False):
        with tempfile.TemporaryDirectory(dir=os.environ.get('AGENT_GATE_TEST_ROOT')) as tmp:
            return exercise_agent(report or empty_report(),Path(tmp),corrupt_cached)

    def test_real_agent_queue_does_not_claim_empty_usable(self):
        r=self.run_runtime()
        self.assertEqual(r['first']['status'],'processed')
        self.assertFalse(r['first']['result']['usable'])
        self.assertEqual(r['ticket_statuses'],['done','done'])
        self.assertTrue(all(m['status']=='success' and m['output_status']=='no_structured_output'
                            and m['usable'] is False for m in r['result_messages']))
        self.assertGreater(r['quality_issue_count'],0)
        self.assertTrue(all(d['status']=='done' for d in r['deliveries']))

    def test_real_agent_reuse_rechecks_old_usable_true(self):
        r=self.run_runtime()
        self.assertTrue(r['duplicate']['result']['reused'])
        self.assertFalse(r['duplicate']['result']['quality']['usable'])
        self.assertFalse(r['runs'][0]['quality_json']['usable'])
        self.assertEqual(len(r['runs']),1);self.assertEqual(r['saved_report_deliveries'],1)

    def test_corrupt_cached_report_is_not_reexecuted_or_usable(self):
        r=self.run_runtime(corrupt_cached=True)
        self.assertEqual(r['saved_report_deliveries'],1)
        self.assertFalse(r['duplicate']['result']['quality']['usable'])
        self.assertEqual(r['ticket_statuses'][1],'failed')

    def test_facts_only_agent_and_reuse_remain_usable(self):
        report=empty_report();report['structured_outputs']['facts']=2
        r=self.run_runtime(report)
        self.assertTrue(r['first']['result']['usable'])
        self.assertTrue(r['duplicate']['result']['usable'])
        self.assertEqual(r['saved_report_deliveries'],1)


if __name__=='__main__':
    unittest.main()
