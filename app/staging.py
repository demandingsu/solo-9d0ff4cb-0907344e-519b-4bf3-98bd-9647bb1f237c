"""待结清暂存：单据未到齐的批次先暂存，按仓次关联成组，确认后整组入账。

业务规则
--------
1. 沿用上传页的三份 CSV（入库 / 转仓 / 交货）；单据还没到的文件可以只传表头。
2. 同一批原样重传只记一次（按内容哈希去重，幂等）。
3. 两批暂存单据只要涉及同一仓次，它们所在的组就并在一起（按仓次连通分组）。
4. 网页按组列出批次和各仓次的箱数差额（收入 - 支出）。
5. 库管按所见分组确认：确认时把整组合并，套用现有台账校验规则，
   全部通过才整组入账（此时才计入核对包汇总）；任一不满足则整组退回，
   原账和暂存资料都不变。
6. 确认以页面所示的组成员为准：入账前有补报并入该组，本次确认退回并提示刷新；
   只补报别组不影响本组确认。
"""
from collections import defaultdict
from datetime import datetime, timezone

from . import ledger


class GroupChanged(Exception):
    """确认时组的成员已变化（有补报并入或已入账）：退回本次确认，提示刷新。"""


# ---------------------------------------------------------------- 暂存

def stage_batch(conn, digest, batch):
    """暂存一批，返回 (staged_id, created)。

    同一批文件原样重传：created=False，不重复记录（幂等）。
    文件内容已直接入账的批次拒绝暂存。
    """
    row = conn.execute(
        "SELECT id FROM staged_uploads WHERE batch_hash = ?", (digest,)
    ).fetchone()
    if row:
        return row["id"], False
    posted = conn.execute(
        "SELECT id FROM uploads WHERE batch_hash = ?", (digest,)
    ).fetchone()
    if posted:
        raise ledger.BatchRejected(["该批文件内容已直接入账，无需暂存；请勿重复提交"])

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO staged_uploads"
                " (batch_hash, uploaded_at, inbound_count, transfer_count, delivery_count)"
                " VALUES (?, ?, ?, ?, ?)",
                (digest, now, len(batch.inbound), len(batch.transfers), len(batch.deliveries)),
            )
            staged_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO staged_inbound (staged_id, session, orchard, crates) VALUES (?, ?, ?, ?)",
                [(staged_id, r["session"], r["orchard"], r["crates"]) for r in batch.inbound],
            )
            conn.executemany(
                "INSERT INTO staged_transfers (staged_id, from_session, to_session, crates) VALUES (?, ?, ?, ?)",
                [(staged_id, t["from_session"], t["to_session"], t["crates"]) for t in batch.transfers],
            )
            conn.executemany(
                "INSERT INTO staged_deliveries (staged_id, session, kind, destination, crates)"
                " VALUES (?, ?, ?, ?, ?)",
                [(staged_id, d["session"], d["kind"], d["destination"], d["crates"]) for d in batch.deliveries],
            )
    except Exception as exc:
        # 并发重传撞哈希：按“只记一次”处理
        if "UNIQUE" in str(exc) and "batch_hash" in str(exc):
            row = conn.execute(
                "SELECT id FROM staged_uploads WHERE batch_hash = ?", (digest,)
            ).fetchone()
            if row:
                return row["id"], False
        raise
    return staged_id, True


# ---------------------------------------------------------------- 分组

def staged_groups(conn):
    """当前暂存批次按仓次连通分组。

    返回按组内最小批次号排序的列表，每组：
      batch_ids  组内批次号（升序）——确认令牌即据此生成
      batches    批次明细（暂存时间、三类行数）
      sessions   各仓次的果园、收入、支出、差额（收入 - 支出）
      balanced   组内所有仓次是否收发平衡
      token      确认表单携带的组成员令牌
    """
    uploads = conn.execute(
        "SELECT id, batch_hash, uploaded_at, inbound_count, transfer_count, delivery_count"
        " FROM staged_uploads ORDER BY id"
    ).fetchall()
    if not uploads:
        return []

    uf = ledger._UnionFind()
    by_session = defaultdict(list)
    sessions_of = defaultdict(set)
    for r in conn.execute("SELECT staged_id, session FROM staged_inbound"):
        sessions_of[r["staged_id"]].add(r["session"])
    for r in conn.execute("SELECT staged_id, from_session, to_session FROM staged_transfers"):
        sessions_of[r["staged_id"]].update((r["from_session"], r["to_session"]))
    for r in conn.execute("SELECT staged_id, session FROM staged_deliveries"):
        sessions_of[r["staged_id"]].add(r["session"])
    for u in uploads:
        uf.find(u["id"])
        for s in sessions_of[u["id"]]:
            by_session[s].append(u["id"])
    # 涉及同一仓次的批次并为一组
    for ids in by_session.values():
        for other in ids[1:]:
            uf.union(ids[0], other)

    inflow = defaultdict(lambda: defaultdict(int))    # 组根 -> 仓次 -> 收入
    outflow = defaultdict(lambda: defaultdict(int))   # 组根 -> 仓次 -> 支出
    orchards = defaultdict(lambda: defaultdict(set))  # 组根 -> 仓次 -> 果园集合
    for r in conn.execute("SELECT staged_id, session, orchard, crates FROM staged_inbound"):
        root = uf.find(r["staged_id"])
        inflow[root][r["session"]] += r["crates"]
        orchards[root][r["session"]].add(r["orchard"])
    for r in conn.execute("SELECT staged_id, from_session, to_session, crates FROM staged_transfers"):
        root = uf.find(r["staged_id"])
        outflow[root][r["from_session"]] += r["crates"]
        inflow[root][r["to_session"]] += r["crates"]
    for r in conn.execute("SELECT staged_id, session, crates FROM staged_deliveries"):
        root = uf.find(r["staged_id"])
        outflow[root][r["session"]] += r["crates"]

    groups = defaultdict(list)
    for u in uploads:
        groups[uf.find(u["id"])].append(dict(u))

    result = []
    for root, batches in groups.items():
        batches.sort(key=lambda b: b["id"])
        sessions = []
        for s in sorted(set(inflow[root]) | set(outflow[root])):
            pinned = sorted(orchards[root].get(s, set()))
            if len(pinned) == 1:
                orchard = pinned[0]
            elif pinned:
                orchard = "、".join(pinned) + "（冲突）"
            else:
                orchard = "—"
            sessions.append(
                {
                    "session": s,
                    "orchard": orchard,
                    "inflow": inflow[root][s],
                    "outflow": outflow[root][s],
                    "diff": inflow[root][s] - outflow[root][s],
                }
            )
        batch_ids = [b["id"] for b in batches]
        result.append(
            {
                "batch_ids": batch_ids,
                "batches": batches,
                "sessions": sessions,
                "balanced": all(row["diff"] == 0 for row in sessions),
                "token": ",".join(str(i) for i in batch_ids),
            }
        )
    result.sort(key=lambda g: g["batch_ids"][0])
    return result


