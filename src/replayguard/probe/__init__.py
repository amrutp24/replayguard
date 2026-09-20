"""Live drift probe: what the platform actually does when you break replay.

Deploys a durable function, suspends it, changes something underneath it on
purpose, resumes it, and records the result. Needs boto3 and an AWS account, and
creates real (billable) resources, all prefixed `rd-`, with a verified teardown.

This is the evidence behind drift-study/FINDINGS.md. `replayguard drift` is the
part you run in CI; this is the part that established what the findings mean.
"""
