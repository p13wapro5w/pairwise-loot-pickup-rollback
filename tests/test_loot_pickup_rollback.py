import pytest
import gameplay.loot_pickup_rollback as mod

CASES = [
    ("pre_authorize", (1, 2)),
    ("rollback_ui", (1, 2)),
    ("idempotent_request", (1, 2)),
    ("same_frame_click", (1, 2)),
    ("server_arbitrate", (1, 2)),
    ("inventory_apply", (1, 2)),
    ("ghost_item_detect", (1, 2)),
    ("retry_pickup", (1, 2)),
    ("lock_item", (1, 2)),
    ("notify_owner", (1, 2)),
    ("consistency_scan", (1, 2)),
    ("validation", (1, 2)),
    ("rollback_metric", (1.0, 2.0)),
    ("batch_guard", (1, 2)),
    ("fallback_queue", (1, 2)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
