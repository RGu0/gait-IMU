# 变更记录

本文件记录**公开契约**的改动：跨模块的数据结构（`gait.contracts` §3）、CLI 的
输入与产出（`report.json` / `report.md` 的字段与列）、以及会改变调用方判断的
行为（新的抛出条件、判据口径）。内部实现的移动不在此记录。

§3 数据结构的改动还要同步 `gait.contracts.CONTRACT_VERSION` —— 那个数会进
`SessionMeta`，是分辨历史会话用哪版结构的唯一依据；本文件不替代它。

## 未发布

### 变更

* **`report.json` 里两个字段的读数口径变了。** `cloud/chain.py` 的事件分割改用
  `analysis/events.py::detect_stance_intervals`（支撑相**区间**），此前走的是
  `refine_stance_edges`（零速区间的**边缘细化**）。

  真机 T-230-03 上的影响（12 趟 × 2 足）：

  | 字段 | 改前 | 改后 |
  | --- | --- | --- |
  | `stance_ratio`（支撑相占比） | **1 ~ 16%** | **39 ~ 54%** |
  | `double_support.fraction` | −0.945 ~ −0.590 | −6.248 ~ +0.062 |

  支撑相占比此前是废数：生理值 60~75%，而零速时刻的跨度只占周期 0.7%~2.1%。

  `double_support.fraction` 的**分布**变了而不是简单变好：旧路径在所有格都给
  −0.9 上下，那是零宽区间求重叠的算术产物，**稳定地错**；改后分段判对的格给
  +0.027~+0.062，判错的格给 −6.2（分段失效见 RAY-354）。**读数不再是伪影，
  开始反映真实缺陷。**

  `stride_length` 与 `gait_speed` 也跟着动（实测 1.115~9.297 m / 0.357~3.490 m/s，
  受控真值 1.2 m）—— 它们同时受惯导影响，本次不设门，只记录。

  **合成数据上两条路几乎不分**（+0.260 vs +0.263 / 60% vs 60%），因为那里的脚是
  真的停住的。所以这次改动在合成回归上看不出任何差别。


* **`core/eskf.py::run_ins` 去掉一个抛出条件，加上另一个。**

  此前：`series.segments` 里只要有一段短于 `zupt_window_samples`，`detect_stance` 就会
  抛 `ZuptError`，整条链失败。而空洞切分（RAY-210）产出这样的碎段是**正常行为** ——
  真机 T-230-03 的 24 格切出 56 段，其中 3 段短于 15 采样（最短 8）。

  现在：碎段被**整段跳过**，与 `detect_stance` 错误信息里写明的契约一致。跳过的段
  仍然被覆盖，其样本 `zupt` / `degraded` 为 False、`score` 为 0、位置在段内不前进。

  **新的抛出条件**：全部段都短于检测窗口时抛 `EskfError` —— 那时没有任何可信的初始
  对准，返回一条轨迹只能是编的。

  **没有碎段时结果逐位不变**（实测：三档时长的 `q/v/p/bg/ba/score/zupt/stances`
  指纹与改动前完全相同）。

* **`core/eskf.py` 新增 `run_ins_with_stances` 与 `SegmentDetection`。** 现有
  `run_ins` / `run_ins_with_history` 签名不变。新入口交出滤波器**用过的那份**逐段
  零速检测，两个用途：`SegmentDetection.skipped` 区分「整段跳过」与「分析过但没检出
  支撑相」（两者在 `NavResult` 里读数相同、含义相反）；`detection.period` 供按周期
  栅格取支撑相区间用（`NavResult` 拍扁时丢掉了它）。


* **`gait.device.capture` 的回放路径新增一个抛出条件。** `replay_raw_frames` /
  `replay_session_foot` / `replay_recording` 此前只在 `DeviceStats.dropped_samples`
  非零时抛 `CaptureError`；现在 `DeviceStats.dropped_before_ready` 非零同样抛，
  且**先于**前者报出。

  两者说的不是一件事：`dropped_samples` 是队列压力（消费者跟不上，处方是调大
  `queue_size` 或按原速回放），`dropped_before_ready` 是连接就绪之前到达的帧
  （流还没开始，与队列无关）。在回放语境下后者意味着**这份回放缺了开头一段**，
  与原录制不是同一串数据 —— 此前它会安静通过。

  wt901 v0.3.0 新增该计数且**没有**并进 `dropped_samples`，所以只看旧计数的
  调用方不会报错，只会少数据（上游 RAY-311）。

* **`linktest` 的判定新增一条 problem。** `dropped_before_ready` 非零时报
  「连接就绪前丢弃 N 帧」，与既有的「主机侧消费队列溢出」**分开两条**，因为
  处方不同：对前者调大队列毫无用处。

### 新增

* **`linktest` 报告表格新增「就绪前丢弃」列**（在「队列溢出」与「电量前→后」
  之间），`report.json` 的 `device_stats` 相应带上 `dropped_before_ready`。

  离线解码路径（`analyze_recordings`）与升级前的历史报告没有这个键，渲染与判定
  都按缺失落到「—」/ 0，不会因此变成不达标。

### 依赖

* **wt901 由 `rev = "cb88cee5…"` 改为 `rev = "80b03c9…"`（= `v0.3.0`）。**
  `pyproject.toml` 里那句「无 tag、无 release，因此按 commit 钉住」自上游 v0.2.0
  起已不成立，一并改掉——留着它会让按 40 位 SHA 钉看起来像唯一选择（RAY-334）。

  `tests/test_wt901_dependency.py` 的版本断言随之由 `"0.1.0"` 改为 `"0.3.0"`，
  并提取为 `PINNED_VERSION`。

  中途改用过 `tag = "v0.3.0"`（RAY-334），又改了回来（RAY-348）：上游把 `v0.3.0`
  从轻量 tag **重打为 annotated tag**，代码一字未变（剥离后的 commit 仍是
  `80b03c9`），但 tag 自身的 SHA 变了，而 uv 以它作解析键 —— `uv.lock` 因此被判
  过期，`uv sync --locked` 在所有分支上失败。采纳 tag 时写下的那句风险
  「tag 是可以被上游移动的引用，而 commit 不会」原样兑现，结论按它修正。
