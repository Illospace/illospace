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
• A ticket from outside text stays unassigned. A person assigns it.
• The title says what is wrong, not the cause: "Export fails for files over 2 GB".
• One problem gets one ticket. Search open and closed tickets first.

Updates:
• Read the lane before you report its state. Give the source link.
• A question for a person becomes an ask, not a message.
