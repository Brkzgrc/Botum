from manual_hybrid_analyzer import render_report


def _zone(low, high):
    return {"low": low, "high": high, "tfs": {"1H"}, "touches": 2, "strength": 2.0}


def _tf(macd="yukari", obv="yukari", ema=1.0):
    return {
        "momentum": {"macd_hist_direction": macd},
        "trend_structure": {"close_vs_ema20_pct": ema},
        "volatility_volume": {"obv_direction_5bar": obv},
    }


def _snapshot():
    return {
        "symbol": "DASH", "live_price": 68.38,
        "coin": {"1H": _tf(), "4H": _tf(), "1D": _tf()},
        "timing_15m": _tf(),
    }


def test_breakout_entry_uses_next_resistance_not_broken_level():
    zones = {"near_support": _zone(65.9, 66.5), "next_support": _zone(65.1, 65.7),
             "resistance_1": _zone(68.3, 68.8), "resistance_2": _zone(70.3, 71.1)}
    plan = {"action": "wait_trigger", "day_regime": "up", "h4_role": "continuation",
            "h1_timing": "mixed", "entry_type": "resistance_break", "btc_effect": "neutral"}
    report = render_report(plan, _snapshot(), zones)
    assert "70.300–71.100" in report
    assert "İlk kâr değerlendirme alanım 68.300–68.800" not in report
    assert "📌 İşlem Fikirleri" in report


def test_buy_is_downgraded_when_price_is_at_resistance():
    zones = {"near_support": _zone(65.9, 66.5), "next_support": None,
             "resistance_1": _zone(68.3, 68.8), "resistance_2": _zone(70.3, 71.1)}
    plan = {"action": "buy_candidate", "day_regime": "up", "h4_role": "continuation",
            "h1_timing": "retrigger", "entry_type": "momentum_retrigger", "btc_effect": "neutral"}
    report = render_report(plan, _snapshot(), zones)
    assert "şu anda tetik beklerdim" in report


def test_overheated_buy_is_downgraded():
    zones = {"near_support": _zone(65.9, 66.5), "next_support": None,
             "resistance_1": _zone(70.3, 71.1), "resistance_2": None}
    plan = {"action": "buy_candidate", "day_regime": "up", "h4_role": "continuation",
            "h1_timing": "overheated", "entry_type": "momentum_retrigger", "btc_effect": "supportive"}
    report = render_report(plan, _snapshot(), zones)
    assert "şu anda tetik beklerdim" in report


def test_unspecified_entry_inside_resistance_gets_concrete_breakout_plan():
    zones = {"near_support": _zone(65.9, 66.5), "next_support": _zone(65.1, 65.7),
             "resistance_1": _zone(68.3, 68.8), "resistance_2": _zone(70.3, 71.1)}
    plan = {"action": "wait_trigger", "day_regime": "up", "h4_role": "controlled_pullback",
            "h1_timing": "sideways_reset", "entry_type": "none", "btc_effect": "supportive",
            "reasons": ["sideways_cooling", "price_near_resistance", "btc_supportive"]}
    report = render_report(plan, _snapshot(), zones)
    assert "direncin kapanmış saatlik mumla aşılması" in report.lower()
    assert "İlk kâr değerlendirme alanım 70.300–71.100" in report
    assert "Fiyatın üzerinde güvenilir hedef oluşmadığı" not in report
    assert "fiyatın direnç bölgesinde bulunması" in report
    assert "ancak" in report.lower()
