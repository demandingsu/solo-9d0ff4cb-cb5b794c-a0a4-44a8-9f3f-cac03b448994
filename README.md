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
pytest -q                     # 17 项单元 / 集成测试
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
Dockerfile                 python:3.12-slim + gunicorn(2 worker)
docker-entrypoint.sh       启动前幂等初始化数据库
hydrology/
  domain.py                严格整数解析、坐标压缩核对、单对判定
  db.py                    SQLite schema、WAL、事务助手、播种、现行口径读取
  publish.py               三类发布 + 交错交叉复核 + 尝试留痕
  seed.py                  初始口径（9 常规网格 + 洪水样本区）
  web.py                   页面路由 / 表单解析 / PRG
  api.py                   JSON API
  templates/ static/       Jinja 页面与样式
scripts/selftest.py        28 项端到端自检（判定/见证/交错/并发查询/页面）
tests/                     17 项 pytest（领域 + API + 表单 + 并发查询）
```

## 8. 自测结果（交付前实跑）

- `pytest -q`：**17 passed**（含十亿级量程压缩性能、漏分/并列见证复现、
  交错提交指针断言、表单 PRG 与载回、重型核对期间 60 次交织查询全部正确）。
- `python scripts/selftest.py`：**28/28 PASS**，覆盖 6 对判读、两类见证、
  完整交错序列、重型核对窗口内外查算均正确、4 个页面冒烟。
- 另以 `gunicorn -w 2` 真实起服，用 HTTP 脚本跑通 **14/14**：
  扩量程被驳（见证 `(101,0)`）→ 指针不变 → 延伸规则通过 → 扩量程通过 →
  `(150,900)` 归洪水样本区 → 并列稿被驳且详情页列出最高级分区与复现链接。
- `docker-compose.yml` 已通过 YAML 解析检查。本环境没有 Docker 守护进程 / CLI，
  因此**未在本机实际 `docker compose build/up`**；镜像定义按标准 python:3.12-slim
  + pip + gunicorn 编写，依赖已在本机 Python 3.11 用相同代码路径验证。

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
