# DeepSeek关闭思考基准：Jev领域仲裁与纯回复过滤

## 不混淆的四组实验

1. 领域局部21条：A为DeepSeek领域分类，B为Jev高分接管＋DeepSeek回退。
2. 领域全局12场景：仅改变DOMAIN_ROUTE_PROVIDER=llm/jev_cascade；事件llm、门控rules固定。
3. 事件局部36条：A为自由文本直接DeepSeek（llm_direct），B为Jev纯回复过滤＋DeepSeek（jev_resume_filter）。
4. 事件全局5场景：两组领域均Jev、门控均rules，只改变上述事件策略；结构化卡片保持宿主协议。

真实请求前冻结数据，随机顺序交替AB/BA，四组串行。各场景一次，小样本、人工模板复用，不是生产SLA。
所有LLM角色均deepseek-flash，thinking.type=disabled；原阈值与1.5秒Jev超时未调整。失败、重试及未知用量均保留。
局部不调用业务工具；全局真实Graph、MCP、外部工具和隔离存储，人工回复由测试策略自动提交，不计用户思考/浏览器展示。
本轮复用此前已查看过的冻结测试样本，属于回归对比，不宣称全新盲测或真实用户分布。

## Jev单任务领域收益

| 范围 | 完整通过/提交 A→B | P50秒 | P95秒 | 总Token |
|---|---|---|---|---|
| 局部领域21条 | 21/21 → 21/21 | 0.748 → 0.302 | 1.316 → 1.232 | 4494 → ≥15428（1次未知） |
| 全链路12场景 | 10/12 → 10/12 | 3.731 → 3.265 | 7.057 → 6.699 | 29965 → ≥34293（1次未知） |

旧Grok局部21条：21/21→21/21，P50 3.551→0.548秒，P95 5.684→1.481秒，Token15161→16575。旧值属于另一次模型/网络条件，不能当成DeepSeek基准。

## 自由文本是否需要事件LLM

| 范围 | 完整通过/提交 A→B | P50秒 | P95秒 | 总Token |
|---|---|---|---|---|
| 完整事件36条 | 34/36 → 34/36 | 0.852 → 1.216 | 1.655 → 2.004 | 24659 → ≥46850（1次未知） |
| 纯回复12条 | 12/12 → 12/12 | 0.777 → 1.068 | 1.655 → 1.816 | 8115 → 14342 |
| 恢复＋新任务12条 | 12/12 → 12/12 | 0.814 → 1.280 | 1.050 → 2.199 | 8220 → ≥16513（1次未知） |
| 边界12条 | 10/12 → 10/12 | 0.892 → 1.223 | 1.748 → 2.004 | 8324 → 15995 |
| 全链路5场景 | 5/5 → 5/5 | 5.452 → 7.595 | 7.918 → 8.699 | 19873 → ≥22511（1次未知） |

A是用户要求的直接LLM方案，不是现在线上规则优先方案。Jev只判resume_only/needs_llm，不创建任务，不授予权限。
只有高分且宿主能唯一绑定完整原文到有效待恢复任务，B才跳过事件LLM；复合、歧义、否定、过期和低分仍交DeepSeek。
高校候选是局部人工快照；真实5场景覆盖天气/音乐，不宣称实时高德已验证高校候选简称。

## 接管、失败与归因

```json
{
  "pure_accepted": 4,
  "pure_submitted": 12,
  "unsafe_shortcuts": 0,
  "non_pure_submitted": 24,
  "raw_filter_returned": 33,
  "raw_filter_labels_correct": 30,
  "llm_attempts": {
    "baseline": 36,
    "candidate": 32
  }
}
```

Jev原始标签正确、通过门槛接管、最终完整事件正确是三个不同分母；标签返回但低分回退不能算作接管，也不把回退LLM错误说成Jev标签错误。

### 启用建议

保留此前已获批的单领域Jev，但把中位数收益、尾部收益和增加的Token分别看，不以局部分类提升替代全局任务完成率。
本批事件过滤没有减少全局事件LLM调用，增加的前置HTTP请求与回退抵消了少数纯回复的收益，不建议启用。保持当前规则优先＋DeepSeek解释，后续再单独验证可唯一绑定时零模型恢复的覆盖率。

### pure01

