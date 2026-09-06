"""Vercel Python serverless entry point.

Vercel's @vercel/python runtime looks for a WSGI-compatible ``app`` object
in this module and routes matching requests to it (see ../vercel.json).
This is a thin import wrapper -- all real application code lives in
webapp/app.py, which is also what gunicorn (Render/Heroku) and the local
Flask dev server import. One Flask app, three ways to run it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Vercel's serverless functions run with a hard execution-time limit (10s on
# the Hobby plan, longer on Pro). The default web-demo caps in webapp/app.py
# are tuned for a long-running server (Render/local), not a cold-started
# function -- tighten them here specifically for the Vercel entry point,
# unless the deployment's own project-level env vars already set them
# (os.environ.setdefault leaves an explicit Vercel dashboard override in
# place).
os.environ.setdefault('SIM_MAX_NODES', '24')
os.environ.setdefault('SIM_MAX_REQUESTS', '400')
os.environ.setdefault('SIM_MAX_COMBOS', '8')

from webapp.app import app  # noqa: E402,F401  (Vercel imports `app` from this module)
