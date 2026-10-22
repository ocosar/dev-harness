---
name: experiment
description: Run a variant experiment the disciplined way — one variable per run, free dry-run first, each paid run approved separately, every run a row in the experiment log, the owner picks the winner. Use for prompt/model/parameter tuning (e.g. video generation recipes).
argument-hint: "<what you want to improve> [variants]"
---

Goal: $ARGUMENTS

1. **Baseline and hypothesis.** Find the current best run (experiment log — `docs/prompt-log.md` or `.harness/experiments.md`; create the latter from the columns below if neither exists). State in one line what is being improved, the metric(s) and the threshold that would count as better.
2. **Design variants:** each changes ONE variable vs the baseline. If two changes cannot be separated, call it a "package" and say why. Max 3 variants per round. Write them as a small table: variant | variable | hypothesis | est. $ (price verified now).
3. **Free first:** run every variant's dry-run / mock path, produce the comparison artefacts (frame strips, side-by-side, measured metrics). Catch broken variants here, not on the bill.
4. **Ask the owner once for the round plan** (variants + total estimate). Then each paid run still goes through /dev-harness:paid-call — one request and one click per call.
5. **Measure the same way every time:** the repo's quality gate/metrics plus the owner's rubric if they have one. Separate what the metric says from what you see; if they disagree, say so — the gate may need calibration with owner-labelled examples.
6. **Log every run** as one row: `date | variant | variable | observation (metrics + what is visible) | conclusion | $ actual`. Update any results table the project keeps (e.g. in Project docs).
7. **Recommend, don't decide.** End the round with: the best variant and why, what you would try next (one variable), and the cost. The winner and the next round are the owner's decision — record it as "Owner decision: …" once given.
