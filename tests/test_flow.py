"""端到端 + 算法对照测试：运行 `python -m unittest tests.test_flow -v`。"""
from __future__ import annotations

import csv
import io
import itertools
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from archive_app import create_app  # noqa: E402
from archive_app.keys import evaluate_subsets  # noqa: E402

HEADER = ["原件号", "馆代码", "卷号", "拍摄年", "页号", "缩微号"]


def csv_bytes(header, rows, encoding="utf-8"):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    text = buf.getvalue()
    if encoding == "utf-8":
        return text.encode("utf-8")
    if encoding == "utf-8-sig":
        return "﻿".encode("utf-8") + text.encode("utf-8")
    return text.encode(encoding)


def brute_force_candidates(rows, columns):
    """独立实现的最简候选键枚举，作为对照基准。"""
    out = set()
    n = len(columns)
    for r in range(1, n + 1):
        for combo in itertools.combinations(range(n), r):
            cols = [columns[i] for i in combo]
            vals = []
            complete = True
            for row in rows:
                t = tuple(str(row.get(c, "")).strip() for c in cols)
                if any(v == "" for v in t):
                    complete = False
                    break
                vals.append(t)
            if not complete:
                continue
            if len(set(vals)) != len(vals):
                continue
            minimal = True
            for j in range(len(cols)):
                sub = cols[:j] + cols[j + 1:]
                if not sub:
                    continue  # 空组合不算可用键
                sub_vals = [tuple(str(row.get(c, "")).strip() for c in sub)
                            for row in rows]
                if len(set(sub_vals)) == len(sub_vals):
                    minimal = False
                    break
            if minimal:
                out.add(tuple(cols))
    return out


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, data, name="t.csv", path="/api/upload"):
        return self.client.post(
            path, data={"file": (io.BytesIO(data), name)},
            content_type="multipart/form-data")

    def first_rows(self):
        return [
            ["YW-0001", "G01", "J1001", "1998", "001", "WX-A0001"],
            ["YW-0002", "G01", "J1001", "1998", "002", "WX-A0002"],
            ["YW-0003", "G01", "J1002", "1999", "001", "WX-A0003"],
            ["YW-0004", "G02", "J1001", "2001", "001", "WX-B0001"],
            ["YW-0005", "G02", "J1002", "2001", "002", "WX-B0002"],
            ["YW-0006", "G03", "J2001", "2010", "001", "WX-C0001"],
        ]

    def upload_first(self):
        data = csv_bytes(HEADER, self.first_rows())
        resp = self.upload(data, "first.csv")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp

    # 1. 候选键与独立暴力枚举一致 ------------------------------------
    def test_candidates_match_bruteforce(self):
        resp = self.upload_first()
        api = self.client.get("/api/candidates").get_json()
        got = {tuple(c["columns"]) for c in api["candidates"]}

        records = [dict(zip(HEADER[1:], r[1:])) for r in self.first_rows()]
        expected = brute_force_candidates(records, HEADER[1:])
        self.assertEqual(got, expected)
        # 汇总项数 = 2^5-1 = 31，且读取不超过 255
        self.assertEqual(api["summary_rows_read"], 31)
        self.assertLessEqual(api["summary_rows_read"], 255)
        self.assertEqual(api["summary_cap"], 255)
        # 每个候选确实满足“去掉任一列不再唯一”
        for c in api["candidates"]:
            self.assertTrue(c["mask"] >= 1)

    # 2. 8 个待选列 -> 255 个汇总项 ----------------------------------
    def test_eight_columns_gives_255_summary(self):
        header = ["原件号"] + [f"c{i}" for i in range(8)]
        rows = []
        for i in range(12):
            rows.append([f"ID{i:03d}"] +
                        [f"v{i}_{j}" if j == 7 else f"g{j}-{i % 3}"
                         for j in range(8)])
        self.upload(csv_bytes(header, rows))
        api = self.client.get("/api/candidates").get_json()
        self.assertEqual(api["summary_rows_read"], 255)
        self.assertEqual(len(api["summary_rows"]), 255)
        self.assertLessEqual(len(api["candidates"]), 255)

    # 3. 确认一次 + 对照 CSV -----------------------------------------
    def test_confirm_once_and_mapping(self):
        self.upload_first()
        # 未确认前下载 -> 409
        self.assertEqual(self.client.get("/mapping.csv").status_code, 409)

        resp = self.client.post("/api/key/confirm", json={"columns": ["缩微号"]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

        again = self.client.post("/api/key/confirm", json={"mask": 16})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.get_json()["code"], "key_already_set")

        csv_resp = self.client.get("/mapping.csv")
        self.assertEqual(csv_resp.status_code, 200)
        text = csv_resp.get_data(as_text=True).lstrip("﻿")
        reader = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(reader), 6)
        self.assertIn("原件号", reader[0])
        mapping = {r["原件号"]: r["缩微号"] for r in reader}
        self.assertEqual(mapping["YW-0001"], "WX-A0001")
        self.assertEqual(mapping["YW-0006"], "WX-C0001")

    # 4. 互换键值允许入库 --------------------------------------------
    def test_swap_two_rows_allowed(self):
        self.upload_first()
        self.client.post("/api/key/confirm", json={"mask": 16})
        swap_rows = [
            ["YW-0001", "G01", "J1001", "1998", "001", "WX-A0002"],
            ["YW-0002", "G01", "J1001", "1998", "002", "WX-A0001"],
        ]
        resp = self.upload(csv_bytes(HEADER, swap_rows), "swap.csv")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        stats = resp.get_json()["stats"]
        self.assertEqual(stats["detail_reads"], 2)
        self.assertEqual(stats["index_reads"], 2)
        self.assertEqual(stats["index_writes"], 2)

        mapping = {r["original_id"]: r["key"]
                   for r in self.client.get("/api/mapping").get_json()["rows"]}
        self.assertEqual(mapping["YW-0001"], "WX-A0002")
        self.assertEqual(mapping["YW-0002"], "WX-A0001")

    # 5. 与未涉及旧行撞键 -> 报双方并回滚 -----------------------------
    def test_collision_with_existing_reports_both(self):
        self.upload_first()
        self.client.post("/api/key/confirm", json={"mask": 16})
        bad = [["YW-0003", "G01", "J1002", "1999", "001", "WX-B0001"]]
        resp = self.upload(csv_bytes(HEADER, bad), "bad.csv")
        self.assertEqual(resp.status_code, 422)
        ids = resp.get_json()["report"]["involved_ids"]
        self.assertIn("YW-0003", ids)
        self.assertIn("YW-0004", ids)
        # 整批退回：YW-0003 键值不变
        mapping = {r["original_id"]: r["key"]
                   for r in self.client.get("/api/mapping").get_json()["rows"]}
        self.assertEqual(mapping["YW-0003"], "WX-A0003")

    # 6. 批内撞键 -----------------------------------------------------
    def test_within_batch_collision_rejected(self):
        self.upload_first()
        self.client.post("/api/key/confirm", json={"mask": 16})
        bad = [
            ["YW-0004", "G02", "J1001", "2001", "001", "DUP"],
            ["YW-0005", "G02", "J1002", "2001", "002", "DUP"],
        ]
        resp = self.upload(csv_bytes(HEADER, bad))
        self.assertEqual(resp.status_code, 422)
        ids = resp.get_json()["report"]["involved_ids"]
        self.assertIn("YW-0004", ids)
        self.assertIn("YW-0005", ids)

    # 7. 缺值退批 -----------------------------------------------------
    def test_missing_value_rejected(self):
        self.upload_first()
        self.client.post("/api/key/confirm", json={"mask": 16})
        bad = [["YW-0006", "G03", "J2001", "2010", "001", ""]]
        resp = self.upload(csv_bytes(HEADER, bad))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.get_json()["report"]["involved_ids"], ["YW-0006"])

    # 8. 表头不一致退批 ----------------------------------------------
    def test_header_mismatch_rejected(self):
        self.upload_first()
        bad_header = ["原件号", "馆代码", "卷号", "拍摄年", "页号", "别的号"]
        row = ["YW-0001", "G01", "J1001", "1998", "001", "X"]
        resp = self.upload(csv_bytes(bad_header, [row]))
        self.assertEqual(resp.status_code, 422)
        self.assertIn("missing_headers", resp.get_json()["report"])

    # 9. 新原件号补传 -> 索引总条数增长 -------------------------------
    def test_new_id_grows_total(self):
        self.upload_first()
        self.client.post("/api/key/confirm", json={"mask": 16})
        new_row = [["YW-0099", "G09", "J9999", "2024", "009", "WX-NEW99"]]
        resp = self.upload(csv_bytes(HEADER, new_row))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        stats = self.client.get("/api/stats").get_json()
        self.assertEqual(stats["index_total"], 7)
        self.assertEqual(stats["key_index_total"], 7)

    # 10. HTML 表单全流程：候选渲染 / 确认 / 退批显示坏号 -------------
    def test_html_flow(self):
        data = csv_bytes(HEADER, self.first_rows())
        resp = self.client.post(
            "/upload", data={"file": (io.BytesIO(data), "first.csv")},
            content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("最简候选键", html)
        self.assertIn('name="mask"', html)
        self.assertIn('id="index-total"', html)
        self.assertIn(">6<", html)

        # 以原生表单方式确认 mask=16（缩微号）
        resp = self.client.post("/confirm", data={"mask": "16"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("current-key", html)
        self.assertIn("download-mapping", html)

        # 重复确认 -> 409 页面
        resp = self.client.post("/confirm", data={"mask": "1"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("只能选择一次", resp.get_data(as_text=True))

        # 冲突补传 -> 422 HTML 且包含涉及原件号
        bad = csv_bytes(HEADER, [["YW-0003", "G01", "J1002", "1999", "001", "WX-B0001"]])
        resp = self.client.post(
            "/upload", data={"file": (io.BytesIO(bad), "bad.csv")},
            content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 422)
        html = resp.get_data(as_text=True)
        self.assertIn("YW-0003", html)
        self.assertIn("YW-0004", html)
        self.assertIn("整批退回", html)
        self.assertIn('id="batch-index-stats"', html)

    # 11. 超过 255 行的大数据：候选仍正确，汇总读取仍 <=255 -----------
    def test_large_dataset_correctness(self):
        header = ["原件号", "馆", "卷", "年", "页", "唯一码"]
        rows = []
        for i in range(420):
            # (年,页) 设计为唯一；馆/卷大量重复；唯一码唯一
            rows.append([f"O{i:05d}", f"G{i % 7}", f"J{i % 13}",
                         f"{1990 + i % 40}", f"P{i:05d}", f"U-{i:06d}"])
        self.upload(csv_bytes(header, rows))
        api = self.client.get("/api/candidates").get_json()
        self.assertLessEqual(api["summary_rows_read"], 255)
        self.assertEqual(api["summary_rows_read"], 31)  # 5 个候选列 -> 2^5-1
        records = [dict(zip(header[1:], r[1:])) for r in rows]
        expected = brute_force_candidates(records, header[1:])
        got = {tuple(c["columns"]) for c in api["candidates"]}
        self.assertEqual(got, expected)
        # 唯一码必须是候选
        self.assertIn(("唯一码",), got)

    # 12. GB18030 编码也能上传 ---------------------------------------
    def test_gbk_encoding(self):
        self.upload(csv_bytes(HEADER, self.first_rows(), encoding="gb18030"))
        api = self.client.get("/api/candidates").get_json()
        self.assertTrue(any(c["columns"] == ["缩微号"] for c in api["candidates"]))

    # 13. BOM UTF-8 ---------------------------------------------------
    def test_utf8_bom(self):
        self.upload(csv_bytes(HEADER, self.first_rows(), encoding="utf-8-sig"))
        ctx = self.client.get("/api/stats").get_json()
        self.assertEqual(ctx["index_total"], 6)

    # 14. 列数越界 ----------------------------------------------------
    def test_too_few_columns_rejected(self):
        header = ["原件号", "a", "b", "唯一"]  # 3 待选列
        rows = [["1", "x", "y", "u1"], ["2", "x2", "y2", "u2"]]
        resp = self.upload(csv_bytes(header, rows))
        self.assertEqual(resp.status_code, 422)

    # 15. 纯函数：缺值列不可能进候选 ---------------------------------
    def test_evaluate_subsets_missing(self):
        rows = [{"a": "1", "b": "x"}, {"a": "", "b": "y"}]
        summary = evaluate_subsets(rows, ["a", "b"])
        by_mask = {s["mask"]: s for s in summary}
        self.assertFalse(by_mask[1]["complete"])   # a
        self.assertTrue(by_mask[2]["complete"])    # b
        self.assertTrue(by_mask[2]["is_candidate"])

    # 16. 复合交接键：两整组键值互换也应入库 --------------------------
    def test_composite_key_swap(self):
        self.upload_first()
        # 用中文顿号描述符确认（拍摄年、页号）
        resp = self.client.post("/confirm", data={"key": "拍摄年、页号"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        swap_rows = [
            ["YW-0001", "G01", "J1001", "1998", "002", "WX-A0001"],
            ["YW-0002", "G01", "J1001", "1998", "001", "WX-A0002"],
        ]
        resp = self.upload(csv_bytes(HEADER, swap_rows), "cswap.csv")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        mapping = {r["original_id"]: r["key"]
                   for r in self.client.get("/api/mapping").get_json()["rows"]}
        self.assertEqual(mapping["YW-0001"], "1998 | 002")
        self.assertEqual(mapping["YW-0002"], "1998 | 001")

    # 17. 复合键批内撞键报多个原件号 ---------------------------------
    def test_composite_collision(self):
        self.upload_first()
        self.client.post("/api/key/confirm", json={"columns": ["拍摄年", "页号"]})
        bad = [
            ["YW-0004", "G02", "J1001", "1999", "001", "WX-B0001"],
            ["YW-0005", "G02", "J1002", "1999", "001", "WX-B0002"],
        ]
        resp = self.upload(csv_bytes(HEADER, bad))
        self.assertEqual(resp.status_code, 422)
        ids = resp.get_json()["report"]["involved_ids"]
        self.assertIn("YW-0004", ids)
        self.assertIn("YW-0005", ids)

    # 18. reset 接口 --------------------------------------------------
    def test_reset(self):
        self.upload_first()
        self.assertEqual(self.client.get("/api/stats").get_json()["index_total"], 6)
        resp = self.client.post("/api/admin/reset")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/api/stats").get_json()["index_total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
