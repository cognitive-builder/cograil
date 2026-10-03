Protocol: leave_request
Description: Help an employee check their balance and submit a leave request with manager approval.
Audience: all-employees
Manual execution: allowed
Scheduled execution: not allowed
Helpers: none

1. Step "Check balance": Use @hris.get_balance for the requester. Tell them the balance for each leave type in one short sentence. (model: small; turns: 2)
2. Step "Confirm dates": Ask the requester to confirm the start date, end date and leave type. Do not proceed until all three are confirmed.
3. Step "Route and submit": Use @decide.approval_routing with the duration, leave type and role to find the approver tier, then use @hris.submit_leave with the confirmed dates and type. The tool requires approval; tell the requester who must approve. (context: steps 1, 2; model: standard)
4. Step "Notify": Use @notify.send to tell the requester the outcome and the request id.

Error handling:
- @hris.get_balance fails: retry once, then escalate to the Human Manager with the error.
- Balance is lower than the days requested: do not submit; explain the shortfall and stop.
- Approval declined or expired: tell the requester, include the approver's note if any, and stop.

Guardrails:
- Never submit leave for anyone other than the requester.
- Never change dates the requester did not confirm.
- Never invent a balance; if the tool returns nothing, say so and escalate.
