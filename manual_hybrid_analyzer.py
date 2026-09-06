# -*- coding: utf-8 -*-
"""Normal Telegram coin sorgusu için kontrollü hibrit analiz motoru.

Zengin çoklu-zaman verisini yalnız kapanmış mumlardan üretir. Ücretsiz Gemini
modeli sadece sınırlı bir karar planı seçer; fiyatlar ve kullanıcıya gösterilen
işlem planı deterministik olarak kod tarafından oluşturulur.
"""
from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

from anton_scanner.gpt_sonnet_analyzer.market_analyst_bot import (
    build_timeframe_snapshot,
    fetch_klines,
    fetch_live_price,
    normalize_pair,
)

TR_TZ = timezone(timedelta(hours=3))
PROMPT_VERSION = "hybrid-1.0"


def _fmt(value: float | None) -> str:
    if value is None:
        return "veri yok"
    value = float(value)
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.3f}"
    if value >= 0.01:
        return f"{value:.4f}"
    if value >= 0.0001:
        return f"{value:.6f}"
    return f"{value:.8f}"


def _pct_gap(price: float, boundary: float, side: str) -> float:
    raw = price - boundary if side == "support" else boundary - price
    return max(0.0, raw / price * 100)


def _cluster_zones(frames: dict, live_price: float) -> list[dict]:
    """Yakınlık, zaman dilimi, güncellik ve tekrar sayısıyla bölge üret."""
    tf_weight = {"1H": 1.0, "4H": 2.0, "1D": 3.0}
    lookbacks = {"1H": 140, "4H": 100, "1D": 90}
    wings = {"1H": 3, "4H": 2, "1D": 2}
    points = []
    for label, candles in frames.items():
        subset = candles[-lookbacks[label]:]
        wing = wings[label]
        length = len(subset)
        for idx in range(wing, length - wing):
            window = subset[idx - wing:idx + wing + 1]
            age_ratio = (length - 1 - idx) / max(length - 1, 1)
            recency = max(0.35, 1.0 - 0.65 * age_ratio)
            if subset[idx].low <= min(c.low for c in window):
                points.append((subset[idx].low, label, tf_weight[label] * recency))
            if subset[idx].high >= max(c.high for c in window):
                points.append((subset[idx].high, label, tf_weight[label] * recency))

    h1 = frames["1H"]
    true_ranges = []
    for prev, cur in zip(h1[-15:-1], h1[-14:]):
        true_ranges.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    atr_1h = float(np.mean(true_ranges)) if true_ranges else 0.0
    tolerance = max(live_price * 0.0035, atr_1h * 0.35)

    clusters: list[dict] = []
    for point, label, score in sorted(points, key=lambda item: item[0]):
        matched = next((z for z in clusters if abs(point - z["center"]) <= tolerance), None)
        if matched is None:
            clusters.append({"center": point, "weighted": point * score, "score": score,
                             "prices": [point], "tfs": {label}, "touches": 1})
            continue
        matched["weighted"] += point * score
        matched["score"] += score
        matched["prices"].append(point)
        matched["tfs"].add(label)
        matched["touches"] += 1
        matched["center"] = matched["weighted"] / matched["score"]

    for zone in clusters:
        pad = max(tolerance * 0.35, (max(zone["prices"]) - min(zone["prices"])) / 2)
        zone["low"] = min(zone["prices"]) - pad
        zone["high"] = max(zone["prices"]) + pad
        zone["strength"] = zone["score"] + min(zone["touches"], 5) * 0.15
    return clusters


