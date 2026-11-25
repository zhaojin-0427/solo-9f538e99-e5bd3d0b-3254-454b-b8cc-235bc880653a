# 缩微目录候选键与交接键管理（Flask + SQLite）

旧扫描清册没有库内原件号，档案员需要从缩微目录的若干列（馆代码、卷号、拍摄年、
页号、缩微号等 4～8 列）中挑出一组能用来联表的列。本应用提供网页完成：

1. 上传首批 CSV：含**唯一原件号列** + **4～8 个待选列**；
2. 列出**所有最简候选键**（每行键值齐全、行间互异，且去掉任意一列后不再成键）；
3. 档案员从“确认时仍合格”的候选中选定**一个交接键，只能选一次**；
4. 启用后下载当前 **键值 ↔ 原件号** 对照 CSV；
5. 补传沿用同一表头、**同号整行覆盖**，按“整批换入后”的数据核对交接键：
   - **两行互换键值可以入库**（任意多行轮换也可以）；
   - 缺值或撞键时**报出涉及的原件号并退回整批**（事务回滚，旧数据不动）；
6. 查候选只读“列组合汇总表”，**至多 255 条**（8 列时非空子集恰为 2⁸−1=255）；
7. 补传**只按本批原件号读取涉及的旧明细/旧索引项**，不全表扫描；
8. 网页显示**索引总条数**与**本批读/写的索引项数**（目录索引、交接键索引两种口径）。

---

## 一、用 Docker Compose 启动（推荐）

```bash
cd /workspace
docker compose up --build
# 或旧版： docker-compose up --build
```

容器内 gunicorn 监听 `8000`，compose 把宿主机的 **8000**（主）、**5000**
（Flask 习惯端口）、**8080**（备用）都映射到该端口，任选其一：

- <http://localhost:8000/>
- <http://localhost:5000/>
- <http://localhost:8080/>

SQLite 数据库保存在命名卷 `archive-data` 的 `/data/archive.db`，
`docker compose down` 后数据仍在；加 `-v` 可连同数据卷删除。
健康检查：`GET /healthz`。

> 复测/自动化时可 `POST http://localhost:8000/api/admin/reset` 清空全部状态
> （compose 默认 `ENABLE_RESET=1`；自行部署时设为 `0` 关闭）。

## 二、不用 Docker 的本地运行

```bash
pip install -r requirements.txt
python app.py                 # 默认 0.0.0.0:8000，可用 PORT=5000 改端口
# 或： flask --app app run --host 0.0.0.0 --port 8000
```

数据库默认在 `./data/archive.db`，可用环境变量 `ARCHIVE_DB=/path/to.db` 覆盖。

## 三、网页操作流程

1. 打开首页，在“① 上传 CSV / 补传”选择 `samples/first_batch.csv` 上传；
2. “② 交接键”列出全部最简候选键（单选），下方可展开全部 31/255 个列组合判定；
3. 选中一个候选（例如 `缩微号`），点“确认选定交接键（仅一次）”；
4. 出现“⬇ 下载当前键值—原件号对照 CSV”，点击得到 `handover_key_mapping.csv`；
5. 之后同一上传框即“补传”：
   - 上传 `samples/supplement_swap.csv`：YW-0001/YW-0002 **互换缩微号，成功**；
   - 上传 `samples/supplement_conflict_old.csv`：YW-0003 占用 YW-0004 的键，
     **422 退批并同时报出 YW-0003、YW-0004**；
   - 上传 `samples/supplement_conflict_batch.csv`：YW-0004/YW-0005 批内撞键，退批；
   - 上传 `samples/supplement_missing.csv`：YW-0006 缺键值，退批。

首页顶部 6 个统计卡与“最近批次”行实时显示：索引总条数、交接键索引条数、
候选汇总读取数、本批读取/写入索引项数、读取旧明细条数、新增/覆盖数。

## 四、HTTP / JSON 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 网页仪表盘 |
| POST | `/upload`（也支持 `/`、`/api/upload`） | multipart 字段 `file` 上传 CSV |
| GET | `/candidates`、`/api/candidates`、`/api/candidate-keys`、`/candidates.json` | 最简候选键 + 汇总读取计数 |
| POST | `/confirm`、`/select`、`/select-key`、`/key/confirm`、`/api/key/confirm`、`/api/confirm` | 确认交接键（仅一次） |
| GET | `/mapping.csv`（别名 `/download`、`/download.csv`、`/mapping`） | 下载键值—原件号对照 CSV |
| GET | `/api/mapping` | 对照数据 JSON |
| GET | `/api/key` | 当前交接键 |
| GET | `/api/stats` | 索引总数与最近批次读写计数 |
| POST | `/api/admin/reset` | 清空重置（`ENABLE_RESET=1`） |
| GET | `/healthz`、`/health` | 健康检查 |

