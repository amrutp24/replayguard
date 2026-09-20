# Findings: what AWS actually does when you break a suspended execution

Measured 2026-09-16, us-east-1. One account, one execution per scenario, python3.13 on arm64, SDK 1.7.0 unless stated. Every number here comes from [`results/live-matrix.json`](results/live-matrix.json), which is the raw output of the run and is committed unedited.

## The answer

Of 12 scenarios, 0 produced a clean failure, 7 survived untouched, 0 never reached a terminal state, and **3 corrupted the execution while reporting success**.

Those 3 are the result worth acting on. In each, a step body the code says should run did not run -- and in 1 of them another step body ran twice as well. Every one finished with status SUCCEEDED. Nothing in the execution history, the status or the error field says otherwise.

2 scenario(s) were insulated: the redeploy happened and the execution never saw it, because it was pinned to a published version. That is the mitigation working, and it is the practical answer this matrix produces.

## The matrix

| Scenario | Status | Verdict | Step bodies executed |
|---|---|---|---|
| `d05-step-reordered` | SUCCEEDED | silent-corruption **(!)** | `first`=2 |
| `d03-step-inserted` | SUCCEEDED | silent-omission | `first`=1, `second`=1 |
| `d04-step-removed` | SUCCEEDED | silent-omission | `first`=1 |
| `d01-control` | SUCCEEDED | survived | `first`=1, `second`=1 |
| `d02-step-renamed` | SUCCEEDED | survived | `first`=1, `second`=1 |
| `d06-nonstep-refactor` | SUCCEEDED | survived | `first`=1, `second`=1 |
| `d08-sdk-upgraded` | SUCCEEDED | survived | `first`=1, `second`=1 |
| `d09-sdk-downgraded` | SUCCEEDED | survived | `first`=1, `second`=1 |
| `d10-env-changed` | SUCCEEDED | survived | `first`=1, `second`=1 |
| `d11-memory-changed` | SUCCEEDED | survived | `first`=1, `second`=1 |
| `d00-version-pinned` | SUCCEEDED | insulated | `first`=1, `second`=1 |
| `d07-alias-repointed` | SUCCEEDED | insulated | `first`=1, `second`=1 |

`first` and `second` count how many times each step body actually ran, measured by an atomic DynamoDB increment inside the step. A count of 1 means the checkpoint was honoured. A count of 2 means the side effect was re-executed.

## Scenario by scenario

### `d00-version-pinned`

**Question.** Does pinning an execution to a published version insulate it from a redeploy, as the mitigation claims?

**Advice under test.** AWS: invoke with version numbers or aliases to pin executions to specific code versions. (Lambda dev guide, durable-best-practices)

**What the harness did.** published a renamed-step version 2 while the execution ran on the pinned, immutable version 1

> **Correction.** re-scored from 'survived' to 'insulated': the redeploy happened and this execution did not see it, which is what the scenario set out to test.

**Observed.** Status `SUCCEEDED`. Step bodies executed: `first`=1, `second`=1. Suspended after 14.2s, total 105.8s.

The platform reports this execution pinned to function version `1`.

**Reading.** The mutation was published and did **not** reach this execution. For this scenario that is the success condition: the qualifier pinned the execution to the code it started with.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> StepStarted -> StepSucceeded -> InvocationCompleted -> ExecutionSucceeded`

### `d01-control`

**Question.** Does an untouched execution on $LATEST suspend and resume cleanly?

**Advice under test.** It must. This is the control for every $LATEST scenario below -- without it, a failure there could not be separated from 'running on $LATEST is itself the problem'.

**What the harness did.** nothing

**Observed.** Status `SUCCEEDED`. Step bodies executed: `first`=1, `second`=1. Suspended after 10.9s, total 102.4s.

The platform reports this execution pinned to function version `$LATEST`.

**Reading.** Completed normally, each step body executing exactly once.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> StepStarted -> StepSucceeded -> InvocationCompleted -> ExecutionSucceeded`

### `d02-step-renamed`

**Question.** What happens when a completed step is renamed mid-flight?

**Advice under test.** AWS: don't rename steps or change their behaviour in ways that break replay; doing so while executions are in progress means they can fail to resume or produce incorrect results. Both outcomes are named. Which one you get is not. (Lambda dev guide, durable-best-practices)

**What the harness did.** renamed step_one to step_one_renamed on $LATEST while suspended