def _select_zones(frames: dict, live_price: float) -> dict:
    zones = _cluster_zones(frames, live_price)
    supports = sorted((z for z in zones if z["high"] < live_price), key=lambda z: live_price - z["high"])
    resistances = sorted((z for z in zones if z["low"] > live_price), key=lambda z: z["low"] - live_price)
    active = sorted((z for z in zones if z["low"] <= live_price <= z["high"]),
                    key=lambda z: -z["strength"])
    # Fiyat bir bölgenin içindeyse bu bölge ilk direnç/karar alanıdır.
    resistance_1 = active[0] if active else (resistances[0] if resistances else None)
    resistance_2 = resistances[0] if active and resistances else (resistances[1] if len(resistances) > 1 else None)
    return {
        "near_support": supports[0] if supports else None,
        "next_support": supports[1] if len(supports) > 1 else None,
        "resistance_1": resistance_1,
        "resistance_2": resistance_2,
    }


def _zone_text(zone: dict | None, live_price: float) -> str:
    if not zone:
        return "güvenilir bölge oluşmadı"
    names = {"1H": "1H", "4H": "4H", "1D": "1G"}
    tfs = " + ".join(names[x] for x in sorted(zone["tfs"]))
    text = f"{_fmt(zone['low'])}–{_fmt(zone['high'])} ({tfs})"
    if zone["low"] <= live_price <= zone["high"]:
        return text + " — fiyat bölgenin içinde"
    if live_price > zone["high"]:
        return text + f" — güncel fiyatın %{(live_price-zone['high'])/live_price*100:.1f} altında"
    return text + f" — güncel fiyatın %{(zone['low']-live_price)/live_price*100:.1f} üstünde"


def _market_snapshot(symbol: str) -> tuple[dict, dict]:
    pair = normalize_pair(symbol)
    base = pair[:-4]
    coin_frames = {"1D": fetch_klines(pair, "1d"), "4H": fetch_klines(pair, "4h"),
                   "1H": fetch_klines(pair, "1h"), "15M": fetch_klines(pair, "15m")}
    btc_frames = {"1D": fetch_klines("BTC", "1d"), "4H": fetch_klines("BTC", "4h"),
                  "1H": fetch_klines("BTC", "1h")}
    live_price = fetch_live_price(pair)
    snapshot = {
        "symbol": base,
        "live_price": live_price,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "coin": {tf: build_timeframe_snapshot(coin_frames[tf]) for tf in ("1D", "4H", "1H")},
        "timing_15m": build_timeframe_snapshot(coin_frames["15M"]),
        "btc": {tf: build_timeframe_snapshot(btc_frames[tf]) for tf in ("1D", "4H", "1H")},
        "note": "Bütün indikatörler yalnız kapanmış mumlardan hesaplandı; live_price ayrıca anlık fiyattır.",
    }
    zones = _select_zones({k: coin_frames[k] for k in ("1D", "4H", "1H")}, live_price)
    return snapshot, zones


