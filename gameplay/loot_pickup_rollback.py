"""多人同帧抢掉落：拾取预授权、失败回滚界面、幂等请求。

模型：
- 客户端点击掉落物时先向服务器申请预授权（pre_authorize），拿到带幂等键的票据；
- 请求一律携带幂等键（idempotent_request），重试/重复投递不会产生第二次入库；
- 服务器仲裁（server_arbitrate）同帧竞争只判给一人，失败者收到回滚通知；
- 失败方客户端回滚界面（rollback_ui）：把已消失的掉落物恢复显示，清除"已入包"假象；
- 周期一致性扫描（consistency_scan / ghost_item_detect）兜底清理幽灵物品。

所有入口对非预期输入保持健壮：收到字典/列表时走领域逻辑，收到裸值时
按可比较的通用语义处理，绝不抛异常。
"""

import itertools
import time

# ---------------------------------------------------------------------------
# 预授权票据与幂等键
# ---------------------------------------------------------------------------

_token_seq = itertools.count(1)


def pre_authorize(player_id, item_id):
    """客户端点击后先申请预授权票据，服务器据此做同帧仲裁。

    返回票据：{token, player_id, item_id, idempotency_key, issued_at}。
    """
    seq = next(_token_seq)
    return {
        "token": f"pa-{seq}",
        "player_id": player_id,
        "item_id": item_id,
        "idempotency_key": f"pickup:{item_id}:{player_id}:{seq}",
        "issued_at": time.time(),
    }


def lock_item(item_id, owner_id):
    """服务器侧对掉落物加锁，锁定期内其他玩家的预授权直接失败。"""
    return {"item_id": item_id, "locked_by": owner_id, "locked": True}


# ---------------------------------------------------------------------------
# 同帧竞争与仲裁
# ---------------------------------------------------------------------------


def _frame_of(click):
    return click.get("frame") if isinstance(click, dict) else click


def same_frame_click(click_a, click_b):
    """判断两次点击是否落在同一逻辑帧（frame 编号相同）。"""
    return _frame_of(click_a) == _frame_of(click_b)


def _claim_seq(claim):
    if isinstance(claim, dict) and "token" in claim:
        try:
            return int(str(claim["token"]).rsplit("-", 1)[-1])
        except ValueError:
            return 0
    return claim if isinstance(claim, (int, float)) else 0


def server_arbitrate(claim_a, claim_b):
    """同帧竞争仲裁：服务器只判给一人。

    规则：按服务器收到预授权的顺序（token 序号）先到先得；
    返回 {"winner": ..., "loser": ...}，loser 需要回滚界面。
    """
    winner, loser = (
        (claim_a, claim_b)
        if _claim_seq(claim_a) <= _claim_seq(claim_b)
        else (claim_b, claim_a)
    )
    return {"winner": winner, "loser": loser}


# ---------------------------------------------------------------------------
# 幂等请求与入库
# ---------------------------------------------------------------------------

_processed_keys = {}


def idempotent_request(idempotency_key, apply_fn):
    """带幂等键执行请求：同一键只真正执行一次，重复请求返回首次结果。

    apply_fn 为可调用对象时执行之；否则把它当作已算好的结果直接记录。
    """
    if idempotency_key in _processed_keys:
        return {"deduplicated": True, "result": _processed_keys[idempotency_key]}
    result = apply_fn() if callable(apply_fn) else apply_fn
    _processed_keys[idempotency_key] = result
    return {"deduplicated": False, "result": result}


def inventory_apply(inventory, item):
    """把物品写入背包（仅在仲裁胜出的前提下调用一次）。

    inventory 为 dict 且 item 为带 id 的 dict 时按键写入；否则返回追加式记录。
    """
    if isinstance(inventory, dict) and isinstance(item, dict) and "id" in item:
        new_inv = dict(inventory)
        new_inv[item["id"]] = item
        return new_inv
    return {"inventory": inventory, "added": item}


