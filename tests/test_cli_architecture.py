import unittest

from ndn_sim.cli import build_arg_parser, run_sweep


class ArchitectureFilterTests(unittest.TestCase):
    def _run(self, **overrides):
        parser = build_arg_parser()
        args = parser.parse_args([])
        for k, v in overrides.items():
            setattr(args, k.replace('-', '_'), v)
        return run_sweep(args)

    def test_default_runs_ip_and_ndn_only(self):
        result = self._run(nodes=10, requests=40, seed=3)
        systems = {row['system'] for row in result['raw_rows']}
        self.assertEqual(systems, {'ip', 'ndn'})
        architectures = {row['architecture'] for row in result['raw_rows']}
        self.assertEqual(architectures, {'ip', 'ndn'})

    def test_single_architecture_filter_produces_only_that_row(self):
        result = self._run(nodes=10, requests=40, seed=3, architecture='ip_sdn')
        self.assertEqual(len(result['raw_rows']), 1)
        row = result['raw_rows'][0]
        self.assertEqual(row['system'], 'ip')
        self.assertEqual(row['architecture'], 'ip_sdn')
        self.assertEqual(row['forwarding'], 'sdn-centralized')
        self.assertGreater(row['completed_requests'], 0)

    def test_all_four_architectures_explicit(self):
        result = self._run(nodes=10, requests=40, seed=3, architectures='ip,ip_sdn,ndn,ndn_sdn')
        architectures = sorted(row['architecture'] for row in result['raw_rows'])
        self.assertEqual(architectures, ['ip', 'ip_sdn', 'ndn', 'ndn_sdn'])
        for row in result['raw_rows']:
            self.assertGreater(row['completed_requests'], 0)

    def test_architectures_overrides_single_architecture(self):
        result = self._run(nodes=10, requests=40, seed=3, architecture='ip', architectures='ndn_sdn')
        architectures = {row['architecture'] for row in result['raw_rows']}
        self.assertEqual(architectures, {'ndn_sdn'})


if __name__ == '__main__':
    unittest.main()
