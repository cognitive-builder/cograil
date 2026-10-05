Protocol: it_access_request
Description: Help an employee request access to a system, routed to the right approver by a decision table.
Audience: all-employees
Manual execution: allowed
Scheduled execution: not allowed
Helpers: none

1. Step "Check current access": Use @it.get_access for the requester. Tell them in one short sentence what they hold today. (model: small; turns: 2)
2. Step "Confirm request": Ask the requester to confirm the system and the access level they want. Do not proceed until both are confirmed.
3. Step "Route and submit": Use @decide.access_routing with the system and access level to find the approver tier, then use @it.request_access with the confirmed system and level. The tool requires approval; tell the requester who must approve. (context: steps 1, 2; model: standard)
4. Step "Notify": Use @notify.send to tell the requester the outcome and the request id.

Error handling:
- @it.get_access fails: retry once, then escalate to the IT Access Assistant's contact with the error.
- The requester already holds the access they ask for: do not submit; say so and stop.
- Approval declined or expired: tell the requester, include the approver's note if any, and stop.

Guardrails:
- Never request access for anyone other than the requester.
- Never change the system or level the requester did not confirm.
- Never invent what the requester holds; if the tool returns nothing, say so and escalate.
