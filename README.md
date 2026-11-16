# 窑火良率分布复盘

描述统计与稳健分布的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览和审计轨迹。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 User、Audit、Delivery。

### 电表结账与逐窑耗电（新增）
共用电表按 `电表 → 抄表链 → 结账单 → 分摊时段 / 逐窑行 → 入账台账 / 更正` 建模，页面在「电表结账」。

- **累计读数带修订号**：`MeterReading` 以 `(meter_id, reading_at, revision)` 唯一；同一抄表时刻取最高修订号，迟到数据用更高修订号补录。
- **换表衔接**：`MeterLink` 登记旧表止码、新表起码与发生时刻；累计读数回退或跨表读数必须能由此承接，否则该时段待核对。
- **按时段分摊**：结账窗口按窑次起止切分；有窑在烧的时段按各在烧窑次的装窑量加权分摊，无窑运行的时段单列为闲置。
- **严格配平**：电量以毫千瓦时为整数记账并用最大余数法分配，结账时 `窑次分摊 + 闲置 = 表差`，逐窑合计等于分摊合计。
- **待核对不准结账**：缺起始/结束读数、抄表空档超过期望间隔、回退无衔接、衔接不清都会生成待核对时段；`pending_count > 0` 时创建即为 `stale`，结账返回 409。
- **失效与重算**：迟到读数与上游窑次修订会使受影响时段内未结账的分摊变为 `stale` 并就地重算；窗口外或其它计量组的变化不影响本单。
- **结账并发**：结账校验分摊版本 `revision`、输入指纹与逐项金额；入账前再次复核，遇并发改数即拒绝旧分摊并清空未完成台账。
- **中断恢复与幂等**：入账按行写入 `Posting` 台账（`posting_key` 唯一），状态与进度持久化在 `posting_progress`；服务启动自动续跑，带同一 `request_id` 重试原单不会重复入账。
- **已结账更正**：`Correction` / `CorrectionRow` 只登记新旧差额与来由，原结账单和已入账明细保持不变；连续更正按上一次更正后的基线累加。
- **页面核对**：逐窑耗电、每合格件耗电、闲置电量与分摊明细、待核对时段、入账台账与更正来由均可核对。

主要接口（前缀 `/accounting`）：`GET/POST /meters`、`POST /meters/{id}/readings`、`GET/POST /meters/links`、`GET/POST /firings`、`GET/POST /settlements`、`GET /settlements/{id}`、`POST /settlements/{id}/{recompute|close|resume|corrections}`、`GET /corrections`。结账与更正仅 admin；登记电表、读数、衔接、窑次与创建结账单需 admin/analyst。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18200；后端文档 http://localhost:19200/docs；数据库宿主端口 17700。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、结账、更正、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、登记电表/读数/窑次、创建结账、读取与下载；不能批准、结账或更正|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；PostgreSQL 16；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed；SettlementStatus：draft/stale/closing/closed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=kiln-yield；FRONTEND_PORT=18200；BACKEND_PORT=19200；DB_PORT=17700；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。
结账口径测试在 `backend/tests/test_accounting.py`（无 pytest 时可直接运行：`cd backend && python tests/test_accounting.py`），覆盖共用电表分摊、闲置单列、修订号覆盖、缺读数与空档、回退/换表衔接、迟到数据重算、结账并发拒绝、中断恢复与幂等、已结账更正差额。测试与截图统一存放 ../../test-artifacts/gb-1001/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
