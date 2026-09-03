# 判据覆盖图

**这张图回答一个问题：每条在验量化判据，有没有东西在真的跑它。**

与同目录的 `REGISTRY.md` 方向相反 —— 那张表答的是「脚本 → 它守哪条判据」，只看得见
**已经有脚本**的那些。本图从判据出发，因此看得见**没有脚本**的那些，而那正是要找的。

## 守法四类

| 类 | 含义 | 跑的频率 |
| --- | --- | --- |
| **合成** | `tests/` 里的回归（含 `dev` 跑的静态红线） | 每次 `./dev` |
| **真机** | `tools/acceptance/` 的脚本，读 RAY-230 现场采集 | 靠约定（`.ai-project/acceptance-governance.md`） |
| **一次性** | 交付时验过一次、结果在证据库，**此后没有任何东西再跑它** | 一次 |
| **无** | 没有东西在跑它 | —— |

**「一次性」是审计过程中加的第四类，原定只有三类。** RAY-227 判据 1 逼出了它：
p95 ≤ 120 s 交付时实测 4.2 s（余量 28 倍），证据俱全，但此后没有任何东西再跑。
它既不是「无人守」（验过），也不是「有守卫」（不会再跑）。

**这一类正是 RAY-343 那次的前身**：RAY-328 的三个脚本当初也是验过的，然后悄悄全挂，
过了两个 Issue 才被发现。把「一次性」记成「有守卫」会让这张图重犯同一个错。

## 审计边界

**查了**：判据约束共享算法路径（`core/` `sync/` `analysis/` `validate/`）**且已交付**
的 27 个 Issue —— RAY-201 202 203 204 205 206 209 210 211 212 215 216 217 218 227
242 262 270 290 296 319 325 328 339 343 346 347。

选这个边界的理由不是省事：**漂移实际咬过三次（RAY-343、346、347），三次全在这条路径上。**

**没查**（本项目共 94 个 Issue，其余 67 个），按类与理由：

| 类 | Issue | 为什么不查 |
| --- | --- | --- |
| UI / 交互 | 219 220 221 222 223 224 246 249 255 265 266 287 323 345 | 判据是交互行为，不是量化的算法性质 |
| 设备 / IO / 打包 | 192 195 196 197 198 199 225 226 229 233 248 250 258 275 302 303 329 334 348 | 不碰共享算法路径 |
| 文档 / 需求更正 / 治理 | 193 194 228 232 234 252 253 254 263 264 326 327 331 332 350 | 判据是文档产出或需求措辞，无代码守卫可言 |
| 一次性实验结论 | 200 230 231 247 272 273 | 判据是那次实验的读数，不描述当前代码该有的行为 |
| 未交付的 Backlog | 207 208 213 259 260 261 274 288 333 337 351 | 判据尚未到期 |

**这一节是判据 2 要求的，不是免责声明。** 一张不说自己边界的覆盖图会被当成「全都查过
了」，而那种错觉比没有图更危险 —— 它正是本 Issue 要消灭的东西。**上表之外的 Issue，
本图没有结论，不是「没问题」。**

## 方法：逐条读判据，不数引用

**不用「Issue key 在 `tests/` 里出现几次」当信号。** 实测它在两个方向都坏：

| Issue | 守它的东西 | 提到 key 的次数 |
| --- | --- | --- |
| RAY-216 | `tests/test_events.py`（在守） | **0** |
| RAY-217 | `tests/test_variability.py`（在守） | **0** |
| RAY-255 | `packages/design-system/test/`（在守） | **0** |

而且前端测试根本不在 `tests/` 下（`packages/*/test/`、`apps/*/src/*.test.jsx`），
一次朴素的 grep 连扫都扫不到。

## 覆盖表

### 时基与数据完整性

