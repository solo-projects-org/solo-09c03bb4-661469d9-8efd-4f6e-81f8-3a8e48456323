# 配送数仓列依赖分析 / 联运交接单对号服务

服务包含两块互不影响的接口：

1. `POST /analyze`：运单源表即将停供旧邮编列，按模型分析待下线源列的列级依赖；
2. `POST /handovers` + `GET /mapping`：联运结算时同一包裹在各家账单上的运单号不同，
   凭转运交接单把号码对上，供结算作业关联账单。

两者共用同一个 FastAPI 进程与 SQLite 数据卷。

## 一、列依赖分析（POST /analyze）

本服务接收源表定义、各模型的 DuckDB SELECT 与待下线源列，
**只用 SQLGlot 解析、不执行任何 SQL**，沿别名、普通星号与非递归 CTE 跨模型追踪列引用，
按模型输出：

- 依赖该源列的**输出列**，每项附完整引用链；
- 依赖该源列的 **WHERE / JOIN ON（含 USING）/ QUALIFY 条件**，每项附引用链。
  条件依赖**不计入**输出列。

## 追踪规则

- 别名：`receiver_zip_old AS zip` 会把依赖从 `zip` 继续追到源列。
- 普通星号：`SELECT *`、`SELECT t.*` 按关系实际输出列展开；支持
  `* EXCLUDE (...)`（DuckDB 里 `EXCEPT` 同义）与 `* REPLACE (...)`。
- 非递归 CTE：CTE 可引用同模型内更早的 CTE，作用域链以 `模型.CTE.列` 呈现。
- 跨模型：模型可以引用其他模型的输出列，分析按依赖拓扑顺序进行，链上保留模型跳。
- 名称匹配大小写不敏感，源表可带 catalog/schema 前缀（按表名匹配）。
- 引用链自当前模型的输出列/条件列起，到 `源表.源列` 止。

## 明确不支持（提交即 400 并指出所在模型）

- 聚合函数、`GROUP BY`、`HAVING`；
- 集合运算 `UNION / INTERSECT / EXCEPT`；
- CTE 之外的子查询（FROM 子查询、标量子查询、`EXISTS` 等）；
- 递归 CTE（`WITH RECURSIVE`）、`LATERAL`、`UNNEST`/表函数、`NATURAL JOIN`。

## 找不到或有歧义的引用（400，指出模型）

- 列在当前查询所有关系中都不存在；
- 未限定的列名可由多个关系提供（歧义，要求加表别名）；
- 表别名不存在、表/CTE 未定义、输出列重名等。

## 二、联运交接单对号（POST /handovers、GET /mapping）

交接单代表一次转运交接，记两端各自的承运商和运单号：

```json
{
  "settlement_month": "2026-10",
  "sheets": [
    {"handover_no": "HJ-0001", "carrier_a": "承运商甲", "waybill_no_a": "YD-1001",
     "carrier_b": "承运商乙", "waybill_no_b": "YT-7001"},
    {"handover_no": "HJ-0002", "carrier_a": "承运商乙", "waybill_no_a": "YT-7001",
     "carrier_b": "承运商丙", "waybill_no_b": "SF-9001"}
  ],
  "revoke": []
}
```

- **批量录入 / 更正 / 撤销**：`sheets` 里同一 `handover_no` 再次提交即为更正；
  `revoke` 列出要撤销的交接单号。两者可混在一批，一次只处理一个结算月。
  撤销不存在的单号是空操作，不算错误。
- **同票判定**：沿**有效交接单**能连通的两端号码归为同一票（可跨多张单、首尾成环）。
- **唯一性约束**：同一票在一家承运商只能有一个运单号。
  **按整批变更后的结果**检查；有冲突则整批不生效（400），
  `detail` 逐项给出 `carrier`、`waybill_nos`、`handover_nos`、`message`。
- **只重建受改动影响的对照**：本批未触及的票保持原 `ticket_id` 不变，
  响应 `rebuilt_tickets` 只列出本批重建的票。
- **撤单拆组**：撤销切断最后一条交接路径时，相关票按剩余有效交接单拆开；
  没有任何有效交接单的号码从对照中移除，查询返回空。
- **持久化**：交接单与对照（`handover_sheets`、`waybill_mapping`）在同一 SQLite
  事务中一起提交，库文件放在 Docker 数据卷上。批次以 `BEGIN IMMEDIATE` 串行化，
  并发报送按**实际提交顺序**判定：先提交的生效，后到的按先提交后的结果检查。
