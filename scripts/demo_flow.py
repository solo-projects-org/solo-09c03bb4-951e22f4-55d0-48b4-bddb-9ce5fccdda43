"""网页端演示脚本：上传样例、登记录像核实、演示无解登记。

优先使用已启动的服务（默认 http://127.0.0.1:8000）。若未启动，可用：

    docker compose up --build          # 推荐
    # 或本地：PM_DB_PATH=./plusminus.db uvicorn app.main:app --port 8000

然后执行：

    python -m scripts.demo_flow
    python -m scripts.demo_flow --base-url http://127.0.0.1:8011

脚本只通过公开 HTTP 接口操作，等价于在网页上点按，不会写入本地数据库。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid

SAMPLES = (
    ("basic", "samples/subs.csv", "samples/scores.csv"),
    ("dense", "samples/subs_dense.csv", "samples/scores_dense.csv"),
    ("scorer", "samples/subs_scorer.csv", "samples/scores_scorer.csv"),
)


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----pm" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    for name, (filename, data) in files.items():
        out += f"--{boundary}\r\n".encode()
        out += (
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\n'
        ).encode()
        out += b"Content-Type: text/csv\r\n\r\n"
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def request(base: str, method: str, path: str, **kwargs):
    body = kwargs.pop("body", None)
    ctype = kwargs.pop("content_type", None)
    headers = kwargs.pop("headers", {})
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(
        base + path, data=body, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"detail": raw.decode("utf-8", "replace")}


def upload(base: str, name: str, sub_path: str, score_path: str):
    body, ctype = _multipart(
        {"name": name, "starters": "A1,A2,A3,A4,A5"},
        {
            "sub_file": (sub_path.rsplit("/", 1)[-1], open(sub_path, "rb").read()),
            "score_file": (
                score_path.rsplit("/", 1)[-1],
                open(score_path, "rb").read(),
            ),
        },
    )
    return request(
        base, "POST", "/api/games", body=body, content_type=ctype
    )


def verify(base: str, game_id: int, payload: dict):
    return request(
        base,
        "POST",
        f"/api/games/{game_id}/verifications",
        body=json.dumps(payload).encode(),
        content_type="application/json",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="网页演示流程")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args(argv)
    base = args.base_url.rstrip("/")

    print(f"服务地址：{base}")
    status, games = request(base, "GET", "/api/games")
    if status != 200:
        print(f"无法访问服务（{status}）：{games}")
        return 1
    print(f"当前比赛数：{len(games)}")

    for label, sub_path, score_path in SAMPLES:
        status, body = upload(base, f"演示-{label}", sub_path, score_path)
        if status != 200:
            print(f"[{label}] 上传失败 {status}：{body}")
            return 1
        game_id = body["id"]
        result = body["result"]
        print(
            f"[{label}] 上传成功 id={game_id}，合法顺序 {result['total_legal_orders']}，"
            f"歧义组 {sum(1 for g in result['groups'] if g['ambiguous'])} 个"
        )

        if label == "dense":
            group = result["groups"][0]
            # 第 3 笔换人先于第 1 笔得分：把 14 种合法交错收敛为 1 种。
            payload = {
                "period": group["period"],
                "remaining": group["remaining"],
                "first_table": "sub",
                "first_row": group["sub_rows"][2],
                "second_table": "score",
                "second_row": group["score_rows"][0],
                "note": "录像：第 3 笔换人先于第 1 笔得分",
            }
            status, body = verify(base, game_id, payload)
            if status == 200:
                print(
                    f"[{label}] 核实生效，合法顺序变为 "
                    f"{body['result']['total_legal_orders']}"
                )
                narrowed = body["result"]["total_legal_orders"]
            else:
                narrowed = None
                print(f"[{label}] 核实失败 {status}：{body}")

            # 另登记一条与已有核实时序矛盾的新核实：应 409，且旧核实与结果不变。
            # 这里反向登记下一对（得分 1 先于换人 3），与上面的方向直接冲突。
            conflict = {
                "period": group["period"],
                "remaining": group["remaining"],
                "first_table": "score",
                "first_row": group["score_rows"][0],
                "second_table": "sub",
                "second_row": group["sub_rows"][2],
                "note": "录像：故意登记反向顺序",
            }
            status, body = verify(base, game_id, conflict)
            print(
                f"[{label}] 反向核实返回 {status}；原因：{body.get('detail')}"
            )
            print(
                f"[{label}] 阻塞记录：{json.dumps(body.get('blocking'), ensure_ascii=False)}"
            )
            _, detail = request(base, "GET", f"/api/games/{game_id}")
            print(
                f"[{label}] 回滚后核实数 {len(detail['verifications'])}，"
                f"合法顺序 {detail['result']['total_legal_orders']}"
                f"（核实生效后为 {narrowed}）"
            )

        if label == "scorer":
            # 得分者 B2 要等第 2 笔换人才上场。若核实声称该得分先于换人，
            # 则无解：接口返回 409，指出节次/时刻/阻塞行，且不新增核实。
            status, body = verify(
                base,
                game_id,
                {
                    "period": 1,
                    "remaining": 600,
                    "first_table": "score",
                    "first_row": 1,
                    "second_table": "sub",
                    "second_row": 1,
                    "note": "录像：误判该得分先于换人",
                },
            )
            print(
                f"[{label}] 无解核实返回 {status}；原因：{body.get('detail')}"
            )
            print(
                f"[{label}] 阻塞记录：{json.dumps(body.get('blocking'), ensure_ascii=False)}"
            )
            _, detail = request(base, "GET", f"/api/games/{game_id}")
            print(
                f"[{label}] 回滚后核实数 {len(detail['verifications'])}，"
                f"合法顺序 {detail['result']['total_legal_orders']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
