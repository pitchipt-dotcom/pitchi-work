import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from pitchi_work.cloud import (CloudRoleAdapter, CloudControlStop, configuration,
    fingerprint, sdk_generate, sdk_client, advisor_trigger, adapter_for, estimate,
    read_key, terminal_approval)
from pitchi_work.model import ModelFailure
from pitchi_work.store import RunStore, dump, load, Cancelled


class CloudAdapters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = RunStore(Path(self.tmp.name)/'run')
        dump(self.run.path/'state.json',{'status':'READY','active_processes':{}})

    def config(self,provider='openai',**overrides):
        return {**configuration(provider,allowed=True),**overrides}

    def client(self,provider='openai',text='{"answer":"synthetic"}',finish='completed'):
        client=Mock()
        if provider=='openai':
            client.responses.create.return_value=NS(model='gpt-6-luna',output_text=text,status=finish,
                usage=NS(input_tokens=20,output_tokens=10,total_tokens=30),id='test-response')
        else:
            client.messages.create.return_value=NS(model='claude-haiku-5-5',content=[NS(type='text',text=text)],
                stop_reason='end_turn' if finish=='completed' else 'max_tokens',
                usage=NS(input_tokens=20,output_tokens=10),id='test-response')
        return client

    def adapter(self,provider='openai',approval=True,client=None,**config):
        return CloudRoleAdapter(self.run,self.config(provider,**config),approval=lambda p,h:approval,
                                client=client or self.client(provider))

    def receipt(self):
        return load(next((self.run.path/'artifacts').glob('role_call_*/receipt.json')))

    def test_both_official_apis_common_worker_and_reviewer_shape(self):
        for provider,role in [('openai','worker'),('anthropic','reviewer')]:
            with self.subTest(provider=provider):
                client=self.client(provider)
                adapter=self.adapter(provider,client=client)
                value=adapter.ask(role,'synthetic evidence only')
                self.assertEqual(value,{'answer':'synthetic'})
                method=client.responses.create if provider=='openai' else client.messages.create
                params=method.call_args.kwargs
                self.assertLessEqual(params.get('max_output_tokens',params.get('max_tokens')),1800)
                if provider=='openai':self.assertIs(params['store'],False)
                else:self.assertTrue(params['system']);self.assertEqual(params['messages'][0]['role'],'user')
        self.assertEqual(self.run.state()['cloud_call_count'],2)

    def test_denied_approval_sends_nothing(self):
        client=self.client()
        with self.assertRaises(CloudControlStop):self.adapter(approval=False,client=client).ask('worker','private fixture')
        client.responses.create.assert_not_called()
        self.assertNotIn('cloud_call_count',self.run.state())

    def test_unavailable_model_is_clear_and_error_body_not_logged(self):
        for code,expected in [(401,'CLOUD_AUTHENTICATION_FAILED'),(403,'CLOUD_MODEL_ACCESS_DENIED'),(404,'CLOUD_MODEL_UNAVAILABLE'),(429,'CLOUD_RATE_LIMITED')]:
            error=RuntimeError('PRIVATE PROVIDER RESPONSE');error.status_code=code
            client=self.client();client.responses.create.side_effect=error
            with self.assertRaises((ModelFailure,CloudControlStop)) as raised:self.adapter(client=client).ask('worker','private input')
            self.assertEqual(raised.exception.failure_class,expected)
            self.assertNotIn('PRIVATE',str(raised.exception))
        for p in (self.run.path/'artifacts').glob('*/receipt.json'):
            self.assertNotIn('PRIVATE',p.read_text());self.assertNotIn('private input',p.read_text())

    def test_per_run_budget_persists_across_adapters(self):
        self.adapter(max_calls_per_run=1).ask('worker','fixture')
        with self.assertRaises(CloudControlStop) as raised:self.adapter(max_calls_per_run=1).ask('reviewer','fixture')
        self.assertEqual(raised.exception.failure_class,'CLOUD_CALL_LIMIT')
        self.assertEqual(self.run.state()['cloud_call_count'],1)

    def test_input_limit_and_context_guard_do_not_send(self):
        for text,limits in [('x'*1000,{'max_input_bytes':100}),('x'*100000,{})]:
            client=self.client()
            with self.assertRaises(CloudControlStop):self.adapter(client=client,**limits).ask('worker',text)
            client.responses.create.assert_not_called()

    def test_output_incomplete_rejected_but_billed_usage_kept(self):
        for provider in ('openai','anthropic'):
            with self.assertRaises(ModelFailure) as raised:
                self.adapter(provider,client=self.client(provider,finish='incomplete')).ask('worker','fixture')
            self.assertEqual(raised.exception.failure_class,'AGENT_OUTPUT_INCOMPLETE')
        for p in (self.run.path/'artifacts').glob('*/receipt.json'):
            r=load(p);self.assertEqual(r['usage']['total_tokens'],30);self.assertFalse(r['success'])

    def test_invalid_json_or_shape_rejected(self):
        for raw in ('not json','[]'):
            with self.assertRaises(ModelFailure):self.adapter(client=self.client(text=raw)).ask('worker','fixture')

    def test_usage_cost_and_receipt_do_not_contain_bodies(self):
        self.adapter().ask('worker','private fixture text')
        r=self.receipt();self.assertTrue(r['success']);self.assertAlmostEqual(r['estimated_cost_usd'],.000007)
        self.assertNotIn('private fixture text',json.dumps(r));self.assertNotIn('synthetic',json.dumps(r))
        self.assertIsNone(estimate(self.config('anthropic'),{'prompt_tokens':20,'completion_tokens':10}))

    def test_prices_can_be_configured(self):
        path=Path(self.tmp.name)/'config.json'
        dump(path,{'model':'custom-model','input_usd_per_million':2,'output_usd_per_million':10})
        config=configuration('anthropic',path=path,allowed=True)
        self.assertEqual(estimate(config,{'prompt_tokens':1000000,'completion_tokens':1000000}),12)

    def test_no_unknown_endpoint_or_unbounded_limits(self):
        path=Path(self.tmp.name)/'config.json'
        for value in ({'endpoint':'https://untrusted.invalid'},{'max_attempts':2},{'max_calls_per_run':0},{'max_output_tokens':999999}):
            dump(path,value)
            with self.assertRaises(ValueError):configuration('openai',path=path,allowed=True)
        with self.assertRaises(CloudControlStop):configuration('openai')

    def test_local_is_default_and_legacy_adapter_not_replaced(self):
        self.run.save_state(local_model_endpoint='http://localhost/v1',local_model='local')
        local=Mock(return_value='local-adapter')
        self.assertEqual(adapter_for(self.run,local),'local-adapter')
        local.assert_called_once()

    def test_advisor_hook_is_off_and_never_executes(self):
        self.assertFalse(advisor_trigger({'status':'STALLED'})['eligible'])
        value=advisor_trigger({'run_id':'subject','status':'STALLED'},enabled=True)
        self.assertTrue(value['eligible']);self.assertFalse(value['automatic_execution'])
        self.assertEqual(value['subject_run_id'],'subject')
        from pitchi_work.cloud import cloud_advice
        with patch('pitchi_work.cloud.CloudRoleAdapter') as adapter:
            self.assertFalse(cloud_advice(self.run,self.config(),{})['called']);adapter.assert_not_called()
            adapter.return_value.ask.return_value={'suggestion':'inspect evidence'}
            result=cloud_advice(self.run,self.config(),{'failure_class':'synthetic'},enabled=True)
            self.assertEqual(result['authority'],'suggestion_only')
            self.assertEqual(self.run.state()['status'],'READY')

    def test_noninteractive_has_no_implicit_content_approval(self):
        with patch('sys.stdin.isatty',return_value=False):
            self.assertFalse(terminal_approval({'secret':'private'},'hash'))

    def test_cancelled_run_cannot_call_cloud(self):
        dump(self.run.path/'cancel_request.json',{})
        with self.assertRaises(Cancelled):self.adapter().ask('worker','fixture')

    def test_messages_and_schema_part_of_exact_approval(self):
        seen=[]
        adapter=CloudRoleAdapter(self.run,self.config(),approval=lambda p,h:seen.append((p,h)) or True,client=self.client())
        adapter.ask('worker','fixture',output_schema={'type':'object'})
        payload,digest=seen[0];self.assertEqual(digest,fingerprint(payload))
        self.assertIn('schema',payload['messages'][-1]['content'])
        self.assertNotIn('api_key',json.dumps(payload))

    def test_payload_changes_during_consent_are_rejected(self):
        def mutate(payload,digest):
            payload['messages'].append({'role':'user','content':'unapproved extra'})
            return True
        client=self.client()
        adapter=CloudRoleAdapter(self.run,self.config(),approval=mutate,client=client)
        with self.assertRaises(CloudControlStop) as raised:adapter.ask('worker','fixture')
        self.assertEqual(raised.exception.failure_class,'CLOUD_APPROVAL_MISMATCH')
        client.responses.create.assert_not_called()

    def test_credentials_blocked_before_review_or_transmission(self):
        approval=Mock(return_value=True);client=self.client()
        adapter=CloudRoleAdapter(self.run,self.config(),approval=approval,client=client)
        with self.assertRaises(CloudControlStop):adapter.ask('worker','sk-'+('a'*20))
        approval.assert_not_called();client.responses.create.assert_not_called()

    def test_environment_key_never_enters_client_logging(self):
        with patch.dict('os.environ',{'OPENAI_API_KEY':'synthetic-not-a-real-key'}):
            self.assertEqual(read_key('openai'),'synthetic-not-a-real-key')

    def test_official_sdk_constructors_disable_retries_and_fix_url(self):
        for provider,constructor in [('openai','OpenAI'),('anthropic','Anthropic')]:
            module=Mock();factory=getattr(module,constructor)
            with patch.dict('sys.modules',{provider:module}),patch('pitchi_work.cloud.read_key',return_value='fake'):
                sdk_client(provider,30)
            kwargs=factory.call_args.kwargs
            self.assertEqual(kwargs['max_retries'],0);self.assertEqual(kwargs['timeout'],30)
            self.assertTrue(kwargs['base_url'].startswith('https://api.'))

    def test_diagnostic_is_offline_and_hash_binds_live_probe(self):
        from pitchi_work.cloud_cli import execute
        args=NS(provider='openai',model=None,config=None,action='test',allow_external=False,approve_sha=None,home=Path(self.tmp.name))
        with patch('pitchi_work.cloud_cli.CloudRoleAdapter') as adapter:
            value=execute(args);self.assertFalse(value['sent']);adapter.assert_not_called()
            args.allow_external=True;args.approve_sha='wrong'
            with self.assertRaises(CloudControlStop):execute(args)
            adapter.assert_not_called()

    def test_host_core_consent_stop_is_not_worker_retry(self):
        from pitchi_work.mission_controller import MissionController
        self.run.save_state(workspace=self.tmp.name,local_model_endpoint='http://localhost/v1',local_model='local')
        controller=MissionController(self.run)
        with patch.object(controller,'setup',side_effect=CloudControlStop('EXTERNAL_CONTENT_APPROVAL_REQUIRED','No consent')):
            result=controller.execute()
        self.assertEqual(result['status'],'NEEDS_HUMAN');self.assertEqual(result['failure_owner'],'policy')

    def test_cloud_cli_binds_provider_without_local_health_or_live_request(self):
        from pitchi_work.cli import main
        with patch('pitchi_work.cli.health') as health,patch('pitchi_work.cli.create_mission',return_value=self.run),\
             patch('pitchi_work.cli.foreground',return_value=0),contextlib.redirect_stdout(io.StringIO()):
            result=main(['--home',self.tmp.name,'start','--workspace',self.tmp.name,
                '--request','Investigate files','--provider','anthropic','--allow-external'])
        self.assertEqual(result,0);health.assert_not_called()
        self.assertEqual(self.run.state()['llm_config']['provider'],'anthropic')

    def test_cloud_cli_without_permission_does_not_create_run(self):
        from pitchi_work.cli import main
        with patch('pitchi_work.cli.create_mission') as create,contextlib.redirect_stderr(io.StringIO()):
            result=main(['start','--workspace',self.tmp.name,'--request','Inspect files','--provider','openai'])
        self.assertEqual(result,2);create.assert_not_called()

    def test_invalid_restored_settings_rejected(self):
        for config in [self.config(max_calls_per_run=99999),self.config(endpoint='https://invalid')]:
            with self.assertRaises(ValueError):CloudRoleAdapter(self.run,config)

    def test_transport_wall_timeout_stops_only_registered_child(self):
        adapter=self.adapter();process=Mock(pid=1234);process.poll.return_value=None
        with patch('pitchi_work.cloud.sdk_python',return_value='python'),\
             patch('pitchi_work.cloud.subprocess.Popen',return_value=process),\
             patch.object(self.run,'register_process') as register,\
             patch.object(self.run,'unregister_process') as unregister,\
             patch('pitchi_work.cloud.time.monotonic',side_effect=[0,2]),\
             patch('pitchi_work.cloud.os.killpg') as stop:
            with self.assertRaises(ModelFailure) as raised:adapter.transport({'provider':'openai'},1)
        self.assertEqual(raised.exception.failure_class,'MODEL_TIMEOUT')
        register.assert_called_once_with(1234,'cloud_llm');unregister.assert_called_once_with(1234)
        self.assertEqual(stop.call_count,1);process.wait.assert_called_once_with(timeout=1.5)

    def test_transport_cancellation_closes_child_and_reaps(self):
        adapter=self.adapter();process=Mock(pid=1234);process.poll.return_value=None
        with patch('pitchi_work.cloud.sdk_python',return_value='python'),\
             patch('pitchi_work.cloud.subprocess.Popen',return_value=process),\
             patch.object(self.run,'register_process'),patch.object(self.run,'unregister_process'),\
             patch.object(self.run,'raise_if_cancelled',side_effect=Cancelled('cancel')) ,\
             patch('pitchi_work.cloud.os.killpg') as stop:
            with self.assertRaises(Cancelled):adapter.transport({'provider':'openai'},1)
        self.assertEqual(stop.call_count,1);process.wait.assert_called_once_with(timeout=1.5)