确认键的参数形式（任选）：

```json
{"mask": 16}
{"columns": ["拍摄年", "页号"]}
{"key": "拍摄年、页号"}
```

`mask` 位定义：候选列按表头顺序对应 bit0…bit(n−1)，如第 5 列“缩微号”=2⁴=16，
“拍摄年+页号”=2²+2³=12。HTML 表单直接提交单选 `mask` 即可。

退批响应示例（HTTP 422）：

```json
{
  "ok": false,
  "message": "补传整批退回，目录与索引均未改动；撞键涉及原件号：YW-0003、YW-0004",
  "report": {
    "missing": [],
    "collisions": [{"key": "WX-B0001", "original_ids": ["YW-0003", "YW-0004"]}],
    "involved_ids": ["YW-0003", "YW-0004"]
  }
}
```

## 五、实现说明

### 存储结构（SQLite，`archive_app/db.py`）

- `meta`：表头 schema、已确认的交接键（单行，确认后不再更改）；
- `items(original_id PRIMARY KEY, values_json)`：目录明细，同号 `UPSERT` 整行覆盖；
- `candidate_summary(mask PRIMARY KEY, columns_json, complete, is_unique,
  is_candidate, missing_count)`：**每个非空列子集一项，最多 255 项**；
- `key_index(original_id PRIMARY KEY, key_text UNIQUE, display_value, ...)`：
  交接键索引，`key_text` 用无歧义的 JSON 数组规范化，避免分隔符碰撞；
- `batches`：每批的 applied/rejected、读/写计数与 JSON 退批报告。

### 最简候选键算法（`archive_app/keys.py`）

- 枚举 `1 … 2^n−1` 全部非空列子集（n≤8，最多 255）；
- “齐全”：组合内每一列的空值行数都为 0（空串/纯空白/短行补空均算缺值）；
- “互异”：把每行这些列组成 tuple 放入集合判定唯一；
- “最简”：本身齐全且唯一，且从中去掉任意一列后的子集都不再唯一。
  由于齐全性对子集单调成立、而重复行在投影掉更多列后不可能重新变唯一，
  只需检查“去掉一列”的直接子集即可；单字段唯一键视为最简。

该算法除自带 18 项端到端测试外，还与一份独立的 `itertools` 暴力枚举实现在
**300 组随机数据**（含缺值、小取值域）上逐一比较，结果完全一致。

### 候选查询为什么“只读汇总、至多 255 条”

`GET /api/candidates` 与首页候选区只执行
`SELECT … FROM candidate_summary ORDER BY mask LIMIT 255`，
不会扫描 `items`。汇总是在首批入库 / 确认键时从事务内全量计算并落表的。

### 补传与“互换键值也能入库”（`archive_app/services.py::apply_supplement`）

整批在**一个 `BEGIN…COMMIT` 事务**内完成，核对顺序：

1. 解析 CSV、校验表头列集合与首批一致、文件内原件号不重复/非空；
2. 用 `WHERE original_id IN (本批ID)`（按 400 个分块）**只读取本批涉及的旧明细**，
   计数 `detail_reads`；同样按原件号读取这些 ID 的旧 `key_index`，计数 `index_reads`；
3. 检查本批新行键值是否缺列；
4. 用“整批换入后”的新键集合检查**批内撞键**；
5. 对每个新键去 `key_index` 探测旧持有者：若持有者原件号**属于本批**，
   说明该项本来就要被整批换入，**排除**——因此 A、B 互换键值（乃至多行轮换）
   不会误判；若持有者**不在本批**，才是与未变动旧行撞键，同时报出新旧两个原件号；
6. 有缺值/撞键 → 写一条 `rejected` 批次报告后 `ROLLBACK`，目录与索引零改动；
7. 全部合格 → `UPSERT items`，`DELETE` 本批 ID 的旧索引项后插入新索引项
   （计数 `index_writes`），写 `applied` 批次后提交。`key_index.key_text UNIQUE`
   是最后一道约束兜底。

