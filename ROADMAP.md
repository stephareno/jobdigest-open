# Roadmap

Source of truth for feature status and chat naming: this file owns **what
state each piece of work is in** and **what the chat about it is called**.

## How IDs work

Every feature, gap and settled decision has a permanent ID. The prefix is the
area, never the status, so an ID never changes when work ships.

| Prefix | Area |
|---|---|
| `SRC` | sourcing and fetch |
| `FIL` | filtering |
| `SCO` | scoring |
| `DEL` | digest and delivery |
| `TRK` | sheet and tracking |
| `OPS` | workflows and state |
| `GAP` | known gap, not yet on a lane |
| `PUB` | public engine / profile-driven refactor |
| `SET` | settled: rejected, retired or not planned |
| `BATCH` | one chat that produced several features |

An ID is used once. Before assigning a new one, check the index below for the
highest number in that prefix.

## Status values

| Status | Means |
|---|---|
| Live | running in production on `origin/main` |
| Complete | a one-off that ran and is finished |
| In flight | exists in the working tree, not committed or not merged |
| Planned | decided, not built |
| Open | a known gap or an undecided question |
| Rejected / Retired / Not planned | settled, kept so it does not come back as an idea |

**"Live" is a claim about `origin/main`, not about a branch.** Verify with
`git merge-base --is-ancestor <sha> origin/main` before marking anything Live.

## Chat naming

Every chat, in Cowork and in Claude Code, is named for the ID it covers:

```
<ID> <short description>
```

Prepend `(done)` once the work is Live or Complete, `(closed)` for a settled
item. Work in flight or planned carries no prefix. A chat covering several
features gets a `BATCH` ID whose row lists what it produced. Job-application
chats stay out of this namespace.

## Keeping this current

When work finishes: verify against `origin/main`, change the status here,
prepend `(done)` to the chat name. When new work starts: add a row with the
next free ID in its prefix, and name the chat for it first.

## Index

| ID | Feature | Status | Chat name |
|---|---|---|---|
| `SRC-01` | Example: one line on what the feature does; once merged, the PR and merge commit | Planned | `SRC-01 Short description` |
