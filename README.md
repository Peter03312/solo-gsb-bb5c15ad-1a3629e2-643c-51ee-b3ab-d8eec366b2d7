# 冷链货物追责服务

面向冷链货物追责的后端服务：录入货物与测温 / 交接事件、**冷链车次与每票货物的装卸时段、车载测温读数**，**按发生时间重建保管链**（货物自有测温与车载读数合并，同刻自有值优先），自动识别越温区间并在交接时刻拆分责任。技术栈 **Python 3.11 + FastAPI + SQLAlchemy 2 + PostgreSQL**。

## 一、一键启动（Docker Compose，含数据库初始化）

```bash
docker compose up --build
```

启动后：

| 组件 | 地址 |
| --- | --- |
| API 服务 | http://localhost:8000 |
| 交互式接口文档 Swagger UI | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |
| 健康检查 | http://localhost:8000/health |
| PostgreSQL | localhost:5432（用户/库/密码默认均为 `coldchain`） |

首次启动时，`db/init.sql` 由 PostgreSQL 官方镜像自动执行（建枚举类型、表、CHECK 约束、部分唯一索引）。应用启动时也会执行 `CREATE TABLE IF NOT EXISTS` 作为兜底。API 容器通过 `depends_on: service_healthy` 等待数据库就绪。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据库数据卷（下次重新初始化）
```

## 二、本地开发（不走 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 需要一个可连接的 PostgreSQL，并先执行 db/init.sql
export DATABASE_URL="postgresql+psycopg2://coldchain:coldchain@localhost:5432/coldchain"
uvicorn app.main:app --reload --port 8000
```

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://coldchain:coldchain@db:5432/coldchain` | SQLAlchemy 数据库连接串（容器内主机名为 `db`） |
| `POSTGRES_USER` | `coldchain` | 数据库用户（compose） |
| `POSTGRES_PASSWORD` | `coldchain` | 数据库密码（compose） |
| `POSTGRES_DB` | `coldchain` | 数据库名（compose） |
| `API_PORT` | `8000` | 宿主机映射的 API 端口 |
| `DB_PORT` | `5432` | 宿主机映射的数据库端口 |

也可在项目根目录放 `.env` 文件供 compose 读取。时间字段统一按 UTC 存储；请求时间建议带时区偏移（如 `+08:00`），无时区时间按 UTC 解释。

## 四、HTTP API

基址：`http://localhost:8000`。所有请求/响应均为 JSON。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/cargo` | 创建货物 |
| `GET` | `/cargo` | 货物列表 |
| `GET` | `/cargo/{cargo_id}` | 货物详情 |
| `POST` | `/events` | 写入货物**自有**测温 / 交接事件 |
| `GET` | `/cargo/{cargo_id}/events` | 按发生时间列出**自有事件**（不含车载读数） |
| `GET` | `/cargo/{cargo_id}/report` | **追责报告**：越温区间 + 责任片段（含读数来源） |
| `GET` | `/cargo/{cargo_id}/readings` | **合并测温时间线**：自有测温 + 各车次车载读数（标注来源/是否采用） |
| `POST` | `/trips` | 登记冷链车次 |
| `GET` | `/trips` / `GET /trips/{trip_id}` | 车次列表 / 详情 |
| `POST` | `/trips/{trip_id}/manifest` | 批量登记车次上各货物的装卸时段（左闭右开） |
| `GET` | `/trips/{trip_id}/manifest` | 车次装卸时段列表 |
| `POST` | `/trips/{trip_id}/readings` | 上报车载测温读数（允许晚到） |
| `GET` | `/trips/{trip_id}/readings` | **车次读数**：原始读数 + 读数时刻在车上的货物及采用/越温情况 |
| `GET` | `/cargo/{cargo_id}/responsibilities` | **责任确认**：全部已闭合区间的确认状态 |
| `GET` | `/cargo/{cargo_id}/responsibilities/{interval_id}` | 单个已闭合区间的责任确认视图 |
| `POST` | `/cargo/{cargo_id}/responsibilities/{interval_id}/stances` | 提交责任方表态（确认 / 异议） |
| `GET` | `/cargo/{cargo_id}/responsibilities/{interval_id}/stances` | 区间历次表态历史（含已改口 / 已失效） |
| `GET` | `/health` | 健康检查 |

### 1. 创建货物 `POST /cargo`

```json
{
  "cargo_id": "COLD-001",
  "started_at": "2026-09-30T08:00:00+08:00",
  "temp_upper_limit": "4.0",
  "initial_holder": "北京冷库"
}
```

字段：货物编号、起始时间、温度上限（℃）、起始保管方。编号重复返回 `409 cargo_exists`。

### 2. 写入事件 `POST /events`

两种事件由请求字段区分（互斥，不能同时出现）：

测温（含 `temperature`）：

```json
{ "event_id": "evt-1", "cargo_id": "COLD-001",
  "occurred_at": "2026-09-30T10:00:00+08:00", "temperature": "8.2" }