### 页面上的读写计数口径

- **索引总条数** `#index-total`：缩微目录原件记录总数（`items`）；
- **交接键索引总条数** `#key-index-total`：`key_index` 条数；
- `#batch-index-reads / #batch-index-writes`：目录索引口径，
  分别等于按本批原件号读到的旧明细数、成功批次的新增+覆盖数（退批为 0）；
- 同区副标与 `data-index-reads / data-index-writes`：交接键索引口径；
- `#batch-detail-reads`：本次按原件号实际读取的旧明细条数。

`GET /api/stats` 同时给出 `index_total`、`batch_index_reads/writes`、
`batch_key_index_reads/writes`、`batch_detail_reads` 等字段。

## 六、实测结果（交付前已执行）

环境无 Docker 守护进程，故镜像构建未能在本沙箱执行（见“缺项”）；
应用本身用与 Dockerfile 相同的 gunicorn 命令做了真实 HTTP 试跑，并用 Flask
test client 跑了自动化测试。

```
python -m unittest tests.test_flow
Ran 18 tests … OK
```

覆盖：候选键与独立暴力枚举一致、8 列时汇总恰为 255、确认一次后再次确认 409、
对照 CSV 内容、单键/复合键互换放行、与未涉及旧行撞键报双方并回滚、批内撞键、
缺值、表头不符、新原件号使总数增长、HTML 全流程、420 行大数据（汇总仍只读 31 条
且候选正确）、GB18030 与 UTF-8 BOM、列数越界、reset。

真实 gunicorn HTTP 试跑摘要：

```
POST /api/upload(samples/first_batch.csv) -> 200  6 条，3 个最简候选键
GET  /api/candidates -> [馆代码+卷号+页号, 拍摄年+页号, 缩微号]，汇总读取 31/255
POST /api/key/confirm {"columns":["缩微号"]} -> 200
再次 POST /api/key/confirm -> 409 key_already_set（只能选一次）
GET  /mapping.csv -> 键值,缩微号,原件号 …（6 行，带 UTF-8 BOM）
swap 补传 -> 200，detail_reads=2 index_reads=2 index_writes=2
            YW-0001=WX-A0002, YW-0002=WX-A0001（互换成功）
conflict_old -> 422 involved_ids=[YW-0003, YW-0004]
conflict_batch -> 422 involved_ids=[YW-0004, YW-0005]
missing -> 422 involved_ids=[YW-0006]
退批后 YW-0003=WX-A0003、YW-0006=WX-C0001，总数仍为 6（整批回滚）
```

另：300 组随机数据上候选算法与暴力枚举完全一致；`python app.py` 与
`gunicorn app:app` 两种启动均实测 `/healthz=200`。

## 七、缺项与说明

- **本沙箱内没有 docker / podman / 守护进程**，无法实际 `docker compose build/up`；
  已用 PyYAML 校验 `compose.yaml` 语法、用相同 gunicorn 命令实测启动，
  但“镜像构建 + compose 拉起”这一步需在有 Docker 的环境最终确认一次。
- 未加登录/鉴权（任务未要求，定位为内网档案工具）；`/api/admin/reset` 默认开启，
  公网部署请设 `ENABLE_RESET=0` 并加访问控制。
- 为避免 SQLite 多写竞争，compose 使用 **1 个 gunicorn worker + 4 线程**；
  本应用规模（单馆缩微目录）足够。
- 未做前端框架/构建，页面为服务端渲染 + 原生表单，便于无 Node 环境运行与自动化定位
  （关键元素均带 `id` / `data-testid`）。
- CSV 按 UTF-8(BOM)→GB18030 顺序解码，兼容 Excel 中文版另存的 CSV。

## 目录结构

```
app.py                     # python app.py / gunicorn app:app 入口
Dockerfile  compose.yaml  docker-compose.yml
requirements.txt
archive_app/
  __init__.py              # create_app 工厂
  db.py                    # SQLite 连接/建表/meta
  keys.py                  # CSV 解析 + 候选键算法（纯函数）
  services.py              # 首批/确认/补传事务、对照 CSV、仪表盘上下文
  views.py                 # HTML + JSON 路由
  templates/  static/
samples/                   # 可直接试跑的 5 个 CSV
tests/test_flow.py         # 18 项端到端测试
```