**Observed.** Status `SUCCEEDED`. Step bodies executed: `first`=1, `second`=1. Suspended after 11.0s, total 100.5s.

The platform reports this execution pinned to function version `$LATEST`.

**Reading.** Completed normally, each step body executing exactly once.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> StepStarted -> StepSucceeded -> InvocationCompleted -> ExecutionSucceeded`

### `d03-step-inserted`

**Question.** What happens when a new step is inserted before a completed one?

**Advice under test.** AWS: ensure new code versions can handle state from older versions. Community guidance is blunter -- do not reorder steps, the engine expects a specific sequence.

**What the harness did.** inserted step_zero ahead of the already-checkpointed step_one

> **Correction.** re-scored from 'survived' to 'silent-omission': the run recorded no execution of inserted never ran at all, though the final code says it should have. The harness's original verdict counted duplicate executions only and could not see an omission.

**Observed.** Status `SUCCEEDED`. Step bodies executed: `first`=1, `second`=1. Suspended after 11.2s, total 195.6s.

The platform reports this execution pinned to function version `$LATEST`.

**Reading.** **Reported success, and a step body never ran.** The execution's own status says SUCCEEDED. A step that the code says should execute did not, and nothing anywhere reports it.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> StepStarted -> StepSucceeded -> InvocationCompleted -> ExecutionSucceeded`

### `d04-step-removed`

**Question.** What happens when a completed step is deleted from the code?

**Advice under test.** The history now holds a checkpoint the code does not account for. No published guidance states the outcome.

**What the harness did.** deleted step_one, which had already checkpointed

> **Correction.** re-scored from 'survived' to 'silent-omission': the run recorded no execution of second never ran at all, though the final code says it should have. The harness's original verdict counted duplicate executions only and could not see an omission.

**Observed.** Status `SUCCEEDED`. Step bodies executed: `first`=1. Suspended after 10.2s, total 104.9s.

The platform reports this execution pinned to function version `$LATEST`.

**Reading.** **Reported success, and a step body never ran.** The execution's own status says SUCCEEDED. A step that the code says should execute did not, and nothing anywhere reports it.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> InvocationCompleted -> ExecutionSucceeded`

### `d05-step-reordered`

**Question.** What happens when two steps swap order across a suspend?

**Advice under test.** Community guidance: do not reorder steps.

**What the harness did.** swapped step_one and step_two

> **Correction.** re-scored from 'silent-reexecution' to 'silent-corruption': first ran more than once and second never ran at all, though the final code says it should have. The harness's original verdict counted duplicate executions only and could not see an omission.

**Observed.** Status `SUCCEEDED`. Step bodies executed: `first`=2. Suspended after 10.3s, total 98.5s.

The platform reports this execution pinned to function version `$LATEST`.

**Reading.** **Reported success, ran one side effect twice AND skipped another.** Both failure modes in a single execution, with status SUCCEEDED and no diagnostic anywhere. This is the worst cell in the matrix.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> StepStarted -> StepSucceeded -> InvocationCompleted -> ExecutionSucceeded`

### `d06-nonstep-refactor`

**Question.** Is a pure refactor outside every step safe across a suspend, as the determinism contract implies?

**Advice under test.** AWS: code outside a durable operation runs on every replay and must be a pure function of the handler inputs and completed operations. Adding pure local computation should therefore be safe. This is the scenario whose failure would be most surprising.

**What the harness did.** added pure local computation outside all steps

**Observed.** Status `SUCCEEDED`. Step bodies executed: `first`=1, `second`=1. Suspended after 10.5s, total 104.3s.

The platform reports this execution pinned to function version `$LATEST`.

**Reading.** Completed normally, each step body executing exactly once.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> StepStarted -> StepSucceeded -> InvocationCompleted -> ExecutionSucceeded`

### `d07-alias-repointed`

**Question.** Does an alias insulate an in-flight execution, or does the platform re-resolve it on resume?

**Advice under test.** AWS: invoke with version numbers or aliases to pin executions to specific code versions. Community guides go further and claim in-flight executions continue on the version they started with. This is THE recommended mitigation and it is untested in public.

**What the harness did.** published a renamed-step version and repointed the alias at it

> **Correction.** re-scored from 'survived' to 'insulated': the redeploy happened and this execution did not see it, which is what the scenario set out to test.

**Observed.** Status `SUCCEEDED`. Step bodies executed: `first`=1, `second`=1. Suspended after 14.3s, total 106.6s.

