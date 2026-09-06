"""WSGI entrypoint for production servers (gunicorn, uWSGI, Render, etc.).

Run locally with:
    gunicorn wsgi:app

Render / most PaaS providers point their start command at this module
(see Procfile / render.yaml). The Flask dev server (webapp/app.py's
``__main__`` block) is for local development only.
"""

from webapp.app import app

if __name__ == '__main__':
    app.run()