```json
[
  {
    "id": "pure01",
    "repeat": 0,
    "arm": "candidate",
    "group": "pure_reply",
    "passed": true,
    "expected_label": "resume",
    "source": "jev",
    "decision": {
      "route": "resume",
      "task_id": "place",
      "answer": "无锡校区吧",
      "new_query": null,
      "question": null
    },
    "actual_label": "resume",
    "grade": {
      "passed": true,
      "errors": []
    },
    "elapsed_ms": 395,
    "attempts": [
      {
        "type": "model.attempt.completed",
        "stage": "input_events_precheck",
        "attempt": 1,
        "span_id": "8d5c364e-b535-4713-9240-4c024f613d82",
        "provider": "typesafe",
        "model_name": "jev-1.14.0",
        "elapsed_ms": 385,
        "usage": {
          "input_tokens": 713,
          "output_tokens": 36
        },
        "usage_known": true
      }
    ],
    "fallback": false,
    "prechecks": [
      {
        "type": "input.precheck.completed",
        "strategy": "resume_filter",
        "accepted": true,
        "reason": "accepted",
        "answer": {
          "type": "choice",
          "choice": "resume_only",
          "probabilities": {
            "resume_only": 0.96,
            "needs_llm": 0.04
          },
          "confidence": 0.92
        },
        "host_can_bind_pure_reply": true
      }
    ],
    "expected_filter": "resume_only",
    "actual_filter": "resume_only",
    "filter_label_correct": true,
    "unsafe_shortcut": false
  },
  {
    "id": "pure01",
    "repeat": 0,
    "arm": "baseline",
    "group": "pure_reply",
    "passed": true,
    "expected_label": "resume",
    "source": "llm",
    "decision": {
      "route": "resume",
      "task_id": "place",
      "answer": "无锡校区吧",
      "new_query": null,
      "question": null
    },
    "actual_label": "resume",
    "grade": {
      "passed": true,
      "errors": []
    },
    "elapsed_ms": 1061,
    "attempts": [
      {
        "type": "model.attempt.completed",
        "stage": "input_events",
        "attempt": 1,
        "span_id": "0996a703-6fa8-4d57-b7b1-c92454e7f03a",
        "model_name": "deepseek-flash",
        "elapsed_ms": 1054,
        "usage": {
          "input_tokens": 653,
          "output_tokens": 28,
          "total_tokens": 681,
          "input_token_details": {
            "cache_read": 384
          },
          "output_token_details": {}
        },
        "usage_known": true
      }
    ],
    "fallback": false,
    "prechecks": [],
    "expected_filter": "resume_only",
    "actual_filter": null,
    "filter_label_correct": null,
    "unsafe_shortcut": false
  }
]
```

### compound01

```json
[
  {
    "id": "compound01",
    "repeat": 0,
    "arm": "baseline",
    "group": "compound",
    "passed": true,
    "expected_label": "resume",
    "source": "llm",
    "decision": {
      "route": "resume",
      "task_id": "place",
      "answer": "无锡校区吧",
      "new_query": "顺便查下明天无锡天气",
      "question": null
    },
    "actual_label": "resume",
    "grade": {
      "passed": true,
      "errors": []
    },
    "elapsed_ms": 807,
    "attempts": [
      {
        "type": "model.attempt.completed",
        "stage": "input_events",
        "attempt": 1,
        "span_id": "4f2df598-d1db-4a10-89c3-220ceeb57ce2",
        "model_name": "deepseek-flash",
        "elapsed_ms": 797,
        "usage": {
          "input_tokens": 660,
          "output_tokens": 27,
          "total_tokens": 687,
          "input_token_details": {
            "cache_read": 512
          },
          "output_token_details": {}
        },
        "usage_known": true
      }
    ],
    "fallback": false,
    "prechecks": [],
    "expected_filter": "needs_llm",
    "actual_filter": null,
    "filter_label_correct": null,
    "unsafe_shortcut": false
  },
  {
    "id": "compound01",
    "repeat": 0,
    "arm": "candidate",
    "group": "compound",
    "passed": true,
    "expected_label": "resume",
    "source": "llm",
    "decision": {
      "route": "resume",
      "task_id": "place",
      "answer": "无锡校区吧",
      "new_query": "顺便查下明天无锡天气",
      "question": null
    },
    "actual_label": "resume",
    "grade": {
      "passed": true,
      "errors": []
    },
    "elapsed_ms": 932,
    "attempts": [
      {
        "type": "model.attempt.completed",
        "stage": "input_events_precheck",
        "attempt": 1,
        "span_id": "3f7b8e38-2456-4fa8-ad1a-08560bac22b5",
        "provider": "typesafe",
        "model_name": "jev-1.14.0",
        "elapsed_ms": 240,
        "usage": {
          "input_tokens": 724,
          "output_tokens": 37
        },
        "usage_known": true
      },
      {
        "type": "model.attempt.completed",
        "stage": "input_events",
        "attempt": 1,
        "span_id": "bfa95341-ba3e-4fcc-9b04-ed388ed9298b",
        "model_name": "deepseek-flash",
        "elapsed_ms": 679,
        "usage": {
          "input_tokens": 660,
          "output_tokens": 27,
          "total_tokens": 687,
          "input_token_details": {
            "cache_read": 512
          },
          "output_token_details": {}
        },
        "usage_known": true
      }
    ],
    "fallback": true,
    "prechecks": [
      {
        "type": "input.precheck.completed",
        "strategy": "resume_filter",
        "accepted": false,
        "reason": "needs_llm_or_unsafe_binding",
        "answer": {
          "type": "choice",
          "choice": "needs_llm",
          "probabilities": {
            "resume_only": 0.01,
            "needs_llm": 0.99
          },
          "confidence": 0.98
        },
        "host_can_bind_pure_reply": false
      }
    ],
    "expected_filter": "needs_llm",
    "actual_filter": "needs_llm",
    "filter_label_correct": true,
    "unsafe_shortcut": false
  }
]
```