def _decision_plan(snapshot: dict, zones: dict, api_key: str, model_name: str) -> tuple[dict, dict, int]:
    zone_payload = {key: None if value is None else {
        "low": round(value["low"], 10), "high": round(value["high"], 10),
        "timeframes": sorted(value["tfs"]), "touches": value["touches"],
        "strength": round(value["strength"], 2),
    } for key, value in zones.items()}
    prompt = """Aşağıdaki Binance spot verisini bir bütün olarak değerlendir. Bütün teknik göstergeler kapanmış
mumlardan hesaplandı; live_price yalnız anlık konumu gösterir. Mekanik tek gösterge kararı verme.

Karar sırası: 1D genel rejim, 4H'nin bu rejimdeki rolü, 1H giriş zamanlaması ve son olarak 15M yardımcı
zamanlama. Fiyat düşmeden yatay kalarak momentum boşaltıyorsa bunu özellikle ayır. Güçlü üst zaman diliminde
1H öncü göstergeler yeniden dönerken MACD'nin gecikmesini tek başına ret nedeni yapma. BTC bağlamını kullan
ama coinin kendi yapısını ezme. Yalnız JSON şemasındaki seçenekleri seç; Türkçe rapor yazma ve seviye uydurma.

VERİ:\n""" + json.dumps({"snapshot": snapshot, "zones": zone_payload}, ensure_ascii=False)
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["buy_candidate", "wait_trigger", "no_buy"]},
            "day_regime": {"type": "string", "enum": ["up", "mixed", "down"]},
            "h4_role": {"type": "string", "enum": ["continuation", "controlled_pullback", "reversal_attempt", "distribution", "breakdown", "mixed"]},
            "h1_timing": {"type": "string", "enum": ["retrigger", "sideways_reset", "pullback_reset", "overheated", "weakening", "mixed"]},
            "entry_type": {"type": "string", "enum": ["support_reaction", "momentum_retrigger", "resistance_break", "none"]},
            "btc_effect": {"type": "string", "enum": ["supportive", "neutral", "caution"]},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "reasons": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string", "enum": [
                "aligned_uptrend", "buyer_participation", "sideways_cooling", "controlled_pullback",
                "early_retrigger", "price_near_resistance", "price_far_support", "overheated_move",
                "momentum_weakness", "timeframe_conflict", "btc_supportive", "btc_weakness"]}},
        },
        "required": ["action", "day_regime", "h4_role", "h1_timing", "entry_type", "btc_effect", "confidence", "reasons"],
    }
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                     "maxOutputTokens": 450, "temperature": 0.1}}, timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    raw = "".join(str(p.get("text") or "") for p in payload.get("candidates", [{}])[0].get("content", {}).get("parts", []))
    if not raw.strip():
        raise ValueError("Ücretsiz model boş karar planı döndürdü")
    return json.loads(raw), (payload.get("usageMetadata") or {}), len(prompt)


def _indicator_bullets(snapshot: dict) -> list[str]:
    coin = snapshot["coin"]
    bullets = []
    obv_up = [tf for tf in ("1H", "4H", "1D") if coin[tf]["volatility_volume"]["obv_direction_5bar"] == "yukari"]
    if obv_up:
        bullets.append(f"OBV {', '.join(obv_up)} görünümünde yükseliyor; alıcı katılımı {len(obv_up)} zaman diliminde fiyatı destekliyor.")
    ema_up = [tf for tf in ("1H", "4H", "1D") if float(coin[tf]["trend_structure"]["close_vs_ema20_pct"] or -999) >= 0]
    if ema_up:
        bullets.append(f"Kapanmış mum fiyatı {', '.join(ema_up)} grafiklerinde EMA20 üzerinde; kısa ve orta vadeli yapı tamamen bozulmuş değil.")
    macd_up = [tf for tf in ("1H", "4H", "1D") if coin[tf]["momentum"]["macd_hist_direction"] == "yukari"]
    if macd_up:
        bullets.append(f"MACD histogramı {', '.join(macd_up)} görünümünde güçleniyor; momentum bu zaman dilimlerinde yukarı dönüyor.")
    if not bullets:
        bullets.append("Ana göstergeler zaman dilimleri arasında ortak bir yön üretmiyor; fiyat seviyeleri daha belirleyici.")
    return bullets[:3]


def _validated_plan(plan: dict, zones: dict, price: float) -> dict:
    plan = dict(plan)
    entry = plan.get("entry_type", "none")
    action = plan.get("action", "wait_trigger")
    r1 = zones.get("resistance_1")
    if action == "buy_candidate" and entry == "none":
        action = "wait_trigger"
    if action == "buy_candidate" and plan.get("h1_timing") in {"overheated", "weakening"}:
        action = "wait_trigger"
    if action == "buy_candidate" and r1 and _pct_gap(price, r1["low"], "resistance") < 0.35 and entry != "resistance_break":
        action = "wait_trigger"
    plan["action"] = action
    return plan


