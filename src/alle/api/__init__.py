"""alle's control API: a loopback HTTP REST API + the Web UI's static app,
served by alled.

One server, two kinds of client: programmatic callers hit ``/api/v1`` with a
Bearer token, and browsers load the bundled Web UI assets and talk to the same
API with a session cookie. The server runs as a thread inside the daemon so
there is nothing extra to deploy; assets ship in the wheel. See ``server.py``
for the transport/auth model.
"""
