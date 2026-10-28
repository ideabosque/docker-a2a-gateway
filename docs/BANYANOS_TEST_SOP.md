# BanyanOS bridge local integration scenarios

Version 1.0, 2026-10-27. Approved by the user in this chat: "Approve local test plan".
Owner: repository maintainer. Environment: local Docker development stacks.

## Purpose and scope

Review the BanyanOS plugin bridge and its Docker test harness. Validate transport,
task persistence, conversation continuation, and streaming. Production, orchestration
workflows, remote cancellation guarantees, and load testing are outside this run.

## Environment and access

A2A: http://127.0.0.1:8775; BanyanOS: http://127.0.0.1:18000.
Runtime configuration and credentials come from the existing local `.env` and container
environment. Never copy secrets into reports. Use the configured published BanyanOS
agent, `a2a-banyanos-agent`, A2A endpoint `a2a`, and partition `default`.
Tests may create task/session/message records and invoke the configured model provider.
No database resets, schema changes, or production access are part of this plan.

## Dependency readiness requirements

Require healthy A2A/BanyanOS gateways and their existing PostgreSQL services, plus
BanyanOS Redis and Neo4j. Validate protected GraphQL, WebSocket auth, and agent execution
to establish configuration and operational readiness beyond container health alone.
Provider availability is established by actual invocation; no provider provisioning.

## Test data and execution order

Use existing agent registration and credentials. Generate unique task IDs and short
counting prompts; use a distinctive marker for the two-turn memory check.

1. Offline plugin and harness regression tests.
2. Container health, gateway health, public discovery, authenticated GraphQL/WS.
3. Non-streaming invocation and retrieval of its completed persisted task.
4. Two-turn session continuation using the returned `contextId`.
5. Unknown-agent failure; reject missing task records and GraphQL errors.
6. SSE token delivery and completion, followed by direct backend native streaming.
7. Run both hello scripts and the interactive chatbot with two scripted turns
   and `quit`; require two nonempty replies and recall of a fresh random marker.
   This extension was requested by the user: "with all testing scripts" / "for banyanos."
8. For the user-requested reasoning switch, validate the resolved default and
   request/agent overrides offline. With the live gateway, verify `.env` off
   suppresses tagged reasoning and HTTP reasoning metadata, then restore `.env`
   on and require reasoning events from the configured model. Recreate only the
   local A2A gateway to pick up environment changes.

This order follows transport dependencies rather than the skill's business-domain
example sequence. Tests run manually; use offline checks on changes and run live checks
when the local stacks and credentials are available. No automation is provisioned.

## Failure and reconciliation checks

Offline checks cover malformed GraphQL, request timeout classification, idle receive
intervals, credential/partition URL encoding, empty close frames, split frames and ping
handling. Existing identity tests cover tenant/config trust. Live checks require
`tasks/get` to return the same task ID in completed state, and require the second turn
to recall the marker through the returned conversation ID. Streaming must deliver text
and a successful terminal event; native backend errors fail the scenario.

## Entry, exit, and reporting

Entry: user approval, existing local dependencies healthy, configured agent/credentials.
Exit: all selected offline and live checks pass with explicit failed/skipped counts.
Report observed results and remaining risks in `docs/test_results/`. Certification is
limited to this local bridge scope; no production readiness claim. Release approval
is outside this review. Sign-off: user approved execution; results recorded by Codex.