def _effective_entry(plan: dict, zones: dict, price: float) -> str:
    """Model belirsiz bıraksa bile mevcut fiyat konumundan somut izleme koşulu üret."""
    entry = plan.get("entry_type", "none")
    if entry != "none":
        return entry
    r1 = zones.get("resistance_1")
    near = zones.get("near_support")
    if r1 and r1["low"] <= price <= r1["high"]:
        return "resistance_break"
    if near and _pct_gap(price, near["high"], "support") <= 1.5:
        return "support_reaction"
    return "momentum_retrigger"


def _next_target(zones: dict, price: float, entry: str) -> dict | None:
    """Girişin gerisinde veya içinde kalan direnç hiçbir zaman hedef olamaz."""
    candidates = [zones.get("resistance_1"), zones.get("resistance_2")]
    valid = [z for z in candidates if z and float(z["low"]) > price]
    if not valid:
        return None
    return min(valid, key=lambda z: float(z["low"]) - price)


def _reason_sentence(plan: dict) -> str:
    positive_labels = {
        "aligned_uptrend": "üst zaman dilimlerinin yükselişi desteklemesi",
        "buyer_participation": "alıcı katılımının sürmesi",
        "sideways_cooling": "fiyat fazla gerilemeden momentumun boşalması",
        "controlled_pullback": "geri çekilmenin şimdilik kontrollü kalması",
        "early_retrigger": "saatlik momentumda erken toparlanma görülmesi",
        "btc_supportive": "Bitcoin görünümünün destekleyici olması",
    }
    risk_labels = {
        "price_near_resistance": "fiyatın direnç bölgesinde bulunması",
        "price_far_support": "yakın desteğin mevcut fiyata göre aşağıda kalması",
        "overheated_move": "kısa vadeli hareketin uzamış olması",
        "momentum_weakness": "kısa vadeli momentumun zayıflaması",
        "timeframe_conflict": "zaman dilimlerinin henüz tam uyumlu olmaması",
        "btc_weakness": "Bitcoin'in kısa vadeli baskı oluşturması",
    }
    reasons = plan.get("reasons", [])
    positives = [positive_labels[x] for x in reasons if x in positive_labels][:2]
    risks = [risk_labels[x] for x in reasons if x in risk_labels][:2]

    def joined(items: list[str]) -> str:
        return items[0] if len(items) == 1 else " ve ".join(items)

    if plan.get("action") in {"wait_trigger", "no_buy"} and risks:
        if positives:
            return f"{joined(positives).capitalize()} olumlu; ancak {joined(risks)} nedeniyle yeni alımı aceleye getirmezdim."
        return f"Beklememin temel nedeni {joined(risks)}."
    if positives:
        return f"Bu görüşü {joined(positives)} destekliyor."
    if risks:
        return f"Bu görüşte {joined(risks)} nedeniyle temkinli kalırdım."
    if not positives and not risks:
        return "Kararda fiyatın bulunduğu bölge ile saatlik zamanlamayı birlikte dikkate alırdım."
    return ""


def _trade_ideas(zones: dict, price: float) -> str:
    near, r1, r2 = zones.get("near_support"), zones.get("resistance_1"), zones.get("resistance_2")
    sentences = []
    if r1 and r1["low"] <= price <= r1["high"]:
        sentences.append(
            f"Fiyat {_fmt(r1['low'])}–{_fmt(r1['high'])} ilk direnç bölgesinin içinde olduğu için "
            "mevcut seviyeden yeni alımın kısa vadeli hareket alanı sınırlı."
        )
        if r2 and r2["low"] > price:
            sentences.append(
                f"İlk direncin kapanmış saatlik mumla aşılması ve ardından korunması hâlinde "
                f"{_fmt(r2['low'])}–{_fmt(r2['high'])} sonraki kâr alanı olarak izlenebilir."
            )
    elif r1 and r1["low"] > price:
        gap = _pct_gap(price, r1["low"], "resistance")
        sentences.append(
            f"İlk dirence yaklaşık %{gap:.1f} alan bulunuyor; yeni alımın anlamlı olması için "
            "saatlik momentumun yeniden güçlenmesi gerekir."
        )
    else:
        sentences.append("Fiyatın üzerinde güvenilir direnç oluşmadığı için önceden kesin hedef uydurulmamalı.")
    if near:
        sentences.append(
            f"Alternatif olarak {_fmt(near['low'])}–{_fmt(near['high'])} yakın desteğine kontrollü geri çekilme "
            "ve bu bölgede satışın durması daha avantajlı bir giriş senaryosu oluşturabilir."
        )
    return " ".join(sentences[:3])


