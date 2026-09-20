# Your code didn't change. Your workflow broke anyway.

## What I learned deliberately corrupting long-running executions

There is a category of bug that cannot exist in ordinary software, and that
becomes almost inevitable the moment you adopt durable execution.

It goes like this. A workflow starts. It does some work, then suspends — waiting
for a human approval, a webhook, a timer, something that will not arrive for
hours or days. While it is suspended, you deploy. Nothing dramatic: you reorder a
couple of steps, delete one that is no longer needed. Your tests pass. Your type
checker is happy. Your code review was uneventful.

Then the workflow wakes up, resumes into code that is no longer the code it
started in, and does something quietly wrong.

I spent a day measuring exactly how wrong, on AWS Lambda's durable functions.
Twelve scenarios, each one breaking a replay assumption against a live suspended
execution. The short version:

**Three of twelve corrupted the execution. All three reported success. Not one of
the twelve produced an error.**

## Why durable execution is unusual

Durable execution engines — Temporal, Restate, Azure Durable Functions, DBOS, and
now AWS Lambda — all rest on the same trick. Your workflow is ordinary code.
When it suspends and later resumes, the engine runs your function **from the top
again**, and every operation that already completed returns its recorded result
instead of executing.

That is what makes a year-long workflow expressible as a normal function. It is
also a contract, and the contract has an unusual property: **one half of it is
your code today, and the other half is a checkpoint written by your code from
last Tuesday.**

Every engine in this space tells you to keep that contract deterministic. What
almost none of them tell you is what the engine does when you break it. Does it
detect the mismatch and fail loudly? Does it silently do the wrong thing? The
documentation for the platform I tested names both possibilities and does not say
which you get.

So the interesting question is not "what are the rules." It is "what is the
failure mode when the rules are broken," because that determines whether this is
a class of bug you will find in staging or a class you will find in an audit six
months later.

## The measurement

I needed to distinguish three outcomes that all look similar from the outside:

1. the step's recorded result was reused correctly
2. the step's body **re-executed**, doing its side effect twice
3. the step's body **never executed**, and something else consumed its result

A return value cannot tell you this, because replay returns the checkpointed
value whether or not the body ran. So every step in my test workflow performs a
real, external, atomic increment against a database row. Afterwards, the count is
the evidence. One means the checkpoint was honoured. Two means the side effect
happened twice. A missing row means the step never ran.

That design decision is the whole experiment. Everything else is plumbing.

## What happened

The worst case: I swapped the order of two steps while the execution was
suspended. The execution completed with status `SUCCEEDED`, and the first step's
side effect had executed **twice** while the second step's had **never executed
at all**.

Map that onto a real workflow. If the first step charges a card and the second
sends a receipt, you have just double-charged a customer and sent them nothing,
and your orchestrator is reporting a clean run. There is no error, no warning, no
entry in the execution history suggesting anything went wrong.

Deleting a completed step caused the step *after* it to never run. Inserting a
step before a completed one caused the inserted step to never run — and, oddly,
caused the workflow to wait a second time, doubling its suspension.

## The mechanism, and why it is counterintuitive

Checkpoints are matched to operations **by position in the sequence**. Not by
name, and — this is the part I did not expect — not by type either.

I could prove this because the operation IDs are stable hashes. The third
operation slot produced the identical ID in two different scenarios, where in one
it was a timer and in the other it was a step. Same ID, different name, different
type. Position is the only thing left that identity could be derived from.

Which means that when I reordered those two steps, position 0 in the new code was
a *timer* that consumed a completed *step's* checkpoint and concluded it had
already waited. Position 1 was a *step* that consumed a completed *timer's*
checkpoint and concluded it had already run. Position 2 found nothing left and
executed for real, a second time.

No type check. No name check. No complaint.

Here is the consequence that matters, and it inverts the advice everyone repeats.
The guidance in this ecosystem — and in the community writing around every
durable execution engine — is to treat step names as immutable identifiers and
never rename them.

**Renaming a step was the only code change I tested that was completely
harmless.** The name was never the identity.

The thing that actually matters is the *sequence* of durable operations, and I
could not find guidance that frames it that way.

## The fix is not what you would guess

I expected the answer to be a discipline: rules about how to edit workflow code
safely, a checklist, maybe a linter. That is not what the data says.

Two of my scenarios tested the platform's recommended mitigation — pinning
executions to an immutable version, and the more realistic variant where you
invoke through an alias and move the alias on deploy. Both held completely. In
the alias case, I published a new version and repointed the alias while the
execution was suspended; the execution reported that it was still on the original
version and finished on the original code.

The platform resolves the alias **once**, when the execution starts, and pins it
for the execution's entire life.

So the rule is mechanical rather than behavioural. Every corruption I produced
required the workflow to be running against a mutable pointer. Pin the execution
and none of it can reach you. That is a far better answer than "be careful when
you edit steps," because being careful does not survive contact with a team of
six and a Friday deploy.

## The thing that did not break

I also swapped the workflow SDK itself underneath a suspended execution — a full
major version bump, 1.x to 2.0, and then back the other way. Both survived
cleanly.

I want to flag that, because I had built a static analyser that predicted the
opposite. Comparing those two SDK versions offline surfaces 50 differences, a
dozen of which can reach an in-flight execution: the replay engine changed, the
checkpoint serialisation changed, the exception hierarchy was rewritten. Every
public method on the main API is byte-for-byte identical across that bump. The
machinery underneath it is not.

And yet a checkpoint written by the old version was read correctly by the new
one. The static analysis flags risk, not breakage, and the risk did not
materialise for this workflow shape.

That leaves an asymmetry worth sitting with. The dependency upgrade — the thing
that gets a changelog read, a careful review, a staged rollout — did no damage.
A routine reordering of two function calls, pushed like any other change, silently
duplicated a side effect. One of those gets reviewed as a risk. The other does
not.

## What I am not claiming

One account, one runtime, one SDK, one execution per scenario. These are
observations, not a statistical characterisation, and a scenario that survived
once is not thereby safe.

The suspensions in my harness last 90 seconds. The scenario I actually care about
— an execution suspended for six months, crossing runtime deprecations and
platform upgrades it never consented to — is exactly the one a day of testing
cannot stage. That remains unmeasured, and it is where I would expect the real
surprises to live.

The type-blindness in particular strikes me as the sort of thing that could be
tightened in a future release without anyone announcing it, which would be a
strict improvement and would also invalidate part of what I found.

## If you take one thing

Durable execution makes a year-long workflow look like ordinary code, and that is
genuinely a good trick. But ordinary code does not have a second half of its
contract sitting in a database, written weeks ago by a version of itself you have
since deleted.

Find out what your engine does when that contract is violated, before you need to
know. In my case the answer was: nothing at all, loudly reported as success.

---

*The harness is open source at
[github.com/amrutp24/replaydrift](https://github.com/amrutp24/replaydrift) — it
runs the full matrix against your own account in about 45 minutes and tears
everything down afterwards. The raw results are committed in the repo, and the
findings document is generated from them rather than written by hand.*
