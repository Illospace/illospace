---
name: Bugs
description: Repair a fault and keep the proof.
holders: [person:ada, agent:build]
default_holder: agent:build
done: [link:ticket]
reviewer: person:ada
silence: {agent_hours: 4, person_hours: 24}
---
Read the fault and its evidence. Split independent repairs into pieces.
The build agent holds each repair. Ada confirms decisions.
Add a ticket link as proof before you return the work.