- **查询只读对照**：`GET /mapping` 只查 `waybill_mapping`，不读交接单、不触发重建。

查询（凭结算月、承运商和运单号）：

```bash
curl 'http://localhost:8000/mapping?settlement_month=2026-10&carrier=承运商丙&waybill_no=SF-9001'
```

```json
{
  "settlement_month": "2026-10",
  "carrier": "承运商丙",
  "waybill_no": "SF-9001",
  "ticket_id": "…",
  "waybill_nos": [
    {"carrier": "承运商丙", "waybill_no": "SF-9001"},
    {"carrier": "承运商乙", "waybill_no": "YT-7001"},
    {"carrier": "承运商甲", "waybill_no": "YD-1001"}
  ]
}
```

撤单后同票只剩甲乙两家，丙的号码不再列入对照：查询 `SF-9001` 返回
`ticket_id: null`、`waybill_nos: []`。完整样例见
`examples/request_handovers.json`（录入）与 `examples/request_handovers_revoke.json`（撤单）。

## 启动（Docker Compose）

```bash
docker compose up --build
# 服务: http://localhost:8000
# Swagger: http://localhost:8000/docs
# OpenAPI: http://localhost:8000/openapi.json
```

SQLite 文件位于数据卷 `handovers-data` 的 `/srv/data/handovers.db`
（容器内由 `HANDOVERS_DB` 指定），`docker compose down -v` 会连数据卷一起删除。

Swagger 的 `POST /analyze` 请求体自带"邮编改名后仍用于筛选"的样例，
`POST /handovers` 自带"录入两张单"和"撤销 HJ-0002"两个样例，可直接 Try it out：
先执行录入样例，用 `GET /mapping` 查 `承运商丙 / SF-9001` 看到三家；
再执行撤销样例，用同一查询确认丙的号码已不在对照中。

## 请求示例

`examples/request_zip_rename.json` 是完整样例（也可用 Swagger 内置样例）：

```bash
curl -X POST http://localhost:8000/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/request_zip_rename.json
```

请求结构：

```json
{
  "sources": [
    {"name": "waybills", "columns": ["waybill_id", "receiver_zip_old", "city_id"]}
  ],
  "models": [
    {"name": "stg_waybill", "sql": "WITH cleaned AS (SELECT waybill_id, receiver_zip_old AS zip FROM waybills WHERE receiver_zip_old IS NOT NULL) SELECT * FROM cleaned"}
  ],
  "targets": [
    {"table": "waybills", "column": "receiver_zip_old"}
  ]
}
```

响应结构（按模型分组）：

```json
{
  "targets": [{"table": "waybills", "column": "receiver_zip_old"}],
  "models": {
    "stg_waybill": {
      "dependent_output_columns": [
        {
          "output_column": "zip",
          "reference_chains": [
            ["stg_waybill.zip", "stg_waybill.cleaned.zip", "waybills.receiver_zip_old"]
          ]
        }
      ],
      "filter_conditions": [
        {
          "type": "WHERE",
          "scope": "stg_waybill.cleaned",
          "expression": "receiver_zip_old IS NOT NULL",
          "column": "receiver_zip_old",
          "reference_chain": ["stg_waybill.cleaned.zip", "waybills.receiver_zip_old"]
        }
      ]
    }
  }
}
```

错误响应统一为 `400`，`detail` 是数组，每项含 `model`、`location`、`message`，
一次返回收集到的全部问题。

## 本地开发（不用 Docker）

```bash
python -m venv --without-pip .venv   # 若系统带 ensurepip 可直接 python -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/uvicorn app.main:app --reload
.venv/bin/pytest                       # 54 个测试
```

## 目录

```
app/
  main.py         FastAPI 应用、/analyze 路由与请求/响应模型
  lineage.py      SQLGlot 解析与列级血缘分析（不执行 SQL）
  handovers.py    交接单 / 同票对照的 SQLite 存储与批量重建
  handover_api.py /handovers 与 /mapping 路由、Swagger 样例与入参校验
  tests/          分析器、交接单对号与 API 测试
examples/         邮编改名与交接单录入/撤单的完整请求样例
Dockerfile, docker-compose.yml, requirements.txt
```
