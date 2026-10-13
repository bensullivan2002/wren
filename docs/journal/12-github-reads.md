# GitHub reads without opening the shell's fence

*Issue #12 · follow-up to step 10 · 13 October 2026*

The request was simple: stop asking before reading GitHub, but keep destructive
commands behind permission. We chose a dedicated `github_read` tool rather than
allowlisting shell commands (D133). Comments, creation and other writes remain
under the existing approval rules too, not just deletion.

## 1. A host is not an action

A GitHub read and a repository deletion both contact the same API host. Allowing
that domain in the sandbox would allow both. And allowing `gh` by its first word
would cover every subcommand: `gh issue view` and `gh repo delete` alike.

The solution is a smaller interface. The model picks from nine known operations
and supplies identifiers. Wren owns the method, host and endpoint. There is no
input for a command line, arbitrary flags, API paths or GraphQL documents.

```python
command = [
    executable,
    "api",
    "--hostname",
    "github.com",
    "--method",
    "GET",
    endpoint,
]
```

This is an **argument list**, not shell text. `shell._run` uses `shell=False`
for a list, so punctuation in an argument cannot run a second command. We still
validate the identifiers: slashes, percent escapes and query characters could
otherwise change the HTTP path even without a shell. Literal `.` and `..`
repository names are refused; `.github` is allowed.

`Literal[...]` in the Pydantic input generates an enum for the model and rejects
unknown operations at runtime. `ToolInput` also forbids extra arguments and type
coercion. An `after` model validator checks rules involving multiple fields:
views need a number, lists must not have one, and pagination belongs to
collections. The registry validates all this before execution.

## 2. Reuse sign-in, not a new secret store

The installed `gh api` built-in handles the user's existing GitHub sign-in.
Wren does not retrieve or print the token. It uses an explicit host instead of
relying on `GH_HOST`, and disables `GH_DEBUG` in a copy of the environment so
HTTP request headers cannot accidentally enter the tool result. No interactive
prompts or custom pager are needed.

There is another trust boundary: which program is actually `gh`? PATH lookup
alone could find a script planted in the project. Sandboxed commands can write
the project and temporary folders, so the resolved executable must be outside
both. Symlink resolution happens before that check. This is not a sandbox for
the installed CLI: the user's installed executable and gh configuration are
trusted local software.

The ordinary shell retains its existing sandbox. Network access and gh's
credential folder are still blocked there; the special path belongs only to
this tool. It runs without a permission request, like the file-reading tools,
and does not add anything to `Permissions.always`.

## 3. Bounded reads and truthful results

GitHub collections are paged. Automatically following all pages can spend a long
time and flood the context, so every operation returns one explicit page per
collection: 30 items by default, at most 100. The next page is a separate call.
The result labels the page and warns when output is truncated.

PR comments read conversation comments from the issues endpoint and inline
review comments from the pulls endpoint. PR checks first read `.head.sha` with
a **fixed** jq expression. Only a validated hexadecimal SHA can then select the
check-runs and combined-status endpoints. Both describe the same head commit,
even if the PR branch moves between requests. GitHub can still update a check's
state while these reads are in progress; this is not an atomic snapshot.

Every request uses the subprocess polling and process-group kill machinery
already used by shell. A single deadline spans the whole tool call:

```python
deadline = time.monotonic() + args.timeout
# Before each request:
remaining = deadline - time.monotonic()
```

`monotonic()` measures elapsed time without being affected by adjustments to
the computer's clock. A later request receives only the remaining budget, not
a fresh two minutes. The stop callback is checked between requests too.

Authentication, access and connection failures become readable error results.
Success output is limited to 20,000 characters. The shared runner buffers the
subprocess output before truncation: the display cap is not a streaming memory
limit. Huge diffs cannot currently be paged or fully recovered through this tool.

## Tests and things to try

`tests/test_github_read.py` fakes the CLI runner, so tests never use the network
or real credentials. They check exact argument lists, approval-free reads on
both platforms/modes, refused mutations and hostile identifiers, explicit
pagination, output caps, deadlines, cancellation, missing/untrusted executables,
and disabled debug logging. An agent round trip checks `history_problems` stays
empty. Existing shell tests continue checking the real macOS sandbox.

A live smoke test on 13 October 2026 exercised all nine operations against
`bensullivan2002/wren`, including issue #12, PR #32, both checks mechanisms and
a truncated diff. No read invoked the approval callback. A comment request with
a denying callback was blocked before execution; nothing was posted and no
“always” grant was created. This checks the tool against real gh/API responses,
not yet a real model choosing the tool in a conversation.

1. Install gh and sign in with `gh auth login` in your own terminal.
2. Ask Wren: “Read issue 12 and its comments in bensullivan2002/wren.” It should
   call `github_read` without asking to leave the sandbox.
3. Ask for a PR's diff and checks. Check that both check runs and commit statuses
   appear; they are different GitHub mechanisms.
4. Ask for a list with `per_page: 1`, then page 2. There should be no hidden
   automatic pagination.
5. Ask Wren to comment on an issue, then decline. Nothing should be posted.
6. Use `/permissions` and `/auto` in either front end. Both distinguish GitHub
   reads from writes and general shell commands.

## Known limits and next steps

- GitHub.com only; Enterprise hosts need a deliberate host/authentication policy.
- No arbitrary endpoints, GraphQL, repository file downloads or write operations.
- GitHub's issue list includes PRs; views and comments are separate operations.
- PR comments include conversation and inline comments, not submitted review summaries.
- Raw REST JSON can be verbose and truncate. Smaller pages or item views help;
  curated output and paged diffs could be future improvements.
- Reads use whatever repositories the user's gh account can access. Private
  content goes to the model and saved session, like file reads.
- “Always allow gh” remains a broad, explicit user grant for shell; this change
  neither grants it automatically nor revokes existing grants.
- Linux shell commands still ask because there is no sandbox there yet. This
  dedicated, fixed read-only path works without one.
