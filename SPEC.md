# Multi-Agent Team Leader — 需求说明书

## 目标

做一个 Claude Code tool，Main Agent 调用后能自主运行，Team Leader 做决策，只在必要时通知 Main Agent。

---

## 核心功能

### 1. Team Leader 自主决策

Team Leader 独立做出以下决策：
- Gate 过/不过（audit / test 结果）
- 任务分配给哪个 subagent
- 并行 vs 串行执行
- 重试策略
- Bug 怎么修复
- 代码质量标准

### 2. Subagent 编排

Team Leader spawn 并管理：
- **Coding/Debug Agent** — 实现功能、修复 bug
- **Audit Agent** — 安全 + 代码质量审查
- **Testing Agent** — 运行测试、覆盖率分析

### 3. Escalation 通知机制

Escalation 不是"等批准"，而是"通知后自行处理"：

| 类型 | Team Leader 动作 |
|------|----------------|
| 安全漏洞 | 通知 Main，自行 fix 或打回 Coding |
| 超出 scope | 通知 Main，暂停等待指示 |
| Token 耗尽 | 通知 Main，终止并返回结果 |
| 需求模糊 | 通知 Main，blocking 等待用户输入 |

### 4. 输出格式

只返回 summary + gate decision，不返回完整结果：

```json
{
  "summary": "Implemented 3 features, fixed 2 bugs, 84% coverage",
  "gateDecision": "pass",
  "completedPhases": ["coding", "audit", "testing"],
  "findings": {"critical": 0, "high": 1, "medium": 3},
  "escalation": {
    "type": "risk",
    "severity": "high",
    "actionTaken": "Fixed by Coding Agent",
    "notified": true
  }
}
```

---

## 架构设计

### 层级

```
User
  ↓
Main Agent (supervisor)
  ↓ tool() 调用
Multi-Agent Team Tool
  ↓
Team Leader (自主决策)
  ↓ spawn
  ├── Coding/Debug Agent
  ├── Audit Agent
  └── Testing Agent
```

### 组件

| 组件 | 职责 |
|------|------|
| MCP Server | 长期运行，维护 Team Leader 状态，提供 tool 接口 |
| Team Leader | 决策中枢，编排 subagents，做 gate 判断 |
| Subagents | Coding / Audit / Testing，通过 Agent API spawn |
| State Manager | Checkpoint 存储，支持断点恢复 |

---

## Subagent 职责

### Coding/Debug Agent

**输入**：任务描述（implement / debug / refactor）+ 相关文件 + 约束

**输出**：
```json
{
  "status": "success",
  "filesChanged": ["File1.swift", "File2.swift"],
  "summary": "Added login feature",
  "confidence": 0.9,
  "contextAdditions": ["User model extended with auth fields"]
}
```

### Audit Agent

**输入**：目标文件 + 审计深度（quick / standard / deep）

**输出**：
```json
{
  "status": "pass",
  "findings": [{"severity": "high", "location": "auth.py", "description": "SQL injection"}],
  "riskScore": 3,
  "blockShip": false
}
```

### Testing Agent

**输入**：目标文件 + 测试类型 + 覆盖率目标

**输出**：
```json
{
  "status": "all-pass",
  "coverage": 0.84,
  "blockShip": false,
  "flakyTests": []
}
```

---

## Gate 决策规则

| Gate | 通过条件 | 不通过动作 |
|------|---------|-----------|
| Audit | riskScore < 5, blockShip = false | 打回 Coding 修复 |
| Testing | status = all-pass, coverage >= target | 打回 Coding 修 test |
| Final | Audit + Testing 都 pass | 返回 escalation |

---

## Escalation 规则

**通知型（Team Leader 自行处理）：**
- 安全漏洞 → Coding Agent 自行 fix
- 代码风格问题 → 自行修复
- 测试覆盖不足 → 自行补充测试

**等待型（Team Leader 暂停）：**
- 需求模糊 → 等 Main Agent / 用户澄清
- 不可逆操作 → 等 Main Agent 确认
- Token 耗尽 → 终止任务，返回结果

---

## Tool 接口

```json
{
  "name": "multi_agent_team",
  "description": "Spawn autonomous team (Team Leader + Coding/Audit/Testing) for task execution",
  "input": {
    "task": "string (required) - Task description",
    "context": "object (optional) - Project files, config, constraints",
    "config": {
      "workflow": "pipeline | parallel | barrier | auto (default: auto)",
      "gates": {
        "coverage_target": 0.8,
        "audit_block_threshold": "high"
      }
    }
  }
}
```

**返回格式：**
```json
{
  "summary": "string",
  "gateDecision": "pass | fail | escalate",
  "completedPhases": ["coding", "audit", "testing"],
  "findings": {"critical": 0, "high": 0, "medium": 0},
  "escalation": { "type": "", "severity": "", "actionTaken": "", "notified": true } | null,
  "nextAction": "string"
}
```

---

## 实现计划

### Phase 1: 基础设施

1. 创建 MCP Server 框架（Python，stdio 模式）
2. 配置 `settings.json` 注册 tool
3. 实现基础的 tool call / response 框架

### Phase 2: Team Leader

4. Team Leader 状态机（INITIALIZING → PLANNING → EXECUTION → BARRIER → FINALIZING → COMPLETE）
5. Gate 决策逻辑
6. Escalation 判断逻辑

### Phase 3: Subagent 集成

7. 实现 Agent spawn 逻辑（调用 Agent API）
8. Subagent prompt 模板（Coding / Audit / Testing）
9. Context 传递机制

### Phase 4: 状态管理

10. Checkpoint 存储（JSON 文件）
11. 断点恢复逻辑
12. 日志和错误处理

### Phase 5: 测试

13. 本地测试 workflow
14. 修复 bug
15. 文档

---

## 文件结构

```
/Users/toby/multi-agent-team/
├── SPEC.md                 # 本文档
├── server.py               # MCP Server 主入口
├── team_leader.py          # Team Leader 逻辑
├── state_manager.py        # Checkpoint 管理
├── agents/
│   ├── coding.md           # Coding Agent prompt
│   ├── audit.md            # Audit Agent prompt
│   └── testing.md          # Testing Agent prompt
├── config.py               # 配置
└── state/                  # Checkpoint 文件
    └── {session_id}.json
```
