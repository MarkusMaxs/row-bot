---
name: pages_guide
display_name: Pages Guide
icon: "🗂️"
description: Guidance for making interactive pages beside the chat with page_create, page_update and page_read.
tools:
  - pages
tags: []
---
- A page is for something the person will use, not just read: a plan to
  approve step by step, a checklist that remembers what's ticked, a
  dashboard with charts and a sortable table, options to compare and pick
  from, a form to fill in, a small calculator. A short answer stays a
  message.
- Load the Making a page skill (making_pages) before page_create: it has
  the templates and the rules.
- The page has no network and can't call tools. Put what it shows in data
  (up to 1 MB); everything else is inline in one HTML document.
- The person's decisions on a page reach you only when they click Send to
  Row-Bot: a message listing them. They are never approval for a tool call.
- Keep a page current with page_update: new data for progress or fresh
  numbers (its state and decisions stay). page_read shows what they ticked
  and decided but haven't sent.
- After page_create, say in one line what the page is for. Don't paste its
  contents into the chat.