```

交接（含 `giver` 与 `receiver`）：

```json
{ "event_id": "evt-2", "cargo_id": "COLD-001",
  "occurred_at": "2026-09-30T12:00:00+08:00",
  "giver": "北京冷库", "receiver": "上海冷库" }
```

每个事件都带 **事件ID、货物编号、发生时间**。这里写入的只是货物**自有**事件：自有测温同刻优先于车载读数，且只有自有交接事件能改变保管方；车载读数见第 5 节。

### 3. 追责报告 `GET /cargo/{cargo_id}/report`

```json
{
  "cargo_id": "COLD-001",
  "temp_upper_limit": "4.000",
  "current_holder": "广州冷库",
  "excursions": [
    {
      "start": "2026-09-30T02:00:00Z",
      "end": "2026-09-30T08:00:00Z",
      "is_open": false,
      "interval_id": "evt-temp-1",
      "start_event_id": "evt-temp-1",
      "end_event_id": "evt-temp-2",
      "start_source": { "source": "own", "event_id": "evt-temp-1", "trip_id": null, "reading_id": null },
      "end_source":   { "source": "onboard", "event_id": null, "trip_id": "T-9", "reading_id": "rd-0800" },
      "segments": [
        { "start": "2026-09-30T02:00:00Z", "end": "2026-09-30T04:00:00Z", "responsible_party": "北京冷库" },
        { "start": "2026-09-30T04:00:00Z", "end": "2026-09-30T06:00:00Z", "responsible_party": "上海冷库" },
        { "start": "2026-09-30T06:00:00Z", "end": "2026-09-30T08:00:00Z", "responsible_party": "广州冷库" }
      ]
    }
  ]
}
```

未闭合区间：`end` 为 `null`、`is_open` 为 `true`，最后一个责任片段的 `end` 也是 `null`（未结束）。区间与片段均按时间升序返回。每个区间附带**稳定标识 `interval_id`** 与开启 / 闭合的**读数来源**：

* 货物自有测温：`source="own"`，`event_id` 为测温事件ID，`interval_id` 即该事件ID，`start_event_id` / `end_event_id` 有值；
* 车载读数：`source="onboard"`，`trip_id` + `reading_id` 定位原始读数，`interval_id` 为 `trip:<车次ID>:reading:<读数ID>`，相应 `*_event_id` 为 `null`。

### 4. 车次与装卸时段 `POST /trips`、`POST /trips/{trip_id}/manifest`

先登记车次，再批量登记每票货物在该车次上的装卸时段（**左闭右开** `[loaded_at, unloaded_at)`，起点必须严格早于终点）：

```json
POST /trips
{ "trip_id": "T-9", "vehicle_no": "沪D-66888" }

