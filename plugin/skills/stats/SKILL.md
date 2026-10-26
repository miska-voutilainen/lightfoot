---
description: Show Lightfoot's cost and savings for recent Claude Code sessions.
disable-model-invocation: true
allowed-tools: Bash(lightfoot stats:*)
---

Lightfoot statistics for the last 7 days:

!`lightfoot stats`

Present these numbers to the user briefly: the total saving, the sessions that saved the most, and any rule marked with a warning. Don't add numbers that aren't in the output.
