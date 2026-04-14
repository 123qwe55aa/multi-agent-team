# Multi-Agent Team Leader — 实现计划

---

## Phase 1: 基础设施

### 1.1 MCP Server 框架

创建 `/Users/toby/multi-agent-team/server.py`：
- Python stdio 模式 MCP Server
- 基本 tool 定义和 handler 框架
- JSON-RPC 2.0 协议实现

### 1.2 注册 Tool

修改 `~/.claude/settings.json`：
```json
{
  "mcpServers": {
    "multi-agent-team": {
      "command": "python",
      "args": ["/Users/toby/multi-agent-team/server.py"]
    }
  }
}
```

### 1.3 测试连接

验证 Claude Code 能调用 multi_agent_team tool。

---

## Phase 2: Team Leader

### 2.1 状态机

文件：`team_leader.py`

状态定义：
```python
class TeamState(Enum):
    INITIALIZING = "initializing"
    PLANNING = "planning"
    EXECUTION = "execution"
    BARRIER = "barrier"
    SYNTHESIZING = "synthesizing"
    CORRECTIVE = "corrective"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    ESCALATED = "escalated"
```

状态转换规则。

### 2.2 Gate 决策逻辑

```python
def evaluate_gate(result: SubagentResult, config: GateConfig) -> GateDecision:
    # Audit gate
    if result.type == "audit":
        if result.riskScore >= 5 or result.blockShip:
            return GateDecision.FAIL
        return GateDecision.PASS

    # Testing gate
    if result.type == "testing":
        if result.status != "all-pass":
            return GateDecision.FAIL
        if result.coverage < config.coverage_target:
            return GateDecision.FAIL
        return GateDecision.PASS
```

### 2.3 Escalation 判断

```python
def should_escalate(result: SubagentResult, state: TeamState) -> EscalationType:
    # Authority gap
    if result.requires_scope_change:
        return EscalationType.AUTHORITY_GAP

    # Risk/safety
    if result.has_critical_security_finding:
        return EscalationType.RISK

    # Resource exhaustion
    if result.tokens_exhausted:
        return EscalationType.RESOURCE

    # Confidence
    if result.confidence < 0.6:
        return EscalationType.CONFIDENCE

    return None
```

---

## Phase 3: Subagent 集成

### 3.1 Agent Spawn

通过 `subprocess` 调用 Claude Code Agent API：

```python
def spawn_agent(agent_type: str, task: str, context: dict) -> AgentResult:
    prompt = load_prompt(f"agents/{agent_type}.md")
    # 调用 Claude Code Agent...
```

### 3.2 Subagent Prompts

**coding.md**:
- Role: Coding/Debug expert
- 任务类型：implement / debug / refactor
- 输出格式要求
- Context 传递规范

**audit.md**:
- Role: Security + Quality auditor
- 审计深度：quick / standard / deep
- 发现分类：critical / high / medium / low
- blockShip 条件

**testing.md**:
- Role: Testing engineer
- 测试类型：unit / integration / e2e
- 覆盖率要求
- Flaky test 检测

### 3.3 Context 传递

```python
class ContextSlice:
    files: dict           # changed files
    facts: list           # agreed facts
    decisions: list       # decisions made
    results: dict         # agent results

    def delta_only(self) -> ContextSlice:
        # 只传递增量，不过度传递
```

---

## Phase 4: 状态管理

### 4.1 Checkpoint 存储

文件：`state_manager.py`

```python
class StateManager:
    def save_checkpoint(self, session_id: str, state: TeamState, context: dict):
        path = f"state/{session_id}.json"
        with open(path, "w") as f:
            json.dump({"state": state.value, "context": context}, f)

    def load_checkpoint(self, session_id: str) -> Optional[dict]:
        path = f"state/{session_id}.json"
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return None
```

### 4.2 断点恢复

启动时检查是否有未完成的任务，有则恢复。

### 4.3 日志

使用 Python logging，记录：
- Agent spawn / complete
- Gate decisions
- Escalations
- 错误

---

## Phase 5: 测试

### 5.1 单元测试

- Team Leader 状态机
- Gate 决策逻辑
- Escalation 判断

### 5.2 集成测试

- 完整的 workflow 执行
- Subagent 协作
- Checkpoint 恢复

### 5.3 手动测试

在 Claude Code 中调用 tool，验证实际运行效果。

---

## 优先级

1. **Phase 1** — 必须先跑起来
2. **Phase 2** — Team Leader 核心逻辑
3. **Phase 3** — Subagent 能工作
4. **Phase 4** — 稳定性保障
5. **Phase 5** — 迭代改进

---

## 预计代码量

| 文件 | 行数估计 |
|------|---------|
| server.py | ~150 |
| team_leader.py | ~400 |
| state_manager.py | ~100 |
| agents/*.md | ~300 |
| config.py | ~50 |
| **总计** | ~1000 |
