# Findings: what AWS actually does when you break a suspended execution

Measured 2026-10-29, us-east-1. One account, one execution per scenario. Every number here comes from [`results/live-matrix.json`](results/live-matrix.json), which is the raw output of the run and is committed unedited.

## The answer

Of 2 scenarios, 2 produced a clean failure, 0 survived untouched, 0 never reached a terminal state, and **0 corrupted the execution while reporting success**.

## The matrix

| Scenario | Status | Verdict | Step bodies executed |
|---|---|---|---|
| `d02-step-renamed` | FAILED | clean-failure | `first`=1 |
| `d05-step-reordered` | FAILED | clean-failure | `first`=1 |

`first` and `second` count how many times each step body actually ran, measured by an atomic DynamoDB increment inside the step. A count of 1 means the checkpoint was honoured. A count of 2 means the side effect was re-executed.

## Scenario by scenario

### `d02-step-renamed`

**Question.** What happens when a completed step is renamed mid-flight?

**Advice under test.** AWS: don't rename steps or change their behaviour in ways that break replay; doing so while executions are in progress means they can fail to resume or produce incorrect results. Both outcomes are named. Which one you get is not. (Lambda dev guide, durable-best-practices)

**What the harness did.** renamed step_one to step_one_renamed on $LATEST while suspended

> **Correction.** step bodies that never executed: second (status was FAILED)

**Observed.** Status `FAILED`. Step bodies executed: `first`=1. Suspended after 11.1s, total 101.6s.

The platform reports this execution pinned to function version `$LATEST`.

```
Non-deterministic operation identity at id='1ced8f5be2db23a6513eba4d819c73806424748a7bc6fa0d792cc1c7d1775a97': name checkpoint='step_one' current='step_one_renamed'
```

**Reading.** Refused and said so. This is the good outcome: the platform detected the violation and no side effect was duplicated.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> InvocationCompleted -> ExecutionFailed`

### `d05-step-reordered`

**Question.** What happens when two steps swap order across a suspend?

**Advice under test.** Community guidance: do not reorder steps.

**What the harness did.** swapped step_one and step_two

> **Correction.** step bodies that never executed: second (status was FAILED)

**Observed.** Status `FAILED`. Step bodies executed: `first`=1. Suspended after 8.3s, total 99.4s.

The platform reports this execution pinned to function version `$LATEST`.

```
Non-deterministic operation identity at id='1ced8f5be2db23a6513eba4d819c73806424748a7bc6fa0d792cc1c7d1775a97': type checkpoint='STEP' current='WAIT', subtype checkpoint='Step' current='Wait', name checkpoint='step_one' current='pause'
```

**Reading.** Refused and said so. This is the good outcome: the platform detected the violation and no side effect was duplicated.

Execution history: `ExecutionStarted -> StepStarted -> StepSucceeded -> WaitStarted -> InvocationCompleted -> WaitSucceeded -> InvocationCompleted -> ExecutionFailed`

## What this does not establish

- One account, one region, one runtime, one execution per scenario. These are observations, not a statistical characterisation, and a scenario that survived once is not thereby safe.
- The suspend is 90 seconds. A real execution suspended for weeks crosses platform changes this harness cannot stage, which is the case the whole question is about and the one still unmeasured.
- Python only. The JS, Java and .NET SDKs have their own replay engines and may not behave the same way.
- A clean failure here means the platform detected *this* violation. It is not evidence that it detects every violation.
