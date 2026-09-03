# RTT pipeline — Project 1

Builds the incomplete-pathway waiting list (PTL) for the snapshot date, plus the
validator worklist and the build log, from six raw feeds and one reference feed.

## The one structural rule

**`rules/` never imports `transforms`.**

`rules/` is plain Python and Polars. It knows about RTT and nothing about the
platform — no `Input`, no `Output`, no dataset path, no `@transform`. That is
what lets `tests/` run in under a second without a build.

`transforms/` is wiring. It reads, calls rules, routes what they decide, and
writes. It holds no rules of its own. If you find yourself writing an RTT
judgement inside a `@transform.using`, it belongs one directory up.

    rules/          plain Python + Polars. Imports nothing from transforms.
      nhs_number.py     Modulus 11
      clocks.py         clock splitting, event attachment, DNA verdicts
      measures.py       weeks, bands, status, compliance
      providers.py      succession inference, resolution as at a date
    transforms/     wiring. Reads, calls rules, writes.
      reference.py      the three reference feeds; where the code sets come from
      referral.py       dedup, NHS number, findings
      activity.py       three feeds into one conformed event stream
      pathway.py        clocks and measures
      publish.py        PTL, validation_task, build_log
    tests/          under a second, no build, no Spark

Foundry supplies its own repository scaffold — the `src/` layout, the conda
recipe, transform registration. Do not fight it; the tree above lives inside it.

## Assumptions

Every RTT implementation in the country rests on a stack of local assumptions.
The difference between a good one and a bad one is whether they are written
down. Replace this section with yours.

### Judgement calls

| Decision | Which way it went | Why |
|---|---|---|
| Invalid NHS number | Row retained, Validation Task raised | The identifier is wrong; the patient is not. Dropping the row shortens the waiting list by one real person. |
| Status event before any clock start | Event discarded, pathway retained | A stop with no start is not a clock. |
| Status 33 not demonstrably communicated | Stop cleared, clock continues | Guidance requires the appointment to have been communicated. Note this makes the number worse. |
| Status 33 after an earlier care activity | Stop cleared, clock continues | "First" means first care activity on this clock. |
| Duplicate referral | Earliest survives | Its clock started earlier. Every dedup rule has a direction; this one favours the patient. |
| Status events on a collapsed duplicate | Discarded | Reparenting them opens a second clock and re-inflates the list. |
| Care activity on a collapsed duplicate | Reparented to the survivor | It happened to a person, and it decides later DNA questions. |
| Elective admission with no referral | Dropped | Treatment nobody was referred for. |
| Emergency admission with no referral | Kept, attached to nothing | Out of RTT scope, but real. |
| Provider succession | Applied, Validation Task raised | ODS adjacency is an inference, not a statement. |

### Parameters

| Parameter | Value | Where |
|---|---|---|
| Snapshot date | 2026-03-31 | `transforms/*.py` |
| Dedup window | 30 days | `transforms/referral.py` |
| Dedup semantics | gaps and islands (gap from the previous referral, not from the survivor) | `transforms/referral.py` |
| Breach bands | 0-17 / 18-25 / 26-51 / 52-64 / 65+ | `rules/measures.py` |
| Max succession depth | 5 | `rules/providers.py` |

### Compute choice

State which transforms run lightweight, which do not, and the row count at
which your answer would change. At the volume tier — 1.2M events, 269k
pathways — lightweight Polars wins every step in this pipeline; the working
set is well under a single node's memory and the shuffle Spark would add costs
more than it saves. Name the point at which that flips and show your working.

### What I would not trust yet

A pipeline with no caveats has not been thought about. List yours.

## Running the tests

    pytest tests/ -q

They do not need a build, a Spark session, or a Foundry connection. If they
take more than a second, something in `rules/` has grown a platform import.
