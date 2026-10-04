Protocol: double_record
Description: Look a value up, then record it twice, each record after its own approval.
Audience: staff
Manual execution: allowed
Scheduled execution: not allowed
Helpers: none

1. Step "Look up": Use @demo.lookup to find the value.
2. Step "Record A": Use @demo.record_a to record the value. (write; requires approval)
3. Step "Record B": Use @demo.record_b to record it again. (write; requires approval)