The platform reports this execution pinned to function version `1`.

**Reading.** The mutation was published and did **not** reach this execution. For this scenario that is the success condition: the qualifier pinned the execution to the code it started with.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> StepStarted -> StepSucceeded -> InvocationCompleted -> ExecutionSucceeded`

### `d08-sdk-upgraded`

**Question.** What happens when the bundled durable SDK goes 1.7.0 to 2.0.0 underneath a suspended execution?

**Advice under test.** AWS: ensure new code versions can handle state from older versions. That burden is placed on the developer and no tooling is shipped to discharge it. The offline diff says this upgrade changes the replay engine, the checkpoint state handling and the exception hierarchy while leaving every public method identical.

**What the harness did.** swapped the bundled SDK from 1.7.0 to 2.0.0, handler unchanged

**Observed.** Status `SUCCEEDED`. Step bodies executed: `first`=1, `second`=1. Suspended after 11.5s, total 104.0s.

The platform reports this execution pinned to function version `$LATEST`.

**Reading.** Completed normally, each step body executing exactly once.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> StepStarted -> StepSucceeded -> InvocationCompleted -> ExecutionSucceeded`

### `d09-sdk-downgraded`

**Question.** And in reverse: what happens on a rollback, 2.0.0 back to 1.7.0, across a suspend?

**Advice under test.** Rollback is the operator's instinct when a deploy goes wrong, and it is the direction nobody tests.

**What the harness did.** rolled the bundled SDK back from 2.0.0 to 1.7.0

**Observed.** Status `SUCCEEDED`. Step bodies executed: `first`=1, `second`=1. Suspended after 13.0s, total 103.7s.

The platform reports this execution pinned to function version `$LATEST`.

**Reading.** Completed normally, each step body executing exactly once.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> StepStarted -> StepSucceeded -> InvocationCompleted -> ExecutionSucceeded`

### `d10-env-changed`

**Question.** An environment variable is read at module scope. What does replay see after it changes?

**Advice under test.** AWS: env vars read at runtime can change between the first invocation and a replay, so capture the value inside a step. The stated consequences are control flow walking a different branch, a downstream step running with wrong inputs, or a return from an operation that never ran. (Durable Execution SDK guide, best-practices/determinism)

**What the harness did.** changed RD_PROBE, which the handler reads outside any step

**Observed.** Status `SUCCEEDED`. Step bodies executed: `first`=1, `second`=1. Suspended after 13.4s, total 104.2s.

The platform reports this execution pinned to function version `$LATEST`.

**Reading.** Completed normally, each step body executing exactly once.

**What this does not show.** A clean result here is weaker than it looks. The handler records RD_PROBE but never branches on it, so this shows the value does drift under a running execution -- the returned probe is 'after' when the execution began on 'before' -- without exercising the consequence AWS documents, which is control flow taking a different path on replay. Read this as confirming the premise, not as evidence that environment drift is harmless.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> StepStarted -> StepSucceeded -> InvocationCompleted -> ExecutionSucceeded`

### `d11-memory-changed`

**Question.** Does changing memory mid-suspend disturb a running execution?

**Advice under test.** No guidance found either way.

**What the harness did.** raised memory from 512MB to 1024MB while suspended

**Observed.** Status `SUCCEEDED`. Step bodies executed: `first`=1, `second`=1. Suspended after 12.8s, total 103.9s.

The platform reports this execution pinned to function version `$LATEST`.

**Reading.** Completed normally, each step body executing exactly once.

**What this does not show.** Memory is not part of the build stamp the handler reports, so unlike every other drift scenario this one has no independent evidence inside the execution that the change landed before the resume. The configuration update was accepted by Lambda, but the delivery is not self-verified the way a variant or SDK swap is.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> StepStarted -> StepSucceeded -> InvocationCompleted -> ExecutionSucceeded`

## What this does not establish

- One account, one region, one runtime, one execution per scenario. These are observations, not a statistical characterisation, and a scenario that survived once is not thereby safe.
- The suspend is 90 seconds. A real execution suspended for weeks crosses platform changes this harness cannot stage, which is the case the whole question is about and the one still unmeasured.
- Python only. The JS, Java and .NET SDKs have their own replay engines and may not behave the same way.
- A clean failure here means the platform detected *this* violation. It is not evidence that it detects every violation.
