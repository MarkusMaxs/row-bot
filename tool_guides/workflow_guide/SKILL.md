---
name: workflow_guide
display_name: Workflow Guide
icon: "⚡"
description: Pipeline steps for task_create and task_update (branching, approvals, notify, subtasks) and the monitoring recipe.
tools:
  - task
tags: []
---
WORKFLOW STEPS (task_create / task_update `steps`):
- Use steps instead of prompts for branching, approvals or notifications. Each step has a 'type':
  prompt | condition | approval | subtask | notify. Ids are assigned as {type}_{counter} (prompt_1,
  condition_1).
- prompt: {'prompt': str, 'on_error': 'stop'|'skip', 'max_retries': int, 'apps': ['Notion']}. 'apps' names
  the set-up apps the prompt uses; the step uses only those.
- condition: {'condition': str, 'if_true': step_id, 'if_false': step_id}. It checks the previous step's
  output and is one of: contains:<text>, not_contains:<text>, equals:<text>, matches:<regex>,
  gt:/lt:/gte:/lte:<number>, length_gt:/length_lt:<count>, empty, not_empty, true, false,
  json:<path>:<condition>, llm:<a yes/no question about the previous output>, and:[...], or:[...].
  Use llm:<question> for a judgement in words ('is any item below its reorder level?').
- approval: {'message': str, 'timeout_minutes': int, 'if_approved': step_id (default: next step),
  'if_denied': step_id or 'end' (default 'end')}.
- subtask: {'task_id': str, 'pass_output': bool}.
- notify: {'message': str, 'channel': 'desktop' or a configured channel such as 'telegram'}. Use a notify
  step, not a prompt step, when the person says 'notify me' or 'tell me'. 'email' is not a notify channel:
  send email from a prompt step with the email tool.
- Every step needs its text (a prompt, or a message). Any step may set 'next' (step_id or 'end') to change
  the flow. Pass data with {{prev_output}} and {{step.<id>.output}}.

MONITORING ('check X and tell me when Y', 'alert me if W changes'):
- schedule='interval_minutes:M', prompts, persistent_thread=true, and a prompt that ends the task with
  task_update(task_id='{{task_id}}', enabled=false) once done.
