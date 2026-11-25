# 水文分区 Flask 发布台

给水文资料员使用的现行口径发布页。SQLite 保存**现行有效**的量程与分区规则版本；
高水位 × 高流量同时出现的读数由高优先级的「洪水样本区」规则优先捕获，其余按常规网格分区。

- 水位、流量均为**整数闭区间**；
- 每条规则含**分区名、优先级、水位区间、流量区间**（优先级约定：数值越小越高，`1` 为最高）；
- 有效量程可与分区表**一并发布**，也可**单独更新**其中一类；
- 发布前核对：量程内每一对整数读数都必须有**唯一的最高优先级规则**；
- 核对使用**区间端点坐标压缩**，不逐值枚举；
- 漏分、最高级并列各给出一个**确定性、可复现的读数对**，并列时列出命中的最高级分区；
- 核对 / 驳回期间，查单条读数的页面与 JSON 接口始终读取已提交的现行口径；
- 两类修订交错提交时，每笔在事务内按**另一项届时生效的版本**交叉复核，失败只记录、不移动现行指针；
- Docker Compose 一键启动。

此外提供**历史档案收料**页：按来源库号与原始编号保存发布记录、量程与分区表三类材料，
材料齐备后按发布记录引用的组合核对，通过后才开放历史查算（见第 10 节）。

---

## 1. Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

- SQLite 文件位于命名卷 `hydrology-sqlite` 内的 `/data/hydrology.db`；
- 容器启动时自动建表并播种一套已核对通过的现行口径（量程 水位 `[0,100]`、流量 `[0,1000]`，
  9 条常规网格规则 + 1 条优先级 1 的「洪水样本区」`[85,100] × [850,1000]`）；
- 自带 `/api/current` 健康检查。
- 历史档案表与现行口径表同库、随启动幂等建表，无需额外迁移；
  全部分批上传样例见 `samples/archive/` 与第 10 节。

> 若使用 bind mount 挂载本地目录，请确保容器内 uid=10001 对该目录可写
> （命名卷无此问题，会自动继承 `/data` 属主）。

## 2. 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python app.py                 # http://127.0.0.1:8000 （开发服务器）
# 或
gunicorn -w 2 -b 0.0.0.0:8000 app:app

python scripts/selftest.py    # 端到端自测（临时 SQLite 库，不影响正式数据）
pytest -q                     # 39 项单元 / 集成测试
```

数据库路径可用环境变量 `HYDROLOGY_DB` 覆盖，默认 `data/hydrology.db`。

---

## 3. 页面

| 路径 | 功能 |
| --- | --- |
| `/` | 单对读数查**现行归属**；展示现行量程与分区表、命中的全部规则 |
| `/publish` | 量程四字段 + 可增删的规则行；三个提交动作：仅量程 / 仅分区表 / 一并发布；被驳回稿可一键载回修改 |
| `/attempts` | 全部发布与核对记录（含被驳回） |
| `/attempts/<id>` | 核对报告：漏分 / 并列见证读数对、最高级分区、其余样本、交叉复核所用版本、候选 JSON |
| `/archive` | 历史档案收料：上传 JSON、按来源 / 发布号列出缺项、查看核对状态与原因 |
| `/archive/records/<id>` | 单份历史档案：缺项、核对报告 / 未通过原因、**按该档案口径查原分区** |

见证读数旁的「查该对读数的现行归属 ↗」会跳到首页，用该对读数查询**现行**口径，
直观体现“核对期间网页照常查现行归属”。

## 4. JSON API

```bash
# 查单条现行归属
curl 'http://localhost:8000/api/classify?wl=90&flow=900'
# -> {"status":"classified","zone":"洪水样本区","priority":1,...}

# 取现行口径
curl http://localhost:8000/api/current

# 只核对、不发布
curl -X POST http://localhost:8000/api/verify \
  -H 'Content-Type: application/json' \
  -d '{"wl_min":0,"wl_max":4,"fl_min":0,"fl_max":4,
       "rules":[{"zone":"全区","priority":1,
                 "wl_min":0,"wl_max":4,"fl_min":0,"fl_max":4}]}'

