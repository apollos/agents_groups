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
from mic.event_resolution import build_context, finalize
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


def pipeline_without_services(repo=None):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = MICConfig(raw={'target_profiles':{'company_300750':{'canonical_name':'宁德时代'}}})
    pipeline.triage = SimpleNamespace(source_type=lambda domain:'industry')
    # Fresh event-only fixtures have no persisted coverage gaps. Cache exports
    # must use their real repository so summary readback still exercises storage.
    pipeline.repo = repo if repo is not None else SimpleNamespace(
        get_coverage_gaps=lambda target_id, *, search_run_id: [])
    return pipeline


def export_fresh(bundle, source):
    pipeline, stats = pipeline_without_services(), RunStats()
    passages = [{'passage_id': e.evidence_locator.passage_id, 'text': '离线证据测试正文：' + e.summary}
                for e in bundle.events]
    for event, passage in zip(bundle.events, passages):
        event.event_resolution = {'reviewed': True, 'verdict': 'new', 'reason': '空库的独立测试事项',
                                 'current_evidence': [{'passage_id': passage['passage_id'], 'quote': passage['text']}],
                                 'comparisons': []}
    finalize(bundle, build_context(None, []), passages, run_id='offline-evidence-replay', link_id=bundle.source_link_id)
    pipeline._tally(stats, bundle, source_metadata=source)
    report = pipeline._summary('offline-evidence-replay', 'company_300750', {}, stats)
    # This fixture represents a pre-content-review saved report. Current
    # protocol admission/queue coverage lives in test_content_review.py.
    report.pop('content_review_protocol', None)
    report['replay_context'] = {'mode':'offline_saved_bundle', 'new_model_calls':0}
    return report


def stored_event_rows(db, source_link_id):
    with db.session() as session:
        rows = session.query(models.EventCardRow).filter_by(source_link_id=source_link_id).all()
        return {row.id: {column.key: copy.deepcopy(getattr(row, column.key))
                         for column in models.EventCardRow.__table__.columns} for row in rows}


def legacy_date_review():
    # A historical approval has no typed date or date-role assessment. It is
    # retained as audit history, never upgraded to current date authorization.
    return {'protocol': 'content_review_v2',
            'claims': {'old-observation': {'kind': 'observation', 'effective_status': 'source_supported',
                       'evidence': [{'passage_id': 'p4', 'quote': '测试总项目'}], 'field_values': {}}},
            'fields': {'event_date': {'status': 'source_supported', 'claim_ids': ['old-observation']}},
            'materialized_digest': 'historical-unverified-digest'}


