# Optional Use Case: Notion AI Instruction Page + Project Management

中文版本：[notion-use-case.zh-CN.md](./notion-use-case.zh-CN.md)

This is an **optional use case** built on top of `notion-local-ops-mcp`.

Use it only if you want all of the following together:

- a page-level instruction page for **Notion AI**
- an **MCP Agent** that executes local repo work
- `Projects` / `Tasks` pages for coordination in Notion

## Public Page

- [Public Notion instruction-page demo](https://ncp.notion.site/Agent-Start-Here-Template-10eb4da3979d8396861281ca608bc34e)

Duplicate that page into your own workspace, then bind it in `Notion AI > Instructions`.

![Notion AI instruction settings](../assets/notion/notion-ai-instruction-settings.png)

## What It Looks Like

### Coordination Hub

- `Agent Start Here` + `Projects` + `Tasks`
- shared coordination surface in Notion
- local repo remains the source of truth

![Notion coordination hub](../assets/notion/notion-coordination-hub.png)

### Task Execution

- start from a task page
- derive working directory from the related project
- let the MCP Agent execute and write back short status / verification

![Notion task execution](../assets/notion/notion-task-execution.png)

## Boundary

- **Notion AI**: page-level instruction layer
- **MCP Agent**: execution layer
- **Projects / Tasks**: coordination layer
- **local repo + local docs**: source of truth