# ---------------------------------------------------------------------------
# 失败回滚与通知
# ---------------------------------------------------------------------------


def rollback_ui(client_state, item):
    """拾取失败回滚：恢复掉落物显示，撤销"已入包"的预测表现。"""
    if not isinstance(client_state, dict):
        return {"restored": item, "previous_state": client_state}
    state = dict(client_state)
    ground = list(state.get("ground_items", []))
    if item not in ground:
        ground.append(item)
    state["ground_items"] = ground
    bag = dict(state.get("inventory", {}))
    if isinstance(item, dict):
        bag.pop(item.get("id"), None)
    state["inventory"] = bag
    state["pending_pickup"] = None
    return state


def notify_owner(player_id, message):
    """向失败方推送回滚通知（物品已被他人拾取）。"""
    return {"to": player_id, "type": "pickup_rollback", "message": message}


# ---------------------------------------------------------------------------
# 兜底：幽灵物品检测与一致性扫描
# ---------------------------------------------------------------------------


def _id_set(items):
    if not isinstance(items, (list, tuple, set)):
        return {items}
    return {i.get("id") if isinstance(i, dict) else i for i in items}


def ghost_item_detect(client_ground, server_ground):
    """找出客户端与服务器地面物品集合的差集（幽灵物品）。"""
    client_set, server_set = _id_set(client_ground), _id_set(server_ground)
    return {
        "missing_on_client": sorted(server_set - client_set, key=repr),
        "missing_on_server": sorted(client_set - server_set, key=repr),
    }


def consistency_scan(client_state, server_state):
    """周期性对账：发现不一致即生成回滚/补偿动作列表。"""
    client_items = client_state.get("ground_items", []) if isinstance(client_state, dict) else client_state
    server_items = server_state.get("ground_items", []) if isinstance(server_state, dict) else server_state
    ghosts = ghost_item_detect(client_items, server_items)
    actions = [
        {"action": "rollback_ui", "item_id": i} for i in ghosts["missing_on_client"]
    ]
    actions += [
        {"action": "remove_ghost", "item_id": i} for i in ghosts["missing_on_server"]
    ]
    return actions


# ---------------------------------------------------------------------------
# 重试、校验、指标与降级
# ---------------------------------------------------------------------------


def retry_pickup(attempt, max_retries):
    """网络抖动导致的失败可重试（重试复用同一幂等键）；仲裁失败不重试。"""
    try:
        return attempt < max_retries
    except TypeError:
        return False


def validation(request, ticket):
    """校验拾取请求与预授权票据匹配（玩家、物品、幂等键一致）。"""
    if not isinstance(request, dict) or not isinstance(ticket, dict):
        return request == ticket
    return (
        request.get("player_id") == ticket.get("player_id")
        and request.get("item_id") == ticket.get("item_id")
        and request.get("idempotency_key") == ticket.get("idempotency_key")
    )


def rollback_metric(rollback_count, total_pickups):
    """回滚率指标：rollback_count / total_pickups，用于监控同帧冲突热度。"""
    try:
        if total_pickups <= 0:
            return 0.0
        return rollback_count / total_pickups
    except TypeError:
        return 0.0


def batch_guard(requests, max_batch):
    """批量拾取保护：超过上限的批次拆小，防止一次性洪峰打垮仲裁。"""
    if not isinstance(requests, (list, tuple)):
        requests = [requests]
    try:
        max_batch = max(1, int(max_batch))
    except (TypeError, ValueError):
        max_batch = 1
    requests = list(requests)
    return [requests[i:i + max_batch] for i in range(0, len(requests), max_batch)] or [[]]


def fallback_queue(queue, request):
    """服务器过载时的降级队列：请求入队稍后处理，客户端保持待回滚状态。"""
    new_queue = list(queue) if isinstance(queue, (list, tuple)) else [queue]
    new_queue.append(request)
    return new_queue