def export_cached(bundle, source, root, *, original_rows=None):
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
        repo.save_merged_analysis('company_300750',previous,bundle,{'merge_method':'single_model'},'offline-old-run', allow_legacy_write=True)
        if original_rows is not None:
            original_rows.update(stored_event_rows(db, previous))
        cloned = repo.clone_latest_analysis(previous,target,'company_300750')
        hit = SearchHit(query='offline replay',url=source['url'],title=source.get('title') or '',domain=source.get('source_name') or '',
                        publish_time_guess=source.get('publish_time'),query_family=source.get('query_family'))
        pipeline, stats = pipeline_without_services(repo), RunStats()
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
    from agent_trade_intel.db import loads_json
    with agent.data_store.session() as con:
        pending = [loads_json(r['payload_json'], {}) for r in con.execute('SELECT payload_json FROM pending_event_resolutions')]
    return {'events':stored,'pending':pending,'outcomes':outcomes,'saved_report_deliveries':count,'new_model_calls':0}


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
        self.bundle.events[1].content_review=legacy_date_review()
        original_bundle=self.bundle.model_dump(mode='json')
        expected={e.summary:e.model_dump(mode='json') for e in self.bundle.events}
        original_rows={}
        report,cloned=export_cached(self.bundle,SOURCE,self.root/'cache',original_rows=original_rows)
        self.assertEqual(self.bundle.model_dump(mode='json'),original_bundle)
        for event in report['all_events']:
            self.assertEqual(event['source_link_id'],'offline-cloned-source')
            for field in EVIDENCE_FIELDS:self.assertEqual(event[field],expected[event['summary']][field])
            self.assertIsNone(event['event_date'])
            old=expected[event['summary']]
            review=event['content_review']
            if old['event_date'] is not None:
                self.assertEqual(review['claims'],old['content_review']['claims'])
                self.assertEqual(review['fields'],old['content_review']['fields'])
                self.assertEqual(review['prior_materialized_digest'],old['content_review']['materialized_digest'])
                self.assertNotIn('materialized_digest',review)
                self.assertEqual(review['materialization_corrections'],[{
                    'field':'event_date','original_materialized_value':'2026-09-15T00:00:00',
                    'published_value':None,'status':'pending_review',
                    'reason':'stored_date_review_missing_or_unverified'}])
            else:
                self.assertEqual(review,old['content_review'])
        self.assertEqual(cloned['events'],2)
        # Read raw rows: the public projection must not rewrite historical data.
        db=Database('sqlite:///'+str(self.root/'cache'/'mic-cache.db'))
        try:
            self.assertEqual(stored_event_rows(db,self.bundle.source_link_id),original_rows)
        finally:
            db.engine.dispose()

    def test_cache_without_fresh_comparison_keeps_evidence_pending(self):
        self.bundle.events[1].content_review=legacy_date_review()
        report,_=export_cached(self.bundle,SOURCE,self.root/'cache')
        result=through_agent(report,self.root/'agent')
        self.assertEqual(result['events'], [])
        self.assertEqual(len(result['pending']), len(report['all_events']))
        for expected, actual in zip(report['all_events'], result['pending']):
            for field in EVIDENCE_FIELDS:
                self.assertEqual(expected.get(field), actual.get(field))
            self.assertIsNone(expected['event_date'])
            self.assertIsNone(actual['event_date'])
            self.assertEqual(actual['content_review'],expected['content_review'])
        corrected=[event for event in result['pending']
                   if event['content_review'].get('materialization_corrections')]
        self.assertEqual(len(corrected),1)
        correction,=corrected[0]['content_review']['materialization_corrections']
        self.assertEqual(correction['original_materialized_value'],'2026-09-15T00:00:00')
        self.assertIsNone(correction['published_value'])
        self.assertEqual(correction['status'],'pending_review')
        self.assertEqual(correction['reason'],'stored_date_review_missing_or_unverified')
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
                s.add(models.EventCardRow(id='old-event',source_link_id='source',merged_analysis_id='old',
                                         summary='旧记录',event_type='other',confidence=0.5))
            event=Repository(db).clone_latest_analysis('source','new','company_300750')['cloned_events'][0]
            for name in ('entities','metrics','impact','evidence_locator','source_corroboration_status'):self.assertIsNone(event[name])
        finally:db.engine.dispose()

    def test_legacy_orphan_event_is_not_assigned_to_latest_analysis_for_cache(self):
        db=Database('sqlite:///'+str(self.root/'orphan.db'));db.create_all()
        try:
            with db.session() as s:
                s.add(models.MergedAnalysis(id='old',source_link_id='source',decision='save_structured',overall_score=80))
                s.add(models.EventCardRow(id='orphan-event',source_link_id='source',
                                         summary='未关联的旧记录',event_type='other',confidence=0.5))
            original=stored_event_rows(db,'source')
            result=Repository(db).clone_latest_analysis('source','new','company_300750')
            self.assertEqual(result['events'],0)
            self.assertEqual(result['cloned_events'],[])
            self.assertEqual(stored_event_rows(db,'new'),{})
            self.assertEqual(stored_event_rows(db,'source'),original)
            self.assertIsNone(original['orphan-event']['merged_analysis_id'])
        finally:db.engine.dispose()

    def test_empty_bundle_stays_empty(self):
        report=export_fresh(BundleExtraction(decision='link_only'),SOURCE)
        self.assertEqual(report['all_events'],[]);self.assertEqual(report['structured_outputs']['events'],0)


if __name__=='__main__': unittest.main()
