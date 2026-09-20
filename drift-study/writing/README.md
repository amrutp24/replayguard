# Write-ups

Three posts on the same measurement, written separately rather than
cross-posted. Duplicate text splits search ranking, and the three audiences want
different things: the Builder Center piece is AWS-native and leads with the
matrix, the Medium piece is for engineers who may not use Lambda at all and
leads with the durable-execution contract, the dev.to piece is a fix-it-now
checklist.

| File | Destination | Angle |
|---|---|---|
| `01-builder-center.md` | AWS Builder Center | The measured matrix and the mechanism behind it |
| `02-medium.md` | Medium | Why durable execution has a failure class ordinary code cannot have |
| `03-devto.md` | dev.to | The deploy checklist, shortest fix first |

Every number in all three traces to `results/live-matrix.json` or
`results/sdk-python-sweep.json` in this repo. Before publishing any of them,
re-run the fact check:

    replaydrift report results/live-matrix.json --out FINDINGS.md

and confirm the counts still match what the posts claim. If a post and the data
disagree, the data is right.
