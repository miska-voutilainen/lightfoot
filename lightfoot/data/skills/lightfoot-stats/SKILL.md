---
name: lightfoot-stats
description: 'Show Lightfoot''s cost and savings for recent sessions. Use when the user asks what Lightfoot saved or costs.'
metadata:
  source: lightfoot
---

Run `lightfoot stats` in the shell. It covers the last 7 days; add `--days N` for another period.

Present the numbers to the user briefly: the total saving, the sessions that saved the most, and any rule marked with a warning. Don't add numbers that aren't in the output.
