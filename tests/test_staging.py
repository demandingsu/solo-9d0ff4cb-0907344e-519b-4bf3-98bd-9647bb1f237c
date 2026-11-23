"""待结清暂存：暂存、分组、确认入账 与 确认/补报交错。"""
import re

from tests.conftest import csv_bytes, download_package, upload_good_batch

HDR_I = "仓次,果园,箱数\n"
HDR_T = "起仓次,讫仓次,箱数\n"
HDR_D = "仓次,类型,去向,箱数\n"


def stage(client, inbound=HDR_I, transfers=HDR_T, deliveries=HDR_D):
    return client.post(
        "/staging",
        data={
            "inbound": (csv_bytes(inbound), "inbound.csv"),
            "transfers": (csv_bytes(transfers), "transfers.csv"),
            "deliveries": (csv_bytes(deliveries), "deliveries.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def confirm(client, members):
    return client.post("/staging/confirm", data={"members": members}, follow_redirects=True)


def group_tokens(client):
    """当前待结清列表里各确认表单携带的组成员令牌。"""
    body = client.get("/staging").get_data(as_text=True)
    return re.findall(r'name="members" value="([0-9,]+)"', body)


def test_stage_header_only_and_dedup(client):
    # 交货单、转仓单还没到：只传表头
    resp = stage(client, inbound=HDR_I + "S1,果园A,100\n")
    assert "已暂存为待结清批次 #1" in resp.get_data(as_text=True)

    body = client.get("/staging").get_data(as_text=True)
    assert "第 1 组" in body
    assert "S1" in body and "+100" in body  # 只入未出，差额 +100

    # 同一批原样重传只记一次
    resp = stage(client, inbound=HDR_I + "S1,果园A,100\n")
    body = resp.get_data(as_text=True)
    assert "只记一次" in body
    assert group_tokens(client) == ["1"]

    # 暂存不计入总览与核对包
    index = client.get("/").get_data(as_text=True)
    assert "待结清暂存 1 批" in index
    assert "已入账批次" in index
    resp = client.get("/packages/果园A/download")
    assert resp.status_code == 302  # 没有可入账汇总，无法生成核对包


def test_all_header_only_rejected(client):
    resp = stage(client)
    body = resp.get_data(as_text=True)
    assert "三个 CSV 都没有有效数据行" in body
    assert group_tokens(client) == []


def test_stage_bad_rows_rejected_nothing_kept(client):
    resp = stage(client, inbound=HDR_I + "S1,果园A,10.5\n")
    body = resp.get_data(as_text=True)
    assert "整箱" in body
    assert "本次暂存已退回" in body
    assert group_tokens(client) == []


def test_stage_already_posted_content_rejected(client):
    upload_good_batch(client)
    from tests.conftest import GOOD_DELIVERIES, GOOD_INBOUND, GOOD_TRANSFERS

    resp = stage(client, GOOD_INBOUND, GOOD_TRANSFERS, GOOD_DELIVERIES)
    assert "已直接入账" in resp.get_data(as_text=True)
    assert group_tokens(client) == []


def test_groups_merge_on_shared_session(client):
    stage(client, inbound=HDR_I + "S1,果园A,100\n")                       # 批1
    stage(client, deliveries=HDR_D + "S1,交货,市场甲,60\n")               # 批2，与批1共享 S1
    stage(client, deliveries=HDR_D + "S9,交货,市场乙,5\n")                # 批3，独立一组
    assert group_tokens(client) == ["1,2", "3"]
    body = client.get("/staging").get_data(as_text=True)
    assert "第 1 组" in body and "第 2 组" in body
    assert "+40" in body  # S1：入库100 - 交货60


def test_confirm_balanced_group_posts_and_leaves_list(client):
    stage(client, inbound=HDR_I + "S1,果园A,100\n")
    stage(client, deliveries=HDR_D + "S1,交货,市场甲,60\nS1,余箱,仓存,40\n")
    assert group_tokens(client) == ["1,2"]

    resp = confirm(client, "1,2")
    body = resp.get_data(as_text=True)
    assert "结清入账成功" in body
    assert "没有待结清的暂存批次" in body  # 整组从待结清列表移走

    # 此时才计入核对包汇总
    pkg = download_package(client, "果园A")
    entries = {e["destination"]: e["crates"] for e in pkg["entries"]}
    assert entries == {"市场甲": 60, "仓存": 40}
    index = client.get("/").get_data(as_text=True)
    assert "待结清暂存" not in index


def test_confirm_unbalanced_group_fails_nothing_changes(client):
    stage(client, inbound=HDR_I + "S1,果园A,100\n")
    stage(client, deliveries=HDR_D + "S1,交货,市场甲,60\n")
    resp = confirm(client, "1,2")
    body = resp.get_data(as_text=True)
    assert "仓次 S1 收发有差额" in body
    assert "确认未通过" in body
    # 原账和暂存资料照旧
    assert group_tokens(client) == ["1,2"]
    assert "台账为空" in client.get("/").get_data(as_text=True)


def test_stale_confirm_rejected_after_supplement_merges(client):
    stage(client, inbound=HDR_I + "S1,果园A,100\n")
    assert group_tokens(client) == ["1"]
    # 确认前补报：新批次涉及 S1，并入该组
    stage(client, deliveries=HDR_D + "S1,交货,市场甲,60\n")
    assert group_tokens(client) == ["1,2"]
    # 用旧分组确认 -> 退回并提示刷新
    resp = confirm(client, "1")
    body = resp.get_data(as_text=True)
    assert "请刷新列表后重新确认" in body
    # 原账和暂存照旧
    assert group_tokens(client) == ["1,2"]
    assert "台账为空" in client.get("/").get_data(as_text=True)


def test_supplement_to_other_group_does_not_block_confirm(client):
    stage(client, inbound=HDR_I + "S1,果园A,100\n")
    stage(client, deliveries=HDR_D + "S1,交货,市场甲,100\n")
    token = group_tokens(client)
    assert token == ["1,2"]
    # 只补报别组（S9 与 S1 无关）
    stage(client, inbound=HDR_I + "S9,果园B,50\n")
    assert group_tokens(client) == ["1,2", "3"]
    # 本组仍可确认
    resp = confirm(client, token[0])
    assert "结清入账成功" in resp.get_data(as_text=True)
    assert group_tokens(client) == ["3"]  # 别组仍在待结清


def test_confirm_then_package_counts_posted_only(client):
    stage(client, inbound=HDR_I + "S1,果园A,100\n")
    stage(client, deliveries=HDR_D + "S1,交货,市场甲,100\n")
    # 未确认：核对包不出库
    assert client.get("/packages/果园A/download").status_code == 302
    confirm(client, "1,2")
    pkg = download_package(client, "果园A")
    assert {e["destination"]: e["crates"] for e in pkg["entries"]} == {"市场甲": 100}


def test_interleaved_supplement_then_confirm_with_fresh_token(client):
    """补单结清全流程：分批到齐 -> 交错确认被退回 -> 刷新后确认成功。"""
    stage(client, inbound=HDR_I + "S1,果园A,100\nS3,果园B,80\n")          # 入库单到
    stage(client, transfers=HDR_T + "S1,S2,40\n")                          # 转仓单到
    stale = group_tokens(client)
    assert stale == ["1,2"]
    stage(client, deliveries=HDR_D + "S1,交货,市场甲,55\nS1,余箱,仓存,5\n")  # 交货单补报，并入同组
    # 交错：按旧分组确认被退回
    resp = confirm(client, stale[0])
    assert "请刷新列表后重新确认" in resp.get_data(as_text=True)
    # 刷新后按新分组确认：S2/S3 仍有差额，确认失败且资料照旧
    token = group_tokens(client)
    assert token == ["1,2,3"]
    resp = confirm(client, token[0])
    body = resp.get_data(as_text=True)
    assert "确认未通过" in body and "差额" in body
    assert group_tokens(client) == ["1,2,3"]
    # 继续补报剩余交货单，整组平衡后确认成功
    stage(
        client,
        deliveries=HDR_D + "S2,交货,市场乙,40\nS3,交货,市场甲,30\nS3,交货,市场丙,45\nS3,余箱,仓存,5\n",
    )
    token = group_tokens(client)
    assert token == ["1,2,3,4"]
    resp = confirm(client, token[0])
    assert "结清入账成功" in resp.get_data(as_text=True)
    assert group_tokens(client) == []
    # 两个果园的核对包都计入
    pkg_a = download_package(client, "果园A")
    assert {e["destination"]: e["crates"] for e in pkg_a["entries"]} == {
        "市场甲": 55,
        "市场乙": 40,
        "仓存": 5,
    }
    pkg_b = download_package(client, "果园B")
    assert {e["destination"]: e["crates"] for e in pkg_b["entries"]} == {
        "市场甲": 30,
        "市场丙": 45,
        "仓存": 5,
    }


def test_confirm_fails_when_session_posted_directly_meanwhile(client):
    """暂存期间同仓次被直接上传入账：确认失败，原账与暂存照旧。"""
    from tests.conftest import upload_batch

    stage(client, inbound=HDR_I + "S1,果园A,100\n")
    stage(client, deliveries=HDR_D + "S1,交货,市场甲,100\n")
    assert group_tokens(client) == ["1,2"]
    # 另一库管把含 S1 的批次直接上传入账（直接上传不看暂存）
    resp = upload_batch(
        client,
        inbound=HDR_I + "S1,果园A,10\n",
        transfers=HDR_T,
        deliveries=HDR_D + "S1,交货,市场甲,10\n",
    )
    assert resp.status_code == 302
    # 确认暂存组：S1 已入账 -> 确认失败，暂存资料照旧
    resp = confirm(client, "1,2")
    body = resp.get_data(as_text=True)
    assert "已入账" in body
    assert "确认未通过" in body
    assert group_tokens(client) == ["1,2"]


def test_post_group_raises_group_changed_for_missing_member(app):
    """post_group 复核：组成员在确认前被移走则抛 GroupChanged（兜底）。"""
    import pytest

    from app import ledger, staging
    from app.db import get_db

    with app.app_context():
        conn = get_db()
        batch = ledger.parse_batch(
            (HDR_I + "S1,果园A,10\n").encode(), HDR_T.encode(), HDR_D.encode()
        )
        staged_id, created = staging.stage_batch(conn, "hash-x", batch)
        assert created
        with pytest.raises(staging.GroupChanged):
            staging.post_group(conn, [staged_id, 999], {"S1": "果园A"})
        # 未入账，暂存照旧
        assert staging.staged_counts(conn)["batches"] == 1