# 单独发量程（用现行分区表复核）；失败返回 422 且现行口径不变
curl -X POST http://localhost:8000/api/publish/ranges \
  -H 'Content-Type: application/json' \
  -d '{"wl_min":0,"wl_max":200,"fl_min":0,"fl_max":1000}'

# 单独发分区表（用现行量程复核）
curl -X POST http://localhost:8000/api/publish/rules -d @rules.json -H 'Content-Type: application/json'

# 量程 + 分区表一并发布（成对复核）
curl -X POST http://localhost:8000/api/publish/bundle -d @bundle.json -H 'Content-Type: application/json'
```

发布接口返回 `200 {accepted:true,...}` 或 `422 {accepted:false, report:{...}}`，
响应里带 `attempt_id`，可跳转 `/attempts/<id>` 看完整报告。

---

## 5. 核对算法（为什么不是逐值枚举）

规则在“水位—流量”平面上就是轴对齐整数闭矩形。把闭区间右端 `+1` 转成半开段，
然后：

1. 收集量程边界与每条有效规则的 `wl_min / wl_max+1 / fl_min / fl_max+1`（裁到量程内）；
2. 排序去重得到 x/y 坐标轴；相邻分点构成**初等整数格**，所有规则边界都落在轴上，
   因此同一格内“哪些规则命中、最高优先级是谁”完全不变；
3. 每格取左下角代表整数对扫描：
   - 无规则命中 → **漏分格**，代表点即漏分见证；
   - 命中规则中最小优先级值被 ≥2 条规则并列 → **最高级并列格**，记录这些最高级分区；
4. 坐标轴按升序扫描，第一个问题格的代表点是坐标字典序确定的，
   **同一口径重复核对必得同一对读数**（种子反例中漏分 `(0,0)`、并列 `(90,900)`）。

复杂度上界约 O(n³)（O(n²) 个格 × 每格 O(n) 条规则），与量程整数跨度无关；
测试里 `0..10^9` 的量程也是毫秒级完成。报告同时给出问题格数量与最多 20 个额外见证格，
并标注每个见证格覆盖的整数边界，方便资料员定位整片问题区。

## 6. 交错提交与“失败沿用现行”

SQLite 中：

- `range_versions` / `rules_versions` / `rules` 保存每次成功发布的版本；
- `meta.current_range_id` / `current_rules_id` 是唯一的现行指针；
- `publish_attempts` 对**每一笔**提交（含结构错误与核对驳回）留痕，
  记录当时参照的 `base_range_id / base_rules_id` 与完整报告、候选 JSON。

`hydrology/publish.py` 的每笔发布在 `BEGIN IMMEDIATE` 事务内：

| 动作 | 待核口径 |
| --- | --- |
| `ranges` | 新量程 ＋ **届时现行**分区表 |
| `rules` | **届时现行**量程 ＋ 新分区表 |
| `bundle` | 新量程 ＋ 新分区表 |

核对通过才写新版本并 `UPDATE` 现行指针；不通过只插入一条 `accepted=0` 记录后提交，
指针原样不动。`BEGIN IMMEDIATE` + `busy_timeout` 串行化发布者，
保证并发两笔发布时后到者一定拿得到前一笔生效后的“届时版本”。

**查算可用性**：查询请求只按已提交指针读现行版本，候选数据不进现行表；
数据库启用 WAL，发布事务持锁复核期间读请求不被阻塞。自测在后台线程连续跑重型坐标压缩核对，
并行窗口内的连续查算与窗口后 20 次请求全部正确返回、0 失败；pytest 另有 60 次交织查询断言。
（生产用 gunicorn 2 个独立 worker 进程，核对 CPU 计算与查算分属不同进程，无 GIL 相互抢占。）

一个典型交错序列（自测与 HTTP 试跑均覆盖）：

1. 现行 `range#1 / rules#1`；
2. 先把水位上限扩到 200 → 现行规则只到 100，新条带 `101..200` 漏分，
   首个见证 `(101,0)`，**422 驳回，指针不变**；
3. 先发把高水位规则延伸到 200 的新分区表 → 按现行量程 `0..100` 复核无变化，通过，
   得到 `rules#2`，量程仍是 `range#1`；
