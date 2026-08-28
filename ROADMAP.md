# Roadmap

The aim is for replayguard to be the determinism check teams run in CI before
their first long-running durable execution comes due. Durable functions went
GA in early 2026 and can suspend for up to 366 days, so the first workflows
started in 2026 come due through 2027. Determinism violations are silent until
resume, which means the bugs being written today are invisible today.

## Done

- Static checker for Python, TypeScript, Java, and Rust: shared IR, six
  rules, text/JSON/SARIF output, GitHub Action, pre-commit hook.
- Interprocedural analysis within a file, in all four languages. Findings
  name their route.
- Validation against 1,547 files of third-party durable-function code:
  AWS's samples, all three official SDKs, eleven community repos, and the
  Rust SDK. See [VALIDATION.md](VALIDATION.md) for what that did and didn't
  establish, including thirteen bugs it found in this tool.
- Dynamic replay-divergence harness (`replayguard replay`,
  `assert_deterministic`): 51 AWS conformance handlers, zero false alarms.
- Rule-by-language capability matrix: every rule fires on its own violation
  in every language, and correct handlers stay silent, enforced in the test
  suite.
- Platform probes against a real AWS account confirming that custom runtimes
  participate fully in durable execution, including the complete
  suspend/checkpoint/resume cycle. See
  [experiments/README.md](experiments/README.md).

## Next

1. **Field evidence for RG002, RG004, and RG005.** These rules work (see the
   matrix) but have never produced a true positive on code written by someone
   else, because public example code doesn't contain the mistakes they catch.
   Reports from real codebases are the most valuable thing anyone could
   contribute right now, in either direction: a real finding, or a false
   positive.
2. **Cross-file call resolution.** Only same-file calls are followed today. A
   handler that calls into another module for its I/O passes clean, and that
   is the largest known blind spot.
3. **Measured failure modes.** AWS's docs imply the consequences of
   determinism violations (they name double-charging) but nothing documents
   observed behaviour: clean failure, silent re-execution, a stuck execution.
   The dynamic harness is the instrument for measuring this properly.

## Not planned

- **Go and .NET frontends.** Community proofs of concept only, no SDK worth
  targeting yet. This changes if that changes.
- **A Rust durable SDK.** [pgdad/durable-rust](https://github.com/pgdad/durable-rust)
  already exists and is mature. Its docs state that its determinism rules are
  documented, not enforced, which is exactly the gap replayguard fills for
  the other three languages, so a Rust frontend was the right move instead.
