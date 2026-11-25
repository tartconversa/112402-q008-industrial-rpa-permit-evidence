# 技改备案材料采集与跨门户申报回执

面向园区服务中心的节能技改项目备案辅助系统：采集政策页面与附件版本、
受理企业表单与材料、按角色处置待确认项、通过浏览器自动化协助填报外部门户、
处理回执并驱动补正闭环。项目与机构均为虚构代号，仅依赖 Python 3 标准库。

## 业务规则如何落地

| 业务要求 | 实现位置 | 处理方式 |
| --- | --- | --- |
| 政策页面正文与附件版本采集 | `filing/crawler.py` | 页面与附件按 SHA-256 内容哈希识别版本，内容不变不产生新版本 |
| 信息冲突/适用性不清不自动选最宽松条件 | `filing/rules.py` | 同日多修订、多条款同时命中、生效边界不明等一律生成**待确认项**并阻断提交 |
| 三类角色各司其职 | `filing/models.Role`、`filing/rules.py` | 政策专员确认条款、企业经办人确认事实、材料审核员确认覆盖；越权确认抛 `PermissionError` |
| 指南更新后的旧申请 | `filing/rules.py::_evaluate_accepted` | 受理时锁定政策修订与材料版本；新版只生成政策变更判断项，由工作人员决定，不自动替换 |
| 按材料版本生成提交记录 | `filing/portal.py` | 幂等键 = 申请 + 材料版本 + 政策版本，同键只允许一张门户申请 |
| 失败/超时先核对门户再决定 | `filing/portal.py::_reconcile_after_uncertain` | 先 `query_status` 核对门户实际状态：已建单则复用，确认无单才允许重试 |
| 回执问题映射到条件与材料 | `filing/receipts.py` | 每个问题生成带 `condition_ref`（条款版本#材料类型）的可办理事项 |
| 补件后只重提受影响部分 | `filing/receipts.py::resubmit_affected` | 未办结事项阻断重提；重提记录只含受影响附件，完整历史保留 |
| 查看当前环节/回执/待补/历史 | `filing/dashboard.py` | 单申请完整视图 + 按环节统计 |

## 目录结构

```
src/filing/
  models.py     领域模型与枚举（申请、材料版本、提交、回执、待确认项、事项）
  storage.py    SQLite 持久化（含轻量迁移）
  crawler.py    政策页面/附件采集（HttpSource 或 LocalSource，均为标准库）
  rules.py      规则引擎：适用判断、材料覆盖、角色确认、受理锁定
  intake.py     企业表单与材料受理（按内容去重、按版本留痕）
  portal.py     门户驱动抽象、模拟门户、幂等提交与对账编排
  browser.py    Playwright 真实门户驱动骨架（选择器注入，缺包时明确报错）
  receipts.py   回执解析、可办理事项、补件复核与受影响部分重提
  dashboard.py  办理总览查询
  service.py    门面组装
  cli.py        命令行入口
scripts/
  demo.py            离线端到端演示（模拟门户，覆盖冲突/超时/部分受理/政策变更）
  import_samples.py  导入 samples/ 下三份 CSV 并回放办理
tests/                unittest 套件（中文 docstring，37 个用例）
```

## 快速开始

```bash
# 运行全部测试（标准库即可）
python3 -m unittest discover -s tests -v

# 离线端到端演示
python3 scripts/demo.py

# 导入样例 CSV（内存库验证；加 --db 可落盘）
python3 scripts/import_samples.py
```

CLI 示例（真实运行时把 `--local-root` 换成包含政策页面镜像的目录）：

```bash
python3 -m filing.cli --db filing.db --local-root ./site --today 2026-11-25 \
    sync-policy --url file:///policy.csv
python3 -m filing.cli --db filing.db register --app A-8 --company C-9 \
    --category energy-saving --location Zone-A --investment 900000
python3 -m filing.cli --db filing.db evaluate --app A-8
python3 -m filing.cli --db filing.db pending
python3 -m filing.cli --db filing.db submit --app A-8
python3 -m filing.cli --db filing.db receipt --app A-8 --portal-ref EXT-922 \
    --kind partial --text "改造范围说明退回" --accepted budget.pdf --rejected scope.pdf
python3 -m filing.cli --db filing.db view --app A-8
```

## 关键状态机

- 申请：`draft → pending_confirm → ready → submitted → accepted`，
  回执分支为 `correction` / `partial`，补正办结后回到提交流程。
- 提交：`planned → uploading → succeeded`；异常路径为 `failed` / `timeout`，
  经门户对账后收敛为 `succeeded`（复用门户已建单）、`duplicate` 或保留 `failed` 待重试。
- 待确认项：`open → resolved/dismissed`，确认动作全部写入 `confirmation_log`。

## 接入真实门户

`filing/browser.py` 提供 `PlaywrightDriver`：页面选择器通过参数注入，
凭据只从环境变量读取、不落库；未安装 playwright 时给出明确安装提示。
业务编排（幂等、对账、防重复）全部位于 `portal.py`，与具体驱动解耦，
因此从模拟驱动切换到真实驱动不需要改动规则与回执逻辑。

## 样例数据

`samples/` 下三份 CSV 为合成数据，UTF-8 编码、首行列名、空单元格表示来源无值：

- `policy.csv`：办事指南条款及修订版本
- `applications.csv`：企业项目事实、材料与创建时采用的规则
- `receipts.csv`：门户提交请求与回执记录
