# example-enterprise

A fictional enterprise pack. Nothing here is real: every id, URL and name is a placeholder.

- **Colleagues:** `ivy` (IT Service Desk, all employees) and `finn` (Finance Policy, finance team only).
- **Sign-in:** Microsoft Entra ID over OIDC. `sso.env.example` lists the variables; `audiences.yaml` maps Entra group ids to groups and `principals.yaml` holds the `oid` of each person (see `docs/auth.md`).
- **Knowledge:** two SharePoint-style sites under `knowledge/sharepoint/`, one folder per library. The finance site is limited to the `finance` group.
- **Tools:** Jira over the `rest` kind (`jira.*`) and a REST `notify.send`. Every write sets `confirm_before_write: true`.
- **Credentials:** `connections.yaml` names environment variables only.

```bash
uv run cograil validate workspaces/example-enterprise
```
