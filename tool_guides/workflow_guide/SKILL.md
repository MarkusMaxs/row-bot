---
name: workflow_guide
display_name: Workflow Guide
icon: "⚡"
description: How workflows run (scheduled chats), how to write their messages and how to check what a run did.
tools:
  - task
tags: []
---
A WORKFLOW IS A SCHEDULED CHAT (task_create / task_update):
- Each run sends the workflow's messages (`prompts`, usually one) as chat turns in the workflow's own
  conversation, so a run sees what earlier runs did there. Keep a running record in that conversation, not
  in files, when the work needs one (posts made, people contacted).
- A run has every tool a chat has, found with tool search. Its approval level decides what it may do:
  block (reads only), approve (each action asks the person), auto (a reviewer approves what matches the
  workflow, else the person). Hands-off is the person's choice alone.
- Say branching in words: "If nothing is new since the last run, stop and say so." Don't build a
  ready/not-ready flag for another step to read.
- Don't ask for approval in the message. A post, email or other action asks on its own card, showing the
  exact account, recipient and full text; nothing asks when there is nothing to do.
- To tell the person something, write it in the reply: it goes where the workflow sends results. Email is
  sent with the email tool, like any action.
- To run another workflow after this one, set it to start when this one finishes (in its settings).

CHECKING A RUN: task_list(include_history=true) gives each run's status, its outcome in words and the
actions it took (with links). "completed" means the turn finished, not that anything was posted or sent:
read the actions.

MONITORING ('check X and tell me when Y', 'alert me if W changes'):
- schedule='interval_minutes:M' and a message that ends the workflow with
  task_update(task_id='{{task_id}}', enabled=false) once done.
