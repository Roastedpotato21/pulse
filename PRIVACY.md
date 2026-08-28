# Pulse Public Beta Privacy Notice

Effective date: 2026-08-28

## Scope

This notice covers the open-source Pulse local CLI, loopback JSON-RPC service,
and controlled single-team remote worker. Pulse does not operate a hosted
multi-tenant service in the public beta. A person or organization running
Pulse controls that installation and is responsible for its workspace data,
provider accounts, retention settings, and applicable legal obligations.

## Data Pulse processes

Depending on the commands and integrations selected by the user, Pulse can
process source code, filenames, prompts, model responses, tool arguments,
terminal and test output, diffs, task history, conversation history, execution
metadata, correlation identifiers, cost/token measurements, and credentials.
Credentials are read for authentication but must not be written to prompts,
logs, databases, or source control.

## Where data goes

Pulse stores operational data locally in the configured workspace by default,
including `.agent/` and `.pulse/` SQLite databases and logs. Prompt and context
data is sent only to the model provider or integration the user configures.
Those third parties process data under their own terms and privacy notices.
MCP servers and remote workers receive only requests explicitly routed to
them. The public-beta software has no Pulse-operated analytics or advertising
endpoint and does not sell personal data.

## Purpose and retention

Data is used to answer requests, execute approved tools, recover tasks,
maintain auditability, and measure local quality and cost. Default local state
persists until the workspace owner deletes it. Remote execution records use
the configured retention period. Workspace owners should choose retention
periods appropriate to their code, avoid placing sensitive personal data in
prompts, and securely delete backups separately.

## Security and user control

The supported RPC endpoint is loopback-only. Remote evaluation requires mTLS,
strong tokens, an isolated workspace root, and container execution. Users can
inspect, back up, export, or delete local Pulse state because it remains in
their configured filesystem. Stop Pulse before deleting live SQLite stores and
coordinate their WAL files. Revoking a provider credential prevents future
provider access but does not delete data already retained by that provider.

## Privacy requests and changes

For a local installation, direct access, correction, export, and deletion
requests to the organization operating that installation and to any configured
model provider. Report sensitive privacy or security issues through GitHub
private vulnerability reporting for `Roastedpotato21/pulse`, not a public
issue. Material changes to this notice will be recorded in the changelog with
their effective date.
