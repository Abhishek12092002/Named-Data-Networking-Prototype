import os
import sys
import time
import unittest

try:
    import flask  # noqa: F401
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False

if FLASK_AVAILABLE:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'webapp'))
    from webapp.app import app  # noqa: E402


@unittest.skipUnless(FLASK_AVAILABLE, 'flask is not installed')
class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_index_renders(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Run simulation', resp.data)

    def test_run_produces_results(self):
        resp = self.client.post('/run', data={
            'topology': 'mesh', 'nodes': '10', 'extra_edges': '4', 'consumer_count': '3',
            'forwarding': 'best-route', 'cache_policy': 'lfu', 'cache_size': '8',
            'workload': 'zipf', 'requests': '100', 'items_per_prefix': '30', 'zipf_alpha': '1.1',
        })
        self.assertEqual(resp.status_code, 200)
        text = resp.data.decode()
        self.assertNotIn('error-box', text)
        self.assertIn('plot-card', text)

    def test_run_sdn_centralized_shows_controller(self):
        resp = self.client.post('/run', data={
            'topology': 'mesh', 'nodes': '10', 'extra_edges': '4', 'consumer_count': '3',
            'forwarding': 'sdn-centralized', 'controller_rtt_ms': '5',
            'cache_policy': 'lfu', 'cache_size': '8',
            'workload': 'zipf', 'requests': '100', 'items_per_prefix': '30', 'zipf_alpha': '1.1',
            'fail_edge': '0,1', 'fail_time_ms': '20', 'recover_time_ms': '60',
        })
        text = resp.data.decode()
        self.assertIn('SDN controller', text)

    def test_run_rejects_oversized_sweep(self):
        resp = self.client.post('/run', data={
            'topology': 'mesh', 'nodes': '10', 'extra_edges': '4', 'consumer_count': '3',
            'forwardings': 'best-route,sdn-centralized', 'cache_policies': 'lfu,sdn-coordinated',
            'cache_sizes': '0,8,16,32', 'seeds': '1,2',
            'workload': 'zipf', 'requests': '100', 'items_per_prefix': '30',
        })
        text = resp.data.decode()
        self.assertIn('error-box', text)

    def test_run_with_architecture_pair_shows_comparison(self):
        resp = self.client.post('/run', data={
            'topology': 'mesh', 'nodes': '10', 'extra_edges': '4', 'consumer_count': '3',
            'architectures': ['ip', 'ip_sdn'],
            'cache_policy': 'lfu', 'cache_size': '8',
            'workload': 'zipf', 'requests': '80', 'items_per_prefix': '20', 'zipf_alpha': '1.1',
        })
        self.assertEqual(resp.status_code, 200)
        text = resp.data.decode()
        self.assertNotIn('error-box', text)
        self.assertIn('Comparison', text)
        self.assertIn('IP + SDN', text)

    def test_run_single_architecture_prompts_for_pair(self):
        resp = self.client.post('/run', data={
            'topology': 'mesh', 'nodes': '10', 'extra_edges': '4', 'consumer_count': '3',
            'architectures': ['ndn'],
            'cache_policy': 'lfu', 'cache_size': '8',
            'workload': 'zipf', 'requests': '80', 'items_per_prefix': '20', 'zipf_alpha': '1.1',
        })
        self.assertEqual(resp.status_code, 200)
        text = resp.data.decode()
        self.assertNotIn('error-box', text)
        self.assertIn('Select both', text)

    def test_network_profile_overrides_manual_link_fields(self):
        resp = self.client.post('/run', data={
            'topology': 'mesh', 'nodes': '10', 'extra_edges': '4', 'consumer_count': '3',
            'network_profile': 'INTERCONTINENTAL',
            'cache_policy': 'lfu', 'cache_size': '8',
            'workload': 'zipf', 'requests': '60', 'items_per_prefix': '20', 'zipf_alpha': '1.1',
        })
        self.assertEqual(resp.status_code, 200)
        text = resp.data.decode()
        self.assertNotIn('error-box', text)
        self.assertIn('avg_propagation_ms_mean', text)

    def test_index_has_simn_branding_and_workflow_nav(self):
        resp = self.client.get('/')
        text = resp.data.decode()
        self.assertIn('SimN', text)
        self.assertIn('id="simulator"', text)
        self.assertIn('id="results"', text)
        self.assertIn('id="experiments"', text)

    def test_run_shows_highlights_and_technical_details_drawer(self):
        resp = self.client.post('/run', data={
            'topology': 'mesh', 'nodes': '12', 'extra_edges': '6', 'consumer_count': '3',
            'architectures': ['ip', 'ip_sdn', 'ndn', 'ndn_sdn'],
            'cache_policy': 'lfu', 'cache_size': '8',
            'workload': 'zipf', 'requests': '80', 'items_per_prefix': '20', 'zipf_alpha': '1.1',
            'forwardings': 'best-route,sdn-centralized',
        })
        self.assertEqual(resp.status_code, 200)
        text = resp.data.decode()
        self.assertNotIn('error-box', text)
        self.assertIn('highlight-card', text)
        self.assertIn('Lowest observed latency', text)
        self.assertIn('tech-drawer', text)
        # The embedded client-side history payload must be valid JSON.
        import re, json
        m = re.search(r'<script type="application/json" id="run-summary-data">\s*(.*?)\s*</script>', text, re.S)
        self.assertIsNotNone(m)
        json.loads(m.group(1))  # raises if malformed

    def test_topology_svg_distinguishes_node_roles_by_shape(self):
        resp = self.client.post('/run', data={
            'topology': 'mesh', 'nodes': '10', 'extra_edges': '4', 'consumer_count': '3',
            'forwardings': 'sdn-centralized',
            'cache_policy': 'lfu', 'cache_size': '8',
            'workload': 'zipf', 'requests': '60', 'items_per_prefix': '20', 'zipf_alpha': '1.1',
        })
        text = resp.data.decode()
        # producer=rect (square), consumer=polygon (triangle), controller=polygon (hexagon)
        self.assertIn('<rect', text)
        self.assertIn('<polygon', text)
        self.assertIn('SDN controller', text)


class AsyncJobTests(unittest.TestCase):
    """Fast (no browser) coverage of the async job flow via the Flask test
    client -- complements test_browser_regressions.py, which proves the
    real browser talks to these endpoints correctly, but doesn't require a
    browser binary to run in CI.
    """

    def setUp(self):
        app.testing = True
        self.client = app.test_client()

    def _post_async(self, **overrides):
        data = {
            'topology': 'mesh', 'nodes': '10', 'extra_edges': '4', 'consumer_count': '3',
            'architectures': ['ip', 'ip_sdn', 'ndn', 'ndn_sdn'],
            'cache_policy': 'lfu', 'cache_size': '8',
            'workload': 'zipf', 'requests': '60', 'items_per_prefix': '20', 'zipf_alpha': '1.1',
            'forwardings': 'best-route,sdn-centralized',
        }
        data.update(overrides)
        return self.client.post('/run/async', data=data)

    def _wait_for_completion(self, job_id, timeout=10.0):
        deadline = time.time() + timeout
        status = None
        while time.time() < deadline:
            status = self.client.get(f'/jobs/{job_id}/status').get_json()
            if status['status'] in ('completed', 'failed'):
                return status
            time.sleep(0.05)
        self.fail(f'job {job_id} did not complete within {timeout}s: {status}')

    def test_async_returns_job_id_immediately(self):
        resp = self._post_async()
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn('job_id', data)

    def test_job_status_reaches_completed_with_real_progress(self):
        job_id = self._post_async().get_json()['job_id']
        status = self._wait_for_completion(job_id)
        self.assertEqual(status['status'], 'completed')
        self.assertGreater(status['combos_total'], 0)
        self.assertEqual(status['combos_done'], status['combos_total'])
        self.assertIsNotNone(status['elapsed'])

    def test_unknown_job_status_returns_404(self):
        resp = self.client.get('/jobs/does-not-exist/status')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()['status'], 'not_found')

    def test_job_view_renders_full_result_after_completion(self):
        job_id = self._post_async().get_json()['job_id']
        self._wait_for_completion(job_id)
        resp = self.client.get(f'/jobs/{job_id}/view')
        self.assertEqual(resp.status_code, 200)
        text = resp.data.decode()
        self.assertNotIn('error-box', text)
        self.assertIn('highlight-card', text)
        self.assertIn('packet-player', text)

    def test_job_view_before_completion_shows_graceful_message_not_500(self):
        # A job that doesn't exist (or hasn't completed) must never 500 --
        # it should render the normal page with an explanatory error.
        resp = self.client.get('/jobs/not-a-real-job/view')
        self.assertEqual(resp.status_code, 200)
        text = resp.data.decode()
        self.assertIn('error-box', text)
        self.assertIn('no longer available', text)

    def test_trace_endpoint_returns_real_captured_events(self):
        job_id = self._post_async().get_json()['job_id']
        self._wait_for_completion(job_id)
        resp = self.client.get(f'/jobs/{job_id}/trace?architecture=ndn_sdn')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['architecture'], 'ndn_sdn')
        self.assertIn('ip', data['available'])
        self.assertIn('ndn_sdn', data['available'])
        self.assertGreater(len(data['events']), 0)
        self.assertGreater(len(data['edges']), 0)
        self.assertGreater(len(data['positions']), 0)
        kinds = {e['kind'] for e in data['events']}
        self.assertTrue(kinds & {'tx', 'rx', 'cache_hit', 'cache_miss'})

    def test_trace_endpoint_falls_back_to_available_architecture(self):
        job_id = self._post_async(architectures=['ip']).get_json()['job_id']
        self._wait_for_completion(job_id)
        # Request an architecture that wasn't run -- should fall back to
        # whichever architecture actually has a trace, not error out.
        resp = self.client.get(f'/jobs/{job_id}/trace?architecture=ndn_sdn')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['architecture'], 'ip')

    def test_trace_endpoint_404_for_unknown_job(self):
        resp = self.client.get('/jobs/does-not-exist/trace')
        self.assertEqual(resp.status_code, 404)

    def test_failed_job_reports_error_not_silent_hang(self):
        # A sweep that exceeds MAX_COMBOS should fail fast via the job
        # mechanism too, not just the synchronous path.
        job_id = self._post_async(
            forwardings='best-route,sdn-centralized',
            cache_policies='lfu,sdn-coordinated',
            cache_sizes='0,8,16,32', seeds='1,2',
        ).get_json()['job_id']
        status = self._wait_for_completion(job_id)
        self.assertEqual(status['status'], 'failed')
        self.assertIsNotNone(status['error'])


if __name__ == '__main__':
    unittest.main()
