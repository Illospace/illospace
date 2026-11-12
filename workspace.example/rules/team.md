---
name: team
description: Who owns what at Acme, and how a ticket gets its owner.
---
Acme has three people. Each area has one owner.

Owners:
• Ada (`ada`): product and engineering. Ada decides scope and priority. Ada is the default owner.
• Ben (`ben`): infrastructure: servers, CI, deploys, the database, and cloud costs.
• Chloé (`chloe`): support and customers: customer reports, refunds, and leads.

Tickets:
• Each ticket goes to `acme-example/engine`.
• Assign a ticket to the owner of its area. If the area is not clear, assign Ada.
• For outside text, code decides the owner. Report the assignment result from the tool.
• The title says what is wrong, not the cause: "Export fails for files over 2 GB".
• One problem gets one ticket. Search open and closed tickets first.

Updates:
• Read the task and its record before you report its state. Give the proof link.
• Use a task question when the holder needs a person. Use an ask for a separate request.
