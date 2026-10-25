---
name: calendar_guide
display_name: Calendar Guide
icon: "📅"
description: Guidance for Google Calendar and Outlook calendar events.
tools:
  - calendar
  - outlook_calendar
tags: []
---
CALENDAR TOOL:
- Each calendar has its own timezone setting. When creating or updating
  events, use the datetime format 'YYYY-MM-DD HH:MM:SS' (24-hour time).
- For day-level calendar reminders visible on all devices, prefer
  create_calendar_event over tasks.
- When listing events, the tool returns start/end times in the calendar's
  timezone. Convert to the user's timezone if different.

OUTLOOK CALENDAR:
- outlook_list_events and outlook_find_free_times take 'YYYY-MM-DD' (that
  whole day) or 'YYYY-MM-DD HH:MM', in the computer's time zone.
- An event with attendees sends invitations, so it asks first; one
  without attendees is added at once.
