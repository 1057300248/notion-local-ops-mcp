# 可选应用场景：Notion AI 页面级指令 + 项目管理

English version: [notion-use-case.md](./notion-use-case.md)

这是一个建立在 `notion-local-ops-mcp` 之上的**可选应用场景**。

只有在你想把下面几层放在一起时，才需要它：

- **Notion AI** 的页面级指令页
- 真正执行本地仓库工作的 **MCP Agent**
- 用于协调的 `Projects` / `Tasks` 页面

## 公开页面

- [公开 Notion 指令页示例](https://ncp.notion.site/Agent-Start-Here-Template-10eb4da3979d8396861281ca608bc34e)

把这张页面 duplicate 到你的 workspace，然后去 `Notion AI > 指令` 里绑定。

![Notion AI 指令设置](../assets/notion/notion-ai-instruction-settings.png)

## 实际效果

### 协调页

- `Agent Start Here` + `Projects` + `Tasks`
- 在 Notion 里提供统一协调入口
- 本地 repo 仍然是 source of truth

![Notion 协调页](../assets/notion/notion-coordination-hub.png)

### 任务执行

- 从 task 页面开始
- 从关联 project 推导工作目录
- 让 MCP Agent 执行，并回写简短状态 / 验证摘要

![Notion 任务执行](../assets/notion/notion-task-execution.png)

## 边界

- **Notion AI**：页面级指令层
- **MCP Agent**：执行层
- **Projects / Tasks**：协调层
- **本地 repo + 本地文档**：source of truth
