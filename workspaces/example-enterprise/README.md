# example-enterprise

A fictional enterprise pack. Nothing here is real: every id, URL and name is a placeholder.

- **Colleagues:** `hazel` (HR: `leave_request` and `policy_question`, all employees), `ivy` (IT Service Desk: `it_ticket` and `it_access_request`, all employees) and `finn` (Finance Policy, finance team only).
- **Sign-in:** Microsoft Entra ID over OIDC. `sso.env.example` lists the variables; `audiences.yaml` maps Entra group ids to groups and `principals.yaml` holds the `oid` of each person (see `docs/auth.md`).
- **Knowledge:** three SharePoint-style sites (HR, IT services, finance) under `knowledge/sharepoint/`, one folder per library. The finance site is limited to the `finance` group.
- **Tools:** Jira over the `rest` kind (`jira.*`), a REST `notify.send`, and a mock HRIS (`tools/hris.py`, python kind) for `leave_request`. Every write sets `confirm_before_write: true`.
- **Credentials:** `connections.yaml` names environment variables only.

```bash
uv run cograil validate workspaces/example-enterprise
```
