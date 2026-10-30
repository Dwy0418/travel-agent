# 国内旅行规划智能体（自校验 + 自动重排）

一个**会自我纠错**的国内旅行规划智能体：DeepSeek 负责决策，高德地图与 Open-Meteo 提供事实数据，
**所有算术与可行性判断由 Python 代码完成**；行程生成后立刻被 6 条硬性规则校验，不通过就把问题
以自然语言回灌给模型重排，直到通过或达到迭代上限。

- 决策层：DeepSeek（OpenAI 兼容接口，`strict` 工具模式，失败自动降级）
- 数据层：高德 Web 服务（地理编码 / POI 检索 / 路径规划，GCJ-02）+ Open-Meteo（天气）
- 校验层：纯 Python，6 条规则，零模型参与
- 展示层：界面全中文（`travel_agent/zh_labels.py` 统一映射），日志只说人话，坐标等机器参数只进后台日志
- 交付层：Streamlit 界面、CLI、Markdown 行程单、评测报告

## 目录结构

```
travel-agent/
├── travel_agent/
│   ├── config.py      # 配置与硬性阈值（超时、重试、迭代上限、每日时长…）
│   ├── tools.py       # 5 个工具：geocode / search_poi / get_weather / plan_route / estimate_budget
│   ├── schemas.py     # strict 工具 Schema + pydantic 参数校验 + 行程数据模型
│   ├── llm.py         # DeepSeek 客户端（strict 模式 + 自动降级）
│   ├── validator.py   # 6 条校验规则，返回自然语言问题
│   ├── zh_labels.py   # 显示层中英映射 + 人话日志描述（界面文案唯一来源）
│   └── agent.py       # 主循环：多轮工具调用 -> 生成 -> 校验 -> 重排 -> 交付
├── app.py             # Streamlit 界面（执行过程关键节点 + 技术明细折叠 + 全中文表格 + 行程单下载）
├── cli.py             # 命令行入口
├── preflight.py       # 上线前预检：Key / 接口 / strict 工具模式
├── test_api.py        # 最小连通性测试：高德 / 天气 / DeepSeek 三个接口逐个试
├── evaluate.py        # 20 用例评测 + 指标报告
├── selftest.py        # 离线自检（无需任何 Key、不联网）
├── 答辩素材.md        # 演示脚本 + 高频问答 + 项目结构说明（答辩用）
├── 部署指南.md        # 全中文部署手册：GitHub 建仓 -> Streamlit Cloud -> Secrets -> 验收
├── deploy.ps1         # 一键建仓并推送（令牌只在内存中使用，不落盘）
├── .streamlit/
│   ├── config.toml            # 主题与运行参数（本地/云端通用）
│   └── secrets.toml.example   # 云端 Secrets 模板（真实文件已 git 忽略）
├── requirements.txt
└── .env.example
```

## 快速开始

```powershell
cd travel-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

copy .env.example .env      # 然后填入 DEEPSEEK_API_KEY 与 AMAP_KEY（高德选「Web服务」类型）

# 1) 先跑离线自检：不需要任何 Key
.\.venv\Scripts\python.exe selftest.py

# 1.5) 预检：验证两个 Key 真实可用 + strict 工具模式是否生效（约 5 秒，几乎不耗 token）
.\.venv\Scripts\python.exe preflight.py
.\.venv\Scripts\python.exe preflight.py --full   # 再加跑一次完整规划（会消耗少量 token）
# 1.6) 只想确认 Key 通不通？三个接口逐个试，失败会指出是哪个 Key 的问题
.\.venv\Scripts\python.exe test_api.py



# 2) Web 界面
.\.venv\Scripts\python.exe -m streamlit run app.py

# 3) 命令行
.\.venv\Scripts\python.exe cli.py --city 梅州市 --days 2 --people 2 --budget 2500 --companions 带老人

# 4) 评测（会真实调用 API，注意额度；先小样本再全量）
.\.venv\Scripts\python.exe evaluate.py --limit 3
.\.venv\Scripts\python.exe evaluate.py
```

## 在线部署（Streamlit Community Cloud）

Streamlit Cloud 只能部署 **GitHub 仓库**里的代码，所以先把本项目推到一个独立仓库，再在网页上点几下：

```powershell
.\deploy.ps1                    # 交互式：粘贴一次 GitHub Token -> 自动建仓 + 提交 + 推送
# 或手动：git init -b main; git add -A; git commit -m "部署"; git push -u origin main
```

代码已推送到 **<https://github.com/Dwy0418/travel-agent>**（`main` 分支），
再打开 <https://share.streamlit.io> → **Create app** → 仓库选 `Dwy0418/travel-agent`、
分支 `main`、主文件 `app.py` → 在 **Settings → Secrets** 粘贴：

