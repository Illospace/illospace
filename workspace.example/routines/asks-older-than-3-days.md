---
name: open-asks-older-than-3-days
schedule: '0 10 * * 1'
destination: person:ada
principal: team
---
List the open asks that are older than 3 days.

1. Read the open asks of each person.
2. Keep only the asks that were created more than 3 days ago.
3. Group them by owner. One line for each ask: the question, the age, the lane, and the link.
4. If an ask waits on a lane that is done or failed, say so. It can probably close.
5. If no ask is older than 3 days, answer one line: "No ask is older than 3 days."
