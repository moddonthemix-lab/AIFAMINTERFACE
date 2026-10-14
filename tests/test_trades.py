"""#trades: the channel, P&L tracking, and Trader's daily report routine."""

import pytest

from hub import seed
from hub.core import Hub, HubError


def test_trader_and_trades_channel_exist(hub):
    assert hub.member("trader")["emoji"] == "💹"
    assert hub.channel("trades") is not None and hub.channel("markets") is None
    r = [x for x in hub.routines(hub.member("boss")) if x["title"] == "Daily trading report"]
    assert len(r) == 1 and r[0]["enabled"] is False and r[0]["schedule"] == "Weekdays at 16:30"
    # Deleting it means it stays deleted.
    hub.delete_routine(r[0]["id"])
    seed.setup(hub)
    assert not [x for x in hub.routines(hub.member("boss")) if x["title"] == "Daily trading report"]


def test_old_markets_channel_is_renamed(tmp_path):
    h = Hub(tmp_path / "old")
    h.ensure_boss()
    h.create_channel("markets", "old purpose")
    h.post_message(h.member("boss"), "markets", "post", "an old message")
    seed.setup(h)
    assert h.channel("markets") is None and "journal" in h.channel("trades")["purpose"]
    msgs = h.list_messages(h.member("boss"), "trades")
    assert [m["text"] for m in msgs] == ["an old message"]


def test_pnl_running_total_and_stats(hub):
    t = hub.member("trader")
    hub.report_pnl(t, 12.5, day="2026-10-12")
    hub.report_pnl(t, -5, day="2026-10-13", trades=2)
    hub.report_pnl(t, 0, day="2026-10-14", note="No setup met my rules")
    p = hub.report_pnl(t, 3, day="2026-10-14", trades=1)  # same day again replaces it
    assert [d["total_usd"] for d in p["days"]] == [12.5, 7.5, 10.5]
    tot = p["totals"]
    assert tot["total_usd"] == 10.5 and tot["win_days"] == 2 and tot["best_day"]["day"] == "2026-10-12"
    # A shorter window still shows the true running total.
    short = hub.pnl(hub.member("boss"), "trader", days=1)
    assert short["days"][0]["total_usd"] == 10.5
    # Paper and live are kept apart; live is shown first once it exists.
    hub.report_pnl(t, 100, day="2026-10-14", mode="live")
    assert hub.pnl(hub.member("boss"))["mode"] == "live"
    assert hub.pnl(hub.member("boss"), mode="paper")["totals"]["total_usd"] == 10.5


def test_pnl_validation_and_permissions(hub):
    t = hub.member("trader")
    for bad in ({"pnl_usd": "lots"}, {"pnl_usd": 1, "day": "yesterday"}, {"pnl_usd": 1, "mode": "fantasy"}):
        with pytest.raises(HubError):
            hub.report_pnl(t, bad["pnl_usd"], day=bad.get("day"), mode=bad.get("mode", "paper"))
    with pytest.raises(HubError) as e:
        hub.report_pnl(hub.member("amo"), 5, for_member="trader")
    assert e.value.status == 403
    hub.report_pnl(hub.member("boss"), 5, for_member="trader", day="2026-10-01")  # the boss can correct


def test_pnl_api_client_and_summary(server, tokens, api, tmp_path):
    from hub_client import HubClient
    c = HubClient(tokens["trader"], server, outbox=tmp_path / "o.jsonl")
    r = c.report_pnl(25.0, trades=3, note="Two winners, one small loss", day="2026-10-14")
    assert r["totals"]["total_usd"] == 25.0
    assert c.pnl()["days"][0]["note"] == "Two winners, one small loss"
    s, summ = api("GET", "/api/summary", token=tokens["boss"])
    assert summ["pnl"]["member"] == "trader" and summ["pnl"]["total_usd"] == 25.0
