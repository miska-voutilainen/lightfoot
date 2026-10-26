---
description: Show what Lightfoot changed in a session (the most recent one by default) and what each change saved.
disable-model-invocation: true
argument-hint: "[session id prefix]"
allowed-tools: Bash(lightfoot why:*)
---

!`lightfoot why $ARGUMENTS`

Explain this to the user in plain words: what was condensed, roughly how much it saved, and where the full text of anything condensed is kept. Don't add numbers that aren't in the output.