| Issue | 判据 | 守它的 | 类 |
| --- | --- | --- | --- |
| RAY-209 | 回放可复现同一时基（逐 bit 相同） | `test_timebase.py::test_replaying_the_same_arrivals_reproduces_the_same_timebase` | 合成 |
| RAY-209 | 实测采样率相邻窗口差 < 0.1% | `test_the_rate_estimate_is_stable_across_windows`（`fs_window_spread < 1e-3`）+ `test_the_stability_flag_detects_what_the_fit_cannot_fix`（+10/+40 ms 参数化） | 合成 |
| RAY-210 | 注入丢包下切分正确、空洞 > 3 样本即切、无静默污染 | `test_integrity.py` 四条（含逐段重拟时基能恢复采样率） | 合成 |
| RAY-210 | 抖动 / 重传 / 慢晶振**不得**误判为空洞 | 同上三条反向测试 | 合成 |
| RAY-211 | 人为注入 offset 后自检可检出 | `test_selfcheck.py`：越预算标记 / 未越预算不标记，正反两条 | 合成 |
| RAY-211 | 左右步周期差 < 10% | `test_the_stride_period_difference_does_not_move_with_the_offset` 等；阈值在 `config.py::selfcheck_stride_period_tolerance` | 合成 |
| RAY-211 | 「双支撑期应为正」 | **判据文本已过期**：实现与文档改判为「ZUPT 边界下负值是常态」，有测试钉住（`test_fast_normal_walking_reads_a_negative_double_support_and_is_still_clean`）。真机实测 −1.003~−0.966 | 合成（文本待重述 → RAY-288） |
| RAY-290 | 配对法的**前提**要写成断言或文档 | `sync/selfcheck.py::double_support` docstring 明写两个前提，且模块文档 §6 说明它**在运行时被守住**、正常数据实测恒为 0 | 合成 |
| RAY-290 | 补参数化到 Δ = 30 ms（PRD 容差上界）的测试 | `test_v3prime.py::test_paired_double_support_bias_equals_twice_delta`（含 0.030）；`test_selfcheck.py` 的 offset 参数化 | 合成 |
| RAY-212 | 20 次对碰的时刻差标准差可量化输出 | `test_anchor.py::test_acceptance_twenty_taps_std_is_quantified`；`AnchorReport.offset_std` 进 snapshot | 合成 |

### 惯导内核

| Issue | 判据 | 守它的 | 类 |
| --- | --- | --- | --- |
| RAY-201 | 与解析解误差在数值精度内（拆成三档，因三种情形差十个数量级） | `test_ins.py` / `test_quaternion.py` | 合成 |
| RAY-201 | **真正的判据是收敛阶**：fs 100→1600 Hz 位置误差逐档 1/4 | `test_ins.py::test_convergence_is_second_order` —— 一个一阶实现能通过任何绝对阈值，却过不了这一条 | 合成 |
| RAY-202 | 初始姿态误差 < 0.5°（无噪四姿态 / 白噪声 / 端到端） | `test_alignment.py` 多条 | 合成 |
| RAY-202 | 派生：0.5° ⟹ 标定后水平残余零偏 < 8.7 mg | `test_the_budget_implies_a_residual_bias_below_about_nine_milli_g`、`test_the_raw_device_spec_blows_the_budget_by_more_than_three_times` | 合成 |
| RAY-202 | 整体设计 §5.3 的符号约定陷阱（照抄差 180° 且不报错） | `test_the_design_document_formula_would_be_180_degrees_off` —— 把文档陷阱本身钉住 | 合成 |
| RAY-204 | 静置 10 min 位置漂移 < 5 cm | `test_eskf.py::test_ten_minutes_still_drifts_less_than_five_centimetres`（实测 0.012 cm；套件里最贵的一条） | 合成 |
| RAY-204 | 合成端到端回归通过 | 由 RAY-206 的 `test_v1a_regression.py` 承担 | 合成 |
| RAY-205 | 开启约束后航向漂移显著下降 | `test_dualfoot.py`：漂移下降 + **只有差分航向可观测** + 发散量被约束移除 | 合成 |
| RAY-205 | 两个函数只输出量与可判定性，**任何情况下不给左右结论** | `test_it_reports_that_it_is_not_identifiable`、步数不足 / 原地踏步都**拒绝出数**而不是给结论 | 合成 |
| RAY-206 | V1-a：标称四档逐 stride 步长误差 < 0.5%（× 三档噪声） | `TestV1aOnNominalGaits` 四条（含「转身 stride 必须剔除否则超预算」） | 合成 |
| RAY-206 | 低速档**不设数值判据**，保留双向哨兵（6% 上界） | `TestLowSpeedIsAKnownFailure` —— **变好也会红**，提示把它移进标称集合 | 合成（哨兵） |
| RAY-206 | 生成器自洽性（反推回真值） | `test_synthetic.py::test_the_residual_converges_to_zero` | 合成 |

### 步态事件与指标

