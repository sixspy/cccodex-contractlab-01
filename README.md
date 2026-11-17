# 入站契约

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

Schema 契约版本约束入站校验，破坏性变更进缓冲或拒收，并与落库结果对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 入站契约 |
| 目录 | `05-contractlab` |
| 领域建议 | 数据采集、爬虫与 RPA |
| 接口 | http://127.0.0.1:10711 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

字段增删改类型、宽严模式、影子契约、缓冲窗、生产者消费者双视角

## 避开

别做成质检签发换皮；别做成纯表单 CRUD

## 互斥切口候选

- 破坏性变更直接拒收 vs 双写缓冲
- 影子只读试跑 vs 双轨分开展示
- 契约回滚整版原子切换 vs 按生产者摘流

## 切版主链路（正式版切换）

正式切版与入站裁决同源：入站校验只沿 `active_pointers` 正式指针取契约，切版写的也是同一指针。互斥写路径二选一，一支到底不得混用：

- **甲支** `POST /api/contracts/switch {"branch":"甲","target_version":...}`：旧正式停用与新正式启用在**同一事务**提交，提交后随即按新必填重裁在途批次（原批次不可变，结论进 `reviews` 复核队列）。
- **乙支** `{"branch":"乙","version":...,"required_fields":...}`：先落「待晋升」草稿，确认无双正式后停用旧正式、原子切换正式指针。

约束与留痕：

- 分支选定即写入 `switch_ledger` 切版策略簿，同主题当天锁定不许再换：同支后到记「争用」、异支后到记「偏了」，均 409 拒绝且不生效；并发抢点由进程锁串行排队，数据库部分唯一索引兜底。
- 部分唯一索引保证每主题至多一条启用正式契约（禁止双正式并存）；切版成功后裁决只读新指针（禁止仍读旧必填）。
- 五处同源：采集目录 `/api/contracts`、入站列表 `/api/ingress`、详情卡 `/api/ingress/{id}`、策略附页 `/api/switch/ledger`、复核队列 `/api/reviews` 全部读同一 SQLite，入库批次与切版事件可逐行对读（见 `tests/test_switch.py`）。

## 启动（一键）

`powershell
cd projects/05-contractlab
docker compose up -d --build
`

浏览器：http://127.0.0.1:10711/  
健康检查：GET http://127.0.0.1:10711/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10602
python -m pytest -q
`
