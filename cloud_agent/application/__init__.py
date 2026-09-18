"""Flows this service runs, named and callable without an HTTP request.

The rules a submission passes before it is written, and the create-job body the
job and on-call surfaces share. They lived under ``api/`` because that is where
their callers are; they live here because that is not where they belong.
"""