| Issue | 判据 | 守它的 | 类 |
| --- | --- | --- | --- |
| RAY-203 | 常速检出率与误检率达标（四档步态，完整支撑相全检出、**误检 0**） | `test_zupt.py` 的 `GAIT_CASES` 参数化组 | 合成 |
| RAY-203 | 预设切换可热换 | `test_zupt.py`：反复交替调用一致 + 两预设结果确实不同 | 合成 |
| RAY-215 | 合成 4 米往返下直行 / 转身分离准确 | `test_segments.py` 四条（含「判据是航向不是步长」） | 合成 |
| RAY-215 | 剔除策略可配且可复算 | 同上四条（同参数复现同结果、报告带回它用的参数、每个被剔的步说明理由） | 合成 |
| RAY-216 | 合成数据下事件时刻误差 < 20 ms | `test_refined_events_land_within_the_acceptance_tolerance` + **阳性对照**（原始 ZUPT 边界必须过不了）+ 与检测窗口无关 | 合成 |
| RAY-216 | **参数量级合理性检查通过** | 合成侧 `test_double_support_lands_in_the_physiological_band` 等；**真机侧由 RAY-351 的 `chain_metrics.py` 补上** —— 此前无人守，产品链路实测支撑相占比 1~16%（生理 60~75%）、DS −0.925~−0.624 | 合成 + 真机 |
| RAY-217 | 60 s 下 CV 正常输出且 `grade` 反映样本量少 | `test_variability.py` 四条（含「低于阈值时翻倍的 CV 分辨不出来」） | 合成 |
| RAY-218 | 任一指标的质量证据入库可查 | `test_quality.py` 三条 | 合成 |
| RAY-218 | `low` 只影响呈现不拦截；门控矩阵默认全关 | `test_quality.py` 四条，正反都有 | 合成 |
| RAY-218 | **R-3：渲染进程不得复算质量分级** | `tools/check_quality_single_source.py`，`dev` 第 54 行跑 | 合成（静态红线） |
| RAY-227 | 本地 / 云端差异可解释（chain 标注区分） | `test_cloud_chain.py` 四条（前向阶段逐 bit 相同、页脚记录哪条链、报告不许混链） | 合成 |
| RAY-227 | **上传完成后 p95 ≤ 120 s** | 交付时验过一次（4.2 s，余量 28 倍，口径：算法链不含下载解包）。**此后没有任何东西再跑它** | **一次性** |
| RAY-296 | 剔除静止前导后 Δ=30 ms 一档能通过 | `test_paired_double_support_bias_equals_twice_delta`（含 0.030）+ `test_the_still_lead_is_dropped_before_anything_else` | 合成 |
| RAY-319 | `--live` 用到的每个外部符号存在且签名兼容 | `test_v3prime.py::test_live_path_api_contract`（不需硬件） | 合成 |

### 真机验收套件（`tools/acceptance/`，逐条见 `REGISTRY.md`）

| Issue | 判据 | 守它的 | 类 |
| --- | --- | --- | --- |
| RAY-325 | 判据 1、4（周期数、慢/快不反号） | `period_cycles.py` | 真机 |
| RAY-325 | 判据 2（DS、同足相邻、支撑占比） | `stance_intervals.py` | 真机 |
| RAY-325 | 判据 3（下游距离误差） | **无** —— 50 m 直线 ×3 与矩形闭环**未采集** | 无（数据未采，已记录） |
| RAY-328 | 判据 1、2 / 3 后半 / 4 | `cross_foot_qc.py` / `antiphase.py` / `alternation_decode.py` | 真机 |
| RAY-339 | 判据 1、2 / 3 / 4 | `event_interval.py` / `common_window.py` / `truncation.py` | 真机 |
| RAY-343 | 判据 1、2 | `drop_prior.py` | 真机 |
| RAY-346 | 判据 3 | `selfcheck_contrast.py` | 真机 |
| RAY-347 | 判据 1、2、3 | `confidence_ceiling.py` | 真机 |

### 文档与清理类

| Issue | 判据 | 守它的 | 类 |
| --- | --- | --- | --- |
| RAY-242 | wt901 rev 指向含上游修复的 commit | `tests/test_wt901_dependency.py` | 合成 |
| RAY-242 | 回读断言用 `AlgorithmMode.SIX_AXIS` 而非裸 `1` | `test_device_ble.py` | 合成 |
| RAY-242 | **`src/` 下 `0x24` 零命中** | **无守卫**（今天仍满足） | **无 —— 遗漏** |
| RAY-270 | **`src/`、`tests/` 下 `identify_feet` / `lateral_offset` 零命中** | **无守卫**（今天仍满足） | **无 —— 遗漏** |
| RAY-270 | 测试数不减（939 → 939） | 一次性，且已被后续大量新增测试淹没，不可复核 | 一次性 |
| RAY-262 | 两处注释改成与实测一致 | **无守卫** —— 注释准确性无法自动化守 | 无（性质如此） |

## 「无人守」清单与处置（判据 3）

