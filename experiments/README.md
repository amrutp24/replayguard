# Platform probes

Three scripts that establish, against a real AWS account, the platform facts
this project relies on. Each one deploys the minimum it needs, measures,
prints its evidence, and deletes everything it created. All were run in
us-east-1 on 2026-08-22; total cost was a few invocations, effectively zero.

They exist because the documentation doesn't answer these questions, and
guessing would have violated the one rule this repo holds itself to: don't
state anything that wasn't measured.

## 1. `durable_runtime_probe.py`

Does Lambda accept `DurableConfig` on a custom runtime? The service model
carries no runtime constraint, but the model permitting something isn't the
service accepting it. Three cells, so a rejection would be interpretable:

```
[A] python3.13       + DurableConfig  -> ACCEPTED
[B] provided.al2023  + DurableConfig  -> ACCEPTED   (the question)
[C] provided.al2023  no DurableConfig -> ACCEPTED
```

Lambda does not gate `DurableConfig` to managed runtimes.

## 2. `durable_invoke_probe.py`

Function creation being accepted says nothing about invoke time. This probe
deployed a `provided.al2023` function with `DurableConfig` and had it echo
back whatever the platform handed it. The custom runtime received both durable
fields:

```json
{"DurableExecutionArn": "arn:aws:lambda:...:function:.../durable-execution/...",
 "CheckpointToken": "<a KMS-encrypted, single-use token>"}
```

Two supporting observations. An earlier attempt returned plain `{"probe":"ok"}`
and Lambda rejected it with `Invalid Status in invocation output`; that error
comes from the durable layer, which proves the durable machinery is engaged on
a custom runtime rather than bypassed. And the protocol needs no privileged
runtime hook. It's plain JSON in and plain AWS API calls out:

| | Shape |
|---|---|
| in | `{DurableExecutionArn, CheckpointToken, InitialExecutionState, UpdatedOperationIds}` |
| out | `{Status: SUCCEEDED \| FAILED \| PENDING \| RETRY, Result?, Error?}` |

`PENDING` is how a handler suspends.

## 3. `durable_suspend_resume_probe.py`

The full lifecycle. A `provided.al2023` function checkpointed a WAIT
operation, returned `PENDING`, and the platform re-invoked it after the timer
fired with that operation present in its state:

| | Operations in state | Updated |
|---|---|---|
| invoke 1 | execution op | execution op |
| invoke 2 | execution op, `rg-wait-1` | `rg-wait-1` |

Final status `SUCCEEDED`. Suspend, checkpoint, resume, and replay-against-state
all work on a custom runtime.

Three things this cost, worth knowing before repeating it:

- Lambda rejects `{"Status": "PENDING"}` when nothing is outstanding
  ("Cannot return PENDING status with no pending operations"). The operation
  has to be checkpointed before suspending; the token is spent afterwards, so
  checkpointing from outside can't work.
- The execution role needs `AWSLambdaBasicDurableExecutionRolePolicy`.
  `AWSLambdaBasicExecutionRole` alone produces "not authorized to perform:
  lambda:CheckpointDurableExecution".
- `provided.al2023` ships curl with `--aws-sigv4`, and Lambda injects
  credentials as environment variables, so a shell bootstrap can sign its own
  checkpoint call. No Rust toolchain was needed to answer a question about
  Rust.

## What this adds up to

A durable SDK for any language is an engineering project, not a research
question: the control-plane APIs are public, both payload contracts are
confirmed empirically, and the full lifecycle has been exercised end to end
from a custom runtime. In Rust's case someone already did it
([pgdad/durable-rust](https://github.com/pgdad/durable-rust)), which is why
this project ships a Rust frontend rather than a competing SDK.
