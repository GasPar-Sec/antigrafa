"""Web interface: start backups, follow them live, cancel them (``antigrafa web serve``).

A small Django site served over HTTPS only (cheroot, on Linux and Windows), behind a
login. Its own data (users, sessions, secret key, certificate) lives in
``[web] data_dir``; a self-signed certificate is created there on the first start.

Needs the ``web`` extra: ``pip install 'antigrafa[web]'``.
"""
