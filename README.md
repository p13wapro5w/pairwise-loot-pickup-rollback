# pairwise-loot-pickup-rollback

## 领域

游戏、图形与多媒体

## 主题

拾取回滚

## 说明

预授权、失败回滚与幂等请求。

初始状态下 `gameplay/loot_pickup_rollback.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