### local_domain

P50变化：-59.63%；P95变化：-6.38%。负数表示耗时下降。
用量完整交集20组：4283→15217 Token，变化+255.29%；不代替全体未知总量。
阶段尝试数、P50/P95与用量见对应summary.json；阶段时长不能直接相加当并行关键路径。

### global_domain

P50变化：-12.49%；P95变化：-5.07%。负数表示耗时下降。
用量完整交集11组：28516→32852 Token，变化+15.21%；不代替全体未知总量。
阶段尝试数、P50/P95与用量见对应summary.json；阶段时长不能直接相加当并行关键路径。
失败保留：`{"id": "multi_completion-04", "arm": "baseline", "source": null, "error_type": null, "grade": null, "failures": ["goal1:wrong_domain", "goal1:leaf_not_completed", "goal1:missing_successful_tool_receipt:maps_weather", "goal2:missing_task", "unexpected_task_count"]}`。
失败保留：`{"id": "multi_completion-04", "arm": "candidate", "source": null, "error_type": null, "grade": null, "failures": ["goal1:wrong_domain", "goal1:leaf_not_completed", "goal1:missing_successful_tool_receipt:maps_weather", "goal2:missing_task", "unexpected_task_count"]}`。
失败保留：`{"id": "jev-four-goals", "arm": "candidate", "source": null, "error_type": null, "grade": null, "failures": ["goal1:wrong_domain", "goal1:leaf_not_completed", "goal1:missing_successful_tool_receipt:maps_weather", "goal2:missing_task", "goal3:missing_task", "goal4:missing_task", "unexpected_task_count"]}`。
失败保留：`{"id": "jev-four-goals", "arm": "baseline", "source": null, "error_type": null, "grade": null, "failures": ["goal1:wrong_domain", "goal1:leaf_not_completed", "goal1:missing_successful_tool_receipt:maps_weather", "goal2:missing_task", "goal3:missing_task", "goal4:missing_task", "unexpected_task_count"]}`。

### local_events

P50变化：+42.72%；P95变化：+21.09%。负数表示耗时下降。
用量完整交集35组：23968→46166 Token，变化+92.62%；不代替全体未知总量。
阶段尝试数、P50/P95与用量见对应summary.json；阶段时长不能直接相加当并行关键路径。
失败保留：`{"id": "boundary04", "arm": "candidate", "source": "llm", "error_type": null, "grade": {"passed": false, "errors": ["route", "target"]}, "failures": null}`。
失败保留：`{"id": "boundary04", "arm": "baseline", "source": "llm", "error_type": null, "grade": {"passed": false, "errors": ["route", "target"]}, "failures": null}`。
失败保留：`{"id": "boundary06", "arm": "candidate", "source": "llm", "error_type": null, "grade": {"passed": false, "errors": ["route", "new_task_presence", "new_task_content"]}, "failures": null}`。
失败保留：`{"id": "boundary06", "arm": "baseline", "source": "llm", "error_type": null, "grade": {"passed": false, "errors": ["route", "new_task_presence", "new_task_content"]}, "failures": null}`。

### global_events

P50变化：+39.31%；P95变化：+9.86%。负数表示耗时下降。
用量完整交集4组：17313→20481 Token，变化+18.30%；不代替全体未知总量。
阶段尝试数、P50/P95与用量见对应summary.json；阶段时长不能直接相加当并行关键路径。

## 运行与数据边界

DeepSeek及此前已获批的领域Jev部署决定与本实验分开；本轮不自动启用事件过滤。Token是供应商reported total（含失败与回退），不是统一分词或货币费用。
datasets/保存输入期望；modules/、end_to_end/分别有cases/results JSONL、details.csv、summary与脱敏manifest。原始SQLite与日志在私有evaluations/runs/deepseek-jev-ab-20261029-01。Key不提交。

```bash
.venv/bin/python -m evaluations.deepseek_jev_ab --render
.venv/bin/python -m evaluations.deepseek_jev_ab --run-live --public <新公开目录> --private <新私有目录>
```

报告复算不调用API；真实试验需新目录并会消耗供应商额度。