```toml
DEEPSEEK_API_KEY = "sk-你的Key"
AMAP_KEY = "你的高德Web服务Key"
```

保存即自动重启，约 2 分钟拿到 `https://<子域名>.streamlit.app`。
密钥读取顺序是 **环境变量（本地 `.env`）→ Streamlit Secrets（线上）→ 默认值**，
同一份代码本地与云端都不用改。完整图文步骤与报错对照见 **[部署指南.md](部署指南.md)**。

`preflight.py` 是「填完 Key 之后、跑评测之前」的那道闸：它会依次检查 Key 是否填写、
高德地理编码 / POI（失败时把高德原始原因如 `INVALID_USER_KEY` 透出来）、Open-Meteo 天气、
高德路径规划、DeepSeek 连通性、**strict 工具模式是否真的生效**（账号不支持时会自动降级并明确报出来）、
模型是否真的会主动调用工具。任何一项不过都会给出可操作提示，退出码 0/1/2。

`selftest.py` 会把 `tools.execute_tool` 替换成离线替身，并用假 LLM 驱动完整的
「出错 -> 校验 -> 重排 -> 通过」闭环，因此可以零成本验证逻辑是否正确。当前 74 项检查覆盖：
预算公式、6 条校验规则（正反用例）、超时反馈的压缩配额（须与「游览 ≤5h / 带老人 ≤4h」取更严者）、
配置取值顺序（环境变量 > 云端 Secrets > 默认值，缺 Secrets 不得崩）、
行程 JSON 容错解析（含**输出被截断时不得把碎片当行程**）、
坐标写反守卫、高德 V5 路径响应解析（driving/walking/transit 三种 shape）、
点间交通费补算、**分时段营业时间（午休闭馆）解析**、Markdown 渲染、
**显示层中英映射与「日志无坐标、缓存按轮汇总」**、
Agent 自纠错 / 迭代上限 / 同组问题连续不改善的提前止损、
以及界面冒烟（Streamlit `AppTest`：首屏 / 缺 Key 提示 / 3 个指标卡 / 中文表格 / 折叠区文案）。

## 一次规划的完整流程

```
用户约束 ──► build_constraints（天数/人数/预算/同行人 -> 结构化字典）
        ──► geocode 取城市中心坐标（代码调用，供天气校验与模型复用）
        ──► 代码侧预取（并行，约 1 秒）：get_weather + 3 个关键词 search_poi
              结果写入工具缓存并整理成候选清单注入提示词（模型不必再花 1-2 轮取数）
        ──► 循环（最多 config.MAX_ITERATIONS = 10 轮）
              ├─ DeepSeek 返回 tool_calls → 参数校验 → 执行工具 → 结果回灌
              ├─ DeepSeek 返回文本     → parse_itinerary 解析结构化行程
              │                          └─ 失败：把解析错误回灌，继续下一轮
              ├─ validator.validate 6 条规则（纯代码，数字全部代码算）
              │     └─ 有问题：把问题清单以自然语言回灌，继续下一轮
              └─ 无问题：结束，交付
        ──► estimate_budget（纯代码）→ render_markdown → 界面 / CLI / 文件
```

关键设计：**模型只做决策与叙述，不做算术**。距离、耗时、预算、步行距离、是否超时全部由 Python
计算，因此「预算超了」「今天太赶」这类结论是确定的，而不是模型的错觉。

## 工具清单

| 工具 | 作用 | 关键防呆 |
| --- | --- | --- |
| `geocode` | 地名 -> GCJ-02 经纬度 + 城市名 | 空结果返回结构化错误 |
| `search_poi` | 按城市 + 关键词搜景点/餐饮/住宿，返回名称、地址、坐标、营业时间、票价 | `count` 限制 1-10 |
| `get_weather` | 未来 1-16 天降水概率/气温/是否适合户外 | 天数上限 16，覆盖整段行程 |
| `plan_route` | 两点距离(米)/耗时(秒)/费用(元)，driving/walking/transit | 失败时按直线距离 × 1.3 估算并标注 `estimated` |
| `estimate_budget` | 门票+交通+餐饮+住宿 | 行程草稿由代码自动注入，模型不传、不算；点间用车费按 `plan_route` 结果代码累加 |

所有工具：10 秒超时、失败重试 2 次（1s -> 2s 指数退避）、异常一律转成
`{"success": false, "error": "..."}`，绝不让主流程崩溃。坐标参数由 pydantic 强校验：
经度必须在 73-136、纬度必须在 3-54，否则判定「经纬度写反」直接拒绝——这是国内数据最常见的坑。