POST /trips/T-9/manifest
{
  "items": [
    { "cargo_id": "COLD-001", "loaded_at": "2026-09-30T09:00:00+08:00", "unloaded_at": "2026-09-30T18:00:00+08:00" },
    { "cargo_id": "COLD-002", "loaded_at": "2026-09-30T09:00:00+08:00", "unloaded_at": "2026-09-30T13:00:00+08:00" }
  ]
}
```

* 一批多条**整体成功或整体拒绝**：装载时段无效、货物或车次不存在、同货物时段与既有登记重叠（含跨车次——同货物不得同时属于两个车次；首尾相接允许）→ 422/404/409，不留部分记录；
* 幂等：同 `(车次, 货物, 装车时刻)` 且终点一致的条目重传不重复写入（全批均为重传时返回 `200 {"all_replayed": true, …}`），终点不同返回 `409 manifest_id_conflict`；批内完全相同的条目自动去重。

### 5. 车载读数 `POST /trips/{trip_id}/readings`

```json
{ "reading_id": "rd-1100", "read_at": "2026-09-30T11:00:00+08:00", "temperature": "3.0" }
```

* **读数允许晚到**（乱序写入）；同编号同内容重传返回 `200 {"replayed": true}`，异内容 `409 reading_id_conflict`；同车次同刻至多一笔读数（`409 reading_same_timestamp_conflict`）；
* 读数仅作用于 **`loaded_at ≤ 读数时刻 < unloaded_at`** 仍在车上的货物，各货物**按各自温度上限**判断越温；读数不改变任何货物的保管方（交接仍只由货物自有事件给出）；
* 读数时刻若货物有**同刻自有测温，自有值优先采用**，车载原始读数保留且可查（查询中标记 `adopted=false`）。

### 6. 查询车次读数 `GET /trips/{trip_id}/readings`

每条原始读数给出读数时刻仍在车上的货物及各自判定：

```json
{
  "trip_id": "T-9",
  "readings": [
    {
      "reading_id": "rd-1100", "read_at": "2026-09-30T03:00:00Z", "temperature": "3.000",
      "onboard_cargos": [
        { "cargo_id": "COLD-001", "temp_upper_limit": "4.000", "over_limit": false, "adopted": true },
        { "cargo_id": "COLD-002", "temp_upper_limit": "2.000", "over_limit": true,  "adopted": true }
      ],
      "affected_cargo_ids": ["COLD-002"]
    }
  ]
}
```

`affected_cargo_ids` 为读数实际作用（在车上、被采用）且按该货物上限构成越温的货物。货物侧的对应查询是 `GET /cargo/{cargo_id}/readings`——返回该货物的**合并测温时间线**（自有测温 + 投影到本货物的全部车载原始读数，逐条标注 `source` / `adopted` / `over_limit`，含被同刻自有测温遮蔽的车载读数）。

### 7. 责任确认 `GET /cargo/{cargo_id}/responsibilities[/{interval_id}]`

围绕**已闭合**越温区间开展责任确认（开放区间不参与）。区间以**稳定区间标识 `interval_id`** 查询与表态：自有测温开启时为开启测温事件ID；车载读数开启时为 `trip:<车次ID>:reading:<读数ID>`（读数晚到 / 装卸时段补登导致开启读数变化时，标识随之变化，旧标识按“区间消失”处理）。响应同时给出 `start_source` / `end_source`（own / onboard）、`start_event_id` / `end_event_id`（车载侧为 null）、责任片段、**当前快照标识**、各责任方最新一条有效表态与汇总状态。

```json
{
  "cargo_id": "COLD-001",
  "intervals": [
    {
      "interval_id": "evt-temp-1",
      "start_event_id": "evt-temp-1",
      "end_event_id": "evt-temp-2",
      "start": "2026-09-30T02:00:00Z",
      "end": "2026-09-30T08:00:00Z",
      "snapshot_id": "sha256:836e6b3a…e797e4",
      "segments": [
        { "start": "…T02:00:00Z", "end": "…T04:00:00Z", "responsible_party": "北京冷库" },
        { "start": "…T04:00:00Z", "end": "…T08:00:00Z", "responsible_party": "广州冷库" }
      ],
      "latest_stances": [
        {
          "stance_id": "stance-bj-1",
          "responsible_party": "北京冷库",
          "stance": "confirm",
          "reason": null,
          "created_at": "2026-09-30T09:00:00Z",
          "segments": [ { "start": "…T02:00:00Z", "end": "…T04:00:00Z", "responsible_party": "北京冷库" } ]
        }
      ],
      "summary": "pending"
    }
  ]
}
```

* **快照标识 `snapshot_id`**：`sha256:` 前缀，对「区间起止 + 责任片段序列」的规范化 JSON 取 SHA-256；边界或责任片段一变，快照标识即变。
* **汇总状态 `summary`**：`all_confirmed` 全确认（当前片段中每个责任方最新有效表态均为确认）／`has_dispute` 有异议（最新有效表态中存在异议）／`pending` 待确认（尚有责任方未表态且无异议）。
* 列表接口只返回已闭合区间；直查单个区间时，区间不存在返回 `404 interval_not_found`，区间仍开放返回 `422 interval_open`。

### 5. 提交表态 `POST /cargo/{cargo_id}/responsibilities/{interval_id}/stances`

请求体（货物编号与区间标识在路径上）：

```json
{
  "snapshot_id": "sha256:836e6b3a…e797e4",
  "responsible_party": "上海冷库",
  "stance": "dispute",
  "reason": "该时段温控由交出方远程设定，接收方未实际接管",
  "stance_id": "stance-sh-1"
}
```

* `stance`：`confirm` 确认担责；`dispute` 异议——**异议必须同时提供非空 `reason`**，确认不得带理由。
* `responsible_party` **必须出现在当前责任片段中**，否则 `422 party_not_in_segment`。
* `snapshot_id` 必须等于提交时的当前快照标识；晚到事件导致区间边界 / 责任片段变化后旧快照被拒：`409 stale_snapshot`。
* `stance_id` 为客户端指定的表态编号：
  - **同编号 + 同内容重传**：返回 `200 {"replayed": true, "stance": …原记录…}`（**即使该表态所依据的快照已失效也返回原记录**）；
  - **同编号 + 异内容**：`409 stance_id_conflict`，原记录不变；
  - **改口**（确认⇄异议）必须使用**新表态编号**，产生一条新记录；历次表态全部保留。
* 区间不存在 `404 interval_not_found`、区间仍开放 `422 interval_open`，同样拒绝且不落库；
  对车载读数开启的开放区间（`trip:...:reading:...`）同样适用。
* **幂等最优先**：同编号同内容重传即使区间已消失 / 重新开放（快照失效）也返回原记录。
* 表态提交与同货物的**事件写入、装卸时段登记、车载读数写入**在事务中共用货物行锁（`SELECT ... FOR UPDATE`，多货物按 cargo_id 升序加锁）**串行化**：校验用的快照一定来自持锁期间按**合并读数时间线**重建出的最新结果。

### 8. 表态历史 `GET /cargo/{cargo_id}/responsibilities/{interval_id}/stances`

返回区间的历次表态（按提交时间升序），即使区间当前已不存在或重新开放也可查。每条记录附：

* `is_current`：对**当前**快照是否仍有效——晚到事件 / 车载读数 / 装卸时段重建后，所在责任片段边界 / 责任方发生变化的旧表态为 `false`，片段未变化的继续有效；区间消失 / 重新开放时全部为 `false`；
* `superseded`：是否已被同责任方更新的有效表态改口（新编号表态）。

头部还给出 `interval_exists`、`is_open`、`current_snapshot_id`。

## 五、业务规则

2. **越温区间（合并读数时间线）**：
   - 测温来源包括**货物自有测温**与**车载读数**：车载读数仅在 `loaded_at ≤ 读数时刻 < unloaded_at`（左闭右开）货物仍在车上时进入该货物时间线；
   - **同刻有货物自有测温时优先采用自有值**（车载读数被遮蔽但原始记录仍可查）；
   - 测温 **高于** 该货物温度上限（`temp > limit`，等于上限不算越温）开启区间；
   - 区间内首次 **不超温**（`temp <= limit`）的测温在其测温时刻闭合区间；
   - 区间内重复超限测温不另开区间；区间外的不超限测温无影响；
   - 同车多票货物按**各自温度上限**独立判定，同一读数可能只对其中一票越温。
3. **责任拆分**：区间跨越交接时，按交接时刻切分——`[区间起, 交接时刻)` 归交出方，**交接时刻起归接收方**；不产生零长度片段。**交接永远只由货物自有 handover 事件改变保管方，车载读数不改变保管方**。
4. **晚到记录**：事件、车载读数、装卸时段均允许乱序写入。每次写入都把候选记录并入既有数据，按发生时间整体重算受影响区间；测温、交接、读数可事后补录，装卸时段补登后历史车载读数按新时段补作用。
5. **交接合法性（整体拒绝）**：任何一笔交接的交出方必须等于其发生时刻的保管方；晚到插入若导致此前已存在的交接不再满足该条件，本次写入**整体回滚拒绝，既有结果保持不变**（`422 giver_mismatch`）。
6. **同刻冲突**：同一货物同一时刻至多一笔自有测温、至多一笔交接（测温与交接可同刻发生，且测温先于交接处理），冲突 → `409 same_timestamp_conflict`；同车次同刻至多一笔车载读数（`409 reading_same_timestamp_conflict`）。
7. **幂等**：
   - 同一 `event_id` 重传：内容完全一致（时间按同一时刻归一化比较）返回 `200 {"replayed": true}` 且不重复写入；内容不一致返回 `409 event_id_conflict`，原记录不变；
   - 车载读数 `reading_id`、装卸时段 `(车次, 货物, 装车时刻)` 同内容重传幂等，异内容冲突。
8. **装卸时段（整体拒绝）**：时段必须左闭右开且起点严格早于终点（`422 invalid_loading_window`）；同货物在任意车次上的时段不得重叠（首尾相接允许，`422 loading_overlap`）；货物或车次不存在 → 404；任一条目不合法整批回滚不留部分记录。
9. **并发串行一致**：同一货物的事件写入、表态、装卸时段登记、车载读数写入，在事务中按 cargo_id 升序 `SELECT ... FOR UPDATE` 锁定货物行串行化（车载读数还锁车次行及读数时刻在车上的全部货物行），并由唯一索引 / CHECK 约束在数据库层兜底。校验用快照一定来自持锁期间按合并时间线重建的最新结果。
10. **责任确认（仅已闭合区间）**：
   - 区间稳定标识 = 自有测温事件ID，或 `trip:<车次ID>:reading:<读数ID>`；快照标识由区间起止与责任片段序列的 SHA-256 决定；
   - 表态为只追加记录：同编号同内容重传返回原记录（区间消失 / 快照失效亦然），同编号异内容冲突，改口必须用新编号且历次表态保留；
   - 新表态以当前快照校验（过期拒绝），责任方必须在当前片段中；区间不存在 / 开放均拒绝且不落库；
   - 晚到事件 / 读数 / 时段登记重建后：边界或责任片段变化的旧表态失效、未变化片段上的表态继续有效，全部历史仍可查。

错误响应统一格式：`{"error": {"code": "...", "message": "..."}}`。

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `cargo_not_found` / `trip_not_found` / `interval_not_found` | 货物、车次或已闭合区间不存在 |
| 409 | `cargo_exists` / `trip_exists` / `event_id_conflict` / `same_timestamp_conflict` / `reading_id_conflict` / `reading_same_timestamp_conflict` / `manifest_id_conflict` / `integrity_conflict` / `stance_id_conflict` / `stale_snapshot` | 编号或同刻冲突、表态编号冲突、快照过期 |
| 422 | `before_start` / `giver_mismatch` / `bad_event_shape` / `invalid_loading_window` / `loading_overlap` / `interval_open` / `party_not_in_segment` / `bad_stance_shape` / `dispute_reason_required` | 业务校验失败（整体拒绝、不落库） |
| 200 | 响应体 `replayed=true` / `all_replayed=true` | 事件 / 表态 / 车载读数 / 装卸时段同 ID 同内容幂等重传 |

## 六、调用示例

```bash
# curl 脚本（演示完整链路 + 幂等 + 报告）
bash examples/curl_examples.sh

