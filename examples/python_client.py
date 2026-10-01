"""冷链追责服务 Python 调用示例（仅依赖 httpx）。

运行：python examples/python_client.py
默认地址 http://127.0.0.1:8000，可用环境变量 BASE_URL 覆盖。
"""
import os

import httpx

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")
CARGO = "COLD-DEMO-2"


def show(title: str, resp: httpx.Response) -> None:
    print(f"\n== {title} [{resp.status_code}] ==")
    print(resp.text)


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=10) as api:
        show(
            "创建货物",
            api.post(
                "/cargo",
                json={
                    "cargo_id": CARGO,
                    "started_at": "2026-09-30T08:00:00+08:00",
                    "temp_upper_limit": "4.0",
                    "initial_holder": "北京冷库",
                },
            ),
        )

        # 测温：温度高于上限 -> 开启越温区间
        show(
            "超限测温",
            api.post(
                "/events",
                json={
                    "event_id": "p-t1",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T10:00:00+08:00",
                    "temperature": "8.2",
                },
            ),
        )

        # 交接：必须带交出方与接收方；交出方须为当时保管方
        show(
            "合法交接 北京->上海",
            api.post(
                "/events",
                json={
                    "event_id": "p-h1",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T12:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "上海冷库",
                },
            ),
        )

        # 错误交出方 -> 422，整体拒绝
        show(
            "非法交接（交出方不是保管方，拒绝）",
            api.post(
                "/events",
                json={
                    "event_id": "p-bad",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T13:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "广州冷库",
                },
            ),
        )

        # 不超限测温 -> 闭合区间
        show(
            "恢复测温（闭合区间）",
            api.post(
                "/events",
                json={
                    "event_id": "p-t2",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T15:00:00+08:00",
                    "temperature": "3.0",
                },
            ),
        )

        show("追责报告", api.get(f"/cargo/{CARGO}/report"))
        show("事件列表（按时间）", api.get(f"/cargo/{CARGO}/events"))

        # ----- 责任确认（仅针对已闭合越温区间）-----
        resp = api.get(f"/cargo/{CARGO}/responsibilities")
        show("责任确认：已闭合区间列表", resp)
        intervals = resp.json().get("intervals", [])
        if not intervals:
            print("（没有已闭合越温区间，跳过责任确认演示）")
            return

        interval = intervals[0]
        iid = interval["interval_id"]       # 区间标识 = 开启测温事件ID
        snap = interval["snapshot_id"]      # 当前快照标识（区间起止 + 责任片段）
        print(f"区间标识={iid} 结束测温事件={interval['end_event_id']} "
              f"快照={snap} 初始汇总={interval['summary']}")

        # 起始保管方确认担责
        show(
            "北京冷库 确认",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "北京冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-1",
                },
            ),
        )

        # 异议必须带理由；忘记理由 -> 422 dispute_reason_required
        show(
            "上海冷库 异议但未带理由（拒绝示例）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "dispute",
                    "stance_id": "py-stance-bad",
                },
            ),
        )

        # 合法异议
        show(
            "上海冷库 异议（带理由）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "dispute",
                    "reason": "温控参数由交出方远程设定，接收方未实际接管",
                    "stance_id": "py-stance-2",
                },
            ),
        )

        show(
            "区间责任确认视图（此时汇总 has_dispute）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}"),
        )

        # 同编号同内容重传 -> 200 replayed，返回原记录
        show(
            "表态幂等重传",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "北京冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-1",
                },
            ),
        )

        # 改口：新编号，历次表态保留
        show(
            "上海冷库 改口为确认（新编号）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-3",
                },
            ),
        )
        show(
            "表态历史（历次表态，含 superseded/is_current 标记）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}/stances"),
        )
        show(
            "最终区间视图（汇总 all_confirmed）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}"),
        )

        # ========================================
        # 本轮新增：车次 + 装卸时段 + 车载测温读数
        # ========================================
        cargo2 = "COLD-DEMO-4"
        trip = "TRUCK-DEMO-2"

        show(
            "创建第二票货物（温度上限 2℃，起始保管方 成都冷库）",
            api.post(
                "/cargo",
                json={
                    "cargo_id": cargo2,
                    "started_at": "2026-09-30T08:00:00+08:00",
                    "temp_upper_limit": "2.0",
                    "initial_holder": "成都冷库",
                },
            ),
        )
        show(
            "登记冷链车次",
            api.post("/trips", json={"trip_id": trip, "vehicle_no": "沪D-66888"}),
        )

        # 装卸时段左闭右开：两票货物同车，C2 在 13:00 已卸车
        show(
            "批量登记装卸时段（整体成功或整体拒绝）",
            api.post(
                f"/trips/{trip}/manifest",
                json={
                    "items": [
                        {
                            "cargo_id": CARGO,
                            "loaded_at": "2026-09-30T09:00:00+08:00",
                            "unloaded_at": "2026-09-30T18:00:00+08:00",
                        },
                        {
                            "cargo_id": cargo2,
                            "loaded_at": "2026-09-30T09:00:00+08:00",
                            "unloaded_at": "2026-09-30T13:00:00+08:00",
                        },
                    ]
                },
            ),
        )

        # 非法窗口（起点不早于终点）-> 422，整批拒绝不留部分记录
        show(
            "非法装卸时段（起点=终点，拒绝）",
            api.post(
                f"/trips/{trip}/manifest",
                json={
                    "items": [
                        {
                            "cargo_id": cargo2,
                            "loaded_at": "2026-09-30T12:00:00+08:00",
                            "unloaded_at": "2026-09-30T12:00:00+08:00",
                        }
                    ]
                },
            ),
        )

        # 3.0℃：CARGO（限4）不越温；cargo2（限2）越温 -> 开启其区间
        show(
            "车载读数 11:00=3.0℃（仅低温货越温）",
            api.post(
                f"/trips/{trip}/readings",
                json={
                    "reading_id": "rd-1100",
                    "read_at": "2026-09-30T11:00:00+08:00",
                    "temperature": "3.0",
                },
            ),
        )
        # 1.5℃：cargo2 首次不超限 -> 闭合；读数允许晚到
        show(
            "车载读数 11:30=1.5℃（闭合低温货区间，可晚到）",
            api.post(
                f"/trips/{trip}/readings",
                json={
                    "reading_id": "rd-1130",
                    "read_at": "2026-09-30T11:30:00+08:00",
                    "temperature": "1.5",
                },
            ),
        )
        # 13:00（右开边界）：cargo2 已卸车不受影响；CARGO 越温开启区间
        show(
            "车载读数 13:00=9.0℃（C2 已下车，仅高温货受影响）",
            api.post(
                f"/trips/{trip}/readings",
                json={
                    "reading_id": "rd-1300",
                    "read_at": "2026-09-30T13:00:00+08:00",
                    "temperature": "9.0",
                },
            ),
        )
        # 同刻自有测温 2℃ 优先采用，车载 8℃ 被遮蔽但原始读数仍可查
        # （选 16:30：避开本示例前面 15:00 的自有恢复测温）
        show(
            "车载读数 16:30=8.0℃（稍后会有同刻自有测温遮蔽它）",
            api.post(
                f"/trips/{trip}/readings",
                json={
                    "reading_id": "rd-1630",
                    "read_at": "2026-09-30T16:30:00+08:00",
                    "temperature": "8.0",
                },
            ),
        )
        show(
            "同刻货物自有测温 2.0℃（自有值优先，车载 8℃ 被遮蔽）",
            api.post(
                "/events",
                json={
                    "event_id": "p-own-1630",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T16:30:00+08:00",
                    "temperature": "2.0",
                },
            ),
        )
        # 13:00 的 9℃ 对 CARGO 开启了车载区间，16:30 同刻自有 2℃ 将其闭合

        show(
            "车次读数（原始读数 + 各货物采用/越温情况）",
            api.get(f"/trips/{trip}/readings"),
        )
        show(
            "低温货合并测温时间线（含来源与 adopted 标记）",
            api.get(f"/cargo/{cargo2}/readings"),
        )
        show(
            "低温货追责报告（区间由车载读数开启/闭合，保管方仍由自有交接决定）",
            api.get(f"/cargo/{cargo2}/report"),
        )

        # 车载读数开启的区间同样可以责任确认：标识 trip:<车次>:reading:<读数>
        resp = api.get(f"/cargo/{cargo2}/responsibilities")
        show("低温货责任确认列表", resp)
        intervals = resp.json().get("intervals", [])
        if intervals:
            iv = intervals[0]
            print(
                f"车载区间标识={iv['interval_id']} "
                f"开启来源={iv['start_source']} 快照={iv['snapshot_id']}"
            )
            show(
                "成都冷库 对车载区间确认担责",
                api.post(
                    f"/cargo/{cargo2}/responsibilities/{iv['interval_id']}/stances",
                    json={
                        "snapshot_id": iv["snapshot_id"],
                        "responsible_party": "成都冷库",
                        "stance": "confirm",
                        "stance_id": "py-stance-cd",
                    },
                ),
            )
            show(
                "车载区间表态历史",
                api.get(
                    f"/cargo/{cargo2}/responsibilities/"
                    f"{iv['interval_id']}/stances"
                ),
            )

        # 幂等 / 冲突
        show(
            "车载读数同编号同内容重传（200 replayed）",
            api.post(
                f"/trips/{trip}/readings",
                json={
                    "reading_id": "rd-1100",
                    "read_at": "2026-09-30T11:00:00+08:00",
                    "temperature": "3.0",
                },
            ),
        )
        show(
            "车载读数同编号异内容（409 reading_id_conflict）",
            api.post(
                f"/trips/{trip}/readings",
                json={
                    "reading_id": "rd-1100",
                    "read_at": "2026-09-30T11:00:00+08:00",
                    "temperature": "3.5",
                },
            ),
        )
        show(
            "货物事件列表只含自有事件（不含车载读数）",
            api.get(f"/cargo/{CARGO}/events"),
        )


if __name__ == "__main__":
    main()