## 六条校验规则

| 规则 | 判定依据 |
| --- | --- |
| 天气 | 降水概率 ≥ 30%（`RAIN_PROB_THRESHOLD`）的当天不允许排户外景点（`indoor=false` 或名称不含室内关键词） |
| 路程耗时 | 游览时长 + `plan_route` 实测交通时长 + 1 小时午休 ≤ 8 小时，超了会指出最远的一段 |
| 预算 | `estimate_budget` 计算的总预算不得超用户上限（点间用车费按 `plan_route` 结果由代码补入，避免模型只写交通方式、不写金额导致漏算） |
| 营业时间 | 开始时间必须落在 `open_time` 窗口内；`周一闭馆` 式说明与当天星期比对 |
| 同行人 | 带老人/小孩：单日景点 ≤ 3 个、步行 ≤ 5 公里（POI 未给 `walk_km` 时按 1.5 公里/景点估） |
| 重复景点 | 同名景点出现多次即报错 |

此外还有基础校验：天数与需求一致、时间格式 `HH:MM` 且不重叠、景点必须有经纬度（禁止编造地点）。

## 评测指标

`evaluate.py` 用 20 个覆盖不同城市/天数/预算/同行人的用例统计（全部由代码计算，无主观打分）：

| 指标 | 目标 | 说明 |
| --- | --- | --- |
| 约束满足率 | ≥ 90% | 最终通过全部硬性校验的用例占比（自纠错重排后通过也算） |
| 工具调用准确率 | ≥ 95% | 工具调用中参数合法（通过 pydantic 校验）的占比 |
| 平均迭代次数 | ≤ 5 | 含首轮生成 |
| 重排率 | ≥ 30% | 触发过自纠错重排的用例占比（证明闭环真的在工作） |
| 平均耗时 | ≤ 30s | 端到端墙钟时间 |
| 耗时中位数 | ≤ 30s | 端到端墙钟时间的中位数；不受个别模型延迟尖峰干扰，更能反映常态 |

> 最近一次全量 20 用例实测（2026-10-30）：约束满足率 **1.0（20/20 全部 ok）**、
> 工具调用准确率 **0.995**、重排率 **0.75**、平均迭代 5.35、平均耗时 35.5s（中位数 36.2s）。
> 其中「代码侧预取」上线前同口径为 6.95 轮 / 45.9s（中位数 45.8s），
> 即迭代次数降低约 23%、端到端耗时降低约 23%。耗时仍略超 30s 的原因见下方「已知边界」。

结果写入 `results/eval-report-YYYY-MM-DD.md`，每个用例的完整行程与 Trace 另存为
`results/eval-<ID>-<城市>.md`。

## 已知边界

- 天气只覆盖未来 16 天，出发日期更远时天气校验会自动跳过（不会瞎猜）。
- 高德 POI 的 `open_time` / 票价并不总是返回，缺失时营业时间与票价校验按「未知即不判错」处理。
- `plan_route` 失败时使用直线距离 × 1.3 的估算值，行程会照常生成但精度下降（响应里 `estimated=true`）。
- strict 工具模式依赖 DeepSeek beta 端点；若账号不支持，`llm.py` 会自动降级为非 strict 并重试。
- 行程 JSON 输出上限由 `MAX_OUTPUT_TOKENS`（默认 4096）控制；若仍被截断，Agent 会检测
  `finish_reason=length` 并要求模型精简 `note` 后重发，而不是把截断碎片当成合法行程。
- 「平均迭代次数」统计的是 LLM 往返轮数，其中包含纯工具调用轮（一轮可并行多个工具），
  因此 3 天以上行程天然偏高；真实的重排次数见报告中的「触发重排」列与 Trace。
  代码侧预取已经把「查天气 + 搜景点」这两轮从模型手里拿走，剩下的纯工具轮基本只有
  `plan_route` 批量查路（无法预取：要等模型挑完景点才知道查哪几段）。
- 端到端耗时 ≈ 30s 里约 90% 是 DeepSeek 生成整份行程 JSON 的时间（多天行程单次输出 2-3k token），
  工具请求只占约 2-4s；因此耗时主要由模型输出速度决定，不是工程瓶颈。
- 同一组校验问题连续 3 轮完全没改善时，Agent 会提前止损（避免空转到 10 轮上限、白耗
  token 与时间），此时终态仍是 `max_iterations`，问题清单照常保留给用户。
- 校验能保证「数据真实 + 时间预算可行 + 偏好合规」，但景点的文化解读、口味好坏等主观质量不做保证。