| # | 条目 | 类别 | 处置 |
| --- | --- | --- | --- |
| 1 | RAY-216 判据 2「参数量级合理性」的**真机侧** | **确实遗漏** | 已立 **RAY-351**（产品链路切 `detect_stance_intervals`） |
| 2 | RAY-242 判据 2：`src/` 下 `0x24` 零命中 | **确实遗漏** | 需后继 Issue，见下 |
| 3 | RAY-270 判据 3：`identify_feet` / `lateral_offset` 零命中 | **确实遗漏** | 同上，可与 2 合并 |
| 4 | RAY-227 判据 1：p95 ≤ 120 s | 一次性 | 需后继 Issue 决定要不要变成会跑的门 |
| 5 | RAY-325 判据 3：下游距离误差 | 数据未采 | 已在 RAY-325 正文记录，等采集 |
| 6 | RAY-205：`inversion_signature` 在合成上恒为零 | 数据未采 | 已在 RAY-205 正文记录，硬阻塞 RAY-230 |
| 7 | RAY-204：Q 的 Allan 方差取值 | 待标定 | 已在 RAY-204 正文记录，归 RAY-207 |
| 8 | RAY-202：真机是否达到 8.7 mg 残余零偏 | 待标定 | 归 RAY-207 |
| 9 | RAY-203 自述三项（真机阈值、触地冲击、真实病理波形） | 已记录的缺口 | 其中真机检出已由 RAY-325 → `period_cycles.py` 补上 |
| 10 | RAY-206 生成器四条模型限制 | 模型限制 | 模块文档明记；其中「不能验 IC/TO 细化」正是第 1 条的成因 |
| 11 | RAY-242 顺带记的 `BANDWIDTH_42HZ = 0x03` | 转出 | 说是"应在 WT901 项目另开 Issue"，**本项目无法核实是否已立** |
| 12 | RAY-262：注释漂移 | 性质如此 | 无法自动化 |

**只有 1、2、3 是真正的遗漏**（4 介于两者之间）。5~12 都已在各自 Issue 的正文里
如实记录过，本图的作用是把它们汇到一处，而不是重新发现。

## 三个跨 Issue 的发现

### 一、grep 形状的判据，只有被写成脚本的那条活着

同一形状出现三次：判据写成「全仓检索 X 零命中」，交付时用 grep 核过一次。

| 判据 | 今天仍满足 | 守卫 |
| --- | --- | --- |
| RAY-242：`src/` 下 `0x24` | 是 | **无** |
| RAY-270：`identify_feet` / `lateral_offset` | 是 | **无** |
| RAY-218 R-3：渲染进程无质量阈值常量 | 是 | **有** —— `check_quality_single_source.py` |

**唯一被守住的那条，恰恰是唯一被写成脚本的。** 另两条只留在 Issue 正文里，靠人记得。
这三条并排就是本 Issue 的论点：**判据的形状不决定它会不会被守住；有没有人把它写成
会跑的东西才决定。**

补法便宜（一条参数化的静态测试，禁止符号列成表，与 `check_layering.py` 同性质），
但按本 Issue「不做的事」第一条，**补它属后继 Issue**。

### 二、注释与实现漂移，至少咬过三次

RAY-262（`config.py` 两处注释与实测矛盾）、RAY-270（文档引用已删除的函数）、
RAY-347（注释写「不再左右结果」而代码仍按它减半）。

前两次是纯文档，第三次**是真缺陷**：一个字段的含义变了，注释跟上了，消费者没跟上。
**注释漂移在这个仓库里不是小事，因为这里的注释承载理由。** 但它无法自动化守 ——
本图只能把频次记下来。

### 三、合成绿 ≠ 真机对，而且合成再多也补不上

RAY-216 判据 2 是最干净的例子：同一条「参数量级合理性」，合成侧有守卫且长绿，
真机侧无人守且**不成立**。成因写在 RAY-206 的生成器限制里 —— 合成模型的支撑相是
**精确静止**的，而 RAY-325 的全部根因正是「真机上脚从来不满足那个判据」。

**所以「加合成测试」对这一类判据是无效的。** 哪些判据必须真机、哪些合成够了，
本身需要一条判据 —— 本图提得出这个问题，答不了。

## 这张图挡不住什么

**挡不住一条谁都没登记的判据。** 图是人读出来的，漏读与漏搬脚本是同一个动作的两种
失败。它能保证的只有：上面**边界一节列出的 27 个 Issue**，逐条读过。

**也挡不住判据本身的过期。** RAY-211 的「双支撑期应为正」有守卫、长绿，而判据文本
本身已被实现推翻 —— 那是 `update-linear-requirement` 的事，不是守卫的事。

真正的门仍然是 `.ai-project/acceptance-governance.md` 里那条**约定**：动了共享路径
就把真机套件跑一遍。而本 Issue 的存在本身，就是那条约定曾经失效过的证据。
