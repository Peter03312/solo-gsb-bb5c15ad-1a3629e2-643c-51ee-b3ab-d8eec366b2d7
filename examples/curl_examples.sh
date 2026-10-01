#!/usr/bin/env bash
# 冷链追责服务 调用示例（curl）。
# 用法：BASE=http://127.0.0.1:8000 bash examples/curl_examples.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
C="COLD-DEMO-1"

echo "1) 创建货物：编号 / 起始时间 / 温度上限 / 起始保管方"
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"$C\",
  \"started_at\": \"2026-09-30T08:00:00+08:00\",
  \"temp_upper_limit\": \"4.0\",
  \"initial_holder\": \"北京冷库\"
}"; echo

echo "2) 测温（10:00，8.2℃ > 上限 4℃，开启越温区间）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T10:00:00+08:00\", \"temperature\": \"8.2\"
}"; echo

echo "3) 交接（12:00，北京冷库 -> 上海冷库），区间在此刻拆分"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-hand-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T12:00:00+08:00\",
  \"giver\": \"北京冷库\", \"receiver\": \"上海冷库\"
}"; echo

echo "4) 交接（14:00，上海冷库 -> 广州冷库）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-hand-2\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T14:00:00+08:00\",
  \"giver\": \"上海冷库\", \"receiver\": \"广州冷库\"
}"; echo

echo "5) 测温（16:00，3.1℃ <= 上限，闭合越温区间）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-2\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T16:00:00+08:00\", \"temperature\": \"3.1\"
}"; echo

echo "6) 幂等重传第 2) 步（相同内容返回 200 replayed，不重复写入）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T10:00:00+08:00\", \"temperature\": \"8.2\"
}"; echo

echo "7) 追责报告：越温起止 + 各责任方片段（按时间，含读数来源）"
curl -sS "$BASE/cargo/$C/report"; echo

echo "8) 责任确认查询：已闭合区间（区间标识=开启测温事件ID）、结束事件ID、责任片段、当前快照标识、最新表态、汇总状态"
curl -sS "$BASE/cargo/$C/responsibilities"; echo

# 取首个已闭合区间的区间标识与当前快照标识（需系统带 python3；也可手工从上一步复制）
read -r IID SNAP < <(python3 - "$BASE" "$C" <<'PY'
import json, sys, urllib.request
base, cargo = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"{base}/cargo/{cargo}/responsibilities") as r:
    iv = json.load(r)["intervals"][0]
print(iv["interval_id"], iv["snapshot_id"])
PY
)
echo "区间标识=$IID 快照标识=$SNAP"

echo "9) 北京冷库 确认担责"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"北京冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-bj-1\"
  }"; echo

echo "10) 上海冷库 提出异议（异议必须带理由 reason）"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"上海冷库\",
    \"stance\": \"dispute\",
    \"reason\": \"该时段冷机由北京冷库远程设定，上海冷库未接管温控\",
    \"stance_id\": \"stance-sh-1\"
  }"; echo

echo "11) 再次查询：汇总状态变为 has_dispute，各责任方返回最新一条有效表态"
curl -sS "$BASE/cargo/$C/responsibilities/$IID"; echo

echo "12) 表态历史：改口用新编号，历次表态（含已改口/已失效）均可查"
curl -sS "$BASE/cargo/$C/responsibilities/$IID/stances"; echo

echo "13) 幂等：同编号同内容重传返回原记录（200 replayed，即使快照随后失效也如此）"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"北京冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-bj-1\"
  }"; echo

echo "14) 改口：上海冷库改为确认（必须使用新表态编号，携带新编号提交）"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"上海冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-sh-2\"
  }"; echo

echo "15) 最终查询：北京/上海均已确认；广州冷库尚未表态 -> 汇总仍为 pending（待确认）"
curl -sS "$BASE/cargo/$C/responsibilities/$IID"; echo

echo "16) 广州冷库也确认 -> 汇总 all_confirmed"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"广州冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-gz-1\"
  }" >/dev/null
curl -sS "$BASE/cargo/$C/responsibilities/$IID"; echo

# ============================================================
# 本轮新增：同一冷链车承载多票货物 + 车载测温读数
# ============================================================
T="TRUCK-DEMO-1"
C2="COLD-DEMO-3"

echo "17) 创建第二票货物（温度上限 2℃，起始保管方 成都冷库）"
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"$C2\",
  \"started_at\": \"2026-09-30T08:00:00+08:00\",
  \"temp_upper_limit\": \"2.0\",
  \"initial_holder\": \"成都冷库\"
}"; echo

