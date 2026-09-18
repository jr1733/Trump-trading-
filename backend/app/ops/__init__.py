"""Operational routines that must be runnable *inside the container*.

Anything the deploy runbook asks you to run on the server belongs here rather
than in a repo-root `scripts/` file: the api image is built from the `backend/`
directory, so `COPY . .` cannot reach repo-root scripts and they are simply not
present in the container.
"""