4. 再发同一扩量程请求 → 按届时生效的 `rules#2` 复核通过，得到 `range#2`；
5. `(150,900)` 随即正确判为「洪水样本区」；
6. 不匹配的 bundle（量程扩到 300 但不扩规则）被整体驳回，两个指针都不动。

---

## 7. 目录结构

```
app.py                     gunicorn/本地入口 app:app
docker-compose.yml         web 服务 + 命名卷 + healthcheck
Dockerfile                 python:3.12-slim + gunicorn(2 worker)，含 samples/
docker-entrypoint.sh       启动前幂等初始化数据库
hydrology/
  domain.py                严格整数解析、坐标压缩核对、单对判定
  db.py                    SQLite schema、WAL、事务助手、播种、现行口径读取、档案表
  publish.py               三类发布 + 交错交叉复核 + 尝试留痕
  seed.py                  初始口径（9 常规网格 + 洪水样本区）
  web.py                   页面路由 / 表单解析 / PRG
  api.py                   JSON API
  archive.py               历史档案：材料规范化、判重 / 冲突、引用组合核对、查算
  archive_web.py           /archive 收料页与档案详情页
  archive_api.py           /api/archive/* 收料、列表、查原分区
  templates/ static/       Jinja 页面与样式
scripts/selftest.py        42 项端到端自检（判定/见证/交错/并发/页面/档案）
scripts/upload_archive_samples.py  分批上传样例投递脚本
samples/archive/*.json     分批上传样例（发布记录 → 量程 → 分区表 → 冲突 → 跨来源 → 漏分）
tests/                     39 项 pytest（领域 + API + 表单 + 并发 + 历史档案）
```

## 8. 自测结果（交付前实跑）

- `pytest -q`：**39 passed**（原 17 项 + 新增 22 项历史档案测试，含并发上传只核一次、
  核对事务未提交前新档案查不到、冲突整份退回、跨来源重号、失败原因与查算门槛）。
- `python scripts/selftest.py`：**42/42 PASS**，覆盖原有判定 / 见证 / 交错序列 / 并发查算 /
  页面冒烟，以及档案的分批到达、缺项、刚补齐、判重、冲突、漏分原因、查原分区。
- 另以 `gunicorn -w 2` 真实起服，用 `scripts/upload_archive_samples.py` 按
  `samples/archive/01…06` 顺序投递：发布记录先到（缺量程 / 分区表）→ 量程到 →
  分区表到（F-1001、F-1002 核对通过）→ `04` 冲突返回 409 → 跨来源重号独立通过 →
  `06` 漏分组合核对未通过并列出原因；HTTP 页面与 `/api/archive/*` 均正常。
- `docker-compose.yml` 未改动；本环境没有 Docker 守护进程 / CLI，因此**未在本机实际
  `docker compose build/up`**，但 `Dockerfile` 已把 `samples/` 一并纳入镜像，
  并按现有 compose 的 `HYDROLOGY_DB=/data/hydrology.db` 路径启动验证过同一代码。

## 9. 余项与已知边界

1. **无鉴权 / 无 CSRF 防护**：定位为内网水文资料员工具，默认信任所有能访问端口的人；
   上公网前应加反向代理鉴权与 HTTPS，并补 CSRF token。
2. **优先级方向**取“数值越小越高”，页面、API、README 均显式标注；
   若测站既有《任务书》采用相反约定，只需调整 `verify/classify_pair` 中取极值的方向与文案。
3. 见证点是“第一个问题初等格”的代表点（确定性、可复现），**没有做相邻问题格合并成多边形**；
   报告已给出问题格数量、格边界与最多 20 个样本，需要的话可进一步做矩形合并 / 导出 Shapefile。
4. 规则完全落在量程外时只给 warning，不阻断发布；量程外读数在查算页明确标为 `out_of_range`。
5. SQLite 适合单站、低频发布；多副本水平扩展时应换 Postgres（事务语义与 SQL 基本可平移），
   并把卷换成共享数据库。
6. 时间戳以 UTC 存储展示；如需北京时间可在模板加过滤器或改存本地时区。
7. 未做规则的可视化矩形编辑器，目前是表格行编辑 + JSON API；后续可加平面图框选。
8. 历史档案材料**入库即不可更替**：同号不同内容整份退回，没有“撤回 / 更正”入口；
   若某组合确实有误，当前做法是用新的发布号与版本号重新归档（见第 10 节边界）。
