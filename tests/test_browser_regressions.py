"""Real-browser regression tests covering behaviors that server-side unit
tests and Flask's test client cannot observe -- real CSS cascade rules and
real HTML form-submission semantics both require an actual browser engine
to verify:

1. The loading overlay must genuinely be hidden (not just carry the
   `hidden` attribute while CSS silently overrides it) so the page stays
   usable and clickable at all times outside an active run.

2. Every field the user fills in must actually reach the server in the
   submitted form data -- disabling form controls before submission would
   silently exclude them per the HTML form-submission spec, so this is
   verified end-to-end with a real browser rather than assumed from code
   review.

Hence this file uses Playwright against the actual Werkzeug server instead
of Flask's test client.

This is an OPTIONAL dev-only test file: Playwright + a browser binary are
heavy dependencies not required for the core test suite. Every test here is
skipped (not failed) if Playwright isn't installed, so
`python -m unittest discover -s tests` keeps working with only
requirements.txt installed. To run these:

    pip install playwright && python -m playwright install chromium
    python -m unittest tests.test_browser_regressions -v
"""

import threading
import time
import unittest

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False

from werkzeug.serving import make_server

from webapp.app import app


@unittest.skipUnless(HAVE_PLAYWRIGHT, "playwright not installed -- see module docstring")
class RealBrowserRegressionTests(unittest.TestCase):
    """Runs the actual Werkzeug dev server (mirroring webapp/app.py's own
    app.run() call) and drives it with a real Chromium instance."""

    @classmethod
    def setUpClass(cls):
        cls.port = 8712
        cls.srv = make_server('127.0.0.1', cls.port, app, threaded=True)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.5)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.srv.shutdown()

    def setUp(self):
        self.page = self.browser.new_page()
        self.console_errors = []
        self.page.on("console", lambda msg: self.console_errors.append(msg.text) if msg.type == 'error' else None)
        self.page.on("pageerror", lambda exc: self.console_errors.append(str(exc)))

    def tearDown(self):
        self.page.close()

    def _url(self, path=''):
        return f'http://127.0.0.1:{self.port}{path}'

    def test_loading_overlay_is_not_visible_on_initial_page_load(self):
        # The overlay must be genuinely hidden
        # (not just carry the `hidden` attribute while CSS overrides it) so
        # the page is actually usable the moment it loads.
        self.page.goto(self._url('/'), timeout=15000)
        self.assertFalse(self.page.is_visible('#loading-overlay'))

    def test_run_button_is_clickable_on_initial_page_load(self):
        # Behavioral check: if the overlay were intercepting pointer events
        # (e.g. a CSS regression reintroducing full-viewport overlay), this
        # click would time out.
        self.page.goto(self._url('/'), timeout=15000)
        self.page.click('#run-btn', timeout=5000)  # should not raise/timeout

    def test_submitted_form_values_actually_reach_the_backend(self):
        # The
        # form now POSTs to /run/async (see webapp/static/app.js), which
        # creates a background job and the page polls + redirects to
        # /jobs/<id>/view on completion. Spy on the real async view to
        # confirm request.form is not empty and reflects what was typed.
        received = {}
        original_view = app.view_functions['run_async']

        from flask import request as flask_request

        def spy_view():
            received['form'] = dict(flask_request.form.lists())
            return original_view()

        app.view_functions['run_async'] = spy_view
        try:
            self.page.goto(self._url('/'), timeout=15000)
            self.page.fill('input[name="nodes"]', '17')
            self.page.fill('input[name="requests"]', '123')
            self.page.check('input[name="architectures"][value="ip_sdn"]')
            # The async flow ends with a client-side navigation to
            # /jobs/<id>/view once polling detects completion.
            with self.page.expect_navigation(timeout=20000):
                self.page.click('#run-btn')
        finally:
            app.view_functions['run_async'] = original_view

        self.assertIn('/jobs/', self.page.url)
        self.assertIn('form', received)
        self.assertGreater(len(received['form']), 0, "form data must not be empty")
        self.assertEqual(received['form'].get('nodes'), ['17'])
        self.assertEqual(received['form'].get('requests'), ['123'])
        self.assertIn('ip_sdn', received['form'].get('architectures', []))

    def test_full_run_renders_with_no_console_errors_and_correct_values(self):
        self.page.goto(self._url('/'), timeout=15000)
        self.page.fill('input[name="nodes"]', '17')
        with self.page.expect_navigation(timeout=20000):
            self.page.click('#run-btn')

        self.assertIn('/jobs/', self.page.url)
        content = self.page.content()
        self.assertIn('17 nodes', content)
        self.assertEqual(self.console_errors, [])

    def test_async_job_flow_does_not_block_and_shows_real_progress(self):
        # This is the direct regression test for the originally reported
        # symptom ("stuck at Running simulation..."): submit, confirm the
        # overlay actually appears (real, not decorative), and confirm the
        # page ends up on a completed job view rather than hanging.
        self.page.goto(self._url('/'), timeout=15000)
        self.page.check('input[name="architectures"][value="ndn_sdn"]')
        with self.page.expect_navigation(timeout=20000):
            self.page.click('#run-btn')
            self.page.wait_for_selector('#loading-overlay:not([hidden])', timeout=3000)
        self.assertIn('/jobs/', self.page.url)
        self.assertFalse(self.page.is_visible('#loading-overlay'))

    def test_packet_player_present_and_fetches_trace_without_errors(self):
        self.page.goto(self._url('/'), timeout=15000)
        self.page.check('input[name="architectures"][value="ndn_sdn"]')
        with self.page.expect_navigation(timeout=20000):
            self.page.click('#run-btn')
        self.page.wait_for_selector('#packet-player', timeout=5000)
        # Give player.js a moment to fetch /jobs/<id>/trace and draw.
        self.page.wait_for_timeout(600)
        self.assertEqual(self.console_errors, [])

    def test_page_never_requires_horizontal_scroll_at_common_viewport_widths(self):
        # Regression test: a global `table` CSS selector (meant only for
        # the metrics table) was unintentionally also forcing every other
        # table on the page -- including the 4-column SDN comparison
        # table -- to a 900px min-width, which propagated up through the
        # layout (CSS grid/flex children default to min-width:auto) and
        # stretched the entire page, and consequently the packet-playback
        # canvas (width:100% of that stretched container), far past any
        # normal viewport. Fixed by scoping that selector to `.table-wrap
        # table` and adding `min-width: 0` at each container in the chain.
        for width in (1920, 1440, 1280, 1024, 768):
            page = self.browser.new_page(viewport={'width': width, 'height': 900})
            page.goto(self._url('/'), timeout=15000)
            page.check('input[name="architectures"][value="ndn_sdn"]')
            page.check('input[name="architectures"][value="ip_sdn"]')
            with page.expect_navigation(timeout=20000):
                page.click('#run-btn')
            page.wait_for_selector('#packet-player', timeout=5000)
            page.wait_for_timeout(300)
            body_w = page.evaluate('document.body.scrollWidth')
            self.assertLessEqual(body_w, width, f'page requires horizontal scroll at {width}px viewport '
                                                  f'(body.scrollWidth={body_w})')
            page.close()

    def test_packet_player_canvas_is_a_reasonable_size_not_thousands_of_pixels(self):
        self.page.goto(self._url('/'), timeout=15000)
        self.page.check('input[name="architectures"][value="ndn_sdn"]')
        with self.page.expect_navigation(timeout=20000):
            self.page.click('#run-btn')
        self.page.wait_for_selector('#packet-player', timeout=5000)
        box = self.page.eval_on_selector(
            '#player-canvas', 'el => { const r = el.getBoundingClientRect(); return {w: r.width, h: r.height}; }')
        self.assertLessEqual(box['w'], 700)
        self.assertLessEqual(box['h'], 550)

    def test_packet_animation_advances_at_a_human_visible_pace(self):
        # Regression test: playback previously mapped 1 real millisecond to
        # ~1 simulated millisecond, meaning an entire multi-hop trace (tens
        # of simulated ms) played out in a handful of real milliseconds --
        # imperceptible. Confirm sim time now advances much more slowly
        # than real wall-clock time at the default 1x speed.
        self.page.goto(self._url('/'), timeout=15000)
        self.page.check('input[name="architectures"][value="ndn_sdn"]')
        with self.page.expect_navigation(timeout=20000):
            self.page.click('#run-btn')
        self.page.wait_for_selector('#packet-player', timeout=5000)
        self.page.click('#player-play')
        self.page.wait_for_timeout(1000)  # 1 real second
        self.page.click('#player-play')
        sim_time_text = self.page.inner_text('#player-time')  # "t = X.X ms"
        sim_ms = float(sim_time_text.split('=')[1].replace('ms', '').strip())
        # At 1x speed the player advances ~6 simulated ms per real second
        # (see webapp/static/player.js); allow generous slack for CI timing
        # jitter, but this must be nowhere near 1000 (the old 1:1 mapping).
        self.assertLess(sim_ms, 100)


if __name__ == '__main__':
    unittest.main()
