"""Offline event-evidence contract tests using real MIC, Agent, queue and SQLite."""
import copy
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mic.config import MICConfig
from mic.pipeline import Pipeline, RunStats
from mic.schemas import BundleExtraction, EventCard, SearchHit
from mic.store.database import Database
from mic.store.repository import Repository
from mic.store import models as models
from agent_trade_intel.agent import IntelligenceCollectorAgent
from agent_trade_intel.config import AgentModelConfig, CollectorConfig, RuntimeConfig, ToolConfig
from agent_trade_intel.reader import IntelligenceReader

EVIDENCE_FIELDS = ('entities', 'metrics', 'evidence_locator', 'impact', 'source_corroboration_status', 'tracking_variables')
SOURCE = {'url':'https://example.invalid/article', 'source_name':'example.invalid', 'source_type':'industry',
          'publish_time':'2026-09-15', 'title':'离线测试材料', 'query_family':'orders_tender'}


def fixture_bundle():
    return BundleExtraction.model_validate({
        'source_link_id':'fixture-source', 'decision':'save_structured', 'overall_score':74,
        'events': [
            {'event_type':'tender', 'event_date':None, 'summary':'测试甲标段', 'confidence':0.88,
             'entities':{'subject':'测试公司', 'counterparty':None, 'counterparty_candidate':'候选业主', 'counterparty_status':'pending_review'},
             'metrics':{'amount':41416220,'currency':'CNY','amount_unit':'元','amount_raw':4141.622,'amount_raw_unit':'万元',
                        'amount_evidence':{'passage_id':'p6','quote':'4141.622万元'},
                        'event_date_review':{'candidate':'2026-09-15','status':'pending_review','candidate_passages':[{'passage_id':'p4'}]},
                        'source_price_basis_status':'pending_review'},
             'evidence_locator':{'passage_id':'p6','section':'正文'}, 'source_corroboration_status':'single_source',
             'impact':{'direction':'positive','channels':['demand'],'horizon':'quarter','magnitude_guess':'low'}},
            {'event_type':'tender','event_date':'2026-09-15','summary':'测试总项目','confidence':0.8,
             'entities':{'subject':'测试项目','counterparty':None},
             'metrics':{'amount':None,'amount_candidate':23603.222,'amount_status':'pending_review','capacity':400},
             'evidence_locator':{'passage_id':'p4'},'source_corroboration_status':'single_source'},
        ]})


def pipeline_without_services():
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = MICConfig(raw={'target_profiles':{'company_300750':{'canonical_name':'宁德时代'}}})
    pipeline.triage = SimpleNamespace(source_type=lambda domain:'industry')
    return pipeline


def export_fresh(bundle, source):
    pipeline, stats = pipeline_without_services(), RunStats()
    pipeline._tally(stats, bundle, source_metadata=source)
    report = pipeline._summary('offline-evidence-replay', 'company_300750', {}, stats)
    report['replay_context'] = {'mode':'offline_saved_bundle', 'new_model_calls':0}
    return report


def export_cached(bundle, source, root):
    root.mkdir(parents=True, exist_ok=True)
    db = Database('sqlite:///'+str(root/'mic-cache.db'))
    try:
        db.create_all()
        repo = Repository(db)
        previous = bundle.source_link_id or 'fixture-source'
        target = 'offline-cloned-source'
        with db.session() as session:
            session.add(models.SourceLink(id=previous,url=source['url']))
            session.add(models.SourceLink(id=target,url=source['url']))
        repo.save_merged_analysis('company_300750',previous,bundle,{'merge_method':'single_model'},'offline-old-run')
        cloned = repo.clone_latest_analysis(previous,target,'company_300750')
        hit = SearchHit(query='offline replay',url=source['url'],title=source.get('title') or '',domain=source.get('source_name') or '',
                        publish_time_guess=source.get('publish_time'),query_family=source.get('query_family'))
        pipeline, stats = pipeline_without_services(), RunStats()
        stats.cached_or_reused_results = 1
        pipeline._tally_cloned(stats,cloned,hit)
        return pipeline._summary('offline-cache-replay','company_300750',{},stats), cloned
    finally:
        db.engine.dispose()


def through_agent(report, root):
    root.mkdir(parents=True,exist_ok=True)
    config = CollectorConfig(raw={'capability_verification':{'run_on_startup':False},'quality':{}},
        path=root/'offline.yaml', runtime=RuntimeConfig(agent_id='offline_event_export',agent_group='intelligence_collector',
            state_sqlite_path=root/'state.db',bus_sqlite_path=root/'bus.db',data_sqlite_path=root/'data.db',
            workspace_root=root,log_dir=root/'logs',reports_dir=root/'reports'),
        model=AgentModelConfig(primary='offline/no-call',fallbacks=[],require_registered=False),
        tools=ToolConfig(mic_enabled=True,stock_enabled=False,mic_config_dir=None,stock_config_dir=None,
                         python_executable='python',stock_working_dir=None))
    agent = IntelligenceCollectorAgent(config)
    task = {'task_id':'offline-evidence','task_type':'mic_deep_collect','idempotency_key':'offline-evidence',
            'target':{'target_id':'company_300750','ticker':'300750.SZ','company_name':'宁德时代'}}
    outcomes=[]
    with patch.object(agent.mic,'_collect_with_timeout',return_value=copy.deepcopy(report)) as provider:
        for number in range(2):
            ticket = agent.tickets.create_ticket(ticket_type='COLLECTION_TASK_TICKET',source_agent='offline-test',
                target_agent_id=config.runtime.agent_id,priority='normal',payload=task)
            agent.queue.publish('intelligence.collection',{'ticket_id':ticket},target_agent_id=config.runtime.agent_id,
                                idempotency_key='offline-delivery-'+str(number))
            outcomes.append(agent.run_once(topics=['intelligence.collection']))
        count = provider.call_count
    stored = IntelligenceReader(agent.data_store,agent.bus_store,agent.state_store).read_recent_events(target_id='company_300750')['items']
    return {'events':stored,'outcomes':outcomes,'saved_report_deliveries':count,'new_model_calls':0}