9. 档案核对只在**材料刚补齐**时执行一次；失败后不会因为同号同内容的重复上传再次核对，
   避免把已失败的档案“刷”成通过。

---

## 10. 历史档案收料

档案员常先收到**发布记录**，过几天才收到**量程**与**分区表**。本节功能把三类材料
按来源库号收进来，等材料齐了再自动核对，通过后才开放历史查算。

### 材料唯一键与引用

| 类别 `kind` | 原编号 `no` | 内容 |
| --- | --- | --- |
| `publish` 发布记录 | 发布原编号 | `{"range_no": …, "rules_no": …}`，引用同一来源下的量程 / 分区表原版本号 |
| `range` 量程 | 量程原版本号 | `{"wl_min","wl_max","fl_min","fl_max"}` |
| `rules` 分区表 | 分区表原版本号 | `{"rules":[…]}` 或直接规则数组 |

- 唯一键为 `(来源 source, 类别 kind, 原编号 no)`；**不同来源可以重号**，彼此独立。
- 同一来源、同一类别、同一编号：同内容只存一份（幂等去重）；内容不同则**整份退回**，
  已入库版本、档案状态、核对结果都不变，响应 `409` 并给出两版内容对比。
- 内容按规范化后的 JSON + SHA-256 判等，所以 `"0"` 与 `0`、键顺序不同、空行差异都算同内容。

### 核对时机

- 发布记录先入库时状态为 `incomplete`，页面按来源与发布号列出**缺量程 / 缺分区表**。
- 每次材料入库都在一个 `BEGIN IMMEDIATE` 写事务里：写入材料后，找出因本次入库而
  **刚补齐**的档案记录（发布记录存在且引用的量程与分区表都已到齐），
  按**各自引用**的量程 + 分区表组合跑坐标压缩核对。若一批里有多个记录刚补齐，全部核对。
- 材料写入、核对结果、状态与事务一起提交。并发上传时不会漏核，也不会在核对完成前
  把 `verified` 暴露给查算接口；WAL 下现行读数与**已通过核对的档案**照常可查。
- 核对通过 → `verified`，开放查算；未通过 → `failed`，保存报告与可读原因（漏分 / 并列 /
  结构错误），同来源其他材料照常接收，该档案不开放查算。

### 页面与接口

- `/archive`：粘贴或上传 JSON（支持单份 / 数组分批），展示收料结果、按来源与发布号列出缺项。
- `/archive/records/<id>`：缺项、核对报告 / 原因、通过后按整数水位与流量查**该档案**的原分区。
- `POST /api/archive/materials`：单份或数组；`200` 成功 / 去重，`409` 冲突退回，`400` 格式错误，
  数组部分失败时 `207`。
- `GET /api/archive/records[?source=&state=]`、`GET /api/archive/records/<id>`。
- `GET /api/archive/classify?source=…&publish_no=…&wl=…&flow=…`：仅 `verified` 档案可查，
  否则 `409`；记录不存在 `404`。

### 分批上传样例

`samples/archive/` 下按顺序投递即可复现完整场景：

```bash
docker compose up --build -d
python3 scripts/upload_archive_samples.py          # 默认 http://127.0.0.1:8000
```

| 批次 | 内容 | 预期 |
| --- | --- | --- |
| `01_publish.json` | 站A 两条发布记录 | 材料未齐，列出缺量程 / 缺分区表 |
| `02_ranges.json` | 站A 两份量程 | 仍缺分区表 |
| `03_rules.json` | 站A 两份分区表 | F-1001、F-1002 刚补齐并核对通过 |
| `04_conflict_range.json` | R-11 同号不同内容 | `409` 整份退回，已入库版本不变 |
| `05_other_source.json` | 站B 用同样的 F-1001 / R-11 / G-11 | 与站A 互不影响，材料齐后通过 |
| `06_failed_combo.json` | 站A F-1003 的分区表漏左岸条带 | 材料照收，核对未通过并给出漏分原因 |