class OfficialSDKMockHTTP(unittest.TestCase):
    """Exercise actual official SDK decoding with in-memory HTTP, never network."""
    def test_openai_responses_sdk_wire_shape(self):
        try:
            import httpx
            from openai import OpenAI
        except ImportError:self.skipTest('Install cloud extras for official SDK MockTransport test')
        requests=[]
        def respond(request):
            requests.append(request)
            return httpx.Response(200,json={'id':'resp_synthetic','object':'response','created_at':1,
                'status':'completed','model':'gpt-6-luna','output':[{'type':'message','id':'msg_test',
                'role':'assistant','status':'completed','content':[{'type':'output_text','text':'{"ok":true}','annotations':[]}]}],
                'usage':{'input_tokens':2,'output_tokens':4,'total_tokens':6}})
        with OpenAI(api_key='fake-test-key',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            result=sdk_generate(client,{'provider':'openai','model':'gpt-6-luna',
                'messages':[{'role':'user','content':'synthetic'}],'max_output_tokens':128})
        self.assertEqual(result['content'],'{"ok":true}');self.assertEqual(result['usage']['total_tokens'],6)
        self.assertEqual(requests[0].url.path,'/v1/responses')
        self.assertFalse(json.loads(requests[0].content)['store'])

    def test_anthropic_messages_sdk_wire_shape(self):
        try:
            import httpx
            from anthropic import Anthropic
        except ImportError:self.skipTest('Install cloud extras for official SDK MockTransport test')
        requests=[]
        def respond(request):
            requests.append(request)
            return httpx.Response(200,json={'id':'msg_synthetic','type':'message','role':'assistant',
                'model':'claude-haiku-5-5','content':[{'type':'text','text':'{"ok":true}'}],
                'stop_reason':'end_turn','stop_sequence':None,'usage':{'input_tokens':2,'output_tokens':4}})
        with Anthropic(api_key='fake-test-key',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            result=sdk_generate(client,{'provider':'anthropic','model':'claude-haiku-5-5',
                'messages':[{'role':'system','content':'Return JSON'},{'role':'user','content':'synthetic'}],
                'max_output_tokens':128})
        self.assertEqual(result['content'],'{"ok":true}');self.assertEqual(result['usage']['total_tokens'],6)
        self.assertEqual(requests[0].url.path,'/v1/messages')
        self.assertEqual(json.loads(requests[0].content)['system'],'Return JSON')


if __name__=='__main__':unittest.main()