def evidence_roundtrip(expected, actual):
    left={(e['event_type'],e['summary']):e for e in expected}
    right={(e['payload']['event_type'],e['payload']['summary']):e for e in actual}
    return (len(left)==len(expected)==len(actual)==len(right)
        and left.keys()==right.keys()
        and all(all(right[key]['payload'].get(field)==ev.get(field) for field in EVIDENCE_FIELDS)
                and right[key]['impact']==ev.get('impact',{})
                and right[key]['payload'].get('event_date')==ev.get('event_date')
                and right[key]['event_date']==ev.get('event_date')
                and right[key]['source_corroboration_status']==ev.get('source_corroboration_status')
                and right[key]['source_url']==ev.get('source',{}).get('url')
                for key,ev in left.items()))


class EventEvidenceExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('EVENT_EXPORT_TEST_ROOT'))
        self.root=Path(self.tmp.name)
        self.bundle=fixture_bundle()
    def tearDown(self): self.tmp.cleanup()

    def test_fresh_exports_full_reviewed_fields(self):
        report=export_fresh(self.bundle,SOURCE)
        for raw,event in zip(self.bundle.events,report['all_events']):
            for name,value in raw.model_dump(mode='json').items(): self.assertEqual(event[name],value)

    def test_fresh_does_not_alias_nested_model_fields(self):
        report=export_fresh(self.bundle,SOURCE)
        report['all_events'][0]['metrics']['event_date_review']['candidate']='changed'
        self.assertEqual(self.bundle.events[0].metrics['event_date_review']['candidate'],'2026-09-15')

    def test_pending_values_stay_unconfirmed(self):
        events=export_fresh(self.bundle,SOURCE)['all_events']
        self.assertIsNone(events[0]['event_date']);self.assertIsNone(events[0]['entities']['counterparty'])
        self.assertIsNone(events[1]['metrics']['amount']);self.assertEqual(events[1]['metrics']['amount_status'],'pending_review')

    def test_compatibility_channels_and_full_event_list(self):
        self.bundle.events.extend([EventCard(summary='minor-'+str(i),confidence=0.1) for i in range(5)])
        report=export_fresh(self.bundle,SOURCE)
        self.assertEqual(len(report['all_events']),7);self.assertEqual(len(report['top_events']),5)
        self.assertEqual(report['all_events'][0]['impact_channels'],['demand'])

    def test_fresh_agent_queue_and_reader_roundtrip(self):
        report=export_fresh(self.bundle,SOURCE)
        result=through_agent(report,self.root/'agent')
        self.assertTrue(evidence_roundtrip(report['all_events'],result['events']))
        self.assertEqual(result['saved_report_deliveries'],1)
        self.assertTrue(result['outcomes'][1]['result']['reused'])

    def test_cache_exports_review_fields_and_new_source(self):
        report,cloned=export_cached(self.bundle,SOURCE,self.root/'cache')
        expected={e.summary:e.model_dump(mode='json') for e in self.bundle.events}
        for event in report['all_events']:
            self.assertEqual(event['source_link_id'],'offline-cloned-source')
            for field in EVIDENCE_FIELDS:self.assertEqual(event[field],expected[event['summary']][field])
            self.assertEqual((event.get('event_date') or '').split('T')[0],
                             (expected[event['summary']].get('event_date') or '').split('T')[0])
        self.assertEqual(cloned['events'],2)

    def test_cache_agent_queue_and_reader_roundtrip(self):
        report,_=export_cached(self.bundle,SOURCE,self.root/'cache')
        result=through_agent(report,self.root/'agent')
        self.assertTrue(evidence_roundtrip(report['all_events'],result['events']))
        self.assertEqual(result['saved_report_deliveries'],1)

    def test_legacy_agent_report_keeps_unknown_evidence_unknown(self):
        report={'search_run_id':'legacy','all_events':[{'event_type':'other','summary':'旧格式事件','confidence':0.5,
                    'source':{'url':SOURCE['url'],'source_type':'industry'}}]}
        result=through_agent(report,self.root/'legacy')
        event=result['events'][0]
        self.assertNotIn('evidence_locator',event['payload']);self.assertIsNone(event['source_corroboration_status'])

    def test_legacy_cached_null_fields_are_not_invented(self):
        db=Database('sqlite:///'+str(self.root/'legacy.db'));db.create_all()
        try:
            with db.session() as s:
                s.add(models.MergedAnalysis(id='old',source_link_id='source',decision='save_structured',overall_score=80))
                s.add(models.EventCardRow(id='old-event',source_link_id='source',summary='旧记录',event_type='other',confidence=0.5))
            event=Repository(db).clone_latest_analysis('source','new','company_300750')['cloned_events'][0]
            for name in ('entities','metrics','impact','evidence_locator','source_corroboration_status'):self.assertIsNone(event[name])
        finally:db.engine.dispose()

    def test_empty_bundle_stays_empty(self):
        report=export_fresh(BundleExtraction(decision='link_only'),SOURCE)
        self.assertEqual(report['all_events'],[]);self.assertEqual(report['structured_outputs']['events'],0)


if __name__=='__main__': unittest.main()