def render_report(plan: dict, snapshot: dict, zones: dict) -> str:
    price = float(snapshot["live_price"])
    base = snapshot["symbol"]
    plan = _validated_plan(plan, zones, price)
    day = {"up": "Günlük ana yapı yukarı eğilimli", "mixed": "Günlük ana yapı karışık", "down": "Günlük ana yapı baskı altında"}[plan["day_regime"]]
    h4 = {"continuation": "4 saatlik görünüm devamı destekliyor", "controlled_pullback": "4 saatlik hareket kontrollü bir düzeltme gösteriyor", "reversal_attempt": "4 saatlik görünüm bir dönüş denemesinde", "distribution": "4 saatlik görünümde alıcı gücü dağılıyor", "breakdown": "4 saatlik yapı aşağı kırılmış görünüyor", "mixed": "4 saatlik görünüm henüz net değil"}[plan["h4_role"]]
    h1 = {"retrigger": "saatlik momentum yeniden yukarı tetikleniyor", "sideways_reset": "saatlik momentum fiyat fazla gerilemeden yatay kalarak soğuyor", "pullback_reset": "saatlik görünüm kontrollü geri çekilme sonrası yeniden güç arıyor", "overheated": "saatlik hareket kısa vadede fazla uzamış", "weakening": "saatlik momentum zayıflıyor", "mixed": "saatlik zamanlama henüz karışık"}[plan["h1_timing"]]
    btc = {"supportive": "Bitcoin görünümü genel piyasa baskısını azaltıyor", "neutral": "Bitcoin belirgin destek veya baskı oluşturmuyor", "caution": "Bitcoin kısa vadeli hareket için ek risk oluşturuyor"}[plan["btc_effect"]]

    action = plan["action"]
    entry = _effective_entry(plan, zones, price)
    if action == "buy_candidate":
        opening = "Ben olsam bunu alım adayı olarak değerlendirirdim; yine de tek seferde tam büyüklükte girmezdim."
    elif action == "no_buy":
        opening = "Ben olsam mevcut koşullarda yeni alım düşünmezdim."
    else:
        opening = "Ben olsam şu anda tetik beklerdim."

    if entry == "support_reaction" and zones.get("near_support"):
        trigger = "Yakın destekte satışın durması ve saatlik momentumun yeniden yukarı dönmesi giriş koşulum olurdu."
    elif entry == "resistance_break" and zones.get("resistance_1"):
        trigger = "İlk direncin kapanmış saatlik mumla aşılması ve sonrasında bu bölgenin korunması giriş koşulum olurdu."
    elif entry == "momentum_retrigger":
        trigger = "Fiyat yapısı korunurken saatlik öncü göstergelerin yeniden yukarı dönmesi giriş koşulum olurdu."
    else:
        trigger = "Yeni alım için saatlik fiyat hareketi ile alıcı katılımının birlikte güçlenmesini beklerdim."

    if zones.get("near_support"):
        invalidation = "Yakın destek kapanmış saatlik mumla kaybedilirse bu kısa vadeli alım düşüncesinden vazgeçerdim."
    else:
        invalidation = "Saatlik ve 4 saatlik yapı birlikte aşağı dönerse alım düşüncesinden vazgeçerdim."

    # Hedefi giriş türüne göre kod seçer; model geride kalmış bir direnci hedef yapamaz.
    target_zone = _next_target(zones, price, entry)
    if target_zone:
        profit = f"İlk kâr değerlendirme alanım {_fmt(target_zone['low'])}–{_fmt(target_zone['high'])} olurdu."
    else:
        profit = "Fiyatın üzerinde güvenilir hedef oluşmadığı için sabit hedef uydurmaz, hareket zayıfladıkça kademeli kâr alırdım."

    support_gap = _pct_gap(price, zones["near_support"]["high"], "support") if zones.get("near_support") else None
    resistance_gap = _pct_gap(price, zones["resistance_1"]["low"], "resistance") if zones.get("resistance_1") else None
    location = []
    if support_gap is not None:
        location.append(f"yakın destek yaklaşık %{support_gap:.1f} aşağıda")
    if resistance_gap is not None:
        location.append(f"ilk direnç yaklaşık %{resistance_gap:.1f} yukarıda")
    location_text = "; ".join(location).capitalize() + "." if location else "Fiyatın yakın bölgelere mesafesi güvenilir biçimde hesaplanamadı."

    timing = snapshot["timing_15m"]
    timing_note = "15 dakikalık kapanmış mumlarda momentum " + (
        "yukarı dönüyor." if timing["momentum"]["macd_hist_direction"] == "yukari" else
        "zayıflıyor." if timing["momentum"]["macd_hist_direction"] == "asagi" else "karışık ilerliyor."
    )
    body = (
        f"🔍 Genel Değerlendirme\n{base} şu anda {_fmt(price)} seviyesinde. {location_text} "
        f"{day}; {h4}; {h1}. {btc}.\n\n"
        "📉 Teknik Göstergeler\n" + "\n".join(f"• {x}" for x in _indicator_bullets(snapshot)) +
        "\n\n📈 Kritik Seviyeler\n"
        f"• Yakın destek: {_zone_text(zones.get('near_support'), price)}\n"
        f"• Sonraki destek: {_zone_text(zones.get('next_support'), price)}\n"
        f"• İlk direnç: {_zone_text(zones.get('resistance_1'), price)}\n"
        f"• Sonraki direnç: {_zone_text(zones.get('resistance_2'), price)}\n\n"
        f"📌 İşlem Fikirleri\n{_trade_ideas(zones, price)}\n\n"
        f"🌌 Benim Beklentim — Ne Yapardım?\n{opening} {_reason_sentence(plan)} "
        f"{trigger} {timing_note} {invalidation} {profit}"
    )
    return body


