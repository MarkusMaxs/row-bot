---
name: task_automation
display_name: Task Automation
icon: "⚙️"
description: Design effective automated workflows using scheduled tasks, clear messages, and delivery channels.
enabled_by_default: true
version: "4.0"
tags:
  - automation
  - productivity
activation:
  phrases:
    - recurring task
    - scheduled task
    - every monday
    - automate this
    - reminder workflow
  keywords:
    - recurring
    - schedule
    - scheduled
    - task
    - workflow
    - monday
    - reminder
  negative_phrases:
    - meeting notes
    - human tone
  examples:
    - Help me set up a recurring task every Monday
author: Row-Bot
---

When the user wants to **set up automations**, **create recurring workflows**, **schedule something**, or **set a reminder**, apply these principles:

## How Workflows Work

A workflow is a chat that runs on its own. Each run sends its message to the workflow's own conversation, in the background, as a chat turn: it has every tool a chat has, finds apps and tools the same way, and asks before actions at the workflow's approval level, on the same cards. Every run is a new turn in that same conversation, so a run can see what earlier runs found.

There are three kinds:
- **Message workflows** — usually one message, sometimes a few. A second message is sent after the first turn is done, so it can build on the answer.
- **Notify-only reminders** — fire a desktop/channel notification with no agent turn. Use for simple reminders and timers.
- **One-shot timers** — use `delay_minutes` for quick "remind me in 30 minutes" requests. These delete themselves after firing.

## Writing The Message

1. **Write it like a briefing** — State the goal, what to check, and the format you want back. Vague messages produce vague results.

2. **Say the branches in words** — There are no condition steps: write what to do in each case.
   *"Check my calendar for tomorrow. If there are no events, just say 'Clear schedule tomorrow.' Otherwise list them with times and highlight any conflicts."*

3. **Say what to do when there is nothing to do** — "If nothing is new since the last run, say so in one line and stop."

4. **Use template variables** — `{{date}}`, `{{day}}`, `{{time}}`, `{{month}}`, `{{year}}` make messages context-aware at runtime. *"Summarise news for {{date}}"* produces different results each day.

5. **Add a second message only when it helps** — for example "Draft a short email to my team from the summary above" after a research message. Most workflows need one.

## When to Use One Workflow vs. Several

6. **One workflow** when the work belongs together — research that searches, reads sources and writes a brief is one message.
7. **Separate workflows** when the pieces are independent — a morning weather check and an inbox summary don't need each other. Separate workflows can be turned on and off, scheduled and delivered on their own.

## Scheduling

8. **Match the schedule to the rhythm:**
   - `daily:HH:MM` — briefings, digests, check-ins
   - `weekly:DAY:HH:MM` — reviews, summaries, reports
   - `interval:H` / `interval_minutes:M` — monitoring, polling, frequent checks
   - `cron:EXPR` — complex schedules (e.g. `cron:0 9 * * mon-fri` for weekdays only)

## Delivery & Notification

9. **Pick the right channel** — Telegram for mobile-friendly results the user needs on the go, email for reports others need to see, desktop notification (`notify_only`) for simple nudges.
10. **Ask before defaulting** — Don't assume a delivery channel. Ask how the user wants to receive results. Some automations are better read on demand in the app.

## Approval Levels

Every workflow has an approval level (`approval_mode`), as a chat does:
- `block` (default) — reads only; actions are refused. Safest for automated runs.
- `approve` — each action asks the person on its card before it runs (a post shows its full text and account; an email its recipients and body). The run waits until they answer.
- `auto` — a reviewer approves actions that clearly match the workflow's own messages; anything else asks the person, as `approve` does.
- Hands-off (`full`) runs every action without asking. Only the person can turn it on, in the workflow's settings; never set or suggest it yourself.

A run's outcome comes from what its tools did, so a post or email that went out is recorded with its link. A run that fails on a passing problem tries again by itself, but never after something has gone out.

## Other Settings

11. **Model override** — Use the `model` parameter to run a workflow on a different model (e.g. a heavier model for complex research, a lighter one for quick checks).
12. **Agent profile** — `agent_profile` picks the profile whose instructions and capability the runs use. Profiles list no tools.

## Maintenance

13. **Check first** — Before creating a new workflow, check `task_list` to avoid duplicates or overlapping schedules.
14. **Test immediately** — Always suggest `task_run_now` after creation so the user can check the result before the first scheduled run.
15. **Iterate the message** — If the output isn't right, update the messages with `task_update` rather than deleting and recreating.

## Monitoring / Polling

When the user wants to **monitor a condition** and be told when it changes ("check X and tell me when Y", "alert me if Z drops below W"), use the **interval + self-disable** pattern:

16. **Use an interval schedule** — `interval_minutes:M` for frequent checks (stock availability, price drops) or `interval:H` for slower checks.

17. **Write a conditional message** — It should:
    - Check the condition (e.g. search the web, read a URL, check a price)
    - If the condition IS met → report the finding and self-disable with `task_update(task_id='{{task_id}}', enabled=false)`
    - If the condition is NOT met → say so briefly
    The runs share one conversation, so each check can compare with the last one ("tell me when there's a new version" needs to know the old version).

18. **Template: polling message pattern** —
    ```
    Search for [condition]. If [success criteria], report your findings
    and call task_update(task_id='{{task_id}}', enabled=false) to stop
    this monitor. If [condition not met], say '[thing] not yet
    available as of {{time}} — will check again.'
    ```

19. **Self-disable, don't self-delete** — Background runs cannot delete their own workflow (safety restriction). Use `task_update(enabled=false)` instead. The user can re-enable, delete, or leave the disabled workflow.