# Python 示例（httpx）
python examples/python_client.py
```

或直接打开 http://localhost:8000/docs 使用 Swagger UI 调用。

## 七、测试

```bash
pip install pytest httpx

pytest -q                                          # 81 个用例（内存库），4 个并发用例自动跳过
DATABASE_URL="sqlite:////tmp/cc.db" \
  pytest tests/test_concurrency.py -q              # 4 个并发串行化用例（事件 / 表态 / 装卸时段 / 读数×表态，需文件型库）
```

测试覆盖：越温开闭、跨交接责任拆分、同刻测温+交接、未闭合区间、晚到重算、交出方整体拒绝、同刻冲突、幂等重传、并发写入串行化，以及责任确认的快照校验、确认/异议、汇总状态、表态幂等与改口、晚到事件后表态逐片段失效与历史可查；本轮另覆盖车次与装卸时段（左闭右开、跨车次重叠拒绝、首尾相接、整批拒绝、幂等/冲突）、车载读数（装卸时段投影、右开边界、各自上限、晚到、同刻自有测温遮蔽、幂等冲突、同刻重复读数冲突）、合并时间线查询，以及车载读数开启区间的稳定标识、表态与晚到失效。

## 八、目录结构

```
.
├── app/
│   ├── main.py            # FastAPI 路由与异常映射
│   ├── crud.py            # 货物事件写入事务、幂等、整链校验、车载点投影
│   ├── trip_crud.py       # 车次 / 装卸时段 / 车载读数写入事务（货物行锁串行）
│   ├── stance_crud.py     # 责任表态写入事务（与事件 / 读数写入共用货物行锁）
│   ├── chain.py           # 保管链重建 / 越温责任拆分（自有测温+车载读数合并，纯函数）
│   ├── responsibility.py  # 快照标识、表态有效性、确认汇总（纯逻辑）
│   ├── models.py          # SQLAlchemy 模型（货物 / 事件 / 车次 / 时段 / 读数 / 表态）
│   ├── schemas.py         # Pydantic 请求/响应模型
│   ├── database.py        # 引擎、Session（含 SQLite 兼容）
│   └── timeutil.py        # UTC 归一化
├── db/init.sql            # PostgreSQL 初始化（枚举、表、约束、索引）
├── examples/              # curl / Python 调用示例（含车次车载读数 + 责任确认完整链路）
├── tests/                 # pytest 单元 + API + 车次车载 + 责任确认 + 并发测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
