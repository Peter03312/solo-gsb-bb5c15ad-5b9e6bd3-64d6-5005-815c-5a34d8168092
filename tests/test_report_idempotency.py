"""重复上报幂等：已受理回执重放、同令牌冲突、失效令牌不留回执。"""
from tests.conftest import make_workflow, t


def submit(client, tasks, **kw):
    body = make_workflow(tasks, key=kw.pop("key", "wf"), **kw)
    return client.post("/workflows", json=body).json()


def claim(client, wf_id):
    resp = client.post(f"/workflows/{wf_id}/claim", json={})
    if resp.status_code == 204:
        return None
    assert resp.status_code == 200, resp.text
    return resp.json()


def status_map(wf):
    return {x["key"]: x["status"] for x in wf["tasks"]}


def test_duplicate_succeed_replays_original_response(client):
    wf = submit(client, [t("a"), t("b", ["a"])], key="rep-succ")
    lease = claim(client, wf["id"])

    body = {"token": lease["token"], "result": "r-1"}
    first = client.post(f"/tasks/{lease['task_id']}/succeed", json=body)
    assert first.status_code == 200

    # 连接断开后重发相同内容：原状态码与响应体，状态不再变化
    for _ in range(3):
        again = client.post(f"/tasks/{lease['task_id']}/succeed", json=body)
        assert again.status_code == 200
        assert again.json() == first.json()

    detail = client.get(f"/workflows/{wf['id']}").json()
    a = next(x for x in detail["tasks"] if x["key"] == "a")
    assert a["status"] == "succeeded"
    assert a["attempts"] == 1
    # 依赖只被解锁一次：接下来可领取的是 b
    follow = claim(client, wf["id"])
    assert follow["key"] == "b"


def test_duplicate_fail_replays_without_extra_attempt_or_cascade(client):
    wf = submit(
        client,
        [t("a", max_attempts=1), t("b", ["a"])],
        key="rep-fail",
        max_attempts=3,
    )
    lease = claim(client, wf["id"])

    body = {"token": lease["token"], "error": "boom"}
    first = client.post(f"/tasks/{lease['task_id']}/fail", json=body)
    assert first.status_code == 200
    assert first.json()["status"] == "failed"

    # 重发相同失败上报：响应一致，不重复级联、不增加尝试次数
    again = client.post(f"/tasks/{lease['task_id']}/fail", json=body)
    assert again.status_code == 200
    assert again.json() == first.json()

    detail = client.get(f"/workflows/{wf['id']}").json()
    assert status_map(detail) == {"a": "failed", "b": "skipped"}
    a = next(x for x in detail["tasks"] if x["key"] == "a")
    assert a["attempts"] == 1
    assert a["error"] == "boom"


def test_same_token_changed_type_conflicts(client):
    wf = submit(client, [t("a")], key="rep-type")
    lease = claim(client, wf["id"])
    ok = client.post(
        f"/tasks/{lease['task_id']}/succeed",
        json={"token": lease["token"], "result": "r"},
    )
    assert ok.status_code == 200

    resp = client.post(
        f"/tasks/{lease['task_id']}/fail",
        json={"token": lease["token"], "error": "changed"},
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "report_conflict"
    assert err["accepted_type"] == "succeed"


def test_same_token_changed_content_conflicts(client):
    wf = submit(client, [t("a")], key="rep-content")
    lease = claim(client, wf["id"])
    ok = client.post(
        f"/tasks/{lease['task_id']}/succeed",
        json={"token": lease["token"], "result": "r1"},
    )
    assert ok.status_code == 200

    resp = client.post(
        f"/tasks/{lease['task_id']}/succeed",
        json={"token": lease["token"], "result": "r2"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "report_conflict"

    # fail 上报改动 error 同样冲突
    wf2 = submit(client, [t("x")], key="rep-content-2")
    lease2 = claim(client, wf2["id"])
    first = client.post(
        f"/tasks/{lease2['task_id']}/fail",
        json={"token": lease2["token"], "error": "e1"},
    )
    assert first.status_code == 200
    resp2 = client.post(
        f"/tasks/{lease2['task_id']}/fail",
        json={"token": lease2["token"], "error": "e2"},
    )
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "report_conflict"


def test_unaccepted_token_leaves_no_receipt(client):
    wf = submit(client, [t("a")], key="rep-invalid")
    lease = claim(client, wf["id"])

    # 错误令牌：拒绝且不落回执，重发仍按失效规则拒绝
    bad = {"token": "never-valid", "result": "r"}
    for _ in range(2):
        resp = client.post(f"/tasks/{lease['task_id']}/succeed", json=bad)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "invalid_token"

    # 工作流取消后，未受理过的旧令牌仍按现有规则拒绝
    client.post(f"/workflows/{wf['id']}/cancel")
    resp = client.post(
        f"/tasks/{lease['task_id']}/succeed",
        json={"token": lease["token"], "result": "r"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "invalid_token"


def test_receipt_replays_after_reclaim_and_workflow_terminal(client):
    wf = submit(client, [t("a")], key="rep-reclaim", max_attempts=3)
    lease1 = claim(client, wf["id"])
    body = {"token": lease1["token"], "error": "try-1"}
    first = client.post(f"/tasks/{lease1['task_id']}/fail", json=body)
    assert first.status_code == 200
    assert first.json()["status"] == "pending"

    # 任务被重领（新令牌）并最终成功，工作流终结
    lease2 = claim(client, wf["id"])
    assert lease2["token"] != lease1["token"]
    done = client.post(
        f"/tasks/{lease2['task_id']}/succeed",
        json={"token": lease2["token"]},
    )
    assert done.status_code == 200
    detail = client.get(f"/workflows/{wf['id']}").json()
    assert detail["status"] == "succeeded"

    # 旧令牌的已受理回执依然可重放：响应体仍是当时的 pending/attempts=1
    replay = client.post(f"/tasks/{lease1['task_id']}/fail", json=body)
    assert replay.status_code == 200
    assert replay.json() == first.json()

    # 回执按令牌隔离：新令牌重发自己的上报，读到的是自己的回执
    other = client.post(
        f"/tasks/{lease2['task_id']}/succeed",
        json={"token": lease2["token"]},
    )
    assert other.status_code == 200
    assert other.json() == done.json()
    assert other.json()["status"] == "succeeded"


def test_receipt_survives_restart(settings, client):
    from fastapi.testclient import TestClient

    from app.main import create_app

    wf = submit(client, [t("a")], key="rep-restart")
    lease = claim(client, wf["id"])
    body = {"token": lease["token"], "result": "persisted"}
    first = client.post(f"/tasks/{lease['task_id']}/succeed", json=body)
    assert first.status_code == 200

    # 模拟进程重启：回执已落库，仍可重放；改动内容仍冲突
    app2 = create_app()
    with TestClient(app2) as c2:
        replay = c2.post(f"/tasks/{lease['task_id']}/succeed", json=body)
        assert replay.status_code == 200
        assert replay.json() == first.json()

        conflict = c2.post(
            f"/tasks/{lease['task_id']}/succeed",
            json={"token": lease["token"], "result": "different"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "report_conflict"