echo "18) 登记冷链车次（车牌号可选）"
curl -sS -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
  \"trip_id\": \"$T\", \"vehicle_no\": \"沪D-66888\"
}"; echo

echo "19) 批量登记两票货物的装卸时段（左闭右开 [装车, 卸车)，整体成功或整体拒绝）"
echo "    $C：09:00 装车、18:00 卸车；$C2：09:00 装车、13:00 卸车"
curl -sS -X POST "$BASE/trips/$T/manifest" -H 'Content-Type: application/json' -d "{
  \"items\": [
    {\"cargo_id\": \"$C\",  \"loaded_at\": \"2026-09-30T09:00:00+08:00\",
     \"unloaded_at\": \"2026-09-30T18:00:00+08:00\"},
    {\"cargo_id\": \"$C2\", \"loaded_at\": \"2026-09-30T09:00:00+08:00\",
     \"unloaded_at\": \"2026-09-30T13:00:00+08:00\"}
  ]
}"; echo

echo "20) 车载读数 11:00 = 3.0℃：对 $C（限4）不越温；对 $C2（限2）越温开启区间"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-1100\",
  \"read_at\": \"2026-09-30T11:00:00+08:00\",
  \"temperature\": \"3.0\"
}"; echo

echo "21) 车载读数 11:30 = 1.5℃（允许晚到上报）：$C2 在其时刻首次不超限，闭合区间"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-1130\",
  \"read_at\": \"2026-09-30T11:30:00+08:00\",
  \"temperature\": \"1.5\"
}"; echo

echo "22) 车载读数 13:00 = 9℃（右开边界：$C2 已卸车，仅 $C 在车上）"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-1300\",
  \"read_at\": \"2026-09-30T13:00:00+08:00\",
  \"temperature\": \"9.0\"
}"; echo

echo "23) 车载读数 17:00 = 3.0℃：$C 首次不超限，闭合其车载读数开启的区间"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-1700\",
  \"read_at\": \"2026-09-30T17:00:00+08:00\",
  \"temperature\": \"3.0\"
}"; echo

echo "24) 同刻货物自有测温优先：15:00 再报一车读数 8℃，同时补一笔自有测温 2℃"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-1500\",
  \"read_at\": \"2026-09-30T15:00:00+08:00\",
  \"temperature\": \"8.0\"
}"; echo
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-own-1500\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T15:00:00+08:00\", \"temperature\": \"2.0\"
}"; echo

echo "25) 车次读数：原始读数全保留；每条给出当时在车上的货物、各自上限下是否越温、是否被采用"
curl -sS "$BASE/trips/$T/readings"; echo

echo "26) $C2 合并测温时间线（车载读数 + 采用标记）"
curl -sS "$BASE/cargo/$C2/readings"; echo

echo "27) $C2 追责报告：区间由车载读数开启/闭合，区间稳定标识 trip:<车次>:reading:<读数>"
curl -sS "$BASE/cargo/$C2/report"; echo

echo "28) $C2 已闭合区间责任确认（保管方仍只由自有交接决定 -> 成都冷库担责）"
curl -sS "$BASE/cargo/$C2/responsibilities"; echo

read -r IID2 SNAP2 < <(python3 - "$BASE" "$C2" <<'PY'
import json, sys, urllib.request
base, cargo = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"{base}/cargo/{cargo}/responsibilities") as r:
    iv = json.load(r)["intervals"][0]
print(iv["interval_id"], iv["snapshot_id"])
PY
)
echo "车载区间标识=$IID2"
curl -sS -X POST "$BASE/cargo/$C2/responsibilities/$IID2/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP2\",
    \"responsible_party\": \"成都冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-cd-1\"
  }"; echo
curl -sS "$BASE/cargo/$C2/responsibilities/$IID2"; echo

echo "29) 校验示例：重复车次 409 / 无效装卸时段 422 / 跨车次重叠 422 / 读数异内容 409"
curl -sS -X POST "$BASE/trips" -H 'Content-Type: application/json' \
  -d "{\"trip_id\": \"$T\"}"; echo
curl -sS -X POST "$BASE/trips/$T/manifest" -H 'Content-Type: application/json' -d "{
  \"items\": [{\"cargo_id\": \"$C2\",
    \"loaded_at\": \"2026-09-30T12:00:00+08:00\",
    \"unloaded_at\": \"2026-09-30T12:00:00+08:00\"}]
}"; echo
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-1100\",
  \"read_at\": \"2026-09-30T11:00:00+08:00\",
  \"temperature\": \"3.1\"
}"; echo