def analyze_coin_hybrid(symbol: str, send_fn, api_key: str, model_name: str, log_usage=None) -> bool:
    pair = normalize_pair(symbol)
    base = pair[:-4]
    try:
        snapshot, zones = _market_snapshot(pair)
        started = time.time()
        plan, usage, prompt_chars = _decision_plan(snapshot, zones, api_key, model_name)
        if log_usage:
            log_usage(
                "manual_coin_hybrid", model_name, PROMPT_VERSION,
                int(usage.get("promptTokenCount") or 0),
                int(usage.get("candidatesTokenCount") or 0),
                time.time() - started, prompt_chars=prompt_chars,
            )
        body = render_report(plan, snapshot, zones)
    except Exception as exc:
        print(f"[MANUEL HYBRID] {pair}: {type(exc).__name__}: {exc}", flush=True)
        send_fn(f"#{html.escape(base)} güncel analizi şu anda oluşturulamadı; daha sonra tekrar dene.")
        return False
    stamp = datetime.now(timezone.utc).astimezone(TR_TZ).strftime("%d/%m/%Y %H:%M")
    message = f"🔎 <b>#{html.escape(base)} GÜNCEL GÖRÜNÜM</b>\n🕐 {stamp}\n━━━━━━━━━━━━━━━━━━━━\n{html.escape(body)}"
    send_fn(message)
    print(f"[MANUEL HYBRID] {pair}: ücretsiz hibrit analiz gönderildi.", flush=True)
    return True
