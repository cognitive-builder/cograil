Protocol: leave_request
Description: Fixture whose step 2 references a tool that tools.yaml does not define.
Audience: everyone
Manual execution: allowed
Scheduled execution: not allowed
Helpers: none

1. Step "Check balance": Use @hris.get_balance for the requester.
2. Step "Submit": Use @hris.submit_leave with the confirmed dates.