def find_group(conn, batch_ids):
    """当前分组中与 batch_ids 完全一致的组；没有（已有变动）返回 None。"""
    want = sorted(batch_ids)
    for g in staged_groups(conn):
        if g["batch_ids"] == want:
            return g
    return None


def staged_counts(conn):
    """总览用：暂存批次数与组数。"""
    batches = conn.execute("SELECT COUNT(*) AS c FROM staged_uploads").fetchone()["c"]
    groups = len(staged_groups(conn)) if batches else 0
    return {"batches": batches, "groups": groups}


# ---------------------------------------------------------------- 确认入账

def merged_batch(conn, batch_ids):
    """把一组暂存批次的行合并成 ledger.Batch（按批次号、原行序）。"""
    batch = ledger.Batch()
    for bid in batch_ids:
        for r in conn.execute(
            "SELECT session, orchard, crates FROM staged_inbound WHERE staged_id = ? ORDER BY id",
            (bid,),
        ):
            batch.inbound.append({"session": r["session"], "orchard": r["orchard"], "crates": r["crates"]})
        for r in conn.execute(
            "SELECT from_session, to_session, crates FROM staged_transfers WHERE staged_id = ? ORDER BY id",
            (bid,),
        ):
            batch.transfers.append(
                {"from_session": r["from_session"], "to_session": r["to_session"], "crates": r["crates"]}
            )
        for r in conn.execute(
            "SELECT session, kind, destination, crates FROM staged_deliveries WHERE staged_id = ? ORDER BY id",
            (bid,),
        ):
            batch.deliveries.append(
                {"session": r["session"], "kind": r["kind"], "destination": r["destination"], "crates": r["crates"]}
            )
    return batch


def post_group(conn, batch_ids, session_orchard):
    """整组入账并移出待结清列表（单事务；失败则原账和暂存照旧）。

    组成员在确认前已变动抛 GroupChanged；与已入账内容冲突抛 BatchRejected。
    """
    marks = ",".join("?" for _ in batch_ids)
    uploads = conn.execute(
        f"SELECT id, batch_hash, inbound_count, transfer_count, delivery_count"
        f" FROM staged_uploads WHERE id IN ({marks}) ORDER BY id",
        list(batch_ids),
    ).fetchall()
    if len(uploads) != len(batch_ids):
        raise GroupChanged()

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with conn:
            for u in uploads:
                cur = conn.execute(
                    "INSERT INTO uploads"
                    " (batch_hash, uploaded_at, inbound_count, transfer_count, delivery_count)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (u["batch_hash"], now, u["inbound_count"], u["transfer_count"], u["delivery_count"]),
                )
                upload_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO inbound (upload_id, session, orchard, crates)"
                    " SELECT ?, session, orchard, crates FROM staged_inbound WHERE staged_id = ?",
                    (upload_id, u["id"]),
                )
                conn.execute(
                    "INSERT INTO transfers (upload_id, from_session, to_session, crates)"
                    " SELECT ?, from_session, to_session, crates FROM staged_transfers WHERE staged_id = ?",
                    (upload_id, u["id"]),
                )
                conn.execute(
                    "INSERT INTO deliveries (upload_id, session, kind, destination, crates)"
                    " SELECT ?, session, kind, destination, crates FROM staged_deliveries WHERE staged_id = ?",
                    (upload_id, u["id"]),
                )
            conn.executemany(
                "INSERT INTO sessions (name, orchard) VALUES (?, ?)",
                [(s, o) for s, o in sorted(session_orchard.items()) if o is not None],
            )
            conn.execute(f"DELETE FROM staged_inbound WHERE staged_id IN ({marks})", list(batch_ids))
            conn.execute(f"DELETE FROM staged_transfers WHERE staged_id IN ({marks})", list(batch_ids))
            conn.execute(f"DELETE FROM staged_deliveries WHERE staged_id IN ({marks})", list(batch_ids))
            conn.execute(f"DELETE FROM staged_uploads WHERE id IN ({marks})", list(batch_ids))
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise ledger.BatchRejected(
                ["该组与已入账内容冲突（仓次或批次文件已入账），本次确认未入账，暂存资料保留"]
            ) from exc
        raise
