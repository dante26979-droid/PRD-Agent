# Issue tracker: GitHub

Issues and PRDs for this repository live in GitHub Issues at
`dante26979-droid/PRD-Agent`. Use the `gh` CLI for repository-local operations.

## Conventions

- Create an issue with `gh issue create --title "..." --body "..."`.
- Read an issue with `gh issue view <number> --comments`, including its labels.
- List issues with `gh issue list --state open --json number,title,body,labels,comments`.
- Comment with `gh issue comment <number> --body "..."`.
- Apply or remove labels with `gh issue edit <number> --add-label "..."` or
  `--remove-label "..."`.
- Close an issue with `gh issue close <number> --comment "..."`.

Infer the repository from the configured Git remote when the command runs inside
this clone.

## Skill mapping

When an engineering skill says “publish to the issue tracker”, create a GitHub
issue. When it says “fetch the relevant ticket”, read the matching GitHub issue
and its comments.
