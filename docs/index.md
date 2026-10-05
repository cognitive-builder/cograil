# Cograil

Cograil turns a Markdown runbook into an AI Colleague. The Colleague follows the runbook one Step at a time.

You write the procedure the way you would hand it to a new hire. Cograil makes the model stick to it.

## What You Get

- Each Step may use only the Tools it names.
- Every write to another system stops at a Gate until a person gives an Approval.
- Each Protocol says which Audience may ask for it.
- Every Tool call, Gate, Approval and completion is written as an AuditEvent, with the person it was for.

The model reasons inside a Step. It never invents Steps, Tools or Gates. Those are data, and the runtime enforces them in code.

## Who It Is For

- **Process owners** in HR, IT, finance and operations. You know the procedure. You can write it in Markdown and review it in git.
- **Engineers** who run Cograil for a team. You add Tools, set up Connections and deploy the service.
- **Reviewers** in security and audit. You need to see what the AI did, who approved it and which rule decided.

You do not need to know how to train or tune a model.

## Where To Go Next

| Page | Read it to |
| --- | --- |
| [Quickstart](quickstart.md) | Validate and run the example Workspace on your own machine. |
| [Concepts](concepts.md) | Learn the vocabulary and the thirteen product rules. |
| [Writing a Protocol](runbooks.md) | Write your own Protocol, Step by Step. |
| [Adding a Tool](tools.md) | Give a Colleague a new Tool. |
| [Deploying](deploy.md) | Put Cograil on a server. |
| [Architecture](architecture.md) | See how a Run moves through the code. |
| [Approvals](approvals.md) | See how approvers decide, including by email link. |
| [Evals](evals.md) | Test a Protocol against a golden set before you ship it. |

## Status

Cograil is pre-release software, built in the open. Some things do not exist yet: a Teams adapter, a multi-tenant mode, a visual editor and a Temporal backend. The [ADR index](adr/README.md) lists the decisions behind the design. An ADR is an Architecture Decision Record.
