#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════
  KRİPTO SİNYAL SİSTEMİ v5.3B — TAM SÜRÜM (Düzeltilmiş)
  3 Seviyeli Karar Destek Motoru + GUI
═══════════════════════════════════════════════════════════════════

  v5 Değişiklikleri (v4'ten):
    • bool_or_min tipi: Volatilite entry + Squeeze radio butonu (gerçek çift giriş)
    • Hacim+Spread birleşik kontrolü: Mock ve Level 2'de iki ayrı giriş
    • 5 test profili: TEST_GOOD, TEST_MEDIUM, TEST_BAD, TEST_VETO, TEST_MISSING
    • Funding dengeli eşik: -0.03 ile +0.01 arası EVET, uç değerler HAYIR
    • BTC veto kuralı: Sadece altcoinler için (symbol != "BTC")
    • Yorum-kod uyumsuzluğu düzeltildi

  Kullanım:
    python kripto_sinyal_sistemi_v5_3b_gui.py
"""

import os
import re
import html
import sys
import math
import time
import socket
import logging
import threading
import itertools
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

import sqlite3
import json
import csv
import urllib.request
import urllib.error

try:
    import requests
    import urllib3.connection as _urllib3_connection
    import urllib3.connectionpool as _urllib3_connectionpool
except ImportError:
    requests = None
    _urllib3_connection = None
    _urllib3_connectionpool = None

try:
    import feedparser
except ImportError:
    feedparser = None

try:
    from rapidfuzz import fuzz as _rfuzz
    _DEDUP_THRESHOLD = 80
except ImportError:
    _rfuzz = None
    _DEDUP_THRESHOLD = 80

try:
    import numpy as np
except ImportError:
    np = None

try:
    from tvDatafeed import TvDatafeed, Interval as TvInterval
except ImportError:
    TvDatafeed = None
    TvInterval = None


# ═══════════════════════════════════════════════════════════════════
# 0. ORTAM DEĞİŞKENLERİ (.env)
# ═══════════════════════════════════════════════════════════════════

def _load_dotenv(path: str = None):
    """Basit .env okuyucu — harici bağımlılık gerektirmez.
    KEY=VALUE satırlarını os.environ'a yazar (mevcut env değişkenlerini EZMEZ)."""
    if path is None:
        from pathlib import Path
        app_dir = Path(__file__).resolve().parent if "__file__" in dir() else Path.cwd()
        path = str(app_dir / ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception as e:
        print(f"[ENV] .env okunamadı: {e}", flush=True)


_load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CMC_API_KEY = os.getenv("CMC_API_KEY", "")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")

# AI Analist V1 — deterministik motordan tamamen bağımsız, izole katman.
# NewsRiskFetcher'ın Haiku çağrısından AYRI, kendi model config'i.
AI_ANALYST_ENABLED = os.getenv("AI_ANALYST_ENABLED", "true").strip().lower() != "false"
AI_ANALYST_MODEL = "claude-sonnet-5"


# ═══════════════════════════════════════════════════════════════════
# 7. GEÇMİŞ VERİTABANI (SQLite)
# ═══════════════════════════════════════════════════════════════════

class HistoryDB:
    @classmethod
    def _db_path(cls):
        # MOBILE WEB MVP / PHASE 1 (onaylı, controlled implementation):
        # KRIPTO_DB_PATH env var set edilmişse (ör. Render persistent disk
        # mount noktası) o kullanılır. Env set edilmemişse (masaüstü
        # kullanımı) davranış BİREBİR AYNI kalır -- additive, geriye dönük
        # uyumlu.
        env_path = os.getenv("KRIPTO_DB_PATH")
        if env_path:
            return env_path
        from pathlib import Path
        app_dir = Path(__file__).resolve().parent if "__file__" in dir() else Path.cwd()
        return str(app_dir / "kripto_sinyal_gecmis.db")

    def __init__(self):
        self._init_db()

    # v3'te eklenen alanlar: karar anındaki risk kapsamı ve giriş zamanlaması
    # ayrıntısını (o anki hesaplanmış haliyle) kalıcı olarak saklamak için.
    _V3_COLUMNS = [
        ("risk_coverage", "REAL"),
        ("entry_timing_score", "REAL"),
        ("entry_timing", "TEXT"),
        ("entry_timing_answered", "INTEGER"),
        ("entry_timing_total", "INTEGER"),
    ]

    def _migrate_v2_to_v3(self, conn):
        """analyses tablosuna eksik v3 sütunlarını ekler ve gerçekten var
        olduklarını doğruladıktan sonra user_version=3 yapar. Duplicate-column
        dışındaki gerçek hatalar burada YUTULMAZ, çağırana yayılır."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(analyses)").fetchall()}
        for col_name, col_type in self._V3_COLUMNS:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE analyses ADD COLUMN {col_name} {col_type}")

        after = {row[1] for row in conn.execute("PRAGMA table_info(analyses)").fetchall()}
        missing = [c for c, _ in self._V3_COLUMNS if c not in after]
        if missing:
            raise RuntimeError(
                f"v3 migrasyonu başarısız: sütunlar eklenemedi: {missing}"
            )

        conn.execute("PRAGMA user_version = 3")
        conn.commit()

    # v4: MODEL D HISTORY PERSISTENCE (controlled implementation, onaylı
    # MODEL B contract). Tek canonical alan -- restricted_candidate/
    # btc_strong_down AYRICA SAKLANMAZ, ikisi de bu reason'dan deterministik
    # türetilebilir (bkz. evaluate_model_d_candidate() -- her reason değeri
    # tam olarak bir (is_candidate, btc_strong_down) çiftine karşılık gelir).
    # NULL = UNKNOWN_LEGACY (Model D o tarihte yoktu) -- asla False'a
    # yuvarlanmaz, ayrı bir "false" değeri DEĞİLDİR.
    _V4_COLUMNS = [
        ("model_d_reason", "TEXT"),
    ]

    def _migrate_v3_to_v4(self, conn):
        """v2_to_v3 ile birebir aynı desen: idempotent ALTER TABLE, gerçek
        hata yutulmaz, başarı doğrulandıktan sonra user_version=4 yapılır."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(analyses)").fetchall()}
        for col_name, col_type in self._V4_COLUMNS:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE analyses ADD COLUMN {col_name} {col_type}")

        after = {row[1] for row in conn.execute("PRAGMA table_info(analyses)").fetchall()}
        missing = [c for c, _ in self._V4_COLUMNS if c not in after]
        if missing:
            raise RuntimeError(
                f"v4 migrasyonu başarısız: sütunlar eklenemedi: {missing}"
            )

        conn.execute("PRAGMA user_version = 4")
        conn.commit()

    def _init_db(self):
        with sqlite3.connect(self._db_path()) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]

            if version < 1:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS analyses (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        analysis_time TEXT NOT NULL,
                        analysis_price REAL,
                        level TEXT NOT NULL,
                        signal_score REAL,
                        risk_score REAL,
                        signal_raw_score REAL,
                        coverage REAL,
                        confidence TEXT,
                        verdict TEXT,
                        entry_status TEXT,
                        vetos TEXT,
                        stage_scores TEXT,
                        answers TEXT,
                        data_source TEXT,
                        created_at TEXT,
                        risk_coverage REAL,
                        entry_timing_score REAL,
                        entry_timing TEXT,
                        entry_timing_answered INTEGER,
                        entry_timing_total INTEGER,
                        model_d_reason TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS tracking_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        analysis_id INTEGER NOT NULL,
                        horizon_hours INTEGER NOT NULL,
                        close_return_pct REAL,
                        mfe_pct REAL,
                        mae_pct REAL,
                        high_price REAL,
                        low_price REAL,
                        close_price REAL,
                        updated_at TEXT,
                        status TEXT,
                        FOREIGN KEY (analysis_id) REFERENCES analyses(id),
                        UNIQUE(analysis_id, horizon_hours)
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_analyses_symbol_time
                    ON analyses(symbol, analysis_time)
                """)
                conn.execute("PRAGMA user_version = 4")
                conn.commit()
            elif version == 1:
                # Mevcut v1→v2 düzeltmesi aynen korunur.
                try:
                    conn.execute("ALTER TABLE analyses RENAME COLUMN signal_signal_raw_score TO signal_raw_score")
                except sqlite3.OperationalError:
                    pass
                conn.execute("PRAGMA user_version = 2")
                conn.commit()
                # Ardından v2→v3→v4 migrasyonları ayrı adımlar olarak çalışır.
                self._migrate_v2_to_v3(conn)
                self._migrate_v3_to_v4(conn)
            elif version == 2:
                self._migrate_v2_to_v3(conn)
                self._migrate_v3_to_v4(conn)
            elif version == 3:
                self._migrate_v3_to_v4(conn)
            else:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS analyses (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        analysis_time TEXT NOT NULL,
                        analysis_price REAL,
                        level TEXT NOT NULL,
                        signal_score REAL,
                        risk_score REAL,
                        signal_raw_score REAL,
                        coverage REAL,
                        confidence TEXT,
                        verdict TEXT,
                        entry_status TEXT,
                        vetos TEXT,
                        stage_scores TEXT,
                        answers TEXT,
                        data_source TEXT,
                        created_at TEXT,
                        risk_coverage REAL,
                        entry_timing_score REAL,
                        entry_timing TEXT,
                        entry_timing_answered INTEGER,
                        entry_timing_total INTEGER,
                        model_d_reason TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS tracking_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        analysis_id INTEGER NOT NULL,
                        horizon_hours INTEGER NOT NULL,
                        close_return_pct REAL,
                        mfe_pct REAL,
                        mae_pct REAL,
                        high_price REAL,
                        low_price REAL,
                        close_price REAL,
                        updated_at TEXT,
                        status TEXT,
                        FOREIGN KEY (analysis_id) REFERENCES analyses(id),
                        UNIQUE(analysis_id, horizon_hours)
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_analyses_symbol_time
                    ON analyses(symbol, analysis_time)
                """)
                conn.commit()

    def save_analysis(self, symbol: str, level: str, report: dict,
                      analysis_price: float = None, data_source: str = "manual",
                      analysis_time: str = None) -> int:
        # analysis_time = sinyal/analiz anı (report üretildiği an); verilmezse
        # (ör. eski çağıran kod) kayıt anına düşülür. created_at = DB'ye
        # kaydedilme anı — bu ikisi kasıtlı olarak farklı alanlardır, gecikmeli
        # kaydetme durumunda tracking başlangıcı analysis_time'a göre kalır.
        saved_at = datetime.now(timezone.utc).isoformat()
        a_time = analysis_time or saved_at
        # Çift kayıt koruması: aynı coin, aynı ANALİZ dakikası
        time_prefix = a_time[:16]  # YYYY-MM-DDTHH:MM
        with sqlite3.connect(self._db_path()) as conn:
            cur = conn.execute(
                "SELECT id FROM analyses WHERE symbol = ? AND analysis_time LIKE ?",
                (symbol.upper(), time_prefix + "%")
            )
            if cur.fetchone():
                return -1  # Zaten kayıtlı

            cur = conn.execute("""
                INSERT INTO analyses
                (symbol, analysis_time, analysis_price, level, signal_score,
                 risk_score, signal_raw_score, coverage, confidence, verdict,
                 entry_status, vetos, stage_scores, answers, data_source, created_at,
                 risk_coverage, entry_timing_score, entry_timing,
                 entry_timing_answered, entry_timing_total, model_d_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol.upper(),
                a_time,
                analysis_price,
                level,
                report.get("signal_score"),
                report.get("risk_score"),
                report.get("signal_raw_score"),
                report.get("coverage"),
                report.get("confidence"),
                report.get("verdict_title"),
                report.get("entry_status"),
                json.dumps(report.get("vetos", []), ensure_ascii=False),
                json.dumps(report.get("stage_scores", {}), ensure_ascii=False),
                json.dumps([{k: v for k, v in a.items() if k != "_item"}
                            for a in report.get("answers", [])], ensure_ascii=False),
                data_source,
                saved_at,
                report.get("risk_coverage"),
                report.get("entry_timing_score"),
                report.get("entry_timing"),
                report.get("entry_timing_answered"),
                report.get("entry_timing_total"),
                # MODEL D HISTORY PERSISTENCE (MODEL B, onaylı): analiz anındaki
                # canonical reason -- restricted_candidate/btc_strong_down AYRICA
                # saklanmıyor (bu değerden deterministik türetilebilir). Model D
                # değerlendirilmediyse (ör. Level 2/3 veya eski call path) None
                # -> NULL -> UNKNOWN_LEGACY, "false" ile KARIŞTIRILMAZ.
                report.get("restricted_reason"),
            ))
            conn.commit()
            return cur.lastrowid

    def get_all_analyses(self) -> List[Dict]:
        with sqlite3.connect(self._db_path()) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM analyses ORDER BY analysis_time DESC"
            )
            return [dict(row) for row in cur.fetchall()]

    def get_analysis_by_id(self, analysis_id: int) -> Optional[Dict]:
        with sqlite3.connect(self._db_path()) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM analyses WHERE id = ?", (analysis_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def delete_analysis(self, analysis_id: int) -> bool:
        with sqlite3.connect(self._db_path()) as conn:
            conn.execute("DELETE FROM tracking_results WHERE analysis_id = ?", (analysis_id,))
            cur = conn.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
            conn.commit()
            return cur.rowcount > 0

    def export_csv(self, filepath: str):
        rows = self.get_all_analyses()
        if not rows:
            return False
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        return True

    # ─── Tracking (MFE / MAE / Kapanış) ───────────────────────────

    def save_tracking(self, analysis_id: int, results: List[Dict]):
        """Bir analiz için tüm ufuk sonuçlarını kaydet / güncelle."""
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._db_path()) as conn:
            for r in results:
                conn.execute("""
                    INSERT INTO tracking_results
                    (analysis_id, horizon_hours, close_return_pct, mfe_pct, mae_pct,
                     high_price, low_price, close_price, updated_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(analysis_id, horizon_hours) DO UPDATE SET
                        close_return_pct=excluded.close_return_pct,
                        mfe_pct=excluded.mfe_pct,
                        mae_pct=excluded.mae_pct,
                        high_price=excluded.high_price,
                        low_price=excluded.low_price,
                        close_price=excluded.close_price,
                        updated_at=excluded.updated_at,
                        status=excluded.status
                """, (
                    analysis_id, r["horizon_hours"], r["close_return_pct"],
                    r["mfe_pct"], r["mae_pct"], r["high_price"],
                    r["low_price"], r["close_price"], now, r["status"]
                ))
            conn.commit()

    def get_tracking(self, analysis_id: int, horizon_hours: int = 24) -> Optional[Dict]:
        with sqlite3.connect(self._db_path()) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM tracking_results WHERE analysis_id = ? AND horizon_hours = ?",
                (analysis_id, horizon_hours)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_analyses_without_tracking(self, horizon_hours: int = 24) -> List[Dict]:
        """Belirli ufuk için henüz tracking kaydı olmayan analizleri döndür.
        NOT: calculate_tracking kendi 'doldu mu?' kontrolünü yapar."""
        with sqlite3.connect(self._db_path()) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("""
                SELECT a.* FROM analyses a
                LEFT JOIN tracking_results t
                    ON a.id = t.analysis_id AND t.horizon_hours = ?
                WHERE t.id IS NULL AND a.analysis_price IS NOT NULL
                ORDER BY a.analysis_time DESC
            """, (horizon_hours,))
            return [dict(row) for row in cur.fetchall()]



# ═══════════════════════════════════════════════════════════════════
# 1. KONFİGÜRASYON
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
# BINANCE CLIENT (ücretsiz, kimlik doğrulamasız)
# ═══════════════════════════════════════════════════════════════════

class BinanceClient:
    BASE = "https://api.binance.com"

    @classmethod
    def _get(cls, endpoint: str) -> dict:
        try:
            with urllib.request.urlopen(endpoint, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as e:
            return {"_error": str(e)}

    @classmethod
    def get_price(cls, symbol: str) -> Optional[float]:
        """Anlık fiyatı çek. symbol = BTC, ETH, SOL ..."""
        pair = f"{symbol.upper()}USDT"
        data = cls._get(f"{cls.BASE}/api/v3/ticker/price?symbol={pair}")
        if "_error" in data or "price" not in data:
            return None
        try:
            return float(data["price"])
        except (ValueError, TypeError):
            return None

    @classmethod
    def get_klines(cls, symbol: str, interval: str = "1h",
                   start_time_ms: int = None, end_time_ms: int = None,
                   limit: int = 100) -> List[List]:
        """
        Kline verisi döndürür.
        Her mum: [open_time, open, high, low, close, volume, ...]
        """
        pair = f"{symbol.upper()}USDT"
        url = f"{cls.BASE}/api/v3/klines?symbol={pair}&interval={interval}&limit={limit}"
        if start_time_ms:
            url += f"&startTime={start_time_ms}"
        if end_time_ms:
            url += f"&endTime={end_time_ms}"
        data = cls._get(url)
        if "_error" in data or not isinstance(data, list):
            return []
        return data

    # Yalnızca calculate_tracking()'in kullandığı temel Binance interval'leri.
    # Desteklenmeyen bir interval verilirse uydurma bir süre KULLANILMAZ —
    # _interval_to_ms None döner ve çağıran taraf bunu açıkça başarısızlık sayar.
    _INTERVAL_MS = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000,
        "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
    }

    @classmethod
    def _interval_to_ms(cls, interval: str) -> Optional[int]:
        return cls._INTERVAL_MS.get(interval)

    @classmethod
    def get_klines_range(cls, symbol: str, interval: str,
                         start_time_ms: int, end_time_ms: int,
                         page_limit: int = 1000, max_pages: int = 50) -> List[List]:
        """[start_time_ms, end_time_ms] aralığındaki mumları get_klines()'in
        tek-çağrı limitini aşarak sayfalayarak eksiksiz çeker.
        get_klines()'e dokunmaz, yalnızca onun üzerine pagination ekler.

        - Her sayfa en fazla page_limit mum getirir (Binance üst sınırı 1000).
        - Sonraki sayfanın startTime'ı bir önceki sayfanın son mumunun
          open_time + interval_ms'i olacak şekilde ilerler.
        - Aynı open_time iki kez eklenmez (seen_open_times).
        - Cursor ilerlemiyorsa veya max_pages aşılırsa sonsuz döngüye
          girmeden kontrollü biçimde durur.
        - Boş/hatalı bir sayfa (get_klines zaten hata durumunda [] döner)
          o ana kadar toplanmış veriyle döngüyü sonlandırır.
        - Dönüş: open_time'a göre sıralı, duplicate'siz mum listesi.
        """
        interval_ms = cls._interval_to_ms(interval)
        if interval_ms is None:
            return []
        if start_time_ms is None or end_time_ms is None or start_time_ms > end_time_ms:
            return []

        all_klines = []
        seen_open_times = set()
        cursor = start_time_ms

        for _ in range(max_pages):
            if cursor > end_time_ms:
                break
            batch = cls.get_klines(symbol, interval=interval,
                                   start_time_ms=cursor, end_time_ms=end_time_ms,
                                   limit=page_limit)
            if not batch:
                break

            for k in batch:
                try:
                    ot = int(k[0])
                except (TypeError, ValueError, IndexError):
                    continue
                if ot in seen_open_times or ot > end_time_ms:
                    continue
                seen_open_times.add(ot)
                all_klines.append(k)

            try:
                last_open = int(batch[-1][0])
            except (TypeError, ValueError, IndexError):
                break
            next_cursor = last_open + interval_ms
            if next_cursor <= cursor:
                break  # ilerleme yok -> sonsuz döngü koruması
            cursor = next_cursor

            if len(batch) < page_limit:
                break  # Binance bu partide sayfayı doldurmadı -> veri burada bitti

        all_klines.sort(key=lambda k: int(k[0]))
        return all_klines

    @classmethod
    def calculate_tracking(cls, symbol: str, analysis_time_iso: str,
                           analysis_price: float,
                           horizons: List[int] = None) -> List[Dict]:
        """
        Belirli ufuklar için MFE, MAE, kapanış getirisi hesapla.
        Yalnızca ufuk gerçekten dolduysa VE veri gerçekten o ufka kadar
        ulaşıyorsa sonuç üretir. horizons: saat cinsinden, örn [1, 6, 12, 24, 48]
        """
        if horizons is None:
            horizons = [1, 6, 12, 24, 48]
        if not analysis_price or analysis_price <= 0:
            return []

        try:
            dt = datetime.fromisoformat(analysis_time_iso.replace("Z", "+00:00"))
            start_ms = int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            return []

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        # Sadece gerçekten dolmuş ufukları hesapla
        valid_horizons = [h for h in horizons
                          if now_ms >= start_ms + h * 3600 * 1000]
        if not valid_horizons:
            return []

        max_h = max(valid_horizons)
        end_ms = start_ms + (max_h + 1) * 3600 * 1000

        # 5 dakikalık mumlar = daha hassas MFE/MAE
        interval = "5m"
        interval_ms = cls._interval_to_ms(interval)
        if interval_ms is None:
            return []

        klines = cls.get_klines_range(symbol, interval=interval,
                                      start_time_ms=start_ms - interval_ms,
                                      end_time_ms=end_ms)
        if not klines:
            return []

        results = []
        for h in valid_horizons:
            target_ms = start_ms + h * 3600 * 1000

            # --- Kapanış: hedeften önce/tam hedefte kapanmış, hedefe en
            # yakın mum. Hedef SONRASINDA kapanan bir mum asla seçilmez.
            closed_in_range = [k for k in klines
                               if int(k[6]) > start_ms and int(k[6]) <= target_ms]
            if not closed_in_range:
                continue  # bu ufuk için uygun kapanış yok -> sonuç üretme
            selected = max(closed_in_range, key=lambda k: int(k[6]))
            selected_close_ms = int(selected[6])
            # Bütünlük kontrolü: seçilen kapanış hedeften en fazla bir mum
            # aralığı kadar önce olmalı. Bu, 5 dakikalık veri çözünürlüğünün
            # doğal sınırı — yeni bir trading eşiği değil.
            if target_ms - selected_close_ms > interval_ms:
                continue  # veri hedef saate yeterince yakın ulaşmamış

            close_price = float(selected[4])
            close_return_pct = ((close_price - analysis_price) / analysis_price) * 100

            # --- MFE/MAE: yalnızca TAMAMEN pencere içinde kalan mumlar.
            # Analiz-öncesi veya hedef-sonrası fiyat hareketi asla sızmaz.
            window = [k for k in klines
                     if int(k[0]) >= start_ms and int(k[6]) <= target_ms]
            if not window:
                continue

            # İç bütünlük (continuity) kontrolü: window içindeki mumlar
            # gerçekten ARDIŞIK mı? Bu, yukarıdaki target-tolerance
            # kontrolünden AYRI ve BAĞIMSIZ bir kontroldür — o kontrol
            # serinin hedefe ulaştığını doğrular, bu kontrol serinin İÇİNİN
            # kesintisiz olduğunu doğrular. close_return_pct/close_price bu
            # kontrolden ETKİLENMEZ (yalnız `selected` mumuna bağlı); yalnız
            # excursion (MFE/MAE) path bütünlüğü gerektirir.
            if not cls._window_continuity_ok(window, interval_ms):
                results.append({
                    "horizon_hours": h,
                    "close_return_pct": round(close_return_pct, 2),
                    "mfe_pct": None,
                    "mae_pct": None,
                    "high_price": None,
                    "low_price": None,
                    "close_price": close_price,
                    "status": "Eksik Veri",
                })
                continue

            highs = [float(k[2]) for k in window]
            lows = [float(k[3]) for k in window]
            high_price = max(highs)
            low_price = min(lows)

            mfe_pct = ((high_price - analysis_price) / analysis_price) * 100
            mae_pct = ((low_price - analysis_price) / analysis_price) * 100

            results.append({
                "horizon_hours": h,
                "close_return_pct": round(close_return_pct, 2),
                "mfe_pct": round(mfe_pct, 2),
                "mae_pct": round(mae_pct, 2),
                "high_price": high_price,
                "low_price": low_price,
                "close_price": close_price,
                "status": cls._status(mfe_pct, mae_pct, h),
            })
        return results

    @staticmethod
    def _window_continuity_ok(window: List[List], interval_ms: int) -> bool:
        """window (open_time'a göre gelen mum listesi) ARDIŞIK mumlar arasında
        tam interval_ms farkı taşıyor mu? İlk mumun open_time'ının neye eşit
        olacağına dair hiçbir varsayım YAPILMAZ (analysis_time candle sınırına
        hizalı olmak zorunda değil — Binance mumları epoch'a hizalıdır)."""
        ots = [int(k[0]) for k in window]
        if ots != sorted(ots):
            return False
        if len(ots) != len(set(ots)):
            return False
        for i in range(1, len(ots)):
            if ots[i] - ots[i - 1] != interval_ms:
                return False
        return True

    @staticmethod
    def _status(mfe_pct: float, mae_pct: float, horizon: int) -> str:
        if horizon == 24:
            if mfe_pct >= 5:
                return "Başarılı"
            elif mfe_pct >= 3:
                return "Kısmi"
            else:
                return "Başarısız"
        return "Bekleniyor"

    # ─── v6: Level 1 gerçek veri için ek uçlar ─────────────────────

    FAPI_BASE = "https://fapi.binance.com"

    @classmethod
    def get_exchange_info_usdt_symbols(cls) -> List[str]:
        """Aktif USDT paritelerinin base asset listesini döndürür (dropdown için).
        Kaldıraçlı token (UP/DOWN/BULL/BEAR) ve stablecoin bazlar elenir."""
        data = cls._get(f"{cls.BASE}/api/v3/exchangeInfo")
        if "_error" in data or "symbols" not in data:
            return []
        leveraged_suffixes = ("UP", "DOWN", "BULL", "BEAR")
        stable_bases = {
            "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "EUR", "GBP",
            "TRY", "BRL", "RUB", "UAH", "ARS", "ZAR", "AUD",
        }
        out = []
        for s in data["symbols"]:
            try:
                if s.get("status") != "TRADING":
                    continue
                if s.get("quoteAsset") != "USDT":
                    continue
                base = s.get("baseAsset", "")
                if not base or base in stable_bases:
                    continue
                if any(base.endswith(suf) for suf in leveraged_suffixes):
                    continue
                out.append(base)
            except Exception:
                continue
        return sorted(set(out))

    @classmethod
    def get_ticker_24h(cls, symbol: str) -> Optional[dict]:
        pair = f"{symbol.upper()}USDT"
        data = cls._get(f"{cls.BASE}/api/v3/ticker/24hr?symbol={pair}")
        if "_error" in data:
            return None
        return data

    @classmethod
    def get_avg_volume_7d_usd(cls, symbol: str) -> Optional[float]:
        """Son 7 TAMAMEN KAPANMIŞ günlük mumun ortalama hacmini (yaklaşık USD,
        quote volume) döndürür. Bugünün hâlâ oluşmakta olan (kapanmamış) günlük
        mumu asla dahil edilmez — 'son mum daima kapalıdır' varsayımı yerine
        close_time gerçek UTC zamanıyla karşılaştırılır."""
        klines = cls.get_klines(symbol, interval="1d", limit=8)
        if not klines:
            return None
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        closed = [k for k in klines if int(k[6]) <= now_ms]
        if len(closed) < 7:
            return None
        try:
            vols = [float(k[7]) for k in closed[-7:]]  # quote asset volume (USDT)
            return sum(vols) / len(vols)
        except (ValueError, IndexError):
            return None

    @classmethod
    def get_order_book_metrics(cls, symbol: str, notional_usd: float = 10000.0) -> Optional[dict]:
        """Spread% ve yaklaşık slippage% (order book derinliğinden, notional_usd'lik market alım simülasyonu)."""
        pair = f"{symbol.upper()}USDT"
        data = cls._get(f"{cls.BASE}/api/v3/depth?symbol={pair}&limit=100")
        if "_error" in data or "bids" not in data or "asks" not in data:
            return None
        try:
            bids = [(float(p), float(q)) for p, q in data["bids"]]
            asks = [(float(p), float(q)) for p, q in data["asks"]]
            if not bids or not asks:
                return None
            best_bid, best_ask = bids[0][0], asks[0][0]
            mid = (best_bid + best_ask) / 2
            spread_pct = ((best_ask - best_bid) / mid) * 100 if mid else None

            # Slippage: notional_usd kadar market alım için ağırlıklı ortalama fiyat sapması
            remaining = notional_usd
            filled_cost = 0.0
            filled_qty = 0.0
            for price, qty in asks:
                level_notional = price * qty
                take = min(remaining, level_notional)
                take_qty = take / price if price else 0
                filled_cost += take
                filled_qty += take_qty
                remaining -= take
                if remaining <= 0:
                    break
            slippage_pct = None
            if filled_qty > 0 and remaining <= 0:
                avg_price = filled_cost / filled_qty
                slippage_pct = ((avg_price - best_ask) / best_ask) * 100 if best_ask else None

            return {
                "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
                "slippage_pct": round(slippage_pct, 4) if slippage_pct is not None else None,
            }
        except Exception:
            return None

    @classmethod
    def get_technical_indicators(cls, symbol: str) -> Optional[dict]:
        """1 saatlik TAMAMEN KAPANMIŞ mumlardan EMA50, RSI14, MACD, OBV, Bollinger
        hesaplar. Şu an oluşmakta olan (henüz kapanmamış) mum -- yüksek/düşük/
        kapanış/hacim değerleri hâlâ değişebileceği için -- hiçbir hesaba dahil
        edilmez. 'Son mum daima açıktır' varsayımıyla klines[:-1] yerine, her
        mumun gerçek close_time'ı şu anki UTC zamanıyla karşılaştırılır.

        Not: bu fonksiyonun döndürdüğü "price" alanı CANLI/GÜNCEL bir ticker
        fiyatı DEĞİLDİR -- yalnızca indikatör hesaplarında kullanılan son
        KAPANMIŞ 1h mumun close değeridir (EMA50/RSI/vb. ile aynı referans
        noktası). Uygulamadaki gerçek anlık/canlı fiyat ihtiyacı ayrı bir
        çağrı olan BinanceClient.get_price() ile karşılanır (ör. HistoryDB'ye
        kaydedilen analysis_price); bu ikisi kasıtlı olarak farklı kaynaklardır
        ve birbirinin yerine geçmez.
        """
        if np is None:
            return None
        klines = cls.get_klines(symbol, interval="1h", limit=250)
        if not klines:
            return None
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # k[6] = close_time. Yalnızca gerçekten kapanmış mumlar (close_time <= now) kullanılır.
        closed = [k for k in klines if int(k[6]) <= now_ms]
        if len(closed) < 60:
            return None
        try:
            closes = np.array([float(k[4]) for k in closed], dtype=float)
            volumes = np.array([float(k[5]) for k in closed], dtype=float)
            highs = np.array([float(k[2]) for k in closed], dtype=float)
            lows = np.array([float(k[3]) for k in closed], dtype=float)

            ema50 = cls._ema(closes, 50)
            price = closes[-1]
            ema50_last = ema50[-1]
            ema50_distance_pct = abs(price - ema50_last) / ema50_last * 100 if ema50_last else None

            rsi = cls._rsi(closes, 14)
            macd_bullish = cls._macd_bullish(closes)
            obv_rising = cls._obv_rising(closes, volumes)
            bb = cls._bollinger(closes, 20, 2)

            # Günlük volatilite: son 24 saatlik mumların (yüksek-düşük)/kapanış ortalaması
            recent = closes[-24:] if len(closes) >= 24 else closes
            recent_h = highs[-24:] if len(highs) >= 24 else highs
            recent_l = lows[-24:] if len(lows) >= 24 else lows
            daily_range_pct = float(np.mean((recent_h - recent_l) / recent)) * 100 if len(recent) else None

            price_change_24h_pct = None
            if len(closes) >= 25:
                price_change_24h_pct = (closes[-1] - closes[-25]) / closes[-25] * 100

            # ADDITIVE: Technical Structure Engine V1 için aynı kapanmış-mum
            # snapshot'ının ham OHLCV'si. Yukarıdaki TÜM mevcut alanlar
            # (price/ema50/rsi/macd/obv/bollinger/volatility/price_change)
            # bu eklemeden ETKİLENMEDİ -- aynı closed/closes/highs/lows/volumes
            # dizilerinden okunuyor, hiçbir değer değişmedi. opens/timestamps
            # yalnızca burada, ek olarak paketleniyor.
            opens = np.array([float(k[1]) for k in closed], dtype=float)
            open_times = [int(k[0]) for k in closed]
            close_times = [int(k[6]) for k in closed]

            return {
                "price": float(price),
                "ema50": float(ema50_last) if ema50_last is not None else None,
                "ema50_distance_pct": round(ema50_distance_pct, 3) if ema50_distance_pct is not None else None,
                "price_above_ema50": bool(price > ema50_last) if ema50_last is not None else None,
                "rsi": round(float(rsi), 2) if rsi is not None else None,
                "macd_bullish": macd_bullish,
                "obv_rising": obv_rising,
                "bb_squeeze": bb.get("squeeze") if bb else None,
                "bb_upper_break": bb.get("upper_break") if bb else None,
                "volatility_pct": round(daily_range_pct, 3) if daily_range_pct is not None else None,
                "price_change_24h_pct": round(price_change_24h_pct, 3) if price_change_24h_pct is not None else None,
                "ohlcv": {
                    "interval": "1h",
                    "open_time": open_times,
                    "close_time": close_times,
                    "open": opens,
                    "high": highs,
                    "low": lows,
                    "close": closes,
                    "volume": volumes,
                },
            }
        except Exception as e:
            print(f"[BINANCE INDICATOR] {symbol}: {e}", flush=True)
            return None

    @staticmethod
    def _ema(values, period):
        if np is None or len(values) < period:
            return None
        alpha = 2 / (period + 1)
        ema = np.zeros_like(values)
        ema[0] = values[0]
        for i in range(1, len(values)):
            ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]
        return ema

    @staticmethod
    def _rsi(closes, period=14):
        if np is None or len(closes) < period + 1:
            return None
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])
        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @classmethod
    def _macd_bullish(cls, closes) -> Optional[bool]:
        if np is None or len(closes) < 35:
            return None
        ema12 = cls._ema(closes, 12)
        ema26 = cls._ema(closes, 26)
        if ema12 is None or ema26 is None:
            return None
        macd_line = ema12 - ema26
        signal_line = cls._ema(macd_line, 9)
        if signal_line is None:
            return None
        hist = macd_line - signal_line
        if len(hist) < 2:
            return None
        # Boğa: histogram pozitife dönüyor veya son iki barda yükseliyor ve pozitif
        return bool(hist[-1] > 0 and hist[-1] >= hist[-2])

    @staticmethod
    def _obv_rising(closes, volumes) -> Optional[bool]:
        if np is None or len(closes) < 15:
            return None
        obv = np.zeros(len(closes))
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                obv[i] = obv[i - 1] + volumes[i]
            elif closes[i] < closes[i - 1]:
                obv[i] = obv[i - 1] - volumes[i]
            else:
                obv[i] = obv[i - 1]
        # Son 10 barlık eğim pozitif mi?
        recent = obv[-10:]
        return bool(recent[-1] > recent[0])

    @staticmethod
    def _bollinger(closes, period=20, num_std=2) -> Optional[dict]:
        if np is None or len(closes) < period + 20:
            return None
        rolling_std = []
        rolling_width = []
        for i in range(period, len(closes)):
            window = closes[i - period:i]
            mean = np.mean(window)
            std = np.std(window)
            upper = mean + num_std * std
            lower = mean - num_std * std
            width = (upper - lower) / mean if mean else 0
            rolling_width.append(width)
            rolling_std.append((mean, std, upper, lower))
        if not rolling_width:
            return None
        current_width = rolling_width[-1]
        # Squeeze: son genişlik, geçmiş genişliklerin alt %20'lik diliminde ise
        threshold = np.percentile(rolling_width[:-1], 20) if len(rolling_width) > 5 else current_width
        squeeze = bool(current_width <= threshold)
        mean, std, upper, lower = rolling_std[-1]
        upper_break = bool(closes[-1] > upper)
        return {"squeeze": squeeze, "upper_break": upper_break}

    # fundingIntervalHours (sembole özel gerçek funding periyodu) yalnızca bir
    # kez çekilir ve süresiz (uygulama ömrü boyunca) önbelleklenir — CoinGecko
    # coin-list önbellek deseniyle aynı: başarısız da olsa tekrar denenmez,
    # tahmini bir varsayılan (ör. sabit 8h) İLE DOLDURULMAZ.
    _funding_interval_cache: Dict[str, int] = {}
    _funding_info_loaded = False

    @classmethod
    def _load_funding_intervals(cls):
        if cls._funding_info_loaded:
            return
        data = cls._get(f"{cls.FAPI_BASE}/fapi/v1/fundingInfo")
        if "_error" in data or not isinstance(data, list):
            # Geçici/başarısız yanıt — loaded=False kalır, sonraki çağrı tekrar dener.
            return
        cls._funding_info_loaded = True
        for d in data:
            try:
                sym = d.get("symbol")
                hrs = d.get("fundingIntervalHours")
                if sym and isinstance(hrs, (int, float)) and hrs > 0:
                    cls._funding_interval_cache[sym] = int(hrs)
            except Exception:
                continue

    @classmethod
    def get_funding_detail(cls, symbol: str) -> Optional[dict]:
        """Son gerçekleşen funding kaydını, SEMBOLE ÖZEL gerçek funding
        periyoduna (fundingInfo'daki fundingIntervalHours) göre freshness
        kontrolünden geçirerek döner. Periyot güvenilir biçimde belirlenemiyorsa
        (fundingInfo'da sembol yoksa/çekilemiyorsa) VEYA kayıt kendi periyodundan
        eskiyse None döner — hiçbir zaman sabit/tahmini bir eşikle "muhtemelen
        taze" varsayılmaz."""
        pair = f"{symbol.upper()}USDT"
        data = cls._get(f"{cls.FAPI_BASE}/fapi/v1/fundingRate?symbol={pair}&limit=1")
        if "_error" in data or not isinstance(data, list) or not data:
            return None
        try:
            rate_pct = float(data[0]["fundingRate"]) * 100
            funding_time_ms = int(data[0]["fundingTime"])
        except (KeyError, ValueError, TypeError, IndexError):
            return None

        cls._load_funding_intervals()
        interval_hours = cls._funding_interval_cache.get(pair)
        if interval_hours is None:
            return None  # periyot güvenilir belirlenemedi -> tahmin yok, nodata

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        interval_ms = interval_hours * 3_600_000
        if now_ms - funding_time_ms > interval_ms:
            return None  # kayıt kendi periyodundan daha eski -> stale

        return {
            "rate_pct": rate_pct,
            "funding_time_ms": funding_time_ms,
            "interval_hours": interval_hours,
            "age_hours": round((now_ms - funding_time_ms) / 3_600_000, 2),
        }

    @classmethod
    def get_funding_rate(cls, symbol: str) -> Optional[float]:
        """Son gerçekleşen funding rate (%). Sembol futures'ta yoksa, funding
        periyodu güvenilir belirlenemiyorsa veya kayıt kendi periyodundan
        eskiyse (stale) None döner."""
        detail = cls.get_funding_detail(symbol)
        return detail["rate_pct"] if detail else None

    @classmethod
    def get_open_interest_trend(cls, symbol: str) -> Optional[dict]:
        """GERÇEK iki-nokta OI karşılaştırması, zaman bütünlüğü doğrulanarak.

        OI kaydındaki 'timestamp' alanı bir snapshot anını temsil eder ve
        (çapraz doğrulandı) 1h/5m kline 'open_time' ızgarasıyla birebir aynı
        hizadadır. Fiyat değişimi bu yüzden OI'nin gerçek [first_ts, last_ts]
        penceresine startTime/endTime ile hizalanarak ve yalnızca KAPANMIŞ
        mumlardan hesaplanır — "şu anki son N mum" gibi hizasız bir varsayım
        kullanılmaz, açık/oluşmakta olan mum asla kullanılmaz.

        Bütünlük (kesin artan sıra, duplicate yok, period=5m'in kendi doğal
        çözünürlüğüne göre ardışıklık, freshness) veya fiyat zaman-hizalaması
        doğrulanamazsa None döner — asla eksik/hizasız veriden 'artıyor/
        uyumlu' sonucu üretilmez."""
        pair = f"{symbol.upper()}USDT"
        period_ms = 300_000  # period=5m -- kendi istek parametremizin doğal çözünürlüğü
        data = cls._get(f"{cls.FAPI_BASE}/futures/data/openInterestHist?symbol={pair}&period=5m&limit=6")
        if "_error" in data or not isinstance(data, list) or len(data) < 2:
            return None
        try:
            points = []
            for d in data:
                points.append((int(d["timestamp"]), float(d["sumOpenInterest"])))
            points.sort(key=lambda p: p[0])

            timestamps = [p[0] for p in points]
            if len(set(timestamps)) != len(timestamps):
                return None  # duplicate timestamp
            for i in range(len(timestamps) - 1):
                if timestamps[i + 1] - timestamps[i] != period_ms:
                    return None  # ardışıklık bozuk / beklenmeyen gap

            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            first_ts, first_oi = points[0]
            last_ts, last_oi = points[-1]
            # Freshness: ham "now_ms - last_ts" farkı yerine 5m ızgarasına göre.
            # last_ts zaten period_ms'e hizalı geliyor (doğrulandı). "Güncel" sayılmak
            # için son nokta ya şu anki slotta ya da bir önceki slotta olmalı — bu,
            # Binance'in yeni slot başladıktan sonra o slotun OI noktasını birkaç/
            # onlarca saniye gecikmeli yayınlayabilmesini (ölçülen: 11-31sn) 5m
            # ızgarasının kendi doğal biriminde tolere eder. İki slot veya daha
            # fazla geride kalan veri hâlâ stale sayılır.
            current_slot_ms = (now_ms // period_ms) * period_ms
            if last_ts < current_slot_ms - period_ms:
                return None  # iki slottan fazla geride -> stale

            if first_oi == 0:
                return None
            oi_change_pct = (last_oi - first_oi) / first_oi * 100

            # Fiyat: OI penceresine hizalı çek, yalnız kapanmış mumları kullan.
            klines = cls.get_klines(symbol, interval="5m",
                                    start_time_ms=first_ts - period_ms,
                                    end_time_ms=last_ts, limit=20)
            closed_klines = [k for k in klines if int(k[6]) <= now_ms]

            def price_at(target_ts):
                candidates = [k for k in closed_klines if int(k[6]) <= target_ts]
                if not candidates:
                    return None
                best = max(candidates, key=lambda k: int(k[6]))
                return float(best[4])

            price_first = price_at(first_ts)
            price_last = price_at(last_ts)
            if price_first is None or price_last is None or price_first == 0:
                return None  # fiyat zaman-hizalı doğrulanamadı -> nodata

            price_change_pct = (price_last - price_first) / price_first * 100

            # NOT: 0.5 eşiği bu round'da da DEĞİŞMEDİ — yalnız "onaylanmış/anlamlı
            # OI artışı" için YES sınırı olarak korunuyor. `oi_rising`/`aligned`
            # boolean'ları GERİYE UYUMLULUK için aynen bırakıldı (tek tüketicileri
            # aşağıdaki oi_rising_aligned dalıydı, o da artık yeni `status` alanını
            # kullanıyor — ama sözleşmeyi bozmamak için bu iki alan da korunuyor).
            oi_rising = oi_change_pct > 0.5
            aligned = oi_rising and price_change_pct > 0

            # Yeni: üç-durumlu (yes/wait/no) sınıflandırma. %0.5 sınırı YES için
            # korunuyor; 0 < OI <= %0.5 VE fiyat pozitifse artık sessizce "no"
            # değil "wait" — pozitif ama henüz teyit edilmemiş OI artışı.
            if oi_change_pct <= 0 or price_change_pct <= 0:
                oi_status = "no"
            elif oi_change_pct > 0.5:
                oi_status = "yes"
            else:
                oi_status = "wait"

            return {
                "oi_change_pct": round(oi_change_pct, 3),
                "price_change_pct": round(price_change_pct, 3),
                "oi_rising": oi_rising,
                "aligned": aligned,
                "status": oi_status,
                "first_ts": first_ts,
                "last_ts": last_ts,
            }
        except Exception:
            return None

    @classmethod
    def get_btc_daily_trend(cls) -> Optional[bool]:
        """BTC GÜNLÜK trendi: TAMAMEN KAPANMIŞ 1D mumlarda son kapanışın
        EMA50 üzerinde olup olmadığı. get_technical_indicators()'ın 1h
        yolundan tamamen bağımsız, yalnızca bu tek soru için minimal ayrı
        bir hesap — MACD/RSI/Bollinger gibi başka günlük gösterge eklenmez."""
        if np is None:
            return None
        klines = cls.get_klines("BTC", interval="1d", limit=60)
        if not klines:
            return None
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # k[6] = close_time. Oluşmakta olan (henüz kapanmamış) bugünün günlük
        # mumu kesinlikle dahil edilmez.
        closed = [k for k in klines if int(k[6]) <= now_ms]
        if len(closed) < 50:
            return None
        try:
            closes = np.array([float(k[4]) for k in closed], dtype=float)
            ema50 = cls._ema(closes, 50)
            if ema50 is None:
                return None
            return bool(closes[-1] > ema50[-1])
        except Exception:
            return None

    @classmethod
    def get_btc_trend(cls) -> Optional[bool]:
        """BTC günlük trendi (1D EMA50 üzerinde mi) — bkz. get_btc_daily_trend()."""
        return cls.get_btc_daily_trend()

    @classmethod
    def get_btc_daily_regime_and_r7(cls) -> "tuple":
        """MODEL D — get_btc_daily_trend()'İN GÖVDESİNİ HİÇ DEĞİŞTİRMEDEN
        (sibling metod, aynen korunuyor), TEK bir 1D kline fetch'inden hem
        günlük EMA50 rejimini (get_btc_daily_trend() ile TAMAMEN AYNI formül
        ve TAMAMEN AYNI kapanmış-mum filtresi) hem de trailing 7 günlük
        getiriyi (r7) hesaplar -- ikinci bir network çağrısı EKLEMEZ.
        Return: (bullish: Optional[bool], r7_pct: Optional[float])
        r7 formülü: (son_kapanan_günlük_close - 7_gün_önceki_close) /
        7_gün_önceki_close * 100."""
        if np is None:
            return None, None
        klines = cls.get_klines("BTC", interval="1d", limit=60)
        if not klines:
            return None, None
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        closed = [k for k in klines if int(k[6]) <= now_ms]
        if len(closed) < 50:
            return None, None
        try:
            closes = np.array([float(k[4]) for k in closed], dtype=float)
            ema50 = cls._ema(closes, 50)
            bullish = None
            if ema50 is not None:
                bullish = bool(closes[-1] > ema50[-1])
            r7 = None
            if len(closes) >= 8 and closes[-8]:
                r7 = float((closes[-1] - closes[-8]) / closes[-8] * 100)
            return bullish, r7
        except Exception:
            return None, None


@dataclass
class QuestionItem:
    label: str
    desc: str
    weight: int
    pro: str
    con: str
    thresholds: Dict[str, Any] = field(default_factory=dict)
    optional: bool = False

@dataclass
class StageConfig:
    title: str
    subtitle: str
    items: List[QuestionItem]

# Sinyal aşamaları (Risk hariç)
SIGNAL_STAGE_WEIGHTS = {
    "Temel filtreleme": 15,
    "Teknik analiz onayı": 35,
    "Zincir üstü veriler": 15,
    "Makro & türev piyasası": 20,
}

# Risk / İşlem uygunluğu aşaması (ayrı skor)
RISK_STAGE_WEIGHTS = {
    "Risk & pozisyon yönetimi": 15,
}

# Geriye uyumluluk için
STAGE_WEIGHTS = {**SIGNAL_STAGE_WEIGHTS, **RISK_STAGE_WEIGHTS}

# Veto kuralları: (aşama, soru_label, sebep, condition_func)
# condition_func: symbol -> bool (True ise veto kuralı aktif)
VETO_RULES = [
    # (aşama, soru_label, sebep, condition_func, trigger_answers)
    # 1 büyük borsa da kabul edilmiyorsa → wait + veto
    ("Temel filtreleme", "Coin en az 2 büyük borsada listeli",
     "Yetersiz borsa listesi — likidite riski", lambda s: True, {"wait", "no"}),
    # Hacim/spread sadece açıkça hayır olduğunda veto
    ("Temel filtreleme", "24s hacim > 5M$ ve spread makul",
     "Hacim çok düşük veya spread yüksek — manipülasyon riski", lambda s: True, {"no"}),
    # %3-10 unlock nötr risk; >%10 veto
    ("Risk & pozisyon yönetimi", "Yakın zamanda büyük token unlock yok (%10+ arz etkisi)",
     "Yakında büyük token unlock var — arz şoku riski", lambda s: True, {"no"}),
    ("Makro & türev piyasası", "BTC günlük trendi boğa veya nötr (EMA50 üzerinde)",
     "BTC günlük trendi ayı — altcoinler baskı altında", lambda s: s.upper() != "BTC", {"no"}),
    # %1-2 slippage sınırda; >%2 veto
    ("Temel filtreleme", "Likidite derinliği yeterli (slippage <%1)",
     "Likidite çok düşük — slippage yüksek", lambda s: True, {"no"}),
    ("Risk & pozisyon yönetimi", "Yakın zamanda hack / exploit / düzenleyici baskı yok",
     "Yakın güvenlik/regülasyon olayı var — risk yüksek", lambda s: True, {"no"}),
]

# MODEL D: BTC günlük-trend veto'sunun tam sebep metni, VETO_RULES'un
# KENDİSİNDEN (literal string kopyası DEĞİL) türetilir -- VETO_RULES hiç
# değişmedi, bu yalnız o listeden okunan bir referans. Model D adaylığı
# yalnız "tek veto mevcut VE bu veto tam olarak bu sebep" durumunda aktif
# olabilir (bkz. evaluate_model_d_candidate).
BTC_DAILY_VETO_REASON = next(
    r[2] for r in VETO_RULES
    if r[1] == "BTC günlük trendi boğa veya nötr (EMA50 üzerinde)")

STAGES_CONFIG = [
    StageConfig("Temel filtreleme", "İlk eleme: hacim, borsa, likidite, volatilite", [
        QuestionItem(
            "24s hacim / 7g ortalama ≥ 1.5x",
            "Hacim artışı = ilgi ve momentum. 1.5x+ güçlü, 1.0-1.5 nötr, <1.0 zayıf.",
            8,
            "24 saatlik hacim, 7 günlük ortalamanın 1.5x üzerinde",
            "Hacim düşük — ilgi ve momentum zayıf",
            {"type": "ratio", "yes": 1.5, "wait": 1.0,
             "input_labels": ["24s hacim (M$)", "7g ortalama hacim (M$)"],
             "unit_hint": "Milyon dolar cinsinden girin"}
        ),
        QuestionItem(
            "Coin en az 2 büyük borsada listeli",
            "Binance, Coinbase, Kraken, OKX gibi. Likidite için zorunlu.",
            10,
            "Yeterli borsa listesi — likidite sağlam",
            "Yetersiz borsa listesi — likidite riski",
            {"type": "count", "yes": 2, "wait": 1,
             "input_labels": ["Büyük borsa sayısı"]}
        ),
        QuestionItem(
            "24s hacim > 5M$ ve spread makul",
            "Düşük hacimli coinlerde manipülasyon ve slipaj riski yüksek. İKİ DEĞER GEREKİR.",
            9,
            "Hacim yüksek ve spread makul",
            "Hacim çok düşük veya spread yüksek — manipülasyon riski",
            {"type": "volume_spread", "yes_vol": 5, "yes_spread": 0.1, "wait_vol": 2, "wait_spread": 0.3,
             "input_labels": ["24s hacim (M$)", "Spread (%)"],
             "unit_hint": "Hacim: Milyon $. Spread: Yüzde. Örn: 0.1 = %0.1"}
        ),
        QuestionItem(
            "Likidite derinliği yeterli (slippage <%1)",
            "Order book derinliği yeterli mi?",
            7,
            "Likidite derinliği yeterli — slippage düşük",
            "Likidite çok düşük — slippage yüksek",
            {"type": "max", "yes": 1.0, "wait": 2.0,
             "input_labels": ["Slippage (%)"]}
        ),
        QuestionItem(
            "Günlük volatilite > %3 veya Bollinger squeeze aktif",
            "Sıkışmış coinlerden kaçınmak için. Squeeze varsa volatilite düşük olabilir.",
            5,
            "Volatilite yeterli veya squeeze aktif — hareket potansiyeli var",
            "Volatilite çok düşük ve squeeze yok — sıkışmış piyasa",
            {"type": "bool_or_min", "yes_vol": 3.0, "wait_vol": 1.5,
             "input_labels": ["Günlük volatilite (%)", "Squeeze aktif mi?"],
             "unit_hint": "Volatilite: Yüzde. Squeeze: 1=Evet, 0=Hayır"}
        ),
    ]),
    StageConfig("Teknik analiz onayı", "Grafik, momentum ve trend göstergeleri", [
        QuestionItem(
            "Fiyat 50 EMA üzerinde veya Golden Cross yakın",
            "Trend yönü. EMA50 üzeri = boğa, altı = ayı.",
            10,
            "Fiyat EMA50 üzerinde — trend pozitif",
            "Fiyat EMA50 altında — trend negatif",
            {"type": "bool", "input_labels": ["Fiyat EMA50 üzerinde mi? (1=evet,0=hayır)"]}
        ),
        QuestionItem(
            "Fiyat EMA50'den makul uzaklıkta (<%5)",
            "Fiyat çok uzaksa giriş geç kalmış olabilir. <%3 ideal, %3-5 nötr, >%5 riskli.",
            7,
            "Fiyat EMA50'e yakın — giriş bölgesi uygun",
            "Fiyat EMA50'den çok uzak — geç kalmış hareket riski",
            {"type": "max", "yes": 3.0, "wait": 5.0,
             "input_labels": ["Fiyat-EMA50 uzaklığı (%)"],
             "unit_hint": "Yüzde cinsinden. <%3 ideal, %3-5 nötr, >%5 riskli"}
        ),
        QuestionItem(
            "Son 24 saat fiyat değişimi sert değil (<%10)",
            "Son 24 saatte sert yükseliş varsa giriş geç olabilir. <%5 normal, %5-10 dikkat, >%10 riskli.",
            6,
            "Son 24s değişim makul — giriş için uygun",
            "Son 24s sert yükseliş — geç kalmış olabilir",
            {"type": "max", "yes": 5.0, "wait": 10.0,
             "input_labels": ["Son 24s fiyat değişimi (%)"],
             "unit_hint": "Yüzde cinsinden. <%5 normal, %5-10 dikkat, >%10 riskli"}
        ),
        QuestionItem(
            # LABEL SEMANTIC CLOSURE (onaylı, controlled implementation): eski
            # metin "...veya 30'dan dönüş yapıyor" diye ikinci bir koşul vaat
            # ediyordu ama production hiçbir zaman bunu (temporal/önceki RSI
            # karşılaştırması) hesaplamadı -- yalnız statik [30,50] bandı
            # kontrol edildi (bkz. RealFetcher.fetch() 'rsi' dalı ve
            # ThresholdEngine 'range' tipi, ikisi de DEĞİŞMEDİ). Davranış
            # AYNEN korunuyor, yalnız label artık gerçek contract'ı tarif
            # ediyor. Eski metin _FACTOR_ID_BY_LABEL'de (History uyumluluğu
            # için) KORUNUYOR, silinmedi.
            "RSI (14) 30-50 aralığında",
            "Aşırı satım bölgesinden çıkış. 70+ aşırı alım = risk.",
            7,
            "RSI birikim bölgesinde veya dönüşte",
            "RSI aşırı alım bölgesinde veya zayıf",
            {"type": "range", "yes_min": 30, "yes_max": 50, "wait_min": 20, "wait_max": 70,
             "input_labels": ["RSI (14)"]}
        ),
        QuestionItem(
            # LABEL SEMANTIC CLOSURE (onaylı, controlled implementation): eski
            # metin "...veya boğa kesişimi" diyerek ayrı bir "yeni kesişim"
            # olayı vaat ediyordu; production `hist[-1]>0 and hist[-1]>=hist[-2]`
            # kontrol ediyor -- pozitif VE güçlenen histogram, ama kesişimin
            # "yeni" olup olmadığını (zero-line'ı bu barda mı, yoksa çok
            # önce mi geçti) ayırt etmiyor. Davranış AYNEN korunuyor, yalnız
            # label artık gerçek contract'ı tarif ediyor. Eski metin
            # _FACTOR_ID_BY_LABEL'de (History uyumluluğu için) KORUNUYOR.
            "MACD histogramı pozitif ve güçleniyor",
            "Momentum dönüşü. Negatiften pozitife geçiş = erken sinyal.",
            8,
            "MACD boğa sinyali veriyor — momentum dönüyor",
            "MACD ayı sinyalinde — momentum zayıf",
            {"type": "bool", "input_labels": ["MACD boğa sinyali mi? (1=evet,0=hayır)"]}
        ),
        QuestionItem(
            "OBV yükseliş trendinde veya pozitif ıraksama",
            "Hacim onaylı birikim. Fiyat düşerken OBV yükseliyorsa = gizli birikim.",
            7,
            "OBV yükselişte — hacim onaylı birikim",
            "OBV düşüyor — birikim zayıf veya dağıtım var",
            {"type": "bool", "input_labels": ["OBV yükselişte mi? (1=evet,0=hayır)"]}
        ),
        QuestionItem(
            "Bollinger Bands daralması (squeeze) veya üst band kırılımı",
            "Squeeze = patlama potansiyeli. Üst band kırılımı = momentum.",
            6,
            "Bollinger squeeze aktif veya üst band kırıldı",
            "Bollinger geniş ve squeeze yok",
            {"type": "bool", "input_labels": ["Squeeze veya üst band kırılımı? (1=evet,0=hayır)"]}
        ),
    ]),
    StageConfig("Zincir üstü veriler", "On-chain metrikler, balina hareketleri, ağ aktivitesi", [
        QuestionItem(
            "MVRV oranı < 2 (veya 1.5 altı ideal)",
            "Piyasa değeri / Realizasyon değeri. <1 = birikim, >3.5 = aşırı değerlenme.",
            8,
            "MVRV düşük — birikim bölgesi",
            "MVRV yüksek — aşırı değerlenme riski",
            {"type": "max", "yes": 2.0, "wait": 3.5,
             "input_labels": ["MVRV oranı"]}
        ),
        QuestionItem(
            "Borsalardan net çıkış var (soğuk cüzdan)",
            "Exchange reserve düşüyor mu? Uzun vadeli birikim.",
            9,
            "Borsalardan net çıkış — uzun vadeli birikim",
            "Borsalara giriş var — satış baskısı yüksek",
            {"type": "bool", "input_labels": ["Net çıkış var mı? (1=evet,0=hayır)"]},
            optional=True
        ),
        QuestionItem(
            "Balina cüzdan sayısı artıyor veya büyük OTC alımı",
            "1K+ token cüzdan sayısı veya büyük transferler.",
            7,
            "Balina birikimi artıyor — akıllı para giriyor",
            "Balina dağıtımı var — büyük oyuncular çıkıyor",
            {"type": "bool", "input_labels": ["Balina birikimi artıyor mu? (1=evet,0=hayır)"]},
            optional=True
        ),
        QuestionItem(
            "SOPR 1'in altına düşüp tekrar yukarı çıkıyor",
            "Spent Output Profit Ratio. Dönüş = trend devamı.",
            6,
            "SOPR dönüş yaptı — zarar satışları bitti",
            "SOPR zayıf — satış baskısı devam ediyor",
            {"type": "bool", "input_labels": ["SOPR 1 altından dönüş yaptı mı? (1=evet,0=hayır)"]}
        ),
        QuestionItem(
            "Aktif adres / işlem sayısı artıyor",
            "Ağ kullanımı büyüyor mu? Network growth.",
            5,
            "Aktif adres ve işlem sayısı artıyor — ağ büyüyor",
            "Ağ aktivitesi düşüyor — temel talep zayıf",
            {"type": "bool", "input_labels": ["Aktif adres/işlem artıyor mu? (1=evet,0=hayır)"]},
            optional=True
        ),
        QuestionItem(
            "Stablecoin inflow borsalara artıyor",
            "USDT/USDC borsalara giriyor mu? Alım gücü birikimi.",
            7,
            "Stablecoin inflow artıyor — alım gücü birikiyor",
            "Stablecoin çıkışı var — alım gücü azalıyor",
            {"type": "bool", "input_labels": ["Stablecoin inflow artıyor mu? (1=evet,0=hayır)"]},
            optional=True
        ),
    ]),
    StageConfig("Makro & türev piyasası", "DXY, funding, ETF, OI, BTC dominans, piyasa duygusu", [
        QuestionItem(
            "DXY (dolar endeksi) gevşiyor veya zirve yapmış",
            "DXY ile kripto genelde ters korelasyon.",
            8,
            "DXY gevşiyor — makro zemin kripto için uygun",
            "DXY güçleniyor — riskli varlıklar baskı altında",
            {"type": "bool", "input_labels": ["DXY gevşiyor veya zirve yaptı mı? (1=evet,0=hayır)"]}
        ),
        QuestionItem(
            "BTC günlük trendi boğa veya nötr (EMA50 üzerinde)",
            "BTC liderdir. BTC ayıdaysa altcoinler genelde daha kötü.",
            10,
            "BTC trendi pozitif — altcoinler için uygun zemin",
            "BTC trendi negatif — altcoinler baskı altında",
            {"type": "bool", "input_labels": ["BTC EMA50 üzerinde mi? (1=evet,0=hayır)"]}
        ),
        QuestionItem(
            "Funding rate dengeli (-%0.03 ile +%0.01 arası)",
            "Perp fonlama. Aşırı pozitif = kısa sıkışması, aşırı negatif = düşüş baskısı. Yüzde cinsinden girin.",
            7,
            "Funding dengeli — piyasa kaldıraç açısından sağlıklı",
            "Funding aşırı — kısa sıkışması veya düşüş baskısı riski",
            {"type": "funding", "yes_min": -0.03, "yes_max": 0.01, "wait_min": -0.10, "wait_max": 0.05,
             "input_labels": ["Funding rate (%)"],
             "unit_hint": "Yüzde cinsinden. Örn: %0.01 için 0.01, -%0.02 için -0.02"}
        ),
        QuestionItem(
            "Open Interest artıyor ve fiyatla uyumlu",
            "OI = türev piyasası ilgisi. Fiyatla birlikte artıyorsa = sağlam momentum.",
            6,
            "OI artıyor ve fiyatla uyumlu — yeni para giriyor",
            "OI düşüyor veya fiyatla uyumsuz — ilgi azalıyor",
            {"type": "bool", "input_labels": ["OI artıyor ve fiyatla uyumlu mu? (1=evet,0=hayır)"]}
        ),
        QuestionItem(
            "ETF'lere sürekli giriş var (varsa)",
            "Bitcoin/Ethereum ETF akışları. Kurumsal talep.",
            5,
            "ETF girişleri devam ediyor — kurumsal talep var",
            "ETF çıkışları var — kurumsal ilgi azalıyor",
            {"type": "bool", "input_labels": ["ETF'lere giriş var mı? (1=evet,0=hayır)"]}
        ),
        QuestionItem(
            "Fear & Greed Index 20-60 aralığında veya nötr-çekimser",
            "Aşırı korku (20-) = dip, aşırı açgözlülük (75+) = zirve.",
            5,
            "Piyasa duygusu nötr-çekimser — aşırı değil",
            "Piyasa duygusu aşırı açgözlü — zirve riski",
            {"type": "range", "yes_min": 20, "yes_max": 60, "wait_min": 10, "wait_max": 75,
             "input_labels": ["Fear & Greed Index"]}
        ),
        QuestionItem(
            "TOTAL3 (altcoin piyasa hacmi) artıyor veya BTC dominansı düşüyor",
            "Altseason sinyali. TOTAL3 yükseliyorsa para altcoinlere kayıyor.",
            6,
            "TOTAL3 artıyor veya BTC dominansı düşüyor — altseason zemin",
            "TOTAL3 düşüyor veya BTC dominansı yükseliyor — altcoin baskısı",
            {"type": "bool", "input_labels": ["TOTAL3 artıyor veya BTC dom. düşüyor mu? (1=evet,0=hayır)"]}
        ),
    ]),
    StageConfig("Risk & pozisyon yönetimi", "Giriş stratejisi, stop-loss, hedef, güvenlik", [
        QuestionItem(
            "Yakın destek seviyesi net ve yaklaşık %5 altında",
            "Stop-loss için mantıklı bir yer olmalı.",
            9,
            "Net destek ve stop-loss seviyesi var",
            "Destek net değil — stop-loss için mantıklı yer yok",
            {"type": "bool", "input_labels": ["Net destek var mı? (1=evet,0=hayır)"]}
        ),
        QuestionItem(
            "Yakın direnç hedefi net ve en az %10 yukarıda (R:R ≥ 1:2)",
            "Risk/ödül oranı en az 1:2 olmalı. %5 risk için %10+ hedef.",
            10,
            "Hedef net ve risk/ödül oranı makul (≥1:2)",
            "Hedef net değil veya risk/ödül oranı kötü",
            {"type": "ratio", "yes": 2.0, "wait": 1.5,
             "input_labels": ["Hedef kazanç (%)", "Stop-loss mesafesi (%)"],
             "unit_hint": "Yüzde cinsinden girin"}
        ),
        QuestionItem(
            "Yakın zamanda büyük token unlock yok (%10+ arz etkisi)",
            "Token unlock takvimi. %10+ arz etkisi = fiyat baskısı. ≤%3 güvenli, %3-10 nötr, >%10 riskli.",
            10,
            "Yakın unlock yok — arz şoku riski düşük",
            "Yakında büyük token unlock var — arz şoku riski",
            {"type": "max", "yes": 3, "wait": 10, "yes_inclusive": True,
             "input_labels": ["Yakın unlock oranı (% arz)"],
             "unit_hint": "Yüzde cinsinden. ≤3 güvenli, 3-10 nötr, >10 riskli"}
        ),
        QuestionItem(
            "Yakın zamanda hack / exploit / düzenleyici baskı yok",
            "Protokol güvenliği ve regülasyon riski. Yakın olay = volatilite artar.",
            8,
            "Yakın güvenlik/regülasyon olayı yok — risk düşük",
            "Yakın güvenlik/regülasyon olayı var — risk yüksek",
            {"type": "bool", "input_labels": ["Yakın olumsuz olay var mı? (0=evet yoksa, 1=varsa)"],
             "unit_hint": "0 = olay yok (olumlu), 1 = olay var (olumsuz)"}
        ),
        # RISK COVERAGE V2 / VOLATILITY RISK DIMENSION (controlled implementation,
        # onaylı contract): coin'in KENDİ causal-rolling volatilite dağılımına göre
        # (mevcut volatility_pct, p25/p90) adverse-excursion/stop-out riski. weight=0
        # KASITLI — bu factor yalnız risk_breadth/risk_reliable'a girer, risk_score'a
        # hiçbir numeric katkısı yoktur (calculate_stage_score() weight=0 item'ları
        # answered_count'a sayar ama w_total/w_yes'e sıfır katar). Signal taraftaki
        # volatility_or_squeeze'den (movement/fırsat okuması) TAMAMEN AYRI, zıt yönlü
        # bir risk okumasıdır (adverse-excursion) — aynı ham veri, iki farklı
        # factor_id, çift-sayım yok (weight=0 garanti eder).
        QuestionItem(
            "Volatilite risk seviyesi (bağımsız risk boyutu)",
            "Coin'in kendi geçmiş volatilite dağılımına göre mevcut adverse-excursion/"
            "stop-out riski. 'Fiyat düşecek' demez — yalnız fiyat yolunun ne kadar "
            "istikrarsız olduğunu, coin'in kendi normaline göre ölçer. Skora numeric "
            "katkısı yoktur (weight=0); yalnız risk kapsamı/güvenilirliği için bağımsız "
            "ikinci bir ölçüm sağlar.",
            0,
            "Volatilite düşük (coin'in kendi geçmişine göre) — daha öngörülebilir",
            "Volatilite yüksek (coin'in kendi geçmişine göre) — adverse-excursion riski yükselmiş",
            {"type": "bool", "input_labels": ["Manuel giriş desteklenmiyor (yalnız Level 1 otomatik)"]}
        ),
    ]),
]


# ═══════════════════════════════════════════════════════════════════
# 1.5 AI ANALİST — FACTOR_ID SÖZLEŞMESİ (yalnız metadata, karar mantığı yok)
# ═══════════════════════════════════════════════════════════════════
# STAGES_CONFIG'in 29 sorusunu AI Analist'in evidence namespace'i olan
# stabil 'factor_id'lere eşler. ScoreEngine/VetoEngine/verdict()/
# entry_timing() bu tabloyu HİÇ okumaz — yalnız AI Analist context
# üretimi kullanır. STAGES_CONFIG değişmeden bu tablo da değişmemeli;
# metric_id sütunu _metric_for_label()'in ürettiği değerlerle birebir
# aynı olacak şekilde elle senkronize tutulur.

FACTOR_ID_TABLE = {
    # factor_id: {metric_id veya None, component_metrics veya None, interpretation_guard veya None}
    "volume_vs_7d_avg": {"metric_id": "volume_24h", "component_metrics": None, "interpretation_guard": None},
    "exchange_listing_count": {"metric_id": "exchange_count", "component_metrics": None, "interpretation_guard": None},
    "volume_spread_combined": {"metric_id": None, "component_metrics": ["volume_24h", "spread_pct"], "interpretation_guard": None},
    "slippage_depth": {"metric_id": "slippage_pct", "component_metrics": None, "interpretation_guard": None},
    "volatility_or_squeeze": {"metric_id": "volatility_pct", "component_metrics": None, "interpretation_guard": None},
    "price_above_ema50_1h": {"metric_id": "price", "component_metrics": None,
        "interpretation_guard": "Bu, gerçek bir Golden Cross tespiti DEĞİLDİR — yalnızca coin'in kendi 1 saatlik EMA50'sine göre fiyat konumunu ölçer."},
    "ema50_distance": {"metric_id": "ema50_distance_pct", "component_metrics": None, "interpretation_guard": None},
    "price_change_24h": {"metric_id": "price_change_24h_pct", "component_metrics": None, "interpretation_guard": None},
    "rsi_zone": {"metric_id": "rsi", "component_metrics": None, "interpretation_guard": None},
    "macd_signal": {"metric_id": "macd_bullish", "component_metrics": None, "interpretation_guard": None},
    "obv_trend": {"metric_id": "obv_rising", "component_metrics": None, "interpretation_guard": None},
    "bollinger_squeeze_or_break": {"metric_id": "bb_squeeze_or_break", "component_metrics": None, "interpretation_guard": None},
    "mvrv_ratio": {"metric_id": "mvrv", "component_metrics": None,
        "interpretation_guard": "Yalnızca BTC için TradingView üzerinden hesaplanır; bu alan yalnızca BTC analizinde görünür."},
    "exchange_net_outflow": {"metric_id": "exchange_outflow", "component_metrics": None, "interpretation_guard": None},
    "whale_accumulation": {"metric_id": "whale_accumulation", "component_metrics": None, "interpretation_guard": None},
    "sopr_recovery": {"metric_id": "sopr_recovery", "component_metrics": None,
        "interpretation_guard": "Yalnızca BTC için hesaplanır; bu alan yalnızca BTC analizinde görünür."},
    "active_address_growth": {"metric_id": "active_addresses_rising", "component_metrics": None, "interpretation_guard": None},
    "stablecoin_inflow": {"metric_id": "stablecoin_inflow", "component_metrics": None, "interpretation_guard": None},
    "dxy_trend": {"metric_id": "dxy_loosening", "component_metrics": None, "interpretation_guard": None},
    "btc_daily_trend": {"metric_id": "btc_above_ema50", "component_metrics": None,
        "interpretation_guard": "Bu, BTC'nin GÜNLÜK (1D, tamamen kapanmış mumlar) EMA50'sine göre trendidir — coin'in kendi (1h) EMA50 durumuyla karıştırılmamalı, farklı varlık ve farklı zaman dilimidir."},
    "funding_balance": {"metric_id": "funding_pct", "component_metrics": None, "interpretation_guard": None},
    "oi_price_alignment": {"metric_id": "oi_rising_aligned", "component_metrics": None, "interpretation_guard": None},
    "etf_inflow": {"metric_id": "etf_inflow", "component_metrics": None, "interpretation_guard": None},
    "fear_greed_zone": {"metric_id": "fear_greed", "component_metrics": None, "interpretation_guard": None},
    "altseason_signal": {"metric_id": "total3_rising_or_btc_dom_falling", "component_metrics": None,
        "interpretation_guard": "Bu, iki koşuldan EN AZ BİRİNİN doğru olduğu anlamına gelir (TOTAL3 yükseliyor VEYA BTC dominansı düşüyor) — ikisinin birden doğrulandığı anlamına gelmez."},
    "clear_support": {"metric_id": "clear_support", "component_metrics": None, "interpretation_guard": None},
    "risk_reward_ratio": {"metric_id": "rr_ratio", "component_metrics": None, "interpretation_guard": None},
    "token_unlock_risk": {"metric_id": "unlock_pct", "component_metrics": None, "interpretation_guard": None},
    "recent_bad_event": {"metric_id": "recent_bad_event", "component_metrics": None, "interpretation_guard": None},
    "volatility_risk": {"metric_id": "volatility_risk", "component_metrics": None,
        "interpretation_guard": "Bu 'fiyat düşecek' anlamına gelmez -- yalnız coin'in "
        "KENDİ geçmiş volatilite dağılımına göre mevcut adverse-excursion/stop-out "
        "riskinin görece yüksek/düşük olduğunu ölçer. Skora numeric katkısı yoktur "
        "(weight=0), Signal taraftaki volatility_or_squeeze'den (fırsat/hareket "
        "okuması) tamamen ayrı ve zıt yönlü bir okumadır."},
}

# Level 1 otomatik modda YAPISAL OLARAK HER ZAMAN nodata olan factor_id'ler
# (güvenilir ücretsiz kaynak yok — geçici değil, kalıcı bir sistem sınırı).
STRUCTURALLY_UNAVAILABLE_FACTOR_IDS = frozenset({
    "clear_support", "risk_reward_ratio", "token_unlock_risk",
    "dxy_trend", "etf_inflow", "whale_accumulation", "stablecoin_inflow",
})

SEMANTIC_ROLE_BY_FACTOR_ID = {
    "volume_vs_7d_avg": "volume",
    "exchange_listing_count": "liquidity",
    "volume_spread_combined": "volume_liquidity",
    "slippage_depth": "liquidity",
    "volatility_or_squeeze": "market_regime",
    "price_above_ema50_1h": "trend",
    "ema50_distance": "entry_timing",
    "price_change_24h": "entry_timing",
    "rsi_zone": "momentum",
    "macd_signal": "momentum",
    "obv_trend": "momentum",
    "bollinger_squeeze_or_break": "momentum",
    "mvrv_ratio": "valuation",
    "exchange_net_outflow": "on_chain",
    "whale_accumulation": "on_chain",
    "sopr_recovery": "valuation",
    "active_address_growth": "on_chain",
    "stablecoin_inflow": "on_chain",
    "dxy_trend": "macro",
    "btc_daily_trend": "market_regime",
    "funding_balance": "derivatives",
    "oi_price_alignment": "derivatives",
    "etf_inflow": "macro",
    "fear_greed_zone": "sentiment",
    "altseason_signal": "market_regime",
    "clear_support": "risk",
    "risk_reward_ratio": "risk",
    "token_unlock_risk": "risk",
    "recent_bad_event": "veto_explanation",
    "volatility_risk": "risk",
}

# ═══════════════════════════════════════════════════════════════════
# 1.6 AI ANALİST — REASSESSMENT TRIGGER DETERMİNİSTİK GUARD KATMANI
# ═══════════════════════════════════════════════════════════════════
# FACTOR_TRANSITION_GUARDS: her factor_id'nin yes/wait/no durumunu
# KENDİ ölçtüğü koşulun tarafsız, tautolojik restatement'ı olarak
# tanımlar — STAGES_CONFIG'teki pro/con metinleri KULLANILMADI, çünkü
# bazıları (ör. oi_price_alignment'ın pro'su "yeni para giriyor",
# btc_daily_trend'in pro'su "altcoinler için uygun zemin") AI
# Analist'in kendi EVIDENCE SEMANTIC CEILING kuralını (AI_ANALYST_
# SYSTEM_PROMPT madde 5) ihlal ediyor. Buradaki metinler ya (a)
# bool-tipi factor'larda QuestionItem.label'ın olumlu/olumsuz hali
# (zaten tautolojik — nedensellik iddia etmiyor) ya da (b) banded-tipi
# factor'larda ThresholdEngine/RealFetcher'daki GERÇEK sayısal bandın
# birebir restatement'ıdır. Claude bu metni HİÇ ÜRETMEZ — yalnız GUI
# render aşamasında, Claude'un seçtiği factor_id+status/from_status/
# to_status'a göre buradan okunur. ScoreEngine/VetoEngine/ThresholdEngine/
# RealFetcher davranışını YANSITIR, asla değiştirmez veya yorumlamaz.
#
# price_change_24h NOTU (ENTRY TIMING V2 / P1 FIX — DÜZELTİLDİ):
# RealFetcher.fetch() ve MockFetcher._auto_status(), price_change_24h_pct
# status'unü artık abs(chg) ile hesaplıyor (ENTRY TIMING CORE FIX DESIGN
# AUDIT + gerçek 10-coin/180-gün replay ile kanıtlandı: simetrik büyüklükteki
# -%7/+%7 hareketler forward performansta karşılaştırılabilir derecede kötü,
# ama eski signed mantık yalnız pozitif hareketleri cezalandırıyordu). Guard
# metni ("+%X sınırı") zaten yön belirtmeden yazılmıştı, bu yüzden
# değişmedi — yalnız KOD artık metnin ifade ettiği yön-bağımsız semantiğe
# uyuyor.
FACTOR_TRANSITION_GUARDS = {
    "volume_vs_7d_avg": {"yes": "24s hacim, 7g ortalamanın en az 1.5 katı",
        "wait": "24s hacim, 7g ortalamanın 1.0-1.5 katı arasında",
        "no": "24s hacim, 7g ortalamanın 1.0 katının altında"},
    "exchange_listing_count": {"yes": "en az 2 büyük borsada listeli",
        "wait": "yalnızca 1 büyük borsada listeli",
        "no": "büyük borsada listeli değil"},
    "volume_spread_combined": {"yes": "24s hacim ≥5M$ ve spread ≤%0.1",
        "wait": "24s hacim ≥2M$ ve spread ≤%0.3",
        "no": "hacim/spread bu bantların dışında"},
    "slippage_depth": {"yes": "slippage %1'in altında",
        "wait": "slippage %1-2 arasında",
        "no": "slippage %2'nin üzerinde"},
    "volatility_or_squeeze": {"yes": "günlük volatilite %3'ün üzerinde",
        "wait": "günlük volatilite %1.5-3 arasında veya Bollinger squeeze aktif",
        "no": "günlük volatilite %1.5'in altında ve squeeze yok"},
    "price_above_ema50_1h": {"yes": "fiyat kendi 1 saatlik EMA50'sinin üzerinde",
        "no": "fiyat kendi 1 saatlik EMA50'sinin altında"},
    "ema50_distance": {"yes": "fiyat EMA50'ye %3'ten az uzaklıkta",
        "wait": "fiyat EMA50'ye %3-5 arasında uzaklıkta",
        "no": "fiyat EMA50'den %5'ten fazla uzaklıkta"},
    "price_change_24h": {"yes": "24s değişim +%5 sınırının altında",
        "wait": "24s değişim +%5 ile +%10 arasında",
        "no": "24s değişim +%10 sınırının üzerinde"},
    "rsi_zone": {"yes": "RSI 30-50 aralığında",
        "wait": "RSI 20-30 arasında veya 50-70 arasında",
        "no": "RSI 20'nin altında veya 70'in üzerinde"},
    "macd_signal": {"yes": "MACD histogramı boğa sinyali veriyor",
        "no": "MACD histogramı boğa sinyali vermiyor"},
    "obv_trend": {"yes": "OBV yükseliş trendinde",
        "no": "OBV yükseliş trendinde değil"},
    "bollinger_squeeze_or_break": {"yes": "Bollinger squeeze aktif veya üst band kırılımı gerçekleşti",
        "no": "Bollinger squeeze yok ve üst band kırılımı yok"},
    "mvrv_ratio": {"yes": "MVRV 2'nin altında",
        "wait": "MVRV 2-3.5 arasında",
        "no": "MVRV 3.5'in üzerinde"},
    "exchange_net_outflow": {"yes": "borsalardan net çıkış var",
        "no": "borsalardan net çıkış yok"},
    "whale_accumulation": {"yes": "balina cüzdan sayısı artıyor veya büyük OTC alımı var",
        "no": "balina birikimi/OTC alımı görülmüyor"},
    "sopr_recovery": {"yes": "SOPR 1 seviyesinin altından tekrar yukarı dönüş yaptı",
        "no": "SOPR bu dönüşü göstermiyor"},
    "active_address_growth": {"yes": "aktif adres/işlem sayısı artıyor",
        "no": "aktif adres/işlem sayısı artmıyor"},
    "stablecoin_inflow": {"yes": "borsalara stablecoin girişi artıyor",
        "no": "borsalara stablecoin girişi artmıyor"},
    "dxy_trend": {"yes": "DXY gevşiyor veya zirve yapmış",
        "no": "DXY güçleniyor"},
    "btc_daily_trend": {"yes": "BTC günlük trendi EMA50 üzerinde",
        "no": "BTC günlük trendi EMA50 altında"},
    "funding_balance": {"yes": "funding oranı -%0.03 ile +%0.01 arasında",
        "wait": "funding oranı -%0.10/-%0.03 veya +%0.01/+%0.05 bandında",
        "no": "funding oranı -%0.10'un altında veya +%0.05'in üzerinde"},
    "oi_price_alignment": {"yes": "OI değişimi %0.5'in üzerinde ve fiyatla birlikte artıyor",
        "wait": "OI değişimi pozitif ama %0.5'in altında",
        "no": "OI değişimi negatif/sıfır veya fiyatla uyumsuz"},
    "etf_inflow": {"yes": "ETF'lere sürekli giriş var",
        "no": "ETF çıkışları var"},
    "fear_greed_zone": {"yes": "Fear & Greed Index 20-60 aralığında",
        "wait": "Fear & Greed Index 10-20 arasında veya 60-75 arasında",
        "no": "Fear & Greed Index 10'un altında veya 75'in üzerinde"},
    "altseason_signal": {"yes": "TOTAL3 artıyor veya BTC dominansı düşüyor (en az biri)",
        "no": "ne TOTAL3 artışı ne BTC dominans düşüşü var"},
    "clear_support": {"yes": "yakın destek seviyesi net ve yaklaşık %5 altında",
        "no": "net bir destek seviyesi yok"},
    "risk_reward_ratio": {"yes": "risk/ödül oranı en az 1:2",
        "wait": "risk/ödül oranı 1:1.5-1:2 arasında",
        "no": "risk/ödül oranı 1:1.5'in altında"},
    "token_unlock_risk": {"yes": "yakın unlock oranı arzın %3'ünden az",
        "wait": "yakın unlock oranı arzın %3-10'u arasında",
        "no": "yakın unlock oranı arzın %10'undan fazla"},
    # recent_bad_event KASITLI OLARAK YOK — olay-temelli, eşik/bant yok,
    # reassessment trigger havuzuna hiçbir zaman giremez.
}

# Bool-tipi (yalnız yes/no üreten, "wait" durumu YAPISAL OLARAK
# YOK) factor_id'ler. NOT: oi_price_alignment STAGES_CONFIG'te
# thresholds.type="bool" olsa da, RealFetcher'ın Level 1 otomatik
# hesaplaması (satır ~1036-1047) bu factor için GERÇEK 3-değerli
# (yes/wait/no) bir status üretiyor — bu yüzden bilinçli olarak bu
# kümenin DIŞINDA tutuluyor (gerçek BTC/SOL context capture'larında
# status="wait" doğrulandı). Reassessment trigger validator'ı bu
# kümedeki factor'lar için to_status/from_status olarak "wait"
# gelirse reddeder.
BOOL_TYPE_FACTOR_IDS = frozenset({
    "price_above_ema50_1h", "macd_signal", "obv_trend",
    "bollinger_squeeze_or_break", "sopr_recovery", "btc_daily_trend",
    "altseason_signal", "exchange_net_outflow",
    "whale_accumulation", "active_address_growth", "stablecoin_inflow",
    "dxy_trend", "etf_inflow", "clear_support",
})

# QuestionItem.label -> factor_id (STAGES_CONFIG ile 1:1, answers[i]["question"]
# üzerinden eşleştirmek için). _metric_for_label()'in substring-eşleştirme
# deseniyle KARIŞTIRILMAZ — burada tam label metni kullanılıyor, STAGES_CONFIG
# değişmediği sürece stabildir.
_FACTOR_ID_BY_LABEL = {
    "24s hacim / 7g ortalama ≥ 1.5x": "volume_vs_7d_avg",
    "Coin en az 2 büyük borsada listeli": "exchange_listing_count",
    "24s hacim > 5M$ ve spread makul": "volume_spread_combined",
    "Likidite derinliği yeterli (slippage <%1)": "slippage_depth",
    "Günlük volatilite > %3 veya Bollinger squeeze aktif": "volatility_or_squeeze",
    "Fiyat 50 EMA üzerinde veya Golden Cross yakın": "price_above_ema50_1h",
    "Fiyat EMA50'den makul uzaklıkta (<%5)": "ema50_distance",
    "Son 24 saat fiyat değişimi sert değil (<%10)": "price_change_24h",
    "RSI (14) 30-50 aralığında": "rsi_zone",
    "MACD histogramı pozitif ve güçleniyor": "macd_signal",
    # LEGACY LABEL ALIASES (label semantic closure turu, onaylı): eski
    # History kayıtlarındaki answers[i]["question"] hâlâ ESKİ metni taşıyor
    # -- bu iki satır SİLİNMEDEN, aynı factor_id'ye çözülmeye devam etmesini
    # sağlıyor (compute_risk_reliable/History detay/AI context factor_id
    # lookup'ları hiç bozulmadan). STAGES_CONFIG'in KENDİSİ artık yeni
    # metni kullanıyor -- bu iki alias yalnız GEÇMİŞ kayıtlar için.
    "RSI (14) 30-50 aralığında veya 30'dan dönüş yapıyor": "rsi_zone",
    "MACD histogram yeşile dönüyor veya boğa kesişimi": "macd_signal",
    "OBV yükseliş trendinde veya pozitif ıraksama": "obv_trend",
    "Bollinger Bands daralması (squeeze) veya üst band kırılımı": "bollinger_squeeze_or_break",
    "MVRV oranı < 2 (veya 1.5 altı ideal)": "mvrv_ratio",
    "Borsalardan net çıkış var (soğuk cüzdan)": "exchange_net_outflow",
    "Balina cüzdan sayısı artıyor veya büyük OTC alımı": "whale_accumulation",
    "SOPR 1'in altına düşüp tekrar yukarı çıkıyor": "sopr_recovery",
    "Aktif adres / işlem sayısı artıyor": "active_address_growth",
    "Stablecoin inflow borsalara artıyor": "stablecoin_inflow",
    "DXY (dolar endeksi) gevşiyor veya zirve yapmış": "dxy_trend",
    "BTC günlük trendi boğa veya nötr (EMA50 üzerinde)": "btc_daily_trend",
    "Funding rate dengeli (-%0.03 ile +%0.01 arası)": "funding_balance",
    "Open Interest artıyor ve fiyatla uyumlu": "oi_price_alignment",
    "ETF'lere sürekli giriş var (varsa)": "etf_inflow",
    "Fear & Greed Index 20-60 aralığında veya nötr-çekimser": "fear_greed_zone",
    "TOTAL3 (altcoin piyasa hacmi) artıyor veya BTC dominansı düşüyor": "altseason_signal",
    "Yakın destek seviyesi net ve yaklaşık %5 altında": "clear_support",
    "Yakın direnç hedefi net ve en az %10 yukarıda (R:R ≥ 1:2)": "risk_reward_ratio",
    "Yakın zamanda büyük token unlock yok (%10+ arz etkisi)": "token_unlock_risk",
    "Yakın zamanda hack / exploit / düzenleyici baskı yok": "recent_bad_event",
    "Volatilite risk seviyesi (bağımsız risk boyutu)": "volatility_risk",
}

# VETO_RULES'un reason metni -> factor_id. VETO_RULES'un kendi reason
# string'leri BENZERSİZ olduğu için (6 kural, 6 farklı metin) bu ters-eşleme
# güvenilir. VETO_RULES metni değişirse bu tablo da güncellenmelidir —
# VETO_RULES "dokunulmaz" kabul edildiği için risk düşük, ama örtük bir
# bağımlılık olduğu burada açıkça belgeleniyor.
_FACTOR_ID_BY_VETO_REASON = {
    v_reason: _FACTOR_ID_BY_LABEL.get(v_label)
    for (_v_stage, v_label, v_reason, _v_cond, _v_trigger) in VETO_RULES
}

# ═══════════════════════════════════════════════════════════════════
# 1.7 AI ANALİST — REASSESSMENT TRIGGER SELECTION v4 (yalnız salt-okunur
# introspeksiyon: STAGES_CONFIG'in weight'i ve VETO_RULES'un kendi kural
# tablosu okunuyor, hiçbir karar mantığı değiştirilmiyor/yeniden
# üretilmiyor. VetoEngine/ScoreEngine hiç çağrılmıyor.)
# ═══════════════════════════════════════════════════════════════════

# factor_id -> (weight, stage_title). STAGES_CONFIG'ten bire bir türetildi.
_FACTOR_ID_WEIGHT_STAGE = {}
for _stage_cfg in STAGES_CONFIG:
    for _item in _stage_cfg.items:
        _fid = _FACTOR_ID_BY_LABEL.get(_item.label)
        if _fid:
            _FACTOR_ID_WEIGHT_STAGE[_fid] = (_item.weight, _stage_cfg.title)
del _stage_cfg, _item, _fid


def base_priority(factor_id: str) -> int:
    """weight × SIGNAL_STAGE_WEIGHTS[stage] — motorun mevcut ağırlık
    yapılandırmasından türetilmiş STATİK bir seçim önceliği sinyalidir.
    Motorun signal_score hesaplamasında GERÇEKTEN kullandığı iki ağırlığın
    (item.weight, stage weight) çarpımı olsa da, coverage/status/stage-
    normalizasyonu dahil edilmediği için "motorun toplam kararına gerçek
    katkısı" İDDİASI TAŞIMAZ — yalnız uygun adaylar arasında (aynı
    selection_basis içinde) tie-break/yardımcı sıralama sinyalidir. Tek
    başına hiçbir reassessment trigger'ı seçtirmez."""
    wt = _FACTOR_ID_WEIGHT_STAGE.get(factor_id)
    if not wt:
        return 0
    weight, stage = wt
    return weight * SIGNAL_STAGE_WEIGHTS.get(stage, 0)


def decision_critical_to_statuses(factor_id: str, symbol: str) -> frozenset:
    """VETO_RULES'un kendi kural tablosunun salt-okunur introspeksiyonu —
    VetoEngine'i hiç çağırmaz, yeni bir karar üretmez. Bu factor_id için,
    VERİLEN sembolde, hangi status(lar)a geçişin VetoEngine tarafından
    gerçekten veto olarak değerlendirileceğini döner (boşsa bu factor bu
    sembol için decision-critical değildir — ör. btc_daily_trend, BTC'nin
    kendisi için boş döner çünkü VETO_RULES'taki ilgili kuralın
    condition_func'ı `symbol != "BTC"` şartına bağlıdır)."""
    triggers = set()
    for _stage, _label, _reason, _cond, _trigger_answers in VETO_RULES:
        if _FACTOR_ID_BY_LABEL.get(_label) == factor_id and _cond(symbol):
            triggers |= set(_trigger_answers)
    return frozenset(triggers)


# Davranışsal probe sembolleri: condition_func'ın gerçekten sembole bağlı olup
# olmadığını, kaynak kodu ayrıştırmadan (string/NLP tahmini YOK), yalnız
# fonksiyonu çağırıp sonucu karşılaştırarak tespit etmek için kullanılır.
_SCOPE_PROBE_SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "___PROBE___")


def decision_critical_scope(factor_id: str, symbol: str) -> Optional[str]:
    """VETO_RULES'taki condition_func'ı, kaynak koduna hiç bakmadan, yalnız
    birden fazla farklı sembolle ÇAĞIRARAK (davranışsal kara-kutu testi)
    sınıflandırır: sonuç tüm probe sembollerinde aynıysa "universal",
    sembole göre değişiyorsa "symbol_specific". Bu factor için hiç veto
    kuralı yoksa (decision-critical değilse) None döner."""
    matching_conds = [_cond for _stage, _label, _reason, _cond, _trigger
                       in VETO_RULES if _FACTOR_ID_BY_LABEL.get(_label) == factor_id]
    if not matching_conds:
        return None
    results = {cond(s) for cond in matching_conds for s in _SCOPE_PROBE_SYMBOLS}
    return "universal" if len(results) <= 1 else "symbol_specific"


def preferred_decision_critical_factor(usable_factors: list, symbol: str) -> Optional[str]:
    """Birden fazla decision-critical aday varsa (bu oturumda gerçek API
    testiyle keşfedildi — exchange_listing_count/volume_spread_combined/
    slippage_depth HER sembolde, btc_daily_trend BTC hariç HER sembolde
    decision-critical), reassessment_triggers'ta gösterilecek TEK adayı
    tamamen deterministik seçer: symbol_specific > universal, eşitlikte
    base_priority, son çare factor_id lexical sıra. Claude'a bu seçim hiç
    bırakılmaz — VetoEngine'in kendisi HİÇ etkilenmez, yalnız AI Analist
    katmanının kullanıcıya göstereceği "shortlist" daraltılır."""
    candidates = []
    for f in usable_factors:
        fid = f["factor_id"]
        if decision_critical_to_statuses(fid, symbol):
            scope = decision_critical_scope(fid, symbol)
            candidates.append((fid, scope, base_priority(fid)))
    if not candidates:
        return None
    symbol_specific = [c for c in candidates if c[1] == "symbol_specific"]
    pool = symbol_specific if symbol_specific else candidates
    pool.sort(key=lambda c: (-c[2], c[0]))
    return pool[0][0]


def format_display_value(factor_id: str, raw_value) -> Optional[str]:
    """CONTEXT + PROMPT CONTRACT v1.1'deki hassasiyet tablosunun tek merkezi
    uygulaması. Claude'a giden TEK sayısal temsil budur — Claude kendi
    yuvarlamasını/hesabını yapmaz, yalnız bu string'i aynen kullanabilir."""
    if raw_value is None:
        return None
    if isinstance(raw_value, bool):
        return None  # bool metrikler için sayı yok, yalnız status anlam taşır
    if isinstance(raw_value, str):
        # exchange_count'un "≥N" alt-sınır string'i gibi zaten hazır metinler
        return raw_value
    try:
        v = float(raw_value)
    except (TypeError, ValueError):
        return None

    two_decimal_pct = {"rsi_zone", "ema50_distance", "price_change_24h",
                        "volatility_or_squeeze", "slippage_depth"}
    if factor_id in two_decimal_pct:
        if factor_id == "rsi_zone":
            return f"{v:.2f}"
        return f"%{v:.2f}"
    if factor_id == "funding_balance":
        return f"%{v:.3f}"
    if factor_id == "mvrv_ratio":
        return f"{v:.2f}"
    if factor_id == "volume_vs_7d_avg":
        return f"{v:.1f}M$"
    if factor_id == "exchange_listing_count":
        return f"{int(v)}"
    return f"{v:.2f}"


def format_component_display_value(component_metric_id: str, raw_value) -> Optional[str]:
    """volume_spread_combined gibi birleşik factor'ların ALT bileşenleri için
    (kendi factor_id'leri olmadığından format_display_value'ya giremezler).
    Aynı merkezi hassasiyet mantığı, yalnız metric_id ile anahtarlanıyor."""
    if raw_value is None:
        return None
    try:
        v = float(raw_value)
    except (TypeError, ValueError):
        return None
    if component_metric_id == "volume_24h":
        return f"{v:.1f}M$"
    if component_metric_id == "spread_pct":
        return f"%{v:.2f}"
    return f"{v:.2f}"


# ═══════════════════════════════════════════════════════════════════
# 2. VERİ SAĞLAYICI ARAYÜZÜ
# ═══════════════════════════════════════════════════════════════════

class DataPoint:
    def __init__(self, value=None, status="missing", source="", available=False,
                 reason="", timestamp=None):
        self.value = value
        self.status = status
        self.source = source
        self.available = available
        self.reason = reason
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()

class BaseFetcher(ABC):
    @abstractmethod
    def fetch(self, symbol: str, metric: str) -> DataPoint:
        pass

class MockFetcher(BaseFetcher):
    """
    5 test profili ile gerçekçi mock veri üreten modül.
    """
    def __init__(self, seed=None):
        import random
        self.rng = random.Random(seed)
        self._mock_db = self._build_mock_db()

    def _build_mock_db(self):
        return {
            # Gerçek coinler (demo)
            "BTC": {
                "volume_24h": 28000.0, "volume_7d_avg": 15000.0,
                "exchange_count": 4, "spread_pct": 0.05,
                "slippage_pct": 0.3, "volatility_pct": 4.2, "squeeze_active": 1,
                "price": 67200.0, "ema50": 64500.0, "rsi": 42.0,
                "ema50_distance_pct": 4.2, "price_change_24h_pct": 2.8,
                "macd_bullish": 1, "obv_rising": 1, "bb_squeeze_or_break": 1,
                "mvrv": 1.8, "exchange_outflow": 1, "whale_accumulation": 1,
                "sopr_recovery": 1, "active_addresses_rising": 1, "stablecoin_inflow": 1,
                "dxy_loosening": 1, "btc_above_ema50": 1, "funding_pct": 0.005,
                "oi_rising_aligned": 1, "etf_inflow": 1, "fear_greed": 55,
                "total3_rising_or_btc_dom_falling": 1,
                "clear_support": 1, "rr_ratio": 2.5, "unlock_pct": 0, "recent_bad_event": 0,
            },
            "ETH": {
                "volume_24h": 15000.0, "volume_7d_avg": 12000.0,
                "exchange_count": 4, "spread_pct": 0.08,
                "slippage_pct": 0.4, "volatility_pct": 3.5, "squeeze_active": 0,
                "price": 3520.0, "ema50": 3450.0, "rsi": 48.0,
                "ema50_distance_pct": 2.0, "price_change_24h_pct": 1.5,
                "macd_bullish": 1, "obv_rising": 1, "bb_squeeze_or_break": 0,
                "mvrv": 2.1, "exchange_outflow": 1, "whale_accumulation": 0,
                "sopr_recovery": 1, "active_addresses_rising": 1, "stablecoin_inflow": 1,
                "dxy_loosening": 1, "btc_above_ema50": 1, "funding_pct": 0.008,
                "oi_rising_aligned": 1, "etf_inflow": 1, "fear_greed": 55,
                "total3_rising_or_btc_dom_falling": 1,
                "clear_support": 1, "rr_ratio": 2.0, "unlock_pct": 0, "recent_bad_event": 0,
            },
            "SOL": {
                "volume_24h": 4200.0, "volume_7d_avg": 3500.0,
                "exchange_count": 3, "spread_pct": 0.15,
                "slippage_pct": 0.8, "volatility_pct": 5.1, "squeeze_active": 1,
                "price": 152.0, "ema50": 145.0, "rsi": 38.0,
                "ema50_distance_pct": 4.8, "price_change_24h_pct": 3.2,
                "macd_bullish": 1, "obv_rising": 1, "bb_squeeze_or_break": 1,
                "mvrv": 1.5, "exchange_outflow": 1, "whale_accumulation": 1,
                "sopr_recovery": 0, "active_addresses_rising": 1, "stablecoin_inflow": 0,
                "dxy_loosening": 1, "btc_above_ema50": 1, "funding_pct": -0.002,
                "oi_rising_aligned": 1, "etf_inflow": 0, "fear_greed": 55,
                "total3_rising_or_btc_dom_falling": 1,
                "clear_support": 1, "rr_ratio": 2.4, "unlock_pct": 0, "recent_bad_event": 0,
            },
            # Test profilleri
            "TEST_GOOD": {
                "volume_24h": 50000.0, "volume_7d_avg": 20000.0,
                "exchange_count": 5, "spread_pct": 0.03,
                "slippage_pct": 0.2, "volatility_pct": 5.0, "squeeze_active": 1,
                "price": 100.0, "ema50": 90.0, "rsi": 38.0,
                "macd_bullish": 1, "obv_rising": 1, "bb_squeeze_or_break": 1,
                "mvrv": 1.2, "exchange_outflow": 1, "whale_accumulation": 1,
                "sopr_recovery": 1, "active_addresses_rising": 1, "stablecoin_inflow": 1,
                "dxy_loosening": 1, "btc_above_ema50": 1, "funding_pct": -0.01,
                "oi_rising_aligned": 1, "etf_inflow": 1, "fear_greed": 45,
                "total3_rising_or_btc_dom_falling": 1,
                "clear_support": 1, "rr_ratio": 3.0, "unlock_pct": 0, "recent_bad_event": 0,
            },
            "TEST_MEDIUM": {
                "volume_24h": 8000.0, "volume_7d_avg": 7000.0,
                "exchange_count": 3, "spread_pct": 0.2,
                "slippage_pct": 0.8, "volatility_pct": 2.5, "squeeze_active": 0,
                "price": 50.0, "ema50": 52.0, "rsi": 55.0, "ema50_distance_pct": 3.85,
                "macd_bullish": 0, "obv_rising": 0, "bb_squeeze_or_break": 0,
                "mvrv": 2.5, "exchange_outflow": 0, "whale_accumulation": 0,
                "sopr_recovery": 0, "active_addresses_rising": 1, "stablecoin_inflow": 0,
                "dxy_loosening": 1, "btc_above_ema50": 1, "funding_pct": 0.03,
                "oi_rising_aligned": 0, "etf_inflow": 0, "fear_greed": 65,
                "total3_rising_or_btc_dom_falling": 0,
                "clear_support": 1, "rr_ratio": 1.8, "unlock_pct": 5, "recent_bad_event": 0,
            },
            "TEST_BAD": {
                "volume_24h": 500.0, "volume_7d_avg": 800.0,
                "exchange_count": 1, "spread_pct": 1.5,
                "slippage_pct": 3.0, "volatility_pct": 1.0, "squeeze_active": 0,
                "price": 10.0, "ema50": 15.0, "rsi": 75.0,
                "macd_bullish": 0, "obv_rising": 0, "bb_squeeze_or_break": 0,
                "mvrv": 4.0, "exchange_outflow": 0, "whale_accumulation": 0,
                "sopr_recovery": 0, "active_addresses_rising": 0, "stablecoin_inflow": 0,
                "dxy_loosening": 0, "btc_above_ema50": 0, "funding_pct": 0.08,
                "oi_rising_aligned": 0, "etf_inflow": 0, "fear_greed": 85,
                "total3_rising_or_btc_dom_falling": 0,
                "clear_support": 0, "rr_ratio": 0.8, "unlock_pct": 15, "recent_bad_event": 1,
            },
            "TEST_VETO": {
                "volume_24h": 8000.0, "volume_7d_avg": 6000.0,
                "exchange_count": 1, "spread_pct": 0.1,
                "slippage_pct": 0.5, "volatility_pct": 4.0, "squeeze_active": 1,
                "price": 80.0, "ema50": 75.0, "rsi": 40.0,
                "macd_bullish": 1, "obv_rising": 1, "bb_squeeze_or_break": 1,
                "mvrv": 1.5, "exchange_outflow": 1, "whale_accumulation": 1,
                "sopr_recovery": 1, "active_addresses_rising": 1, "stablecoin_inflow": 1,
                "dxy_loosening": 1, "btc_above_ema50": 1, "funding_pct": 0.005,
                "oi_rising_aligned": 1, "etf_inflow": 1, "fear_greed": 50,
                "total3_rising_or_btc_dom_falling": 1,
                "clear_support": 1, "rr_ratio": 2.5, "unlock_pct": 0, "recent_bad_event": 0,
            },
            "TEST_MISSING": {
                "volume_24h": 20000.0, "volume_7d_avg": 15000.0,
                "exchange_count": 4, "spread_pct": 0.05,
                "slippage_pct": 0.3, "volatility_pct": 4.0, "squeeze_active": 1,
                "price": 120.0, "ema50": 110.0, "rsi": 42.0,
                "macd_bullish": 1, "obv_rising": 1, "bb_squeeze_or_break": 1,
                "mvrv": 1.8, "exchange_outflow": 1, "whale_accumulation": 1,
                "sopr_recovery": 1, "active_addresses_rising": 1, "stablecoin_inflow": 1,
                "dxy_loosening": 1, "btc_above_ema50": 1, "funding_pct": 0.005,
                "oi_rising_aligned": 1, "etf_inflow": 1, "fear_greed": 55,
                "total3_rising_or_btc_dom_falling": 1,
                "clear_support": 1, "rr_ratio": 2.5, "unlock_pct": 0, "recent_bad_event": 0,
            },
        }

    def fetch(self, symbol: str, metric: str) -> DataPoint:
        symbol = symbol.upper()

        # TEST_MISSING için bazı metrikleri eksik bırak (önce kontrol et!)
        if symbol == "TEST_MISSING" and metric in (
            "mvrv", "sopr_recovery", "stablecoin_inflow", "fear_greed",
            "whale_accumulation", "active_addresses_rising"
        ):
            return DataPoint(value=None, status="missing", source="MockFetcher",
                             available=False, reason="Metric not available for TEST_MISSING")

        data = self._mock_db.get(symbol, {})
        if metric in data:
            val = data[metric]
            status = self._auto_status(metric, val, data)
            return DataPoint(value=val, status=status, source="MockFetcher",
                             available=True, reason="Mock data")
        return DataPoint(value=None, status="missing", source="MockFetcher",
                         available=False, reason="Metric not supported")

    def _auto_status(self, metric: str, value, full_data: dict) -> str:
        if metric == "volume_24h":
            avg = full_data.get("volume_7d_avg", 1)
            if avg == 0: return "nodata"
            ratio = value / avg
            if ratio >= 1.5: return "yes"
            if ratio >= 1.0: return "wait"
            return "no"

        if metric == "volume_7d_avg":
            return "yes"

        if metric == "exchange_count":
            if value >= 2: return "yes"
            if value >= 1: return "wait"
            return "no"

        if metric == "spread_pct":
            if value <= 0.1: return "yes"
            if value <= 0.3: return "wait"
            return "no"

        if metric == "slippage_pct":
            return ThresholdEngine.eval_max(value, {"yes": 1.0, "wait": 2.0})

        if metric == "volatility_pct":
            vol = value
            squeeze = full_data.get("squeeze_active", 0)
            if vol >= 3.0: return "yes"
            if vol >= 1.5 or squeeze: return "wait"
            return "no"

        if metric == "squeeze_active":
            return "yes" if value else "no"

        if metric == "price":
            ema50 = full_data.get("ema50", None)
            if ema50 is None: return "nodata"
            return "yes" if value > ema50 else "no"

        if metric == "ema50":
            return "yes"

        if metric == "rsi":
            if 30 <= value <= 50: return "yes"
            if 20 <= value < 30 or 50 < value <= 70: return "wait"
            return "no"

        if metric in ("macd_bullish", "obv_rising", "bb_squeeze_or_break",
                       "exchange_outflow", "whale_accumulation", "sopr_recovery",
                       "active_addresses_rising", "stablecoin_inflow",
                       "dxy_loosening", "btc_above_ema50", "oi_rising_aligned",
                       "etf_inflow", "total3_rising_or_btc_dom_falling",
                       "clear_support"):
            return "yes" if value else "no"

        if metric == "funding_pct":
            # Dengeli eşik: -0.03 ile +0.01 arası EVET
            if -0.03 <= value <= 0.01: return "yes"
            if -0.10 <= value < -0.03 or 0.01 < value <= 0.05: return "wait"
            return "no"

        if metric == "fear_greed":
            if 20 <= value <= 60: return "yes"
            if 10 <= value < 20 or 60 < value <= 75: return "wait"
            return "no"

        if metric == "mvrv":
            return ThresholdEngine.eval_max(value, {"yes": 2.0, "wait": 3.5})

        if metric == "rr_ratio":
            if value >= 2.0: return "yes"
            if value >= 1.5: return "wait"
            return "no"

        if metric == "unlock_pct":
            return ThresholdEngine.eval_max(value, {"yes": 3, "wait": 10, "yes_inclusive": True})

        if metric == "recent_bad_event":
            return "no" if value else "yes"

        if metric == "ema50_distance_pct":
            return ThresholdEngine.eval_max(value, {"yes": 3.0, "wait": 5.0})

        if metric == "price_change_24h_pct":
            # ENTRY TIMING V2 / P1 FIX -- bkz. RealFetcher.fetch() ile AYNI gerekçe.
            return ThresholdEngine.eval_max(abs(value), {"yes": 5.0, "wait": 10.0})

        return "yes"


# ═══════════════════════════════════════════════════════════════════
# 2b. GERÇEK VERİ SAĞLAYICILARI (Level 1 — v6)
# ═══════════════════════════════════════════════════════════════════

_CG_DEBUG_REQUEST_COUNT = [0]  # yalnız teşhis: debug_label verilen çağrılar sayılır


def _http_get_json(url: str, params: dict = None, headers: dict = None, timeout: int = 12,
                    debug_label: str = None, session=None):
    """Ortak HTTP GET yardımcı fonksiyonu. Hata durumunda None döner, asla raise etmez.
    debug_label yalnız TEŞHİS amaçlı — verilmezse davranış/performans tamamen aynı.
    session verilirse (yalnız CoinGeckoFetcher veriyor — connection reuse için)
    session.get() kullanılır; verilmezse ESKİ davranış (bağımsız requests.get(),
    Binance/CMC/diğer TÜM çağıranlar için hiç değişmedi). Ne retry ne backoff/sleep
    var — bu fonksiyon HER ZAMAN tam olarak TEK istek atar (session verilse bile,
    session yalnız bağlantı havuzunu paylaştırır, retry/backoff eklemez)."""
    if requests is None:
        return None
    t0 = time.time()
    status_repr = "exception"
    try:
        getter = session.get if session is not None else requests.get
        resp = getter(url, params=params, headers=headers, timeout=timeout)
        status_repr = str(resp.status_code)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        status_repr = f"exception:{type(e).__name__}"
        return None
    finally:
        if debug_label:
            _CG_DEBUG_REQUEST_COUNT[0] += 1
            print(f"[CG] #{_CG_DEBUG_REQUEST_COUNT[0]} {debug_label} -> "
                  f"{status_repr}, {time.time() - t0:.1f}s", flush=True)


class CoinGeckoFetcher:
    """CoinGecko public API — coin id çözümleme, borsa listesi, market verisi."""

    BASE = "https://api.coingecko.com/api/v3"
    MAJOR_EXCHANGES = {"binance", "gdax", "coinbase-exchange", "coinbase_exchange",
                        "kraken", "okex", "okx"}
    # Server-side filtreleme için CoinGecko exchange_ids parametresi — canlı test ile
    # doğrulandı (BTC/ETH/MEME'de full-pagination ile birebir aynı unique-major-count
    # sonucu üretiyor, yalnız sayfa sayısını azaltıyor, pagination hâlâ gerekebiliyor).
    _MAJOR_EXCHANGE_IDS_CSV = ",".join(sorted(MAJOR_EXCHANGES))

    _coins_list_cache = None  # symbol_lower -> [ {id, name, symbol}, ... ]

    # VERIFIED COIN-ID PROCESS CACHE (Model D, onaylı controlled implementation):
    # symbol_lower -> UNAMBIGUOUS, GERÇEKTEN DOĞRULANMIŞ coin_id. `_coins_list_cache`
    # ile AYNI yaşam süresi (class-level, process ömrü boyunca, RealFetcher/
    # CoinGeckoFetcher instance'ları arasında paylaşılır). Yalnız POZİTİF sonuç
    # yazılır -- unsupported/ambiguous/inconclusive/429/timeout/malformed HİÇBİRİ
    # buraya girmez (negatif cache YOK, `None` girişi YOK). Bu, mevcut instance-level
    # `_id_cache`'in YERİNE değil, ONA EK bir hızlı-yol katmanı -- `_id_cache`'in
    # kendi (analiz-içi negatif cache dahil) davranışı DEĞİŞMEDİ.
    _verified_id_cache: Dict[str, str] = {}
    _verified_id_cache_lock = threading.Lock()

    def __init__(self):
        self._headers = {}
        if COINGECKO_API_KEY:
            self._headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
        self._id_cache = {}
        # Yalnız CoinGecko'ya özel, kalıcı Session — Binance/CMC/Anthropic/diğer
        # fetcher'ları hiç etkilemez (onlar bu Session'ı hiç görmüyor, kendi
        # bağımsız requests.get() çağrılarını kullanmaya devam ediyor). Global
        # socket/network monkeypatch YOK — yalnız bu Session'ın kendi connection
        # pool'u, aynı host'a (api.coingecko.com) yapılan TÜM çağrılarda paylaşılır.
        self._session = requests.Session() if requests is not None else None

    @classmethod
    def _ensure_coins_list(cls, session=None):
        if cls._coins_list_cache is not None:
            return
        data = _http_get_json(f"{cls.BASE}/coins/list", debug_label="/coins/list", session=session)
        if not isinstance(data, list):
            # Geçici/başarısız yanıt — cache None kalır, sonraki çağrı tekrar dener.
            return
        mapping = {}
        for c in data:
            sym = (c.get("symbol") or "").lower()
            mapping.setdefault(sym, []).append(c)
        cls._coins_list_cache = mapping

    def _verify_binance_listing(self, coin_id: str, symbol: str) -> Optional[bool]:
        """coin_id'nin GERÇEKTEN Binance'te {symbol}/USDT olarak işlem gördüğünü
        doğrular. get_unique_major_exchanges() ile AYNI pagination deseni
        (sayfa=100, <100 dönen sayfa son sayfa, MAX_PAGES güvenlik sınırı) —
        ikinci bir pagination mantığı icat edilmedi, aynı sabitler paylaşılıyor.

        Döner: True (doğrulandı), False (tüm sayfalar tarandı, eşleşme yok —
        kesin), None (pagination tamamlanamadı — belirsiz, kesin 'yok' DEĞİL)."""
        page = 1
        complete = False
        while page <= self._TICKERS_MAX_PAGES:
            # exchange_ids=binance: server-side filtreleme — canlı test ile
            # doğrulandı (BTC/ETH/MEME), collision/ambiguity mantığını değiştirmez,
            # yalnız CoinGecko'nun döndürdüğü ticker kapsamını daraltır.
            data = _http_get_json(f"{self.BASE}/coins/{coin_id}/tickers",
                                   params={"include_exchange_logo": "false", "page": page,
                                           "exchange_ids": "binance"},
                                   headers=self._headers, session=self._session,
                                   debug_label=f"candidate={coin_id} /tickers page={page} "
                                               f"exch=binance (verify_binance_listing, symbol={symbol})")
            if not data or "tickers" not in data:
                return None  # bu sayfa başarısız -> belirsiz, kesin "yok" değil
            tickers = data["tickers"]
            for t in tickers:
                market = t.get("market", {}) or {}
                if (str(market.get("identifier", "")).lower() == "binance"
                        and str(t.get("base", "")).upper() == symbol.upper()
                        and str(t.get("target", "")).upper() == "USDT"):
                    return True
            if len(tickers) < self._TICKERS_PAGE_SIZE:
                complete = True
                break
            page += 1
        return False if complete else None

    def resolve_coin_id(self, symbol: str) -> Optional[str]:
        """Binance'te {symbol}/USDT olarak işlem gören CoinGecko coin ID'sini
        döner. Aynı ticker'ı paylaşan birden fazla CoinGecko varlığı varsa
        (gerçek gözlem: Binance USDT paritelerinin ~%35'i çakışıyor — ör. 'MEME'
        20 aday arasından yalnız 'memecoin-2' gerçekten Binance'te işlem
        görüyor, market cap'i daha yüksek olan 'memetoon' görmüyor), yalnızca
        Binance ticker verisiyle GERÇEKTEN doğrulanan aday seçilir — market cap
        büyüklüğü asla tek başına karar kriteri değildir.

        - Tam olarak 1 aday doğrulanırsa: o ID.
        - 0 aday doğrulanır (ve tüm kontroller kesin tamamlanmışsa): None,
          kalıcı olarak cache'lenir (gerçekten yok).
        - 0 aday doğrulanır ama bazı kontroller ağ hatasıyla tamamlanamadıysa:
          None, ama CACHE'LENMEZ (geçici hatayı kalıcı 'coin yok' sayma).
        - 2+ aday doğrulanırsa (CoinGecko veri kalitesi sorunu/duplicate kayıt
          ihtimali): None, sezgisel/market-cap tie-break YAPILMAZ, cache'lenmez.

        VERIFIED COIN-ID PROCESS CACHE: doğrulanmış pozitif bir sonuç
        (tek-aday direkt eşleşme VEYA çoklu-aday Binance-doğrulamalı eşleşme)
        `_verified_id_cache`'e de yazılır -- sonraki çağrılar (aynı process
        içinde, FARKLI bir RealFetcher/CoinGeckoFetcher instance'ından bile
        olsa) resolver'ı hiç çalıştırmadan bu sonucu kullanır. Algoritmanın
        kendisi (candidate sıralama, Binance doğrulama, ambiguity tespiti)
        DEĞİŞMEDİ -- bu yalnız sonucu process ömrü boyunca yeniden kullanan
        bir hızlı-yol katmanı."""
        symbol = symbol.lower()
        with self._verified_id_cache_lock:
            cached_verified = self._verified_id_cache.get(symbol)
        if cached_verified is not None:
            return cached_verified
        if symbol in self._id_cache:
            return self._id_cache[symbol]
        self._ensure_coins_list(session=self._session)
        if self._coins_list_cache is None:
            # coins/list geçici olarak alınamadı — kalıcı 'coin yok' sayılmaz,
            # cache'lenmez, bir sonraki analizde yeniden denenir.
            return None
        candidates = self._coins_list_cache.get(symbol, [])
        if not candidates:
            self._id_cache[symbol] = None
            return None
        if len(candidates) == 1:
            # coins_list_cache zaten 'symbol' alanına göre indekslenmiş —
            # tek aday olması, sembolün doğrudan eşleştiği anlamına gelir,
            # ek doğrulamaya gerek yok.
            coin_id = candidates[0]["id"]
            self._id_cache[symbol] = coin_id
            with self._verified_id_cache_lock:
                self._verified_id_cache[symbol] = coin_id
            return coin_id

        # Birden fazla aday: TÜMÜ değerlendirilir (ilk 20'ye kırpma YOK — gerçek
        # eşleşme market cap sıralamasında geç sırada olabilir, MEME örneği
        # bunu kanıtladı). Market cap sıralaması yalnızca kontrol SIRASI için
        # kullanılıyor (verimlilik), KARAR KRİTERİ değil.
        ids = ",".join(c["id"] for c in candidates)
        markets = _http_get_json(f"{self.BASE}/coins/markets",
                                  params={"vs_currency": "usd", "ids": ids},
                                  headers=self._headers, session=self._session,
                                  debug_label=f"/coins/markets ids_count={len(candidates)} "
                                              f"(resolve_coin_id ordering, symbol={symbol})")
        if isinstance(markets, list) and markets:
            order = [m["id"] for m in
                     sorted(markets, key=lambda m: m.get("market_cap") or 0, reverse=True)]
            seen = set(order)
            order += [c["id"] for c in candidates if c["id"] not in seen]
        else:
            order = [c["id"] for c in candidates]

        validated = []
        any_inconclusive = False
        for cid in order:
            result = self._verify_binance_listing(cid, symbol)
            if result is True:
                validated.append(cid)
            elif result is None:
                any_inconclusive = True
            # result is False -> kesin eşleşme yok, devam

        if len(validated) == 1:
            self._id_cache[symbol] = validated[0]
            with self._verified_id_cache_lock:
                self._verified_id_cache[symbol] = validated[0]
            return validated[0]

        if len(validated) >= 2:
            return None  # ambiguous — tie-break yok, cache'lenmez

        # len(validated) == 0
        if any_inconclusive:
            return None  # belirsiz — geçici hata olabilir, cache'lenmez
        self._id_cache[symbol] = None  # tam tarandı, gerçekten hiçbiri yok
        return None

    def get_coin_info(self, coin_id: str) -> Optional[dict]:
        """İsim + market verisi (piyasa değeri, dolaşımdaki/toplam arz)."""
        data = _http_get_json(f"{self.BASE}/coins/{coin_id}", params={
            "localization": "false", "tickers": "false", "market_data": "true",
            "community_data": "false", "developer_data": "false",
        }, headers=self._headers, session=self._session,
            debug_label=f"/coins/{coin_id} (get_coin_info)")
        if not data:
            return None
        md = data.get("market_data", {})
        return {
            "name": data.get("name"),
            "market_cap": (md.get("market_cap") or {}).get("usd"),
            "circulating_supply": md.get("circulating_supply"),
            "total_supply": md.get("total_supply"),
        }

    # CoinGecko /coins/{id}/tickers sayfa başına 100 ticker döner (resmi API
    # referansı + canlı XRP testiyle doğrulandı: sayfa 1-4 tam 100, sayfa 5
    # 43/100, sayfa 6 boş — <100 dönen sayfa son sayfayı işaret eder). XRP
    # gerçekte 5 sayfa (~443 ticker) gerektirdi; MAX_PAGES bu gözlemin
    # üzerine geniş bir güvenlik payıyla (icat edilmiş değil, gözlenen gerçek
    # aralığın birkaç katı) belirlendi.
    _TICKERS_PAGE_SIZE = 100
    _TICKERS_MAX_PAGES = 20

    def get_unique_major_exchanges(self, coin_id: str) -> Optional[dict]:
        """Büyük borsalarda BENZERSİZ listelenme sayısını TÜM sayfaları
        tarayarak hesaplar (market-pair sayısı değil, benzersiz borsa sayısı).

        Döner: {"count": int, "complete": bool} veya pagination hiç
        tamamlanamadıysa VE eşik (>=2) henüz kanıtlanmadıysa None.

        - complete=True  → count kesin/tam toplam.
        - complete=False → sayfalama yarım kaldı (ağ hatası/MAX_PAGES) ama
          count>=2 zaten doğrulandı — sonraki sayfalar bu sayıyı yalnızca
          ARTIRABİLİR, asla azaltamaz, bu yüzden >=2 eşiği için güvenilir;
          ancak count TAM toplamı temsil etmeyebilir.
        - is_stale filtresi (FRESHNESS GATE PARITY, onaylı controlled
          implementation): CoinGecko'nun kendi verdiği `is_stale` alanı
          okunur. `is_stale is True` olan ticker'lar sayılmaz. Alan eksikse
          (missing/None) veya ticker'ın kendisi/`market` alanı beklenen
          şekilde değilse (malformed) CONSERVATIVE SKIP — fresh
          VARSAYILMAZ, sayılmaz. Bu, projenin genel ilkesiyle tutarlı
          (funding'de periyot bilinmiyorsa tahmin edilmez, burada da
          tazelik bilinmiyorsa taze sayılmaz).

        exchange_ids=<MAJOR_EXCHANGES listesi>: server-side filtreleme — canlı
        test ile doğrulandı (BTC/ETH/MEME'de full-pagination ile BİREBİR AYNI
        unique-major-count sonucu). PAGINATION KORUNUYOR — filtreli yanıt bile
        >100 ticker dönebiliyor (BTC'de canlı olarak 144 ticker/2 sayfa
        gözlendi), "filtre varsa tek sayfa yeter" varsayımı YAPILMADI."""
        found = set()
        complete = False
        page = 1
        while page <= self._TICKERS_MAX_PAGES:
            data = _http_get_json(f"{self.BASE}/coins/{coin_id}/tickers",
                                   params={"include_exchange_logo": "false", "page": page,
                                           "exchange_ids": self._MAJOR_EXCHANGE_IDS_CSV},
                                   headers=self._headers, session=self._session,
                                   debug_label=f"coin_id={coin_id} /tickers page={page} "
                                               f"exch=<major> (unique_major_exchanges)")
            if not data or "tickers" not in data:
                break  # bu sayfa başarısız -> pagination tamamlanamadı
            tickers = data["tickers"]
            for t in tickers:
                if not isinstance(t, dict):
                    continue  # malformed ticker -> skip
                if t.get("is_stale") is not False:
                    # True -> onaylı stale, sayılmaz. Alan eksik/None -> tazelik
                    # bilinmiyor, conservative skip (fresh varsayılmaz).
                    continue
                market = t.get("market")
                if not isinstance(market, dict):
                    continue  # malformed market alanı -> skip
                ident = market.get("identifier", "")
                if ident:
                    found.add(ident.lower())
            if len(tickers) < self._TICKERS_PAGE_SIZE:
                complete = True
                break
            page += 1

        count = len(found & self.MAJOR_EXCHANGES)
        if complete:
            return {"count": count, "complete": True}
        if count >= 2:
            return {"count": count, "complete": False}
        return None  # kesin karar veremeyiz -> NODATA (uydurma sayı yok)


class CMCFetcher:
    """CoinMarketCap — destekleyici veri. Borsa sayısı için ASLA kullanılmaz
    (num_market_pairs != borsa sayısı). API key yoksa/plan izin vermiyorsa tüm
    metodlar None döner, uygulama çökmez."""

    BASE = "https://pro-api.coinmarketcap.com"

    def __init__(self):
        self._headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"} \
            if CMC_API_KEY else None

    def get_quotes(self, symbol: str) -> Optional[dict]:
        if not self._headers:
            return None
        data = _http_get_json(f"{self.BASE}/v1/cryptocurrency/quotes/latest",
                               params={"symbol": symbol.upper()}, headers=self._headers)
        if not data or "data" not in data:
            return None
        try:
            entry = data["data"][symbol.upper()]
            if isinstance(entry, list):
                entry = entry[0]
            quote = entry.get("quote", {}).get("USD", {})
            return {
                "market_cap": quote.get("market_cap"),
                "num_market_pairs": entry.get("num_market_pairs"),  # skor motoruna GİRMEZ
            }
        except (KeyError, IndexError, TypeError):
            return None

    def get_btc_dominance_trend(self) -> Optional[dict]:
        """CMC global-metrics tek snapshot'ından İKİ BAĞIMSIZ kol hesaplar:
        (1) BTC dominansı düşüyor mu, (2) TOTAL3 (BTC+ETH hariç piyasa değeri)
        artıyor mu. Her iki kol de aynı yanıttaki '*_yesterday' alanlarından
        (btc_dominance_yesterday, eth_dominance_yesterday,
        total_market_cap_yesterday) türetilir — böylece iki kol da AYNI referans
        anına göre hesaplanır (CMC'nin ayrıca sunduğu hazır
        'btc_dominance_24h_percentage_change' alanı ile çapraz kontrol edildi;
        aralarında küçük ama gerçek bir sayısal fark var — muhtemelen farklı iç
        referans anı kullanıyorlar — bu yüzden iki kolu TUTARLI tutmak için
        ikisi de _yesterday alanlarından hesaplanıyor, karışık kaynak
        kullanılmıyor).

        Her kol bağımsız olarak None (belirlenemedi) kalabilir; OR
        değerlendirmesi RealFetcher.fetch() içinde üç-durumlu (yes/no/nodata)
        olarak yapılır — bu fonksiyon yalnız ham kol sonuçlarını döner.

        FRESHNESS GATE (onaylı, canlı kadans örneklemesiyle kanıtlanmış):
        `data.last_updated` (data-level, response-level `status.timestamp`
        DEĞİL) okunur; eksik/bozuk/stale (>15dk) veya gelecek-zamanlı
        (>120s tolerans) ise None döner — hesaplama hiç yapılmaz. Bu,
        yalnız CURRENT snapshot'ın tazeliğini garanti eder; `*_yesterday`
        referans noktasının üretim anını AYRICA garanti etmez (CMC bunu
        ayrı bir alan olarak vermiyor)."""
        if not self._headers:
            return None
        data = _http_get_json(f"{self.BASE}/v1/global-metrics/quotes/latest", headers=self._headers)
        if not data or "data" not in data:
            return None
        try:
            last_updated_raw = data["data"].get("last_updated")
            ts = datetime.fromisoformat(str(last_updated_raw).replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError, AttributeError):
            return None
        if not _timestamp_is_fresh(ts, _CMC_DOMINANCE_TTL_SECONDS):
            return None
        try:
            d = data["data"]
            dom_now = d.get("btc_dominance")
            dom_yst = d.get("btc_dominance_yesterday")
            dom_change = d.get("btc_dominance_24h_percentage_change")
            eth_dom_now = d.get("eth_dominance")
            eth_dom_yst = d.get("eth_dominance_yesterday")
            quote = d.get("quote", {}).get("USD", {})
            tmc_now = quote.get("total_market_cap")
            tmc_yst = quote.get("total_market_cap_yesterday")

            result = {
                "dominance_falling": None,
                "btc_dominance_24h_change": dom_change,
                "total3_rising": None,
                "total3_change_pct": None,
            }

            # Kol 1: BTC dominansı düşüyor mu — dominance_yesterday ile
            # doğrudan karşılaştırılıyor (TOTAL3 koluyla aynı referans anı).
            if dom_now is not None and dom_yst is not None:
                result["dominance_falling"] = dom_now < dom_yst

            # Kol 2: TOTAL3 (piyasa değeri - BTC payı - ETH payı) artıyor mu.
            required = (tmc_now, tmc_yst, dom_now, dom_yst, eth_dom_now, eth_dom_yst)
            if all(v is not None for v in required) and tmc_yst != 0:
                total3_now = tmc_now * (1 - dom_now / 100 - eth_dom_now / 100)
                total3_yst = tmc_yst * (1 - dom_yst / 100 - eth_dom_yst / 100)
                # Yapısal olarak anlamsız sonuç (negatif/sıfır piyasa değeri) ise
                # bu kolu geçersiz say — nodata'ya düşer, uydurma değer üretilmez.
                if total3_now > 0 and total3_yst > 0:
                    result["total3_rising"] = total3_now > total3_yst
                    result["total3_change_pct"] = (total3_now - total3_yst) / total3_yst * 100

            if result["dominance_falling"] is None and result["total3_rising"] is None:
                return None  # hiçbir kol belirlenemedi
            return result
        except Exception:
            return None


class CoinMetricsFetcher:
    """CoinMetrics Community API — ücretsiz, API key gerektirmez
    (community-api.coinmetrics.io). Yalnızca gerçekten desteklenen iki metrik
    kullanılır:
      - AdrActCnt (aktif adres sayısı): ~138 varlık, çoğu major/mid-cap coin.
      - SplyExNtv (borsalarda tutulan arz): yalnızca btc/eth (community tier'da
        başka hiçbir varlık için mevcut değil — doğrulandı, catalog'da yalnız
        bu ikisi listeleniyor).
    Whale accumulation / stablecoin borsa girişi için community tier'da
    HİÇBİR metrik yok (araştırıldı: cohort/holder-balance metrikleri ve
    stablecoin flow verisi yalnızca ücretli planlarda) — bu ikisi için bu
    sınıf hiç kullanılmaz, RealFetcher onları dürüstçe nodata bırakır."""

    BASE = "https://community-api.coinmetrics.io/v4"

    @classmethod
    def _asset_id(cls, symbol: str) -> str:
        return symbol.lower()

    @classmethod
    def _get_series(cls, symbol: str, metric: str, days: int = 4) -> Optional[list]:
        asset = cls._asset_id(symbol)
        data = _http_get_json(f"{cls.BASE}/timeseries/asset-metrics",
                               params={"assets": asset, "metrics": metric,
                                       "frequency": "1d", "page_size": days})
        if not data or "data" not in data or not isinstance(data["data"], list):
            return None
        points = []
        for row in data["data"]:
            try:
                if metric not in row or row[metric] is None:
                    continue
                points.append((row["time"], float(row[metric])))
            except (KeyError, ValueError, TypeError):
                continue
        return points if len(points) >= 2 else None

    @classmethod
    def _validate_daily_pair(cls, points):
        """Son iki noktanın (a) bugüne göre freshness'ını ve (b) ardışık günlük
        slot olduğunu doğrular. Canlı gözlemle doğrulandı: CoinMetrics 'time'
        alanı UTC gece yarısı slot'u, TAMAMLANMIŞ günü temsil ediyor — bugünün
        (henüz bitmemiş) günü hiçbir zaman dönmüyor (BTC/ETH/ZEC gibi güncel
        varlıklarda en son nokta her zaman tam olarak dün). Bu yüzden:
          - age_days == 1 (dün)  -> normal, kabul.
          - age_days == 2        -> bir günlük yayın gecikmesi payı, kabul
            (OI freshness round'undaki 'bir tam periyot gecikmesi' ilkesiyle
            aynı mantık, yalnız burada birim saat değil UTC gün).
          - age_days <= 0 veya >= 3 -> reddedilir (gelecek tarih anomalisi
            veya gerçek staleness/veri kesintisi).
        Ayrıca prev/last arasındaki fark tam 1 gün DEĞİLSE (duplicate tarih
        veya atlanmış gün) reddedilir — iki nokta gerçek ardışık gün olmalı.

        Döner: (last_date, prev_date) date nesneleri, ya da None (reddedildi)."""
        prev_date_str, _ = points[-2]
        last_date_str, _ = points[-1]
        try:
            last_date = datetime.strptime(last_date_str[:10], "%Y-%m-%d").date()
            prev_date = datetime.strptime(prev_date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

        today = datetime.now(timezone.utc).date()
        age_days = (today - last_date).days
        if age_days not in (1, 2):
            return None  # stale (>=3 gün) veya anormal (gelecek tarih)

        if (last_date - prev_date).days != 1:
            return None  # ardışık günlük slot değil (gap veya duplicate)

        return last_date, prev_date

    @classmethod
    def get_active_address_trend(cls, symbol: str) -> Optional[dict]:
        """Son iki TAMAMLANMIŞ, ARDIŞIK ve TAZE günlük AdrActCnt noktasını
        karşılaştırır. Tek noktadan asla 'artıyor' sonucu üretilmez; yıllarca
        eski veya aralarında gün atlanmış noktalardan da üretilmez."""
        points = cls._get_series(symbol, "AdrActCnt", days=4)
        if not points:
            return None
        points.sort(key=lambda p: p[0])
        validated = cls._validate_daily_pair(points)
        if not validated:
            return None
        last_date, prev_date = validated
        prev_val = points[-2][1]
        last_val = points[-1][1]
        if prev_val == 0:
            return None
        change_pct = (last_val - prev_val) / prev_val * 100
        return {
            "rising": last_val > prev_val,
            "latest": last_val, "previous": prev_val,
            "change_pct": round(change_pct, 2),
            "latest_date": last_date.isoformat(), "previous_date": prev_date.isoformat(),
        }

    @classmethod
    def get_exchange_reserve_trend(cls, symbol: str) -> Optional[dict]:
        """Son iki TAMAMLANMIŞ, ARDIŞIK ve TAZE günlük SplyExNtv (borsa
        rezervi) noktasını karşılaştırır. Rezerv düşüyorsa net çıkış var
        demektir. Yalnızca btc/eth için veri mevcut — diğer sembollerde None
        (nodata). Yıllarca eski veya gün-atlamalı verilerden sonuç üretilmez."""
        points = cls._get_series(symbol, "SplyExNtv", days=4)
        if not points:
            return None
        points.sort(key=lambda p: p[0])
        validated = cls._validate_daily_pair(points)
        if not validated:
            return None
        last_date, prev_date = validated
        prev_val = points[-2][1]
        last_val = points[-1][1]
        if prev_val == 0:
            return None
        change_pct = (last_val - prev_val) / prev_val * 100
        return {
            "net_outflow": last_val < prev_val,
            "latest": last_val, "previous": prev_val,
            "change_pct": round(change_pct, 2),
            "latest_date": last_date.isoformat(), "previous_date": prev_date.isoformat(),
        }


# FRESHNESS GATE PARITY (onaylı, canlı kadans örneklemesiyle kanıtlanmış TTL'ler):
# CMC F&G ~15dk sabit aralıkla güncelleniyor (2 bağımsız gözlemle doğrulandı,
# Δ=900.0s) -- 1h eşiği ~4x güvenlik payı. alternative.me tam 86400s (24h)
# aralıkla güncelleniyor -- 36h eşiği normal yayın gecikmesini tolere ederken
# 2 günlük bayat veriyi reddediyor. CMC dominance ~1dk kadansla değişiyor
# (8/8 bağımsız gözlemde tutarlı) -- 15dk eşiği ~15x güvenlik payı.
# 120s future-tolerance, bu makinede ölçülen ~95s yerel clock-skew'i
# karşılıyor (CMC tarafı anomalisi değil, doğrudan ölçüldü).
_FG_CMC_TTL_SECONDS = 3600
_FG_ALTERNATIVE_ME_TTL_SECONDS = 36 * 3600
_CMC_DOMINANCE_TTL_SECONDS = 15 * 60
_FRESHNESS_FUTURE_TOLERANCE_SECONDS = 120


def _timestamp_is_fresh(ts: datetime, ttl_seconds: int, now: Optional[datetime] = None) -> bool:
    """ts (tz-aware) şu an (now, verilmezse gerçek UTC) referansına göre
    [-FUTURE_TOLERANCE, +ttl_seconds] penceresinde mi? TTL'yi aşan (çok eski)
    veya toleranstan fazla gelecekte olan (bozuk/clock-skew) bir zaman damgası
    taze SAYILMAZ. Provider-specific parsing burada YOK -- yalnız ortak
    yaşlanma/tazelik karşılaştırması, her kaynağın kendi ayrıştırdığı ts ile
    kullanılır."""
    now = now or datetime.now(timezone.utc)
    age_seconds = (now - ts).total_seconds()
    return -_FRESHNESS_FUTURE_TOLERANCE_SECONDS <= age_seconds <= ttl_seconds


def _fetch_fear_greed_cmc(now: Optional[datetime] = None) -> Optional[int]:
    """CMC Fear & Greed -- `data.update_time` (ISO8601 UTC) ile freshness
    doğrulanır. TTL/tolerans dışı, eksik veya bozuk timestamp -> None (çağıran
    taraf alternative.me'ye düşer). Timestamp taze ama value ayrıştırılamazsa
    da None."""
    if not CMC_API_KEY:
        return None
    data = _http_get_json(
        "https://pro-api.coinmarketcap.com/v3/fear-and-greed/latest",
        headers={"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"})
    if not data or "data" not in data:
        return None
    try:
        ts = datetime.fromisoformat(str(data["data"]["update_time"]).replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    if not _timestamp_is_fresh(ts, _FG_CMC_TTL_SECONDS, now):
        return None
    try:
        return int(data["data"]["value"])
    except (KeyError, ValueError, TypeError):
        return None


def _fetch_fear_greed_alternative_me(now: Optional[datetime] = None) -> Optional[int]:
    """alternative.me Fear & Greed -- `data[0]["timestamp"]` (Unix epoch
    saniye, string) ile freshness doğrulanır. Eksik/bozuk/stale -> None."""
    data = _http_get_json("https://api.alternative.me/fng/", params={"limit": 1})
    if not data or not data.get("data"):
        return None
    try:
        ts = datetime.fromtimestamp(int(data["data"][0]["timestamp"]), tz=timezone.utc)
    except (KeyError, ValueError, TypeError, IndexError, OverflowError, OSError):
        return None
    if not _timestamp_is_fresh(ts, _FG_ALTERNATIVE_ME_TTL_SECONDS, now):
        return None
    try:
        return int(data["data"][0]["value"])
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def fetch_fear_greed() -> Optional[int]:
    """Fear & Greed Index — önce CMC (varsa VE taze), sonra alternative.me
    (taze). Her iki kaynak da kendi timestamp/TTL contract'ına göre freshness
    doğrular; stale/eksik/malformed/gelecek-zamanlı payload diğer kaynağa
    düşülmesine (CMC -> alternative.me) veya nihayetinde None'a (nodata) yol
    açar -- hiçbir zaman "muhtemelen taze" varsayılmaz. Dönüş tipi (Optional[int])
    ve tek consumer (_build_context -> metric=='fear_greed') DEĞİŞMEDİ."""
    value = _fetch_fear_greed_cmc()
    if value is not None:
        return value
    return _fetch_fear_greed_alternative_me()


class TradingViewOnChainFetcher:
    """BTC MVRV/SOPR için TradingView'deki Glassnode/CoinMetrics serilerini okur.
    GAYRI RESMİ bir yöntemdir (tvDatafeed, TradingView'in dahili WebSocket protokolünü
    kullanır) — resmi bir API değildir, TradingView ToS'una göre kırılgan/riskli olabilir.
    Yalnızca BTC için kullanılır (bkz. RealFetcher). Paket kurulu değilse veya herhangi bir
    hata olursa None döner, hiçbir zaman raise etmez."""

    _client = None
    _cache: Dict[str, Any] = {"data": None, "fetched_at": None}
    _CACHE_TTL_HOURS = 6
    # tvDatafeed.get_hist() HER çağrıda paylaşılan _client'ın self.ws/
    # self.chart_session'ını ÜZERİNE YAZIYOR (kaynak kod doğrulandı) -- iki
    # Level1Worker (ör. iptal edilmiş-ama-hâlâ-çalışan eski worker + yeni
    # worker) aynı anda buraya girerse birbirinin websocket okumasını
    # bozabilir (sessiz veri karışması, exception garantisi yok). Bu kilit
    # YALNIZ TradingView çağrılarını serileştirir -- Binance/CoinGecko/CMC/
    # RSS/News gibi paylaşılan state'i olmayan diğer tüm aşamalar bundan
    # etkilenmeden serbestçe örtüşmeye devam edebilir.
    _client_lock = threading.Lock()

    @classmethod
    def _get_client(cls):
        if TvDatafeed is None:
            return None
        if cls._client is None:
            try:
                cls._client = TvDatafeed()  # anonim (login yok)
            except Exception:
                cls._client = None
        return cls._client

    @staticmethod
    def _only_completed_daily(df):
        """Bugünün (UTC) hâlâ oluşuyor olabilecek son barını dışlar."""
        if df is None or df.empty:
            return None
        today_utc = datetime.now(timezone.utc).date()
        idx_dates = df.index.date
        mask = idx_dates < today_utc
        completed = df[mask]
        return completed if not completed.empty else None

    @classmethod
    def get_btc_series(cls):
        """Döner: {'market_cap': df, 'realized_cap': df, 'sopr': df} veya None."""
        if TvDatafeed is None or TvInterval is None:
            return None

        now = datetime.now(timezone.utc)
        cached = cls._cache
        if cached["data"] is not None and cached["fetched_at"] is not None:
            age_hours = (now - cached["fetched_at"]).total_seconds() / 3600
            if age_hours < cls._CACHE_TTL_HOURS:
                return cached["data"]

        client = cls._get_client()
        if client is None:
            return None

        # bkz. _client_lock tanımı: paylaşılan TvDatafeed instance'ının
        # self.ws/self.chart_session'ını eşzamanlı çağrılar arasında
        # korumak için TÜM get_hist() dizisi tek kilit altında.
        with cls._client_lock:
            try:
                mc = client.get_hist(symbol="BTC_MARKETCAP", exchange="GLASSNODE",
                                      interval=TvInterval.in_daily, n_bars=20)
                rc = client.get_hist(symbol="BTC_MARKETCAPREAL", exchange="COINMETRICS",
                                      interval=TvInterval.in_daily, n_bars=20)
                sopr = client.get_hist(symbol="BTC_SOPR", exchange="GLASSNODE",
                                        interval=TvInterval.in_daily, n_bars=20)
            except Exception as e:
                print(f"[TV ONCHAIN] Veri çekilemedi: {e}", flush=True)
                return None

        mc_c = cls._only_completed_daily(mc)
        rc_c = cls._only_completed_daily(rc)
        sopr_c = cls._only_completed_daily(sopr)
        if mc_c is None or rc_c is None or sopr_c is None:
            return None

        data = {"market_cap": mc_c, "realized_cap": rc_c, "sopr": sopr_c}
        cls._cache = {"data": data, "fetched_at": now}
        return data

    @staticmethod
    def _is_fresh_daily_date(d) -> bool:
        """CoinMetrics freshness round'unda onaylanan aynı ilke: günlük veri
        için age_days in (1,2) kabul edilir (bugün-1 = normal, bugün-2 = bir
        yayın döngüsü gecikmesi). Canlı doğrulandı: GLASSNODE (BTC_MARKETCAP,
        BTC_SOPR) tutarlı biçimde today-2, COINMETRICS (BTC_MARKETCAPREAL)
        tutarlı biçimde today-1 geliyor — ikisi de bu aralığa düşüyor, farklı
        bir semantik gerekmiyor."""
        today = datetime.now(timezone.utc).date()
        age_days = (today - d).days
        return age_days in (1, 2)

    @staticmethod
    def compute_mvrv(series: dict) -> Optional[dict]:
        """Yalnızca tamamlanmış barların ORTAK son tarihinden, bu tarih
        bugüne göre TAZE ise MVRV hesaplar. Ortak tarihin freshness'ı zaten
        iki serinin de daha bayat olanına göre sınırlı olduğu için (min of
        the two latest dates), ayrıca her seri için ayrı bir freshness
        kontrolüne gerek yok."""
        try:
            mc, rc = series["market_cap"], series["realized_cap"]
            common_dates = sorted(set(mc.index.date) & set(rc.index.date))
            if not common_dates:
                return None
            last_common = common_dates[-1]
            if not TradingViewOnChainFetcher._is_fresh_daily_date(last_common):
                return None  # ortak tarih çok eski -> stale, nodata
            mc_val = float(mc[mc.index.date == last_common]["close"].iloc[-1])
            rc_val = float(rc[rc.index.date == last_common]["close"].iloc[-1])
            if rc_val == 0:
                return None
            return {"date": last_common, "market_cap": mc_val, "realized_cap": rc_val,
                    "mvrv": mc_val / rc_val}
        except Exception:
            return None

    @staticmethod
    def compute_sopr_recovery(series: dict) -> Optional[dict]:
        """Son iki TAMAMLANMIŞ, ARDIŞIK ve TAZE günlük SOPR barını
        karşılaştırır. Tek noktadan asla sonuç üretilmez; aralarında gün
        atlanmışsa veya en son bar bugüne göre çok eskiyse nodata."""
        try:
            sopr = series["sopr"]
            if len(sopr) < 2:
                return None
            latest_date = sopr.index[-1].date()
            previous_date = sopr.index[-2].date()
            if not TradingViewOnChainFetcher._is_fresh_daily_date(latest_date):
                return None  # en son bar bugüne göre çok eski -> stale
            if (latest_date - previous_date).days != 1:
                return None  # ardışık günlük slot değil (gap veya duplicate)
            latest_val = float(sopr.iloc[-1]["close"])
            previous_val = float(sopr.iloc[-2]["close"])
            recovered = (previous_val < 1.0) and (latest_val >= 1.0)
            return {
                "previous_date": previous_date, "previous_value": previous_val,
                "latest_date": latest_date, "latest_value": latest_val,
                "recovered": recovered,
            }
        except Exception:
            return None


class _IPv4OnlyHTTPSConnection(_urllib3_connection.HTTPSConnection if _urllib3_connection else object):
    """Yalnızca AF_INET (IPv4) adaylarıyla bağlanan bir HTTPSConnection.
    socket.getaddrinfo/create_connection GLOBAL olarak değiştirilmiyor —
    yalnız bu sınıfın _new_conn()'u kendi bağlantısını IPv4 ile kuruyor.
    Bu makinede bazı haber host'larına (Cloudflare arkasındaki) IPv6 rotası
    bozuk; her YENİ bağlantı ~20-45s SYN-retransmit cezası ödeyip sonra
    IPv4'e düşüyor (bkz. FAZ A audit ölçümleri). IPv4'ü baştan zorlamak bu
    cezayı sıfırlıyor, DNS/TLS/response davranışını DEĞİŞTİRMİYOR."""

    def _new_conn(self):
        host = self._dns_host
        port = self.port
        last_err = None
        for family, socktype, proto, canonname, sockaddr in socket.getaddrinfo(
                host, port, socket.AF_INET, socket.SOCK_STREAM):
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                if self.socket_options:
                    for opt in self.socket_options:
                        sock.setsockopt(*opt)
                sock.settimeout(self.timeout)
                sock.connect(sockaddr)
                return sock
            except OSError as e:
                last_err = e
                if sock is not None:
                    sock.close()
                continue
        raise OSError(f"IPv4 bağlantısı kurulamadı: {host}:{port} ({last_err})")


class _IPv4OnlyHTTPSConnectionPool(_urllib3_connectionpool.HTTPSConnectionPool if _urllib3_connectionpool else object):
    ConnectionCls = _IPv4OnlyHTTPSConnection


class NewsRSSIPv4Adapter(requests.adapters.HTTPAdapter if requests else object):
    """Yalnızca bu adapter'in mount edildiği Session üzerinden giden HTTPS
    isteklerini IPv4'e zorlar. Global urllib3.util.connection.allowed_gai_family
    veya socket.getaddrinfo DEĞİŞTİRİLMEZ — Binance/CoinGecko/CMC/Anthropic/
    TradingView için kullanılan hiçbir başka Session/requests.get() çağrısı bu
    adapter'dan etkilenmez (izole prototip testiyle doğrulandı: aynı process
    içinde bu adapter'sız bir requests.get() hâlâ eski IPv6 gecikmesini yaşıyor
    — yani etki gerçekten yalnız bu adapter'e mount edilmiş Session'a özgü).
    Thread-safe: urllib3'ün PoolManager/ConnectionPool'ları zaten thread-safe;
    ThreadPoolExecutor'daki paralel _fetch_one_source() çağrıları aynı
    Session'ı güvenle paylaşabilir."""

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(
            self.poolmanager.pool_classes_by_scheme or {})
        self.poolmanager.pool_classes_by_scheme["https"] = _IPv4OnlyHTTPSConnectionPool


_news_anthropic_client = None
_news_anthropic_client_lock = threading.Lock()


def _get_news_anthropic_client():
    """classify_risk() için PAYLAŞILAN, tembel (lazy) başlatılan Anthropic
    client. NewsRiskFetcher her analizde YENİDEN oluşturulduğu (bkz.
    RealFetcher.__init__) için instance-seviyesinde bir client hiçbir
    analizler-arası bağlantı yeniden kullanımı sağlamazdı — bu yüzden
    modül seviyesinde, tüm analizler arasında paylaşılan TEK bir client.
    httpx.Client (anthropic SDK'nın altında kullandığı) resmi olarak
    "can be shared between threads" — thread-safe'tir; ayrıca bu proje
    kapsamında gerçek eşzamanlı (4 thread, aynı client, gerçek API çağrısı)
    testle de doğrulandı: response mixing yok, ikinci dalga çağrılar
    bağlantı yeniden kullanımıyla saniyeler mertebesine iniyor (~22s -> ~1s).
    Double-checked locking: ilk oluşturma anında birden fazla thread aynı
    anda buraya girerse yalnız biri client'ı yaratır."""
    global _news_anthropic_client
    if _news_anthropic_client is None:
        with _news_anthropic_client_lock:
            if _news_anthropic_client is None:
                import anthropic
                _news_anthropic_client = anthropic.Anthropic(
                    api_key=ANTHROPIC_API_KEY, timeout=60.0, max_retries=0)
    return _news_anthropic_client


class NewsRiskFetcher:
    """news_watcher.py'deki RSS + dedup mantığından uyarlanmıştır. Coin-bazlı,
    tek seferlik sorgu: hack/exploit/regülasyon/delist gibi riskleri tarar."""

    def __init__(self):
        # Tüm RSS kaynakları (paralel _fetch_one_source() çağrıları dahil) bu
        # TEK Session'ı paylaşır -- hem IPv4-adapter'in etkisi tüm kaynaklara
        # uygulanır, hem de bağlantı havuzu bu analiz ömrü boyunca yeniden
        # kullanılabilir. requests.Session thread-safe'tir.
        self._session = requests.Session() if requests is not None else None
        if self._session is not None:
            self._session.mount("https://", NewsRSSIPv4Adapter())

    # BITCOINIST RSS REMOVAL (controlled implementation, onaylı audit sonucu):
    # bitcoinist.com/feed/ bu ortamda tekrarlanabilir şekilde %0 başarı oranı
    # (her çağrıda tam ~10s ReadTimeout, retry yok) gösterdi -- ölçülebilir
    # coverage katkısı yok (hiç veri dönmedi), ama sources_total'ı şişirerek
    # classify_risk()'in "tüm kaynaklar tarandı, olumsuz olay yok -> yes"
    # dalını (ok==total gerektirir) fiilen erişilemez kılıyordu. Kaldırılması
    # timeout/retry/concurrency mimarisini DEĞİŞTİRMEZ, yalnız listeden çıkar.
    RSS_FEEDS = [
        ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("CoinTelegraph", "https://cointelegraph.com/rss"),
        ("The Block",     "https://www.theblock.co/rss.xml"),
        ("Decrypt",       "https://decrypt.co/feed"),
        ("CryptoSlate",   "https://cryptoslate.com/feed/"),
        ("Blockworks",    "https://blockworks.co/feed/"),
    ]

    RISK_SUFFIXES = ["hack", "exploit", "breach", "regulation", "sec", "delist",
                      "token unlock", "outage", "lawsuit", "ban", "halt", "exit scam",
                      "rug pull", "vulnerability"]

    @staticmethod
    def _strip_html(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text or "").strip()

    @staticmethod
    def _parse_pub(entry):
        try:
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                import calendar
                return datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=timezone.utc)
        except Exception:
            pass
        return None

    def build_keywords(self, symbol: str, coin_name: str) -> List[str]:
        name = (coin_name or symbol).strip()
        base = {name.lower(), symbol.lower()}
        kws = set(base)
        for suf in self.RISK_SUFFIXES:
            kws.add(f"{name.lower()} {suf}")
            kws.add(f"{symbol.lower()} {suf}")
        return list(kws)

    def _fetch_one_source(self, source_name: str, url: str, symbol_pattern, coin_name_lower: str,
                           cutoff) -> dict:
        """Tek bir RSS kaynağını çeker ve eşleşen entry'leri filtreler. THREAD-SAFE:
        hiçbir shared/mutable state'e (self.RSS_FEEDS DIŞINDA hiçbir instance/class
        alanına) yazmaz — yalnız kendi sonucunu bir dict olarak döner. Paralel
        worker'lardan çağrılabilir; aggregation (items/meta birleştirme) BURADA
        YAPILMAZ, tamamen fetch_relevant_news()'in ana thread'inde, tek yerde
        yapılır — race condition'a açık hiçbir ortak yazma işlemi yok.
        _strip_html/_parse_pub @staticmethod (paylaşılan state yok), thread-safe."""
        _t_source = time.time()
        matched_items = []
        ok = False
        entry_count = 0
        error = None
        try:
            _t_req = time.time()
            getter = self._session.get if self._session is not None else requests.get
            resp = getter(url, timeout=10,
                           headers={"User-Agent": "Mozilla/5.0 (compatible; RiskBot/1.0)"})
            _dt_req = time.time() - _t_req
            print(f"[RSS] {source_name} requests.get: {_dt_req:.2f}s status={resp.status_code} "
                  f"redirects={len(resp.history)} final_url={resp.url}", flush=True)
            if resp.status_code != 200:
                error = f"http_status:{resp.status_code}"
            else:
                _t_parse = time.time()
                feed = feedparser.parse(resp.content)
                print(f"[RSS] {source_name} feedparser.parse: {time.time() - _t_parse:.2f}s", flush=True)
                # feedparser ağ/parse hatasında bile bir obje döndürebilir (bozo=1) — gerçek
                # başarı için en az entries listesinin var olmasını arıyoruz.
                if feed is None or not hasattr(feed, "entries"):
                    error = "parse_failed"
                else:
                    ok = True
                    entry_count = len(feed.entries)
                    for entry in feed.entries[:40]:
                        title = self._strip_html(getattr(entry, "title", ""))
                        desc = self._strip_html(getattr(entry, "summary", ""))[:400]
                        link = getattr(entry, "link", "")
                        pub = self._parse_pub(entry)
                        if pub and pub < cutoff:
                            continue
                        original_haystack = f"{title} {desc}"
                        lower_haystack = original_haystack.lower()
                        # Coin adı: case-insensitive substring (isimler yeterince spesifik/uzun).
                        name_ok = bool(coin_name_lower) and coin_name_lower in lower_haystack
                        # Sembol: orijinal case'te, bağımsız token (kelime-sınırı) olarak — kısa
                        # ticker'ların ("SC", "ETH") kelime içine gömülü yanlış eşleşmesini önler
                        # (örn. "discover", "method"). Anthropic'e gitmeden ÖNCEKİ birincil filtre.
                        symbol_ok = bool(symbol_pattern) and bool(symbol_pattern.search(original_haystack))
                        if not (name_ok or symbol_ok):
                            continue
                        matched_items.append({"source": source_name, "title": title, "desc": desc, "link": link})
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            print(f"[RSS] {source_name} EXCEPTION: {error}", flush=True)
        finally:
            print(f"[RSS] === {source_name} TOPLAM: {time.time() - _t_source:.2f}s "
                  f"entries={entry_count} ok={ok} ===", flush=True)
        return {"source": source_name, "ok": ok, "items": matched_items,
                "entry_count": entry_count, "error": error}

    def fetch_relevant_news(self, symbol: str, coin_name: str, hours_back: int = 96) -> Tuple[List[dict], dict]:
        """Döner: (items, meta). meta['sources_ok'] == 0 → hiçbir kaynağa erişilemedi
        (bu durum 'ilgili haber yok' ile KARIŞTIRILMAMALI — çağıran taraf ayırt etmeli).
        7 kaynak artık PARALEL çekiliyor (ThreadPoolExecutor) — yalnız BEKLEME şekli
        değişti (seri->paralel), karar semantiği (sources_ok/sources_total, partial-scan
        güvenlik kuralları) BİREBİR AYNI: her worker yalnız kendi sonucunu döner,
        items.append/meta["sources_ok"]+=1 gibi ortak yazmalar TEK THREAD'de (aşağıda,
        ana thread'de, sıralı) yapılıyor — race condition yok. Bir worker'ın exception'ı
        (_fetch_one_source kendi try/except'i içinde yakalıyor) diğerlerini etkilemez;
        ayrıca burada da savunma amaçlı ikinci bir except var (worker'ın kendisi hiç
        beklenmedik şekilde patlarsa bile Level 1 analizi çökmesin)."""
        meta = {"sources_ok": 0, "sources_total": len(self.RSS_FEEDS), "dependency_missing": False}
        if feedparser is None or requests is None:
            meta["dependency_missing"] = True
            return [], meta
        keywords = self.build_keywords(symbol, coin_name)
        coin_name_lower = (coin_name or "").strip().lower()
        symbol_pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(symbol.upper())}(?![A-Za-z0-9])"
        ) if symbol else None
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

        import concurrent.futures
        _t_parallel = time.time()
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.RSS_FEEDS)) as executor:
            future_to_source = {
                executor.submit(self._fetch_one_source, source_name, url, symbol_pattern,
                                 coin_name_lower, cutoff): source_name
                for source_name, url in self.RSS_FEEDS
            }
            for future in concurrent.futures.as_completed(future_to_source):
                source_name = future_to_source[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    # _fetch_one_source zaten kendi içinde her şeyi yakalıyor — bu yalnız
                    # savunma amaçlı ikinci bir katman (ThreadPoolExecutor/future mekanizmasının
                    # kendisiyle ilgili beklenmedik bir sorun olursa bile diğer kaynakları etkilemesin).
                    print(f"[RSS] {source_name} WORKER EXCEPTION (beklenmeyen): "
                          f"{type(e).__name__}: {e}", flush=True)
                    results.append({"source": source_name, "ok": False, "items": [],
                                     "entry_count": 0, "error": str(e)})
        print(f"[RSS] === PARALEL BLOK TOPLAM DUVAR SAATİ: {time.time() - _t_parallel:.2f}s ===", flush=True)

        # Aggregation — TEK THREAD'de (ana thread), TEK YERDE. items.append ve
        # meta["sources_ok"] += 1 işlemleri BURADAN başka hiçbir yerde yapılmıyor.
        items = []
        for r in results:
            if r["ok"]:
                meta["sources_ok"] += 1
            items.extend(r["items"])

        # Basit dedup (başlık benzerliği)
        unique = []
        for it in items:
            t = it["title"].lower()
            if _rfuzz:
                dup = any(_rfuzz.token_set_ratio(t, u["title"].lower()) >= _DEDUP_THRESHOLD for u in unique)
            else:
                norm = re.sub(r"\W+", "", t)[:60]
                dup = any(re.sub(r"\W+", "", u["title"].lower())[:60] == norm for u in unique)
            if not dup:
                unique.append(it)
        return unique[:15], meta

    def classify_risk(self, symbol: str, coin_name: str, items: List[dict], meta: dict = None) -> dict:
        """Döner: {'status': 'yes'|'wait'|'no'|'nodata', 'reason': str, 'sources': [...]}"""
        meta = meta or {}
        sources = sorted({it["source"] for it in items})

        if meta.get("dependency_missing"):
            return {"status": "nodata",
                    "reason": "feedparser/requests kütüphanesi yok — haberler taranamadı.",
                    "sources": []}

        if meta.get("sources_ok", 0) == 0:
            return {"status": "nodata",
                    "reason": "Hiçbir haber kaynağına erişilemedi (ağ/RSS hatası) — "
                              "haber riski değerlendirilemedi.",
                    "sources": []}

        if not items:
            # "absence of evidence" (ilgili haber bulunamadı) yalnızca TÜM
            # kaynaklar gerçekten güvenilir biçimde tarandıysa "olumsuz olay
            # yok" anlamına gelir. Kısmi tarama + sıfır bulgu, "aramadım" ile
            # "bulamadım"ı ayırt edemeyeceğimiz bir durumdur -> nodata.
            ok, total = meta.get("sources_ok", 0), meta.get("sources_total", 0)
            if total > 0 and ok == total:
                return {"status": "yes",
                        "reason": f"{ok}/{total} kaynak tarandı, ilgili haber bulunamadı — "
                                  f"yakın olumsuz olay tespit edilmedi.",
                        "sources": []}
            return {"status": "nodata",
                    "reason": f"Yalnızca {ok}/{total} kaynak taranabildi, ilgili haber "
                              f"bulunamadı — ancak tarama tamamlanamadığı için 'olumsuz "
                              f"olay yok' güvenilir biçimde söylenemez.",
                    "sources": []}

        if not ANTHROPIC_API_KEY:
            return {"status": "nodata", "reason": "ANTHROPIC_API_KEY yok — haberler sınıflandırılamadı.",
                    "sources": sources}

        _t_prep = time.time()
        try:
            # coin_name kimlik doğrulanamadığında None olabilir — bu yalnızca
            # metin gösterimi için sembole düşürülüyor, eşleştirme kararını
            # etkilemiyor (o karar zaten fetch_relevant_news()'te verildi).
            display_name = coin_name or symbol
            items_text = "\n\n".join(
                f"[{it['source']}] {it['title']}\n{it['desc']}" for it in items[:12]
            )
            prompt = f"""Sen kripto risk analistisin. Aşağıda "{display_name} ({symbol})" ile ilgili haberler var.

Bu haberlerde {display_name}/{symbol} için hack, exploit, güvenlik açığı, borsa delist kararı,
düzenleyici (SEC vb.) soruşturma/dava, ciddi ağ kesintisi gibi DOĞRULANMIŞ VE CİDDİ bir
olumsuz olay var mı?

Kurallar:
- Doğrulanmış ciddi olumsuz olay varsa: HAYIR
- Belirsiz, spekülatif veya düşük etkili bir şey varsa (örn. sadece söylenti, küçük bir güncelleme): NOTR
- Ciddi olumsuz bir olay yoksa: EVET
- Haberler konuyla ilgisizse veya yorum yapamıyorsan: VERI_YOK

Yanıtının İLK SATIRI TAM OLARAK şunlardan biri olsun: EVET / NOTR / HAYIR / VERI_YOK
İkinci satırdan itibaren 1-2 cümlelik Türkçe gerekçe yaz.

HABERLER:
{items_text}"""
            # Sınırlı (bounded) timeout + SDK'nın kendi otomatik retry'ı KAPALI
            # (max_retries=0): bu çağrı bir tek haber sınıflandırması, "10
            # dakika bekle sonra 2 kez daha dene" istemiyoruz — makul sürede
            # cevap gelmezse ana Level 1 analizi bloklanmadan devam etmeli.
            # Herhangi bir hata (timeout/bağlantı/API) zaten aşağıdaki genel
            # except Exception bloğunda "nodata" ile karşılanıyor — asla
            # sahte "yes"/"no" üretmiyor. Client artık PAYLAŞILAN/persistent
            # (bkz. _get_news_anthropic_client) -- her çağrıda yeniden
            # oluşturulmuyor, bağlantı havuzu analizler arasında korunuyor.
            client = _get_news_anthropic_client()
            _t_classify_call = time.time()
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            _t_classify_done = time.time()
            _usage = getattr(resp, "usage", None)
            print(f"[NEWS CLAUDE][TIMING] prompt uzunlugu: {len(prompt)} karakter, "
                  f"client.messages.create() suresi: {_t_classify_done - _t_classify_call:.2f}s "
                  f"(input_tokens={getattr(_usage, 'input_tokens', '?')}, "
                  f"output_tokens={getattr(_usage, 'output_tokens', '?')}) "
                  f"TOPLAM classify_risk: {_t_classify_done - _t_prep:.2f}s", flush=True)
            text = resp.content[0].text.strip()
            lines = text.split("\n", 1)
            verdict = lines[0].strip().upper()
            reason = lines[1].strip() if len(lines) > 1 else ""
            mapping = {"EVET": "yes", "NOTR": "wait", "NÖTR": "wait", "HAYIR": "no", "VERI_YOK": "nodata",
                       "VERİ_YOK": "nodata"}
            status = mapping.get(verdict, "nodata")
            if status == "nodata" and not reason:
                reason = f"Model beklenmeyen yanıt döndürdü: {text[:100]}"
            return {"status": status, "reason": reason or text[:200], "sources": sources}
        except Exception as e:
            print(f"[NEWS CLAUDE][TIMING] hata ONCESI gecen sure: {time.time() - _t_prep:.2f}s", flush=True)
            return {"status": "nodata", "reason": f"Anthropic hatası: {e}", "sources": sources}


class Level1Cancelled(Exception):
    """RealFetcher._build_context() içindeki kontrol noktalarından biri,
    çağıranın (Level1Worker) iptal istediğini bildirdiğinde fırlatılır.
    Gerçek bir hata DEĞİLDİR -- Level1Worker.run() bunu ayrı yakalayıp
    cancelled sinyali yayınlar (failed DEĞİL)."""
    pass


class TechnicalStructureEngine:
    """Technical Structure Engine V1 -- PHASE 1 (yalnız tespit doğruluğu).

    Tamamen saf hesaplama: HİÇBİR network çağrısı yapmaz. Girdi olarak
    yalnız BinanceClient.get_technical_indicators()'ın zaten ürettiği "ohlcv"
    snapshot'ını (aynı kapanmış 1h mumlar, ek çağrı yok) alır.

    V1 kapsamı: ATR14, look-ahead-safe onaylı swing high/low, S/R zone
    clustering, HH/HL/LH/LL/EH/EL, trend_structure. BOS/CHoCH, mum
    formasyonları, grafik formasyonları BU FAZDA YOK (bkz. proje audit notu).

    KALİBRASYON PARAMETRELERİ (DEFAULT_PARAMS): bunlar "doğru eşik" olarak
    kabul EDİLMEMİŞTİR -- gerçek grafik karşılaştırmasıyla kalibre edilecek
    başlangıç hipotezleridir, kodda kasıtlı olarak açık/isimli tutulmuştur:
      - pivot_confirmation_bars (N): pivot'un solunda VE sağında kaç bar
        gerekli. Büyüdükçe daha az ama daha güvenilir pivot, daha uzun
        onay gecikmesi.
      - zone_atr_tolerance (k): S/R zone kümeleme mesafesi VE HH/LH/EH -
        HL/LL/EL eşitlik toleransı = k × ATR14.
      - min_swing_atr_distance (m): ardışık onaylı aynı-yön swing'ler arası
        minimum hareket = m × ATR14 -- altında kalan mikro-gürültü swing'i
        filtrelenir (ayrı bir yapı noktası sayılmaz).

    Bu motor bu fazda: AI Analyst'e gönderilmiyor, ScoreEngine/VetoEngine/
    verdict()/entry_timing()'i hiçbir şekilde etkilemiyor -- yalnız
    ctx["technical_structure"]'a yazılıyor, saf gözlem/kalibrasyon amaçlı."""

    DEFAULT_PARAMS = {
        "pivot_confirmation_bars": 2,
        # PHASE 2: swing-equality (HH/LH/EH sınıflaması) ile S/R zone
        # kümeleme artık AYRI parametreler -- PHASE 1'de ikisi de tek
        # "zone_atr_tolerance"a bağlıydı (audit'te bulunan gerçek bir
        # konflasyon hatası, bkz. calibration raporu Part B). Geriye
        # uyumluluk: birisi eski "zone_atr_tolerance" anahtarını params
        # olarak geçirirse (aşağıdaki __init__), o TEK değer HER İKİSİNE
        # de uygulanır -- eski çağıran kod kırılmaz, ama yeni kod iki
        # değeri bağımsız verebilir.
        "swing_equality_tolerance": 0.3,
        "zone_clustering_tolerance": 0.5,
        # PHASE 2: bir S/R zone'unun toplam genişliği için MUTLAK üst sınır
        # (ayrıca bkz. _cluster_zones_v2 -- zincirleme/chaining düzeltmesi).
        "max_zone_span_atr": 0.5,
        "min_swing_atr_distance": 0.5,
        "atr_period": 14,
        # PHASE 2 (v2, artık YALNIZ OLD-vs-NEW karşılaştırma/regresyon için
        # tutuluyor -- bkz. PHASE 2B raporu, event-count yaklaşımı çok
        # kırılgan bulundu): rejim değişimi için HER İKİ tarafta (high VE
        # low) en az kaç aykırı-yönlü onaylı swing birikmesi gerekir.
        "structural_break_confirmation": 1,
        # Ardışık kaç "nötr" (EH/EL) swing'den sonra mevcut rejim,
        # sonsuza kadar taşınmak yerine 'range'e çöker (item F).
        "range_decay_swings": 6,
        # PHASE 2 için asgari toplam confirmed swing sayısı (high+low)
        "trend_min_confirmed_swings": 3,

        # ── PHASE 2B (v2b) -- displacement-bazlı, iki-taraflı state machine ──
        # structural_break_confirmation'ın yerini alıyor: "kaç adet aykırı
        # swing" yerine "ne kadar GERÇEK fiyat hareketi (ATR-normalize)"
        # sorusu soruluyor -- bkz. PHASE 2B raporu madde 7.
        #
        # Son kaç confirmed swing (aynı taraf -- yalnız high'lar arası veya
        # yalnız low'lar arası) üzerinden net yer değiştirme (displacement)
        # hesaplanır.
        "structure_lookback_swings": 3,
        # displacement/ATR bu eşiği aşarsa o taraf (high veya low) net
        # yönlü sayılır; aşmazsa 'neutral' (consolidation/compression
        # sinyali) -- EH/EL etiketine bağlı DEĞİL, bu yüzden "non-E
        # consolidation" da doğru yakalanır (bkz. PHASE 2B raporu madde 5).
        "structure_displacement_threshold": 0.8,
        # candidate='consolidation' (bir veya iki taraf 'neutral') ardışık
        # kaç kez tekrarlanırsa mevcut rejim 'range'e çöker.
        "consolidation_decay_swings": 5,
        # candidate='conflict' (high ve low taraf AÇIKÇA ters yönlerde,
        # ikisi de net yönlü) ardışık kaç kez tekrarlanırsa mevcut rejim
        # 'range'e çöker (trend_reason='structural_conflict').
        "conflict_persistence_swings": 4,
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        # Geriye uyumluluk: biri hâlâ eski tek "zone_atr_tolerance" anahtarını
        # geçirirse, bu TEK değer hem equality hem clustering'e uygulanır
        # (PHASE 1 davranışını birebir simüle eder) -- körlemesine "iki yere
        # aynı sabit" DEĞİL, yalnız kullanıcı açıkça eski anahtarı verdiğinde.
        if params and "zone_atr_tolerance" in params:
            legacy_val = params["zone_atr_tolerance"]
            merged.setdefault("swing_equality_tolerance", legacy_val)
            merged["swing_equality_tolerance"] = legacy_val
            merged["zone_clustering_tolerance"] = legacy_val
        self.params = merged

    # ── ATR ──────────────────────────────────────────────────────────
    @staticmethod
    def _true_range(highs, lows, closes):
        tr = np.zeros(len(highs))
        tr[0] = highs[0] - lows[0]
        for i in range(1, len(highs)):
            tr[i] = max(highs[i] - lows[i],
                        abs(highs[i] - closes[i - 1]),
                        abs(lows[i] - closes[i - 1]))
        return tr

    @classmethod
    def compute_atr(cls, highs, lows, closes, period=14):
        """Wilder'ın klasik ATR düzgünleştirmesi -- projedeki _rsi()'nin
        kullandığı aynı Wilder-stili artımlı ortalama deseniyle tutarlı.
        Döner: period-1 index'inden itibaren geçerli değerler taşıyan tam
        boy dizi (öncesi 0 -- çağıran taraf yalnız [-1]'i veya
        period-1..sonu aralığını kullanmalı)."""
        if np is None or len(highs) < period + 1:
            return None
        tr = cls._true_range(highs, lows, closes)
        atr = np.zeros(len(tr))
        atr[period - 1] = np.mean(tr[:period])
        for i in range(period, len(tr)):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
        return atr

    # ── Swing pivot tespiti (look-ahead-safe) ───────────────────────
    @staticmethod
    def _find_confirmed_swings(values, n: int, mode: str):
        """values: 1D dizi (highs veya lows). mode='high' local max, 'low'
        local min arar. Her pivot solundaki VE sağındaki n bar'a göre
        değerlendirilir -- confirmed_at_bar_index = bar_index + n. Son n
        bar HİÇBİR ZAMAN onaylı pivot üretemez (look-ahead bias koruması --
        confirmed_at_bar_index, mevcut son bar index'ini aşan pivotlar
        listeye hiç girmez).

        Eşit-değer platoları (ör. iki komşu bar aynı high'a sahip ve ikisi
        de pencerenin ekstremumu): yalnız İLK bar tekil pivot olarak
        sayılır -- aynı platodan iki ayrı swing ÜRETİLMEZ."""
        if len(values) < 2 * n + 1:
            return []
        last_idx = len(values) - 1
        candidates = []
        for i in range(n, len(values) - n):
            window = values[i - n:i + n + 1]
            extreme = np.max(window) if mode == "high" else np.min(window)
            if values[i] == extreme:
                candidates.append(i)
        merged = []
        for idx in candidates:
            if merged and values[idx] == values[merged[-1]] and (idx - merged[-1]) <= n:
                continue  # aynı platonun devamı -- zaten temsil edildi
            merged.append(idx)
        pivots = []
        for idx in merged:
            confirmed_at = idx + n
            if confirmed_at > last_idx:
                continue  # henüz bu snapshot içinde onaylanamaz
            pivots.append({"bar_index": idx, "price": float(values[idx]),
                            "confirmed_at_bar_index": confirmed_at})
        return pivots

    @staticmethod
    def _filter_noise_swings(pivots, atr_value, m, min_bar_gap, per_pivot_atr=None):
        """Ardışık onaylı pivotlar arasında m×ATR'den küçük hareketleri
        mikro-gürültü sayıp eler -- AMA yalnız bu pivotlar ZAMANDA da
        yakınsa (<= min_bar_gap bar). Fiyatça yakın olup zamanda uzak olan
        pivotlar (ör. büyük bir hareketten sonra AYNI seviyeye geri dönüp
        gerçek bir yeniden-test oluşturan swing'ler) filtrelenmez -- bunlar
        S/R açısından meşru, tekrarlanan temaslardır, gürültü değildir.
        Yalnız fiyata bakan bir filtre bu ikisini ayırt edemezdi (gerçek
        veriyle test edilerek bulundu).

        per_pivot_atr (opsiyonel, PHASE 2C): `pivots` ile aynı uzunlukta,
        her pivot'un KENDİ confirmation-anındaki ATR'ını taşıyan liste --
        verilirse gürültü eşiği bunu kullanır (yalnız v2b). None ise
        (varsayılan) eskisi gibi TEK global `atr_value` kullanılır -- bu,
        pivot'un "gürültü mü değil mi" statüsünün (dolayısıyla hangi
        swing'in bir sonrakine 'önceki' referans olacağının) serinin GÜNCEL
        ucundaki ATR'ye göre retroaktif değişmesini önler; v1/v2 legacy
        path bilerek değiştirilmedi."""
        if not pivots or (not atr_value and not per_pivot_atr):
            return pivots
        filtered = [pivots[0]]
        for i, p in enumerate(pivots[1:], start=1):
            prev = filtered[-1]
            tol_atr = atr_value
            if per_pivot_atr is not None and per_pivot_atr[i]:
                tol_atr = per_pivot_atr[i]
            close_in_price = bool(tol_atr) and abs(p["price"] - prev["price"]) < m * tol_atr
            close_in_time = (p["bar_index"] - prev["bar_index"]) <= min_bar_gap
            if close_in_price and close_in_time:
                continue
            filtered.append(p)
        return filtered

    @staticmethod
    def _atr_at_bar(atr_arr, bar_index, atr_period):
        """PHASE 2C: bir bar'daki ATR'yi, o bar'a kadarki bilgiyle SABİT
        şekilde okur. compute_atr() zaten tamamen causal bir dizi döner
        (atr_arr[i], yalnızca 0..i bar'larına bağlıdır -- bkz.
        phase2c_atr_series_lookahead.py kanıtı) -- bu yüzden ayrı bir
        'compute_atr_series' fonksiyonuna gerek yok, doğrudan indexleniyor.
        atr_period-1'den önceki index'ler henüz warm-up aşamasında (0
        değerli) olduğu için ilk geçerli ATR'ye (atr_period-1) sabitlenir."""
        if atr_arr is None:
            return None
        idx = max(bar_index, atr_period - 1)
        idx = min(idx, len(atr_arr) - 1)
        val = float(atr_arr[idx])
        return val if val > 0 else None

    @staticmethod
    def _classify_swing_sequence(swings, atr_value, k, high_labels=True, per_swing_atr=None):
        """Ardışık aynı-yön (yalnız high'lar arası veya yalnız low'lar
        arası) swing'leri karşılaştırıp HH/LH/EH (ya da HL/LL/EL) etiketler.
        İlk swing için referans yok -> label None.

        per_swing_atr (opsiyonel, PHASE 2C): `swings` ile aynı uzunlukta,
        HER swing'in KENDİ confirmation-anındaki ATR'ını taşıyan liste --
        verilirse equality karşılaştırması bu look-ahead-safe değeri
        kullanır (yalnız v2b). None ise (varsayılan) eskisi gibi TEK bir
        global `atr_value` tüm swing'ler için kullanılır -- bu, v1/v2'nin
        PHASE 1/2 calibration karşılaştırmasını birebir koruması için
        BİLEREK değiştirilmedi (legacy path)."""
        labels = ("HH", "LH", "EH") if high_labels else ("HL", "LL", "EL")
        higher_lbl, lower_lbl, equal_lbl = labels
        out = []
        for i, p in enumerate(swings):
            item = dict(p)
            if i == 0:
                item["label"] = None
            else:
                diff = p["price"] - swings[i - 1]["price"]
                tol_atr = atr_value
                if per_swing_atr is not None and per_swing_atr[i]:
                    tol_atr = per_swing_atr[i]
                if tol_atr and abs(diff) <= k * tol_atr:
                    item["label"] = equal_lbl
                elif diff > 0:
                    item["label"] = higher_lbl
                else:
                    item["label"] = lower_lbl
            out.append(item)
        return out

    @staticmethod
    def _determine_trend_structure_v1(labeled_highs, labeled_lows):
        """PHASE 1 BASELINE (kırılgan) -- yalnız EN SON confirmed high/low
        ilişkisine bakar. PHASE 2 calibration raporunda kanıtlanan problem:
        önceki anlamlı HH/HL veya LH/LL yapısını, son swing 'E' (eşit)
        ailesindeyse veya taraflar çelişiyorsa TAMAMEN görmezden gelir.
        Yalnız OLD-vs-NEW karşılaştırması için tutuluyor, production
        varsayılanı artık _determine_trend_structure_v2."""
        if len(labeled_highs) < 2 or len(labeled_lows) < 2:
            return "insufficient_data"
        high_label = labeled_highs[-1]["label"]
        low_label = labeled_lows[-1]["label"]
        high_bullish, high_bearish = high_label == "HH", high_label == "LH"
        low_bullish, low_bearish = low_label == "HL", low_label == "LL"
        if high_bullish and low_bullish:
            return "bullish"
        if high_bearish and low_bearish:
            return "bearish"
        return "range"

    @staticmethod
    def _determine_trend_structure_v2(labeled_highs, labeled_lows, break_confirmation, decay_swings,
                                       min_confirmed_swings):
        """PHASE 2 -- 'yapısal kalıcılık' (structural persistence) modeli.

        Tasarım gerekçesi (MODEL 3/4 hibriti, calibration raporunda
        değerlendirilen MODEL 0/1/2/3/4 arasından seçildi): "son K'nın
        çoğunluğu" gibi kaba bir kural İCAT EDİLMEDİ -- bunun yerine mevcut
        rejim, yalnızca YETERİNCE GÜÇLÜ, İKİ TARAFLI (hem high hem low)
        kanıt birikince değişen bir DURUM (state) olarak modellendi:

        - Rejim yalnız hem son confirmed high (HH/LH) HEM son confirmed low
          (HL/LL) AYNI YÖNDE olduğunda dogrudan KURULUR/GÜÇLENİR.
        - Tek taraflı bir sinyal (ör. yalnızca bir LH, low tarafı hâlâ HL)
          veya 'E' ailesi (EH/EL) mevcut rejimi ANINDA SIFIRLAMAZ --
          rejim KORUNUR (persistence) -- item A/B'nin gerektirdiği davranış.
        - Rejime AYKIRI yönde kanıt biriktirilir: hem high hem low tarafında
          en az `break_confirmation` adet aykırı-yönlü confirmed swing
          birikince (item D/E: 'hem LH hem LL' / 'hem HH hem HL') rejim
          GERÇEKTEN kırılır -- yalnız bir tarafın tekrarlı sinyali yetmez.
        - Ardışık `decay_swings` adet nötr (EH/EL) swing'den sonra --
          hiçbir taraf yön vermiyorsa -- rejim 'range'e ÇÖKER (item F: eski
          trend sonsuza kadar taşınmaz).
        - High/low taraflarının kalıcı biçimde ÇELİŞTİĞİ (biri sürekli
          yukarı, diğeri sürekli aşağı üretiyor, hiçbiri 'break' eşiğine
          ulaşamıyor) durumlar da mevcut rejimi korur ya da (rejim hiç
          kurulmadıysa) 'range' kalır -- item C'nin istediği gibi kararı
          UYDURMAZ, gerçek bir çelişki/range olarak bırakır."""
        combined = sorted(
            [dict(p, kind="high") for p in labeled_highs if p["label"] is not None] +
            [dict(p, kind="low") for p in labeled_lows if p["label"] is not None],
            key=lambda p: p["bar_index"])
        if len(combined) < min_confirmed_swings:
            return {"trend_structure": "insufficient_data", "trace": []}

        regime = None
        opposing_high, opposing_low = 0, 0
        neutral_streak = 0
        trace = []

        for item in combined:
            lbl = item["label"]
            if item["kind"] == "high":
                direction = "up" if lbl == "HH" else "down" if lbl == "LH" else "neutral"
            else:
                direction = "up" if lbl == "HL" else "down" if lbl == "LL" else "neutral"

            if direction == "neutral":
                neutral_streak += 1
                # DÜZELTME (test 3'te bulundu): regime henüz None (hiç
                # kurulmamış) olsa bile, yeterince uzun bir nötr seri
                # 'range'e çökmeli -- eskiden yalnız regime is not None
                # durumunda çöküyordu, bu yüzden baştan sona düz/nötr bir
                # seri sonsuza kadar None kalıp yanlışlıkla
                # 'insufficient_data' dönüyordu (range DEĞİL).
                if neutral_streak >= decay_swings:
                    trace.append({"bar_index": item["bar_index"], "kind": item["kind"], "label": lbl,
                                   "action": f"neutral_streak={neutral_streak}>=decay -> range'e çöktü"})
                    regime = "range"
                    opposing_high = opposing_low = 0
                else:
                    trace.append({"bar_index": item["bar_index"], "kind": item["kind"], "label": lbl,
                                   "action": f"nötr (neutral_streak={neutral_streak}), rejim korunuyor"})
                continue

            neutral_streak = 0
            implied = "bullish" if direction == "up" else "bearish"

            if regime is None:
                # rejim henüz hiç kurulmadı -- ilk net yönlü (E-ailesi
                # olmayan) sinyal aday rejimi başlatır. Bundan sonraki
                # KIRILMALAR yine aşağıdaki dual-side break mekanizmasından
                # geçer -- bu yalnız bir bootstrap, kısayol değil.
                regime = implied
                trace.append({"bar_index": item["bar_index"], "kind": item["kind"], "label": lbl,
                               "action": f"ilk yönlü sinyal -> rejim başlangıcı: {regime}"})
                continue

            if implied == regime:
                # rejimi doğruluyor -- karşıt sayaç sıfırlanır (rejim yeniden güçlendi)
                if item["kind"] == "high":
                    opposing_high = 0
                else:
                    opposing_low = 0
                trace.append({"bar_index": item["bar_index"], "kind": item["kind"], "label": lbl,
                               "action": f"rejimi ({regime}) doğruluyor"})
            else:
                if item["kind"] == "high":
                    opposing_high += 1
                else:
                    opposing_low += 1
                trace.append({"bar_index": item["bar_index"], "kind": item["kind"], "label": lbl,
                               "action": f"rejime ({regime}) aykırı -- opposing_high={opposing_high} "
                                         f"opposing_low={opposing_low}"})
                if opposing_high >= break_confirmation and opposing_low >= break_confirmation:
                    old_regime = regime
                    regime = implied
                    opposing_high = opposing_low = 0
                    trace[-1]["action"] += f" -> STRUCTURAL BREAK: {old_regime} -> {regime}"

        return {"trend_structure": regime or "insufficient_data", "trace": trace}

    # ── PHASE 2B: displacement-bazlı, iki-taraflı state machine ─────
    @staticmethod
    def _side_state(swings, atr_value, lookback, threshold):
        """Son `lookback` confirmed swing (AYNI taraf -- yalnız high'lar
        veya yalnız low'lar) arasındaki NET fiyat yer değiştirmesini
        (ilk fiyat -> son fiyat), ATR'ye normalize ederek 'bullish'/
        'bearish'/'neutral' döner. EH/EL etiketine BAĞLI DEĞİL -- yalnız
        ham fiyat/ATR kullanır. Bu yüzden 'non-E consolidation' (küçük ama
        equality-threshold'una tam girmeyen sallanmalar) da doğru
        yakalanır: LH/HL/LH/HL gibi teker teker 'yönlü' etiketlenen ama
        NET olarak hiçbir yere gitmeyen bir dizi, burada doğru şekilde
        'neutral' (displacement küçük) çıkar.

        Döner: (state, displacement_in_atr)."""
        if len(swings) < 2 or not atr_value:
            return "neutral", 0.0
        recent = swings[-lookback:] if len(swings) > lookback else swings
        if len(recent) < 2:
            return "neutral", 0.0
        displacement = recent[-1]["price"] - recent[0]["price"]
        displacement_atr = displacement / atr_value
        if displacement_atr >= threshold:
            return "bullish", displacement_atr
        if displacement_atr <= -threshold:
            return "bearish", displacement_atr
        return "neutral", displacement_atr

    @staticmethod
    def _determine_trend_structure_v2b(labeled_highs, labeled_lows, atr_value, lookback, threshold,
                                        consolidation_decay, conflict_persistence, min_confirmed_swings):
        """PHASE 2B -- V2'nin iki bilinen hatasını (persistent one-sided
        conflict kilitlenmesi + EH/EL'ye bağımlı consolidation tespiti)
        kök nedeninden çözen yeniden tasarım.

        Temel fark: 'kaç ADET aykırı swing oldu' (event-count, V2'nin
        kırılganlığının kaynağı) yerine, high ve low tarafları AYRI AYRI
        'ne kadar GERÇEK, ATR-normalize fiyat hareketi oldu' (displacement)
        sorusuyla değerlendirilir (bkz. _side_state). Bu tek değişiklik
        üç sorunu birden çözer:
          1) Persistent conflict artık asla 'sıkışmız' -- high_state/
             low_state her adımda SIFIRDAN, güncel pencereden yeniden
             hesaplanır (event sayaçları YOK, birikip kalan bir şey yok).
          2) Consolidation artık EH/EL etiketine bağlı değil -- küçük
             LH/HL sallanmaları bile NET displacement küçükse 'neutral'
             sayılır.
          3) structural_break_confirmation'ın "1 vs 2-3" kırılganlığı
             ortadan kalkar -- flip artık tek bir "olay sayısı" eşiğine
             değil, sürekli/ölçülebilir bir displacement büyüklüğüne
             dayanıyor (bkz. PHASE 2B raporu madde 7 duyarlılık testleri).

        high_state × low_state matrisi:
          bullish × bullish -> candidate='bullish'
          bearish × bearish -> candidate='bearish'
          (en az biri 'neutral')  -> candidate='consolidation'
          bullish × bearish (veya tersi, İKİSİ de net yönlü ama TERS) ->
                                      candidate='conflict'

        Rejim hafızası (persistence): candidate bullish/bearish ise rejim
        DOĞRUDAN o olur (event-count katmanı yok -- displacement zaten
        yeterli kanıt). candidate='consolidation' veya 'conflict' ise
        rejim KORUNUR, yalnız ilgili streak sayacı birikir; streak eşiği
        aşılırsa rejim 'range'e çöker ve trend_reason bunu açıkça söyler
        (structural_conflict / consolidation) -- kör bir sabit sayı
        DEĞİL, bu eşikler de ayrıca duyarlılık testine tabi tutuldu."""
        combined = sorted(
            [dict(p, kind="high") for p in labeled_highs] +
            [dict(p, kind="low") for p in labeled_lows],
            key=lambda p: p["bar_index"])
        total_confirmed = len([c for c in combined if c["label"] is not None])
        if total_confirmed < min_confirmed_swings:
            return {"trend_structure": "insufficient_data", "trend_reason": "insufficient_data", "trace": []}

        regime = None
        trend_reason = None
        consolidation_streak = 0
        conflict_streak = 0
        trace = []
        seen_highs, seen_lows = [], []

        for item in combined:
            if item["kind"] == "high":
                seen_highs.append(item)
            else:
                seen_lows.append(item)
            if item["label"] is None:
                continue  # ilk swing, henüz karşılaştırma referansı yok

            high_state, high_disp = TechnicalStructureEngine._side_state(
                seen_highs, atr_value, lookback, threshold)
            low_state, low_disp = TechnicalStructureEngine._side_state(
                seen_lows, atr_value, lookback, threshold)

            if high_state == "bullish" and low_state == "bullish":
                candidate = "bullish"
            elif high_state == "bearish" and low_state == "bearish":
                candidate = "bearish"
            elif high_state == "neutral" or low_state == "neutral":
                candidate = "consolidation"
            else:
                candidate = "conflict"  # high/low NET olarak ters yönlerde

            entry = {"bar_index": item["bar_index"], "kind": item["kind"], "label": item["label"],
                     "high_state": high_state, "high_disp_atr": round(high_disp, 3),
                     "low_state": low_state, "low_disp_atr": round(low_disp, 3),
                     "candidate": candidate}

            if candidate in ("bullish", "bearish"):
                consolidation_streak = 0
                conflict_streak = 0
                if regime != candidate:
                    entry["action"] = f"REJİM: {regime} -> {candidate} (displacement-confirmed)"
                    regime = candidate
                    trend_reason = "structural_displacement"
                else:
                    entry["action"] = f"rejim ({regime}) doğrulandı"
            elif candidate == "consolidation":
                conflict_streak = 0
                consolidation_streak += 1
                entry["action"] = f"consolidation (streak={consolidation_streak})"
                if consolidation_streak >= consolidation_decay and regime != "range":
                    entry["action"] += " -> RANGE'E ÇÖKTÜ (consolidation)"
                    regime = "range"
                    trend_reason = "consolidation"
            else:  # conflict
                consolidation_streak = 0
                conflict_streak += 1
                entry["action"] = f"conflict (streak={conflict_streak})"
                if conflict_streak >= conflict_persistence and regime != "range":
                    entry["action"] += " -> RANGE'E ÇÖKTÜ (structural_conflict)"
                    regime = "range"
                    trend_reason = "structural_conflict"

            trace.append(entry)

        return {"trend_structure": regime or "insufficient_data",
                "trend_reason": trend_reason or "insufficient_data", "trace": trace}

    # ── S/R zone clustering ──────────────────────────────────────────
    @staticmethod
    def _cluster_zones_v1(pivots, atr_value, k, total_bars):
        """PHASE 1 BASELINE -- zincirleme/chaining kümeleme. Her pivot yalnız
        kümedeki SON eklenen noktaya olan mesafeyle karşılaştırılır; bu
        yüzden güçlü/merdiven-şeklindeki trendlerde küme sınırsız
        genişleyebilir (bkz. calibration raporu Part I -- BTC 2.8×ATR,
        SOL 3×ATR genişliğinde tek dev zone). Yalnız OLD-vs-NEW
        karşılaştırması için tutuluyor."""
        if not pivots:
            return []
        sorted_p = sorted(pivots, key=lambda p: p["price"])
        tol = (k * atr_value) if atr_value else 0.0
        clusters, current = [], [sorted_p[0]]
        for p in sorted_p[1:]:
            if p["price"] - current[-1]["price"] <= tol:
                current.append(p)
            else:
                clusters.append(current)
                current = [p]
        clusters.append(current)
        return TechnicalStructureEngine._zones_from_clusters(clusters, total_bars)

    @staticmethod
    def _cluster_zones_v2(pivots, atr_value, k, max_span_atr, total_bars):
        """PHASE 2 -- ANCHOR-bazlı kümeleme + mutlak max-span kilidi
        (audit Part B seçeneği D: B+C kombinasyonu).

        Neden bu tasarım: pivotlar fiyata göre SIRALANDIKTAN sonra, her yeni
        pivot kümedeki SON noktaya değil, kümenin İLK (en düşük fiyatlı)
        noktasına -- 'anchor'a -- olan mesafeyle karşılaştırılır. Sıralı bir
        dizide anchor sabit tutulduğu için bu, cebirsel olarak kümenin
        TOPLAM genişliğini `tol` ile SINIRLAR (zincirleme artık mümkün
        değil: A yakın B, B yakın C olsa bile, C anchor'a (A'ya) uzaksa
        C ayrı bir kümeye gider). Buna ek olarak, bağımsız bir
        `max_span_atr` mutlak üst sınırı da uygulanır (audit'in istediği
        B+C kombinasyonu, çift güvence) -- normalde tol zaten bunu
        sağladığı için max_span_atr yalnızca tol'dan DAHA SIKI bir sınır
        istenirse devreye girer.

        Determinism/look-ahead: pivotlar yalnız FİYATA göre sıralanır,
        zaman bilgisi kümeleme kararını etkilemez -- pivot sırası
        (girdi listesi sırası) sonucu DEĞİŞTİRMEZ (yalnızca fiyat sırası
        önemli), bu yüzden pivot-sırası-bağımsızlığı testleri geçer.
        Look-ahead: burada kullanılan pivotlar zaten yalnızca CONFIRMED
        (confirmed_at_bar_index geçerli) pivotlardır -- bu fonksiyon kendi
        başına hiçbir zaman-bilgisi eklemez/çıkarmaz."""
        if not pivots:
            return []
        sorted_p = sorted(pivots, key=lambda p: p["price"])
        tol = (k * atr_value) if atr_value else 0.0
        max_span = (max_span_atr * atr_value) if (atr_value and max_span_atr) else tol
        effective_tol = min(tol, max_span) if max_span else tol

        clusters, current = [], [sorted_p[0]]
        anchor_price = sorted_p[0]["price"]
        for p in sorted_p[1:]:
            if (p["price"] - anchor_price) <= effective_tol:
                current.append(p)
            else:
                clusters.append(current)
                current = [p]
                anchor_price = p["price"]
        clusters.append(current)
        return TechnicalStructureEngine._zones_from_clusters(clusters, total_bars)

    @staticmethod
    def _zones_from_clusters(clusters, total_bars):
        zones = []
        for cluster in clusters:
            prices = [p["price"] for p in cluster]
            last_touch_bar = max(p["bar_index"] for p in cluster)
            touch_count = len(cluster)
            zone = {
                "zone_low": min(prices), "zone_high": max(prices),
                "center": sum(prices) / len(prices),
                "touch_count": touch_count,
                "confirmed": touch_count >= 2,
                "last_touch_bar_index": last_touch_bar,
                "last_touched_bars_ago": total_bars - 1 - last_touch_bar,
                "touches": cluster,
            }
            touch_score = min(1.0, touch_count / 4.0)
            recency_score = max(0.0, 1.0 - zone["last_touched_bars_ago"] / total_bars)
            zone["confidence"] = round(0.5 * touch_score + 0.5 * recency_score, 3)
            zones.append(zone)
        return zones

    @staticmethod
    def _nearest_zones(zones, current_price):
        confirmed = [z for z in zones if z["confirmed"]]
        below = [z for z in confirmed if z["zone_high"] < current_price]
        above = [z for z in confirmed if z["zone_low"] > current_price]
        nearest_support = max(below, key=lambda z: z["zone_high"]) if below else None
        nearest_resistance = min(above, key=lambda z: z["zone_low"]) if above else None
        return nearest_support, nearest_resistance

    # ── Ana giriş noktası ────────────────────────────────────────────
    def analyze(self, ohlcv: dict, algorithm_version: str = "v2b") -> dict:
        """ohlcv: BinanceClient.get_technical_indicators()['ohlcv'] ile
        AYNI biçim (open/high/low/close/volume numpy dizileri, aynı
        kapanmış-mum snapshot'ı -- yeni network çağrısı YOK).

        algorithm_version:
          'v2b' (PHASE 2B, varsayılan) -- displacement-bazlı trend state
                machine + PHASE 2'nin anchor-bazlı S/R clustering'i.
          'v2'  (PHASE 2 -- yalnız OLD-vs-NEW karşılaştırma/regresyon için
                tutuluyor, event-count trend modeli kırılgan bulundu).
          'v1'  (PHASE 1 baseline -- yalnız karşılaştırma amaçlı)."""
        p = self.params
        highs, lows, closes = ohlcv["high"], ohlcv["low"], ohlcv["close"]
        n_bars = len(closes)
        n = p["pivot_confirmation_bars"]
        min_required = p["atr_period"] + 2 * n + 5
        if np is None or n_bars < min_required:
            return {"status": "insufficient_data",
                    "reason": f"yalnız {n_bars} bar mevcut, en az {min_required} gerekli",
                    "params": dict(p), "bar_count": n_bars, "algorithm_version": algorithm_version}

        atr_arr = self.compute_atr(highs, lows, closes, p["atr_period"])
        atr_last = float(atr_arr[-1]) if atr_arr is not None else None

        swing_highs_raw = self._find_confirmed_swings(highs, n, mode="high")
        swing_lows_raw = self._find_confirmed_swings(lows, n, mode="low")

        # PHASE 2C: her HAM pivota, FİLTRELEMEDEN ÖNCE, KENDİ confirmation
        # anındaki ATR'yi damgala -- additive alan, tüm algorithm_version'
        # larda dolduruluyor (diagnostic amaçlı). Filtrelemeden önce
        # yapılması kritik: aksi halde gürültü filtresi de global atr_last
        # kullanmaya devam eder ve hangi pivotun 'gürültü' sayılıp elendiği
        # (dolayısıyla hangi swing'in bir sonrakine 'önceki' referans
        # olacağı) serinin güncel ucuna göre retroaktif değişebilirdi.
        for _sw in swing_highs_raw + swing_lows_raw:
            _sw["atr_at_confirmation"] = self._atr_at_bar(atr_arr, _sw["confirmed_at_bar_index"], p["atr_period"])

        # min_bar_gap = 2n: pivot_confirmation_bars penceresinin kendisiyle
        # aynı büyüklük mertebesinde -- ayrı bir kalibrasyon sabiti icat
        # etmemek için mevcut N parametresine bağlı tutuldu.
        noise_bar_gap = 2 * n
        if algorithm_version == "v2b":
            _high_raw_atrs = [s.get("atr_at_confirmation") for s in swing_highs_raw]
            _low_raw_atrs = [s.get("atr_at_confirmation") for s in swing_lows_raw]
            swing_highs = self._filter_noise_swings(swing_highs_raw, atr_last, p["min_swing_atr_distance"],
                                                      noise_bar_gap, per_pivot_atr=_high_raw_atrs)
            swing_lows = self._filter_noise_swings(swing_lows_raw, atr_last, p["min_swing_atr_distance"],
                                                     noise_bar_gap, per_pivot_atr=_low_raw_atrs)
        else:
            # v1/v2: LEGACY path -- global atr_last, PHASE 1/2 calibration
            # karşılaştırmasını birebir korumak için bilerek değiştirilmedi.
            swing_highs = self._filter_noise_swings(swing_highs_raw, atr_last, p["min_swing_atr_distance"], noise_bar_gap)
            swing_lows = self._filter_noise_swings(swing_lows_raw, atr_last, p["min_swing_atr_distance"], noise_bar_gap)

        equality_tol = p["zone_clustering_tolerance"] if algorithm_version == "v1" else p["swing_equality_tolerance"]
        if algorithm_version == "v2b":
            # Look-ahead-safe path: her swing kendi confirmation-anı ATR'ıyla
            # sınıflanır -- gelecekteki bar'ların ATR'si geçmiş label'ı
            # asla değiştiremez (PHASE 2C acceptance kriteri).
            _high_atrs = [s.get("atr_at_confirmation") for s in swing_highs]
            _low_atrs = [s.get("atr_at_confirmation") for s in swing_lows]
            labeled_highs = self._classify_swing_sequence(swing_highs, atr_last, equality_tol, True, per_swing_atr=_high_atrs)
            labeled_lows = self._classify_swing_sequence(swing_lows, atr_last, equality_tol, False, per_swing_atr=_low_atrs)
        else:
            # v1/v2: LEGACY path -- global atr_last, PHASE 1/2 calibration
            # karşılaştırmasını birebir korumak için bilerek değiştirilmedi.
            labeled_highs = self._classify_swing_sequence(swing_highs, atr_last, equality_tol, True)
            labeled_lows = self._classify_swing_sequence(swing_lows, atr_last, equality_tol, False)

        trend_trace = []
        trend_reason = None
        if algorithm_version == "v2b":
            trend_result = self._determine_trend_structure_v2b(
                labeled_highs, labeled_lows, atr_last,
                p["structure_lookback_swings"], p["structure_displacement_threshold"],
                p["consolidation_decay_swings"], p["conflict_persistence_swings"],
                p["trend_min_confirmed_swings"])
            trend_structure = trend_result["trend_structure"]
            trend_trace = trend_result["trace"]
            trend_reason = trend_result["trend_reason"]
        elif algorithm_version == "v2":
            trend_result = self._determine_trend_structure_v2(
                labeled_highs, labeled_lows,
                p["structural_break_confirmation"], p["range_decay_swings"], p["trend_min_confirmed_swings"])
            trend_structure = trend_result["trend_structure"]
            trend_trace = trend_result["trace"]
        else:
            trend_structure = self._determine_trend_structure_v1(labeled_highs, labeled_lows)

        all_pivots = swing_highs + swing_lows
        if algorithm_version in ("v2", "v2b"):
            zones = self._cluster_zones_v2(all_pivots, atr_last, p["zone_clustering_tolerance"],
                                            p["max_zone_span_atr"], n_bars)
        else:
            zones = self._cluster_zones_v1(all_pivots, atr_last, p["zone_clustering_tolerance"], n_bars)
        current_price = float(closes[-1])
        support_zones = [z for z in zones if z["zone_high"] < current_price]
        resistance_zones = [z for z in zones if z["zone_low"] > current_price]
        nearest_support, nearest_resistance = self._nearest_zones(zones, current_price)

        dist_support_pct = (
            (current_price - nearest_support["zone_high"]) / current_price * 100
            if nearest_support else None)
        dist_resistance_pct = (
            (nearest_resistance["zone_low"] - current_price) / current_price * 100
            if nearest_resistance else None)

        return {
            "status": "ok",
            "algorithm_version": algorithm_version,
            "params": dict(p),
            "bar_count": n_bars,
            "current_price": current_price,
            "atr14": atr_last,
            "swing_highs": labeled_highs,
            "swing_lows": labeled_lows,
            "trend_structure": trend_structure,
            "trend_reason": trend_reason,
            "trend_trace": trend_trace,
            "support_zones": support_zones,
            "resistance_zones": resistance_zones,
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resistance,
            "distance_to_support_pct": round(dist_support_pct, 3) if dist_support_pct is not None else None,
            "distance_to_resistance_pct": round(dist_resistance_pct, 3) if dist_resistance_pct is not None else None,
        }


def _compute_recent_price_action(ohlcv: dict) -> Optional[dict]:
    """RECENT PRICE ACTION V1: TechnicalStructureEngine.analyze()'nin KENDİSİNE
    (confirmed swing/trend algoritması) HİÇ dokunmadan, AYNI ohlcv snapshot'ından
    (yeni network çağrısı YOK) ayrı, basit ve KAPANMIŞ mumlarla sınırlı bir özet
    üretir. Yeni pivot/swing algoritması YOK -- yalnız close/open karşılaştırması
    ve yüzde değişim. ohlcv["close"]/["open"] TSE'nin de kullandığı "closed"
    (yalnız kapanmış mum) diziyle AYNI kaynaktır -- canlı/açık mum hiç dahil
    değildir, bu yüzden confirmed structure ile aynı look-ahead-safe zemin
    üzerinde durur (yalnız symmetric-window confirmation gerektirmez)."""
    opens, closes = ohlcv.get("open"), ohlcv.get("close")
    if opens is None or closes is None or len(closes) < 5:
        return None
    n = len(closes)

    def _pct_change(k):
        if n <= k:
            return None
        base = closes[-1 - k]
        if not base:
            return None
        return round(float((closes[-1] - base) / base * 100), 3)

    def _candle_counts(k):
        bulls = bears = 0
        for i in range(1, k + 1):
            if i > n:
                break
            c, o = closes[-i], opens[-i]
            if c > o:
                bulls += 1
            elif c < o:
                bears += 1
        return bulls, bears

    last3_bulls, last3_bears = _candle_counts(3)
    last4_bulls, last4_bears = _candle_counts(4)
    return {
        "timeframe": "1h",
        "basis": "closed_candles_only",
        "previous_close": float(closes[-2]) if n >= 2 else None,
        "change_2h_pct": _pct_change(2),
        "change_3h_pct": _pct_change(3),
        "change_4h_pct": _pct_change(4),
        "last_3_bullish_count": last3_bulls,
        "last_3_bearish_count": last3_bears,
        "last_4_bullish_count": last4_bulls,
        "last_4_bearish_count": last4_bears,
    }


class RealFetcher(BaseFetcher):
    """Level 1 için gerçek veri orkestratörü. MockFetcher ile aynı arayüzü
    (fetch(symbol, metric) -> DataPoint) sağlar, böylece run_level1 döngüsü değişmeden çalışır."""

    def __init__(self, on_progress=None, should_cancel=None):
        self.binance = BinanceClient
        self.coingecko = CoinGeckoFetcher()
        self.cmc = CMCFetcher()
        self.coinmetrics = CoinMetricsFetcher
        self.news = NewsRiskFetcher()
        self._cache: Dict[str, dict] = {}
        self._source_status: Dict[str, str] = {}
        self._on_progress = on_progress or (lambda msg: None)
        # should_cancel: parametresiz, bool döndüren bir callable (tipik
        # olarak Level1Worker.isInterruptionRequested). Verilmezse (örn.
        # TEST_ profilleri MockFetcher kullanır, RealFetcher hiç
        # örneklenmez) davranış tamamen eskisiyle AYNI -- hiçbir zaman
        # iptal edilmiş sayılmaz. Yalnız büyük aşamalar ARASINDA
        # kontrol edilir (bkz. _check_cancel çağrı noktaları) -- aktif
        # bloklanmış bir ağ çağrısını kesmeye ÇALIŞMAZ.
        self._should_cancel = should_cancel or (lambda: False)

    def _check_cancel(self):
        if self._should_cancel():
            raise Level1Cancelled()

    def _progress(self, msg: str):
        try:
            self._on_progress(msg)
        except Exception:
            pass

    @staticmethod
    def _timed(label: str, fn, *args, **kwargs):
        """YALNIZ TEŞHİS amaçlı zaman ölçümü — fn'in davranışını, dönüş
        değerini veya hata yayılımını HİÇ değiştirmez (try/finally), yalnız
        console/IDLE'a [TIMING] satırı yazar. Timeout/retry/cache mantığına
        dokunmuyor."""
        t0 = time.time()
        try:
            return fn(*args, **kwargs)
        finally:
            print(f"[TIMING] {label}: {time.time() - t0:.1f}s", flush=True)

    def _build_context(self, symbol: str) -> dict:
        symbol = symbol.upper()
        if symbol in self._cache:
            return self._cache[symbol]

        ctx: Dict[str, Any] = {}
        _t_total = time.time()

        self._progress("Binance verileri alınıyor...")
        _t_binance = time.time()
        # BINANCE PARALLEL FETCH (controlled implementation, onaylı concurrency-
        # safety audit sonucu): bu 7 çağrı birbirinden TAMAMEN BAĞIMSIZ (hiçbiri
        # diğerinin sonucunu okumuyor, BinanceClient tamamen stateless/classmethod,
        # paylaşılan requests.Session yok -- yalnız urllib, her çağrı kendi
        # bağlantısını açıyor). RSS tarafındaki mevcut future/as_completed
        # deseninin birebir aynısı: her future kendi try/except'iyle izole,
        # aggregation TEK THREAD'de (burada, ana thread'de) yapılıyor. _timed()
        # zaten fn'in davranışını/hata yayılımını değiştirmiyor (yalnız [TIMING]
        # logluyor) -- bu satır DEĞİŞMEDİ, yalnız 7 çağrı artık paralel submit
        # ediliyor. BinanceClient metric formülleri/threshold/status logic'i
        # HİÇ değişmedi.
        import concurrent.futures as _cf
        _binance_calls = {
            "ticker_24h": ("Binance ticker_24h", lambda: self.binance.get_ticker_24h(symbol)),
            "avg_volume_7d": ("Binance avg_volume_7d", lambda: self.binance.get_avg_volume_7d_usd(symbol)),
            "order_book": ("Binance order_book", lambda: self.binance.get_order_book_metrics(symbol)),
            "technical_indicators": ("Binance technical_indicators (klines)",
                                      lambda: self.binance.get_technical_indicators(symbol)),
            "funding_detail": ("Binance funding_detail", lambda: self.binance.get_funding_detail(symbol)),
            "open_interest_trend": ("Binance open_interest_trend",
                                      lambda: self.binance.get_open_interest_trend(symbol)),
            "btc_regime_and_r7": ("Binance btc_regime_and_r7 (1d)",
                                    lambda: self.binance.get_btc_daily_regime_and_r7()),
        }
        binance_results = {}
        with _cf.ThreadPoolExecutor(max_workers=7) as executor:
            future_to_key = {
                executor.submit(self._timed, label, fn): key
                for key, (label, fn) in _binance_calls.items()
            }
            for future in _cf.as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    binance_results[key] = future.result()
                except Exception as e:
                    # _timed()/BinanceClient metotları kendi hatalarını zaten
                    # yakalayıp None/[] döndürüyor (mevcut davranış) -- bu yalnız
                    # savunma amaçlı ikinci katman (RSS'teki desenle aynı),
                    # beklenmedik bir worker/future hatası diğer 6 sonucu
                    # ETKİLEMESİN diye.
                    print(f"[BINANCE] {key} WORKER EXCEPTION (beklenmeyen): "
                          f"{type(e).__name__}: {e}", flush=True)
                    binance_results[key] = None

        ticker = binance_results.get("ticker_24h")
        self._source_status["Binance 24h"] = "ok" if ticker else "hata"
        if ticker:
            try:
                ctx["volume_24h_usd"] = float(ticker.get("quoteVolume", 0))
                ctx["price_change_24h_pct_ticker"] = float(ticker.get("priceChangePercent", 0))
            except (TypeError, ValueError):
                pass

        avg_vol = binance_results.get("avg_volume_7d")
        ctx["volume_7d_avg_usd"] = avg_vol
        self._source_status["Binance 7g hacim"] = "ok" if avg_vol else "hata"

        book = binance_results.get("order_book")
        ctx["order_book"] = book
        self._source_status["Binance order book"] = "ok" if book else "hata"

        self._progress("Teknik göstergeler hesaplanıyor...")
        ind = binance_results.get("technical_indicators")
        ctx["indicators"] = ind
        self._source_status["Binance teknik göstergeler"] = "ok" if ind else "hata"

        # ADDITIVE: Technical Structure Engine V1 (PHASE 1 -- yalnız tespit,
        # skor/verdict'e sıfır etkisi var). Aynı ind["ohlcv"] snapshot'ı
        # kullanılır -- YENİ NETWORK ÇAĞRISI YOK. ctx["indicators"] ve
        # yukarıdaki hiçbir alan bundan etkilenmedi. Bu motor şu an ne
        # AI Analyst context'ine ne de report'a hiç taşınmıyor -- yalnız
        # ctx içinde kalıyor, RealFetcher.get_technical_structure() ile
        # (yalnız debug/kalibrasyon amaçlı) okunabilir.
        ctx["technical_structure"] = None
        if ind and ind.get("ohlcv"):
            _t_struct = time.time()
            try:
                ctx["technical_structure"] = TechnicalStructureEngine().analyze(ind["ohlcv"])
                # RECENT PRICE ACTION V1: TSE.analyze()'nin KENDİSİ hiç
                # değişmeden, dönen dict'e AYRICA (bu çağrı sitesinde,
                # class'ın dışında) additive bir alan eklenir -- aynı
                # ohlcv snapshot'ından, yeni network çağrısı olmadan.
                if ctx["technical_structure"].get("status") == "ok":
                    ctx["technical_structure"]["recent_price_action"] = (
                        _compute_recent_price_action(ind["ohlcv"]))
            except Exception as e:
                print(f"[TECH STRUCTURE] hesaplama hatası: {e}", flush=True)
                ctx["technical_structure"] = {"status": "error", "reason": str(e)}
            print(f"[TIMING] TechnicalStructureEngine: {(time.time() - _t_struct) * 1000:.1f}ms", flush=True)

        funding_detail = binance_results.get("funding_detail")
        funding = funding_detail["rate_pct"] if funding_detail else None
        ctx["funding_pct"] = funding
        ctx["funding_time_ms"] = funding_detail["funding_time_ms"] if funding_detail else None
        ctx["funding_interval_hours"] = funding_detail["interval_hours"] if funding_detail else None
        ctx["funding_age_hours"] = funding_detail["age_hours"] if funding_detail else None
        self._source_status["Binance funding rate"] = "ok" if funding is not None else \
            "yok (spot-only, periyot belirlenemedi veya kayıt bayat)"

        oi = binance_results.get("open_interest_trend")
        ctx["oi_trend"] = oi
        self._source_status["Binance Open Interest"] = "ok" if oi else \
            "yok (tek nokta, futures yok, veya zaman bütünlüğü/freshness doğrulanamadı)"

        # BTC günlük trendi artık HER ZAMAN ayrı 1D hesaptan gelir — sembol
        # BTC olsa bile coin'in kendi 1h göstergeleri (ind) bu soru için
        # kullanılmaz (1h ile 1d farklı kavramlar, karıştırılmaz).
        # MODEL D: get_btc_trend() yerine get_btc_daily_regime_and_r7()
        # çağrılıyor -- AYNI formül/AYNI kapanmış-mum filtresiyle bullish
        # flag'i (ctx["btc_above_ema50"] DEĞERİ ve semantiği DEĞİŞMEDİ),
        # TEK fetch'ten ayrıca r7 (trailing 7 günlük getiri) de çıkarılır.
        # İkinci bir network çağrısı YOK.
        btc_bullish, btc_r7_pct = binance_results.get("btc_regime_and_r7") or (None, None)
        ctx["btc_above_ema50"] = btc_bullish
        ctx["btc_daily_bearish"] = (not btc_bullish) if btc_bullish is not None else None
        ctx["btc_7d_return_pct"] = round(btc_r7_pct, 4) if btc_r7_pct is not None else None
        self._source_status["BTC trend"] = "ok" if btc_bullish is not None else "hata"
        print(f"[TIMING] === Binance TOPLAM: {time.time() - _t_binance:.1f}s ===", flush=True)
        self._check_cancel()

        self._progress("CoinGecko kontrol ediliyor (kimlik doğrulanıyor)...")
        _t_coingecko = time.time()
        _cg_req_before = _CG_DEBUG_REQUEST_COUNT[0]
        coin_id = self._timed("CoinGecko resolve_coin_id", self.coingecko.resolve_coin_id, symbol)
        ctx["coingecko_id"] = coin_id
        # ÖNEMLİ: kimlik doğrulanamazsa coin_name SEMBOLE düşürülmez — None kalır.
        # fetch_relevant_news()'teki name_ok kontrolü (coin_name_lower boşsa
        # otomatik False) bu sayede devre dışı kalır; kısa/generic ticker'larda
        # (ör. "MEME") yalnızca zaten var olan kelime-sınırlı symbol_ok kontrolü
        # geçerli olur, zayıf substring eşleşmesi devreye girmez.
        coin_name = None
        if coin_id:
            info = self._timed("CoinGecko get_coin_info", self.coingecko.get_coin_info, coin_id)
            if info and info.get("name"):
                coin_name = info["name"]
            ctx["coin_info"] = info
            excount_result = self._timed("CoinGecko unique_major_exchanges",
                                          self.coingecko.get_unique_major_exchanges, coin_id)
            ctx["exchange_count"] = excount_result["count"] if excount_result else None
            ctx["exchange_count_complete"] = excount_result["complete"] if excount_result else None
            self._source_status["CoinGecko"] = "ok"
        else:
            ctx["exchange_count"] = None
            ctx["exchange_count_complete"] = None
            self._source_status["CoinGecko"] = "coin kimliği güvenilir biçimde doğrulanamadı"
        ctx["coin_name"] = coin_name
        _cg_req_count = _CG_DEBUG_REQUEST_COUNT[0] - _cg_req_before
        print(f"[TIMING] === CoinGecko TOPLAM ({symbol}): {time.time() - _t_coingecko:.1f}s, "
              f"{_cg_req_count} HTTP istek ===", flush=True)
        self._check_cancel()

        self._progress("CoinMarketCap kontrol ediliyor...")
        _t_cmc = time.time()
        cmc_quotes = self._timed("CMC get_quotes", self.cmc.get_quotes, symbol)
        ctx["cmc_quotes"] = cmc_quotes
        dom_trend = self._timed("CMC btc_dominance_trend", self.cmc.get_btc_dominance_trend)
        ctx["btc_dominance_trend"] = dom_trend
        self._source_status["CoinMarketCap"] = "ok" if (cmc_quotes or dom_trend) else \
            ("key yok" if not CMC_API_KEY else "plan kısıtlı/hata")
        print(f"[TIMING] === CMC TOPLAM: {time.time() - _t_cmc:.1f}s ===", flush=True)

        fg = self._timed("Fear & Greed (alternative.me/CMC)", fetch_fear_greed)
        ctx["fear_greed"] = fg
        self._source_status["Fear & Greed"] = "ok" if fg is not None else "hata"
        self._check_cancel()

        self._progress("Haber riski analiz ediliyor...")
        _t_news = time.time()
        news_items, news_meta = self._timed("RSS haber kaynakları (fetch_relevant_news)",
                                             self.news.fetch_relevant_news, symbol, coin_name)
        # RSS bitti, News Claude (en kötü durumda 60s) henüz başlamadı --
        # burada iptal edilmişse en pahalı tek çağrıdan (Anthropic) tamamen
        # kaçınılır.
        self._check_cancel()
        news_result = self._timed("NewsRiskFetcher Claude classification (classify_risk)",
                                   self.news.classify_risk, symbol, coin_name, news_items, news_meta)
        ctx["news"] = news_result
        ctx["news_items"] = news_items
        self._source_status["Haber Riski"] = (
            f"ok ({news_meta['sources_ok']}/{news_meta['sources_total']} kaynak)"
            if news_result["status"] != "nodata" else
            f"sınıflandırılamadı ({news_meta.get('sources_ok', 0)}/{news_meta.get('sources_total', 0)} kaynak erişildi)"
        )
        print(f"[TIMING] === Haber Riski (RSS+Claude) TOPLAM: {time.time() - _t_news:.1f}s ===", flush=True)
        self._check_cancel()

        ctx["tv_onchain"] = None
        if symbol == "BTC":
            self._progress("BTC MVRV/SOPR (TradingView) kontrol ediliyor...")
            _t_tv = time.time()
            try:
                tv_series = TradingViewOnChainFetcher.get_btc_series()
            except Exception:
                tv_series = None
            finally:
                print(f"[TIMING] TradingView on-chain (tvDatafeed get_btc_series): "
                      f"{time.time() - _t_tv:.1f}s", flush=True)
            ctx["tv_onchain"] = tv_series
            self._source_status["TradingView (BTC MVRV/SOPR)"] = "ok" if tv_series else \
                ("kurulu değil" if TvDatafeed is None else "hata/erişilemedi")

        print(f"[TIMING] >>> RealFetcher._build_context TOPLAM ({symbol}): "
              f"{time.time() - _t_total:.1f}s <<<", flush=True)
        self._cache[symbol] = ctx
        return ctx

    def get_status_report(self) -> Dict[str, str]:
        return dict(self._source_status)

    def get_news_detail(self, symbol: str) -> dict:
        ctx = self._cache.get(symbol.upper(), {})
        return {"items": ctx.get("news_items", []), "result": ctx.get("news", {})}

    def get_technical_structure(self, symbol: str) -> Optional[dict]:
        """PHASE 1 debug/kalibrasyon erişimi -- AI Analyst veya report'un BİR
        PARÇASI DEĞİL, yalnız test/inceleme amaçlı. bkz. TechnicalStructureEngine."""
        ctx = self._cache.get(symbol.upper(), {})
        return ctx.get("technical_structure")

    def get_btc_regime_r7(self, symbol: str) -> "tuple":
        """MODEL D erişimi -- _build_context içinde ZATEN hesaplanmış
        (yeni network çağrısı YOK) btc_daily_bearish/btc_7d_return_pct
        çiftini döner. Return: (btc_daily_bearish: Optional[bool],
        btc_7d_return_pct: Optional[float])."""
        ctx = self._cache.get(symbol.upper(), {})
        return ctx.get("btc_daily_bearish"), ctx.get("btc_7d_return_pct")

    def fetch(self, symbol: str, metric: str) -> DataPoint:
        symbol = symbol.upper()
        ctx = self._build_context(symbol)

        def ok(value, status, source, reason=""):
            return DataPoint(value=value, status=status, source=source, available=True, reason=reason)

        def nodata(source="", reason="Veri alınamadı"):
            return DataPoint(value=None, status="nodata", source=source, available=False, reason=reason)

        ind = ctx.get("indicators") or {}

        if metric == "volume_24h":
            vol = ctx.get("volume_24h_usd")
            avg = ctx.get("volume_7d_avg_usd")
            if vol is None or not avg:
                return nodata("Binance", "24s hacim veya 7g ortalama alınamadı")
            ratio = vol / avg if avg else 0
            status = "yes" if ratio >= 1.5 else "wait" if ratio >= 1.0 else "no"
            return ok(round(vol / 1_000_000, 2), status, "Binance 24h + 7g ortalama")

        if metric == "volume_7d_avg":
            avg = ctx.get("volume_7d_avg_usd")
            if not avg:
                return nodata("Binance", "7g ortalama hacim alınamadı")
            return ok(round(avg / 1_000_000, 2), "yes", "Binance (1d klines x7)")

        if metric == "exchange_count":
            cnt = ctx.get("exchange_count")
            if cnt is None:
                return nodata("CoinGecko", "Borsa listesi alınamadı (sayfalama tamamlanamadı ve eşik kesinleşmedi)")
            status = "yes" if cnt >= 2 else "wait" if cnt >= 1 else "no"
            complete = ctx.get("exchange_count_complete")
            if complete:
                value_display = cnt
                src = "CoinGecko (benzersiz büyük borsa sayısı)"
            else:
                # Sayfalama tamamlanamadı — count kesin toplam değil, yalnızca
                # doğrulanmış bir ALT SINIR. Karar (status) buna rağmen
                # güvenilir (>=2 asla azalmaz), ama kullanıcıya gösterilen
                # DEĞER çıplak sayı olarak sunulmamalı — DataPoint.value zaten
                # bu kod tabanında string'i de destekliyor (ör. volume_spread),
                # bu yüzden yeni bir alan/tip eklemeden "≥N" string'i taşınıyor.
                value_display = f"≥{cnt}"
                src = f"CoinGecko (en az {cnt} büyük borsa doğrulandı, sayfalama tamamlanamadı)"
            return ok(value_display, status, src)

        if metric == "spread_pct":
            book = ctx.get("order_book")
            if not book or book.get("spread_pct") is None:
                return nodata("Binance order book", "Spread hesaplanamadı")
            return ok(book["spread_pct"], "yes" if book["spread_pct"] <= 0.1 else
                       "wait" if book["spread_pct"] <= 0.3 else "no", "Binance order book")

        if metric == "slippage_pct":
            book = ctx.get("order_book")
            if not book or book.get("slippage_pct") is None:
                return nodata("Binance order book", "Slippage hesaplanamadı (yetersiz derinlik)")
            s = book["slippage_pct"]
            status = ThresholdEngine.eval_max(s, {"yes": 1.0, "wait": 2.0})
            return ok(s, status, "Binance order book (10K$ market alım simülasyonu)")

        if metric == "volatility_pct":
            vol = ind.get("volatility_pct")
            squeeze = ind.get("bb_squeeze")
            if vol is None:
                return nodata("Binance 1h klines", "Volatilite hesaplanamadı")
            status = "yes" if vol >= 3.0 else "wait" if (vol >= 1.5 or squeeze) else "no"
            return ok(vol, status, "Binance 1h klines (24s high-low aralığı)")

        if metric == "squeeze_active":
            squeeze = ind.get("bb_squeeze")
            if squeeze is None:
                return nodata("Binance Bollinger", "Squeeze hesaplanamadı")
            return ok(int(squeeze), "yes" if squeeze else "no", "Binance Bollinger(20,2)")

        if metric == "volatility_risk":
            # RISK COVERAGE V2 / VOLATILITY RISK DIMENSION (controlled implementation,
            # onaylı T1 contract: causal p25/p90). YENİ NETWORK ÇAĞRISI YOK -- aynı
            # ind["ohlcv"] snapshot'ı (get_technical_indicators'ın zaten çektiği ~250
            # kapanmış 1h bar) reuse edilir. LOOK-AHEAD GÜVENLİĞİ: current bar (son
            # bar) hiçbir zaman kendi referans percentile dağılımına dahil edilmez --
            # geçmiş gözlemler yalnız [0..n-2] barlarından, current değer ayrıca [n-1]
            # (mevcut volatility_pct, ind["volatility_pct"]) ile karşılaştırılır.
            vol_now = ind.get("volatility_pct")
            ohlcv = ind.get("ohlcv")
            if vol_now is None or not ohlcv:
                return nodata("Binance 1h klines", "Volatilite hesaplanamadı")
            closes, highs, lows = ohlcv.get("close"), ohlcv.get("high"), ohlcv.get("low")
            if closes is None or highs is None or lows is None or len(closes) < 2:
                return nodata("Binance 1h klines", "Yetersiz geçmiş veri")
            n = len(closes)
            # P0 PARITY FIX: her historical observation, current'in kendisiyle AYNI
            # primitive'i taşımalı -- yani TAM 24-bar pencere (approved contract:
            # "24-bar volatility observation", "minimum 20 valid historical 24-bar
            # volatility observation"). i<23 için pencere <24 bar olurdu (kısa/farklı
            # varyans karakteristiği taşıyan, current ile KIYASLANAMAZ bir primitive)
            # -- bu yüzden yalnız i>=23 (TAM 24-bar pencere üretilebilen endpoint'ler)
            # dahil edilir. i=n-1 (current bar) HARİÇ -- look-ahead yok.
            hist_obs = []
            for i in range(23, n - 1):
                seg_c, seg_h, seg_l = closes[i-23:i+1], highs[i-23:i+1], lows[i-23:i+1]
                hist_obs.append(float(np.mean((seg_h - seg_l) / seg_c)) * 100)
            if len(hist_obs) < 20:
                return nodata("Binance 1h klines",
                               f"Yetersiz geçmiş volatilite gözlemi ({len(hist_obs)}/20)")
            arr = np.array(hist_obs)
            p25, p90 = np.percentile(arr, [25, 90])
            status = "yes" if vol_now <= p25 else ("no" if vol_now >= p90 else "wait")
            return ok(round(vol_now, 3), status,
                       f"Binance 1h klines (causal percentile n={len(hist_obs)}, "
                       f"p25={p25:.2f}, p90={p90:.2f})")

        if metric == "price":
            price = ind.get("price")
            above = ind.get("price_above_ema50")
            if price is None or above is None:
                return nodata("Binance 1h klines", "Fiyat/EMA50 hesaplanamadı")
            return ok(price, "yes" if above else "no", "Binance 1h klines")

        if metric == "ema50":
            e = ind.get("ema50")
            if e is None:
                return nodata("Binance 1h klines", "EMA50 hesaplanamadı")
            return ok(e, "yes", "Binance 1h klines")

        if metric == "ema50_distance_pct":
            d = ind.get("ema50_distance_pct")
            if d is None:
                return nodata("Binance 1h klines", "EMA50 uzaklığı hesaplanamadı")
            status = ThresholdEngine.eval_max(d, {"yes": 3.0, "wait": 5.0})
            return ok(d, status, "Binance 1h klines")

        if metric == "price_change_24h_pct":
            chg = ind.get("price_change_24h_pct")
            if chg is None:
                chg = ctx.get("price_change_24h_pct_ticker")
            if chg is None:
                return nodata("Binance", "24s değişim hesaplanamadı")
            # ENTRY TIMING V2 / P1 FIX: status artık abs(chg) ile hesaplanıyor --
            # değer (chg) yine SIGNED olarak gösterilir/saklanır, yalnız yes/wait/no
            # sınıflandırması yön-bağımsız büyüklüğe göre yapılır (bkz. ENTRY TIMING
            # CORE FIX DESIGN AUDIT: "sert değil" ifadesi zaten yön-bağımsız bir
            # büyüklük kavramı -- büyük bir DÜŞÜŞ de büyük bir YÜKSELİŞ kadar "sert").
            status = ThresholdEngine.eval_max(abs(chg), {"yes": 5.0, "wait": 10.0})
            return ok(chg, status, "Binance 24h ticker / 1h klines")

        if metric == "rsi":
            r = ind.get("rsi")
            if r is None:
                return nodata("Binance 1h klines", "RSI hesaplanamadı")
            status = "yes" if 30 <= r <= 50 else "wait" if (20 <= r < 30 or 50 < r <= 70) else "no"
            return ok(r, status, "Binance 1h klines (RSI-14)")

        if metric == "macd_bullish":
            v = ind.get("macd_bullish")
            if v is None:
                return nodata("Binance 1h klines", "MACD hesaplanamadı")
            return ok(int(v), "yes" if v else "no", "Binance 1h klines (MACD 12,26,9)")

        if metric == "obv_rising":
            v = ind.get("obv_rising")
            if v is None:
                return nodata("Binance 1h klines", "OBV hesaplanamadı")
            return ok(int(v), "yes" if v else "no", "Binance 1h klines (OBV)")

        if metric == "bb_squeeze_or_break":
            squeeze = ind.get("bb_squeeze")
            brk = ind.get("bb_upper_break")
            if squeeze is None and brk is None:
                return nodata("Binance 1h klines", "Bollinger hesaplanamadı")
            v = bool(squeeze or brk)
            return ok(int(v), "yes" if v else "no", "Binance 1h klines (Bollinger 20,2)")

        if metric == "btc_above_ema50":
            v = ctx.get("btc_above_ema50")
            if v is None:
                return nodata("Binance BTC 1d klines", "BTC günlük trendi hesaplanamadı")
            return ok(int(v), "yes" if v else "no", "Binance BTC 1d klines (EMA50, kapanmış günlük mumlar)")

        if metric == "funding_pct":
            f = ctx.get("funding_pct")
            if f is None:
                return nodata("Binance Futures", "Funding rate alınamadı (futures'ta olmayabilir)")
            status = "yes" if -0.03 <= f <= 0.01 else "wait" if (-0.10 <= f < -0.03 or 0.01 < f <= 0.05) else "no"
            return ok(f, status, "Binance Futures (son gerçekleşen funding)")

        if metric == "oi_rising_aligned":
            oi = ctx.get("oi_trend")
            if not oi:
                return nodata("Binance Futures OI", "OI/fiyat zaman bütünlüğü veya freshness doğrulanamadı")
            t0 = datetime.fromtimestamp(oi["first_ts"] / 1000, tz=timezone.utc).strftime("%H:%M")
            t1 = datetime.fromtimestamp(oi["last_ts"] / 1000, tz=timezone.utc).strftime("%H:%M")
            src = (f"Binance Futures openInterestHist 5m (OI {oi['oi_change_pct']:+.2f}%, "
                   f"fiyat {oi['price_change_pct']:+.2f}%, {t0}-{t1} UTC)")
            return ok(oi["oi_change_pct"], oi["status"], src)

        if metric == "fear_greed":
            fg = ctx.get("fear_greed")
            if fg is None:
                return nodata("Alternative.me / CMC", "Fear & Greed alınamadı")
            status = "yes" if 20 <= fg <= 60 else "wait" if (10 <= fg < 20 or 60 < fg <= 75) else "no"
            return ok(fg, status, "Alternative.me Fear & Greed Index")

        if metric == "total3_rising_or_btc_dom_falling":
            dom = ctx.get("btc_dominance_trend")
            if not dom:
                return nodata("CoinMarketCap", "BTC dominans/TOTAL3 verisi alınamadı (plan kısıtlı olabilir)")

            dominance_falling = dom.get("dominance_falling")
            total3_rising = dom.get("total3_rising")

            # OR semantiği, kısa-devre + bilinmeyen≠False:
            # bir kol kesin True ise diğer kol eksik olsa bile sonuç YES'tir.
            if dominance_falling is True or total3_rising is True:
                status = "yes"
            elif dominance_falling is False and total3_rising is False:
                status = "no"
            else:
                status = "nodata"

            total3_txt = f"TOTAL3 {dom['total3_change_pct']:+.2f}%" \
                if dom.get("total3_change_pct") is not None else "TOTAL3 eksik"
            dom_txt = f"BTC dom {dom['btc_dominance_24h_change']:+.2f}%" \
                if dom.get("btc_dominance_24h_change") is not None else "BTC dom eksik"
            src = f"CMC: {total3_txt}, {dom_txt}"

            if status == "nodata":
                return nodata(src, "OR'un hiçbir kolu kesin doğrulanamadı (bir kol eksik, diğeri False)")
            return ok(int(status == "yes"), status, src)

        if metric == "recent_bad_event":
            news = ctx.get("news", {})
            status = news.get("status", "nodata")
            src = " + ".join(news.get("sources", [])) or "Anthropic sınıflandırma"
            return DataPoint(value=status, status=status, source=src,
                              available=status != "nodata", reason=news.get("reason", ""))

        if metric == "mvrv":
            # Yalnızca BTC — altcoin'e BTC'nin on-chain verisi ASLA yazılmaz.
            if symbol != "BTC":
                return nodata("", "MVRV yalnızca BTC için TradingView üzerinden hesaplanıyor")
            tv = ctx.get("tv_onchain")
            if not tv:
                return nodata("TradingView (gayrı resmi)",
                               "BTC MVRV serisi alınamadı (paket kurulu değil veya erişim hatası)")
            res = TradingViewOnChainFetcher.compute_mvrv(tv)
            if not res:
                return nodata("TradingView (gayrı resmi)",
                               "Market Cap / Realized Cap serilerinde ortak tamamlanmış tarih bulunamadı")
            v = res["mvrv"]
            status = ThresholdEngine.eval_max(v, {"yes": 2.0, "wait": 3.5})
            return ok(round(v, 3), status,
                       f"TradingView GLASSNODE/COINMETRICS (gayrı resmi, {res['date']})")

        if metric == "sopr_recovery":
            if symbol != "BTC":
                return nodata("", "SOPR yalnızca BTC için TradingView üzerinden hesaplanıyor")
            tv = ctx.get("tv_onchain")
            if not tv:
                return nodata("TradingView (gayrı resmi)",
                               "BTC SOPR serisi alınamadı (paket kurulu değil veya erişim hatası)")
            res = TradingViewOnChainFetcher.compute_sopr_recovery(tv)
            if not res:
                return nodata("TradingView (gayrı resmi)",
                               "Yeterli sayıda tamamlanmış günlük SOPR barı yok")
            status = "yes" if res["recovered"] else "no"
            return ok(round(res["latest_value"], 4), status,
                       f"TradingView GLASSNODE (gayrı resmi, {res['previous_date']}→{res['latest_date']})")

        if metric == "active_addresses_rising":
            # CoinMetrics Community API (ücretsiz, key gerekmez) — ~138 varlık
            # kapsıyor. Kapsamda olmayan sembolde (ör. SOL) dürüstçe nodata.
            trend = self._timed("CoinMetrics active_address_trend",
                                 self.coinmetrics.get_active_address_trend, symbol)
            if not trend:
                return nodata("CoinMetrics Community API",
                               "Bu varlık için aktif adres verisi mevcut değil veya alınamadı")
            return ok(int(trend["rising"]), "yes" if trend["rising"] else "no",
                       f"CoinMetrics AdrActCnt ({trend['previous_date']}→{trend['latest_date']}, "
                       f"{trend['change_pct']:+.2f}%)")

        if metric == "exchange_outflow":
            # CoinMetrics Community API — yalnızca btc/eth için borsa rezervi
            # (SplyExNtv) mevcut; doğrulandı, community tier'da başka varlık yok.
            trend = self._timed("CoinMetrics exchange_reserve_trend",
                                 self.coinmetrics.get_exchange_reserve_trend, symbol)
            if not trend:
                return nodata("CoinMetrics Community API",
                               "Bu varlık için borsa rezervi verisi mevcut değil (yalnızca BTC/ETH'de var) veya alınamadı")
            return ok(int(trend["net_outflow"]), "yes" if trend["net_outflow"] else "no",
                       f"CoinMetrics SplyExNtv ({trend['previous_date']}→{trend['latest_date']}, "
                       f"{trend['change_pct']:+.2f}%)")

        # Otomatik üretilemeyen / dürüstçe Veri Yok bırakılan metrikler.
        # whale_accumulation/stablecoin_inflow de burada: araştırıldı — CryptoQuant/
        # Glassnode/Nansen bu metrikleri ücretsiz sunmuyor, CoinMetrics Community
        # tier'da da whale/holder-cohort metriği ve stablecoin borsa-akış verisi hiç
        # yok (doğrulandı — stablecoin sorgusu API'den açıkça "desteklenmiyor" dönüyor).
        # Kullanıcı bu metriği tik ettiyse (kapsama dahil ettiyse) sistem yine de bu
        # koda ulaşır (gerçek bir arama denemesi olmuş olur) ama kaynak yok — asla
        # "tik edildi" diye manuel yes/no varsayılmaz.
        if metric in ("clear_support", "rr_ratio", "unlock_pct",
                      "dxy_loosening", "etf_inflow",
                      "whale_accumulation", "stablecoin_inflow"):
            return nodata("", "Bu metrik güvenilir ücretsiz API ile otomatik hesaplanamaz")

        return nodata("", "Desteklenmeyen metrik")


# ═══════════════════════════════════════════════════════════════════
# 3. EŞİK MOTORU (Level 2)
# ═══════════════════════════════════════════════════════════════════

class ThresholdEngine:
    @staticmethod
    def eval_max(value: float, th: dict) -> str:
        """'max' tipi (küçük değer istenen metrikler) için TEK, ortak sınır
        mantığı — RealFetcher, MockFetcher ve Level 2/3 (evaluate() üzerinden)
        buradan geçer, üç ayrı yerde tekrar hardcode edilmez.

        STAGES_CONFIG'teki 5 "max" sorusunun yazılı açıklamaları taranarak
        doğrulandı: 'wait' üst sınırı (no'ya geçiş) TÜMÜNDE dahil edici (<=) —
        yalnızca 'yes' alt sınırı değişiyor: 4/5 soru dışlayıcı (<), yalnızca
        unlock_pct açıkça '≤%3 güvenli' dediği için dahil edici (<=). Bu tek
        fark `yes_inclusive` bayrağıyla ifade ediliyor; yeni bir sayısal eşik
        icat edilmedi, yalnızca mevcut yazılı sınır anlamı kodda görünür
        kılındı."""
        yes_th = th.get("yes", -999)
        wait_th = th.get("wait", 999)
        yes_ok = (value <= yes_th) if th.get("yes_inclusive", False) else (value < yes_th)
        if yes_ok:
            return "yes"
        if value <= wait_th:
            return "wait"
        return "no"

    @staticmethod
    def evaluate(item: QuestionItem, inputs: List[float]) -> str:
        th = item.thresholds
        ttype = th.get("type", "bool")

        if ttype == "bool":
            return "yes" if inputs and inputs[0] >= 1 else "no"

        if ttype == "bool_or_min":
            vol = inputs[0] if len(inputs) > 0 else 0
            squeeze = inputs[1] if len(inputs) > 1 else 0
            if vol >= th.get("yes_vol", 999):
                return "yes"
            if vol >= th.get("wait_vol", 0) or squeeze >= 1:
                return "wait"
            return "no"

        if ttype == "volume_spread":
            # Birleşik hacim + spread kontrolü
            vol = inputs[0] if len(inputs) > 0 else 0
            spread = inputs[1] if len(inputs) > 1 else 999
            if vol >= th.get("yes_vol", 999) and spread <= th.get("yes_spread", 0):
                return "yes"
            if vol >= th.get("wait_vol", 0) and spread <= th.get("wait_spread", 999):
                return "wait"
            return "no"

        if ttype == "ratio":
            if len(inputs) < 2 or inputs[1] == 0:
                return "nodata"
            ratio = inputs[0] / inputs[1]
            if ratio >= th.get("yes", 999): return "yes"
            if ratio >= th.get("wait", 0): return "wait"
            return "no"

        if ttype == "min":
            if inputs[0] >= th.get("yes", 999): return "yes"
            if inputs[0] >= th.get("wait", 0): return "wait"
            return "no"

        if ttype == "max":
            return ThresholdEngine.eval_max(inputs[0], th)

        if ttype == "range":
            val = inputs[0]
            if th.get("yes_min", -999) <= val <= th.get("yes_max", 999): return "yes"
            if th.get("wait_min", -999) <= val <= th.get("wait_max", 999): return "wait"
            return "no"

        if ttype == "funding":
            val = inputs[0]
            if th.get("yes_min", -999) <= val <= th.get("yes_max", 999): return "yes"
            if th.get("wait_min", -999) <= val <= th.get("wait_max", 999): return "wait"
            return "no"

        if ttype == "count":
            val = inputs[0]
            if val >= th.get("yes", 999): return "yes"
            if val >= th.get("wait", 0): return "wait"
            return "no"

        return "nodata"


# ═══════════════════════════════════════════════════════════════════
# 4. VETO MOTORU
# ═══════════════════════════════════════════════════════════════════

class VetoEngine:
    def __init__(self, rules):
        self.rules = rules

    def check(self, answers: List[Dict], symbol: str = "") -> List[str]:
        vetos = []
        for ans in answers:
            answer = ans.get("answer", "")
            for rule in self.rules:
                v_stage, v_label, v_reason, v_cond, trigger_answers = rule
                if ans.get("stage") == v_stage and ans.get("question") == v_label:
                    if answer in trigger_answers and v_cond(symbol):
                        vetos.append(v_reason)
        return vetos


# ═══════════════════════════════════════════════════════════════════
# 5. SKOR MOTORU
# ═══════════════════════════════════════════════════════════════════

class ScoreEngine:
    def __init__(self, signal_weights: Dict[str, int], risk_weights: Dict[str, int],
                 veto_engine: VetoEngine):
        self.signal_weights = signal_weights
        self.risk_weights = risk_weights
        self.veto_engine = veto_engine

    def calculate_stage_score(self, stage_answers, stage_items):
        w_yes = 0.0
        w_wait = 0.0
        w_total = 0.0
        answered_count = 0

        for ans, item in zip(stage_answers, stage_items):
            if ans == "nodata":
                continue
            w = item.weight
            w_total += w
            answered_count += 1
            if ans == "yes":
                w_yes += w
            elif ans == "wait":
                w_wait += w * 0.5

        if w_total == 0:
            # ZERO-WEIGHT ANSWER ACCOUNTING FIX: answered_count zaten yukarıda
            # doğru sayıldı (weight=0 item'lar da "answered" -- ör. volatility_risk).
            # Eskiden burada 0'a sıfırlanıyordu, GUI Aşama Dökümü ve
            # normalized/effective_weights ile Risk Coverage V2'nin (risk_breadth,
            # answers listesinden ayrı hesaplanan) arasında çelişki yaratıyordu.
            # Skor (0.0) ve w_total (0) DEĞİŞMEDİ -- yalnız answered_count korunuyor.
            return 0.0, answered_count, 0
        score = ((w_yes + w_wait) / w_total) * 100
        return score, answered_count, int(w_total)

    def _calculate_weighted(self, stage_scores, stage_answered, stage_items_count, weights):
        raw_weights = {}
        for title in stage_scores:
            total_q = stage_items_count.get(title, 1)
            answered = stage_answered.get(title, 0)
            coverage = answered / total_q if total_q else 0
            effective = weights.get(title, 0) * coverage
            if answered > 0:
                raw_weights[title] = effective

        weight_sum = sum(raw_weights.values())
        if weight_sum == 0:
            return 0.0, {}, {}

        normalized = {k: (v / weight_sum) * 100 for k, v in raw_weights.items()}

        total = 0.0
        for title in normalized:
            total += stage_scores[title] * normalized[title]

        return total / 100, normalized, raw_weights

    def confidence_level(self, coverage_pct: float):
        if coverage_pct >= 85:
            return "Yüksek", "Güvenilir veri seti."
        elif coverage_pct >= 65:
            return "Orta", "Bazı eksikler var ama karar verilebilir."
        elif coverage_pct >= 40:
            return "Düşük", "Önemli veri eksikleri var, dikkatli olun."
        else:
            return "Yetersiz", "Veri çok eksik, karar vermek riskli."

    def verdict(self, signal_score: float, risk_score: float,
                has_veto: bool, confidence: str, risk_coverage: float,
                risk_available: bool, risk_reliable: bool):
        # Veto en üstte — veri kapsamından bağımsız
        if has_veto:
            return (
                "Elenir",
                "Red",
                "Kritik veto koşulu mevcut. Veri kapsamından bağımsız olarak işlem değerlendirilmez."
            )

        # Risk verisi hiç yoksa — sinyal güçlü olsa bile karar verilemez
        if not risk_available:
            if signal_score >= 80:
                return (
                    "Güçlü sinyal — risk verisi eksik",
                    "İşlem uygunluğu değerlendirilemedi",
                    "Sinyal güçlü ancak risk aşamasında kullanılabilir veri yok. Risk verileri tamamlanmalı."
                )
            elif signal_score >= 65:
                return (
                    "İzleme listesi — risk verisi eksik",
                    "Risk verileri tamamlanmalı",
                    "Sinyal olumlu ancak işlem uygunluğu hesaplanamadı. Risk verileri tamamlanmalı."
                )
            else:
                return (
                    "Sinyal zayıf — risk verisi eksik",
                    "Pas",
                    "Sinyal yetersiz ve risk verileri mevcut değil."
                )

        # RISK COVERAGE V2 (R3): risk kapsamı YÜZDESİ yerine risk_reliable
        # (bağımsız measurable risk boyutu sayısı >= 2) kullanılıyor -- tek
        # measurable boyutla (breadth=1) %100 risk_coverage görünse bile
        # "tek cevapla yanıltıcı güven" korumasının denominator-independent
        # karşılığı budur (bkz. MINIMUM BREADTH POLICY APPROVAL). risk_coverage
        # yalnız aşağıdaki mesaj metinlerinde bilgi amaçlı gösterilmeye devam eder.
        if not risk_reliable:
            # RISK COVERAGE V2 SEMANTIC CLOSURE: bu branch'e yalnız risk_available=True
            # (breadth>=1) VE risk_reliable=False (breadth<2) iken girilir -- yani
            # risk verisi VAR ama tek bağımsız boyutla sınırlı. risk_coverage yüzdesi
            # (breadth=1 iken tipik olarak %100) artık bu durumun NEDENİ değil --
            # metin bunu "kapsam düşük" gibi göstermez (eski, denominator-bağımlı ifade
            # anlamsızlaşırdı: "%100 ama yetersiz" çelişkisi).
            if signal_score >= 80:
                return (
                    "Güçlü sinyal — risk verisi yetersiz",
                    "İşlem uygunluğu değerlendirilemedi",
                    "Sinyal güçlü ancak risk değerlendirmesi tek bağımsız boyutla sınırlı — çok boyutlu doğrulama için yeterli değil. Risk verileri tamamlanmalı."
                )
            elif signal_score >= 65:
                return (
                    "İzleme listesi — risk verisi yetersiz",
                    "Risk verileri tamamlanmalı",
                    "Sinyal olumlu ancak risk değerlendirmesi tek bağımsız boyutla sınırlı — işlem uygunluğu hesaplanamadı. Risk verileri tamamlanmalı."
                )
            else:
                return (
                    "Sinyal zayıf — risk verisi yetersiz",
                    "Pas",
                    "Sinyal yetersiz ve risk değerlendirmesi tek bağımsız boyutla sınırlı."
                )

        if confidence == "Yetersiz":
            return "Analiz yapılamaz", "Veri eksik", "Veri kapsamı %40'ın altında. Daha fazla veri toplayın."

        if confidence == "Düşük":
            if signal_score >= 65:
                return "Ön aday", "Düşük güven", f"Sinyal yüksek ({signal_score:.1f}%) ancak veri kapsamı düşük."
            elif signal_score >= 50:
                return "Ön aday", "Düşük güven", "Sınırlı veriyle orta skor. Tam analiz önerilir."
            else:
                return "Yetersiz veri", "Pas", "Veri hem eksik hem olumsuz."

        # Orta güven: tam "Güçlü aday" değil, ön aday
        if confidence == "Orta":
            if signal_score >= 80 and risk_score >= 65:
                return (
                    "Güçlü ön aday",
                    "Eksik veriler tamamlanmalı",
                    "Skor güçlü ancak veri kapsamı tam karar için yeterli değil."
                )
            elif signal_score >= 65 and risk_score >= 50:
                return "İzleme listesi", "Onay bekliyor", "Olumlu göstergeler çoğunlukta. Veri kapsamı tam değil."
            elif signal_score >= 50:
                return "Orta-zayıf sinyal", "Bekle-izle", "Bazı olumlu veriler var ancak negatifler yüksek."
            else:
                return "Zayıf sinyal", "Pas", "Çok sayıda kırmızı bayrak. Henüz zamanı değil."

        # Yüksek güven: tam karar motoru (risk_reliable garanti)
        if signal_score >= 80 and risk_score >= 65:
            return "Güçlü aday", "Tetikleyici beklenebilir", "Konfluans yüksek. Risk/ödül makul, makro zemin uygun."
        elif signal_score >= 65 and risk_score < 50:
            return (
                "Sinyal var, işlem uygun değil",
                "Pas",
                f"Sinyal güçlü ({signal_score:.1f}%) ama risk/ödül veya işlem koşulları uygun değil."
            )
        elif signal_score >= 65 and risk_score >= 50:
            return "İzleme listesi", "Onay bekliyor", "Olumlu göstergeler çoğunlukta ama eksikler var."
        elif signal_score >= 50 and risk_score >= 50:
            return "Orta-zayıf sinyal", "Bekle-izle", "Bazı olumlu veriler var ancak negatifler yüksek."
        elif signal_score >= 35:
            return "Zayıf sinyal", "Pas", "Çok sayıda kırmızı bayrak. Henüz zamanı değil."
        else:
            return "Elenir", "Red", "Göstergeler olumsuz."

    def top_factors(self, answers, top_n=5):
        pros = []
        cons = []
        for ans in answers:
            if ans.get("answer") == "nodata":
                continue
            item = ans.get("_item")
            if not item:
                continue
            if ans["answer"] == "yes" and item.pro:
                pros.append((item.pro, item.weight))
            elif ans["answer"] == "no" and item.con:
                cons.append((item.con, item.weight))
        pros.sort(key=lambda x: x[1], reverse=True)
        cons.sort(key=lambda x: x[1], reverse=True)
        return pros[:top_n], cons[:top_n]

    def entry_timing(self, answers) -> Tuple[Optional[float], str, str, int, int]:
        """
        Ağırlıklı Giriş Uygunluğu Skoru.
        Dönüş: (skor_0_100 veya None, etiket, açıklama, answered_count, total_factors)

        ENTRY TIMING V2 / R2 FIX (RESMİ 3-FAKTÖR CONTRACT): "R:R ve direnç
        uygunluğu" (risk_reward_ratio) Level 1'de STRUCTURALLY UNAVAILABLE
        (STRUCTURALLY_UNAVAILABLE_FACTOR_IDS) — gerçek production'da bu
        soru HER ZAMAN "nodata" dönüyordu, yani skor zaten fiilen 3
        faktörle (available_weight tabanlı renormalizasyon) hesaplanıyordu.
        Eski "4 faktörlü, biri sürekli eksik" contract'ı yanıltıcıydı (ör.
        GUI'de kalıcı "Eksik veri · 3/4" amber uyarısı). Bu fix skor
        HESAPLAMASINI DEĞİŞTİRMEZ (aynı renormalizasyon mekanizması, aynı
        ağırlık değerleri) — yalnız resmi contract'ı gerçekte ölçülen 3
        faktöre indirir (bkz. ENTRY TIMING CORE FIX DESIGN AUDIT, RR-A
        kararı). R:R için yeni veri/proxy İCAT EDİLMEDİ.

        Ağırlıklar (RESMİ 3 FAKTÖR):
          EMA50 uzaklığı: 35%
          Son 24s fiyat değişimi (magnitude, bkz. P1 fix): 30%
          RSI: 20%
        """
        weights = {
            "ema50_dist": 0.35,
            "chg24": 0.30,
            "rsi": 0.20,
        }
        subscores = {}
        details = []
        answered_count = 0

        # 1) EMA50 uzaklığı
        ema_dist = next((a for a in answers if "EMA50'den makul uzaklık" in a.get("question", "")), None)
        if ema_dist and ema_dist["answer"] != "nodata":
            answered_count += 1
            if ema_dist["answer"] == "yes":
                subscores["ema50_dist"] = 100
                details.append("EMA50'e yakın (<%3)")
            elif ema_dist["answer"] == "wait":
                subscores["ema50_dist"] = 50
                details.append("EMA50'den biraz uzak (%3-5)")
            else:
                subscores["ema50_dist"] = 0
                details.append("EMA50'den çok uzak (>%5)")

        # 2) Son 24s fiyat artışı
        chg_24 = next((a for a in answers if "Son 24 saat fiyat değişimi" in a.get("question", "")), None)
        if chg_24 and chg_24["answer"] != "nodata":
            answered_count += 1
            if chg_24["answer"] == "yes":
                subscores["chg24"] = 100
                details.append("24s değişim makul (<%5)")
            elif chg_24["answer"] == "wait":
                subscores["chg24"] = 50
                details.append("24s değişim orta (%5-10)")
            else:
                subscores["chg24"] = 0
                details.append("24s sert yükseliş (>%10)")

        # 3) RSI
        rsi_ans = next((a for a in answers if "RSI" in a.get("question", "")), None)
        if rsi_ans and rsi_ans["answer"] != "nodata":
            answered_count += 1
            if rsi_ans["answer"] == "yes":
                subscores["rsi"] = 100
                details.append("RSI birikim bölgesi (30-50)")
            elif rsi_ans["answer"] == "wait":
                subscores["rsi"] = 40
                details.append("RSI nötr/dikkat (50-70)")
            else:
                subscores["rsi"] = 0
                details.append("RSI aşırı alım (>70) veya çok düşük (<20)")

        # NOT (R:R KALDIRILDI): Eskiden burada 4. faktör olarak "Yakın direnç
        # hedefi" (R:R) okunuyordu. R2 fix ile bu, resmi contract'tan
        # ÇIKARILDI (answers içinde bu soru hâlâ geçiyor olsa bile artık
        # HİÇ okunmuyor/skora katılmıyor -- bkz. yukarıdaki fonksiyon
        # docstring'i).

        # Kapsam kontrolü — RESMİ 3-FAKTÖR CONTRACT (R2 fix)
        total_factors = 3
        coverage = answered_count / total_factors

        if coverage < 0.5:  # 0/3 veri
            return (None, "Hesaplanamadı", f"Giriş uygunluğu için yeterli veri yok ({answered_count}/{total_factors}).",
                    answered_count, total_factors)

        # Ağırlıklı toplam — sadece var olan faktörleri normalize et
        available_weight = sum(weights[k] for k in subscores)
        if available_weight == 0:
            return None, "Hesaplanamadı", "Giriş uygunluğu için yeterli veri yok.", answered_count, total_factors

        total = sum(subscores[k] * weights[k] for k in subscores) / available_weight
        total = round(total, 1)

        # ENTRY TIMING V2 / R2 FIX: R:R artık resmi contract dışında olduğu
        # için bu, kalıcı ve HER zaman eklenen (coverage'dan bağımsız) tek
        # bir açıklama notu -- yeni bir warning/flag/policy MEKANİZMASI
        # DEĞİL, yalnız mevcut açıklama metnine (desc) sabit bir cümle.
        RR_EXCLUDED_NOTE = " · R:R/direnç mesafesi bu skora dahil değildir"
        if coverage < 0.75:  # 2/3 veri
            suffix = f" ({answered_count}/{total_factors} veri — eksik veri uyarısı)" + RR_EXCLUDED_NOTE
        else:
            suffix = f" ({answered_count}/{total_factors} veri)" + RR_EXCLUDED_NOTE

        if total >= 85:
            label = "Uygun"
            desc = f"Giriş bölgesi uygun. Skor: {total:.0f}" + suffix + " | " + " | ".join(details)
        elif total >= 60:
            label = "Retest Bekle"
            desc = f"Fiyat bir miktar uzamış, geri çekilme bekleyin. Skor: {total:.0f}" + suffix + " | " + " | ".join(details)
        elif total >= 40:
            label = "Erken Olabilir"
            desc = f"Henüz kırılım yok veya erken aşama. Skor: {total:.0f}" + suffix + " | " + " | ".join(details)
        else:
            label = "Geç Kalınmış"
            desc = f"Fiyat çok uzamış. Yeni giriş için bekleyin. Skor: {total:.0f}" + suffix + " | " + " | ".join(details)

        return total, label, desc, answered_count, total_factors

    def full_report(self, answers, stages_config, symbol=""):
        signal_stages = [s for s in stages_config if s.title in self.signal_weights]
        risk_stages = [s for s in stages_config if s.title in self.risk_weights]

        # Sinyal skoru hesapla
        signal_scores = {}
        signal_answered = {}
        signal_items_count = {}

        for stage in signal_stages:
            title = stage.title
            stage_answers = []
            stage_items = []
            in_scope = 0
            for item in stage.items:
                ans = next((a for a in answers if a["stage"] == title and a["question"] == item.label), None)
                if ans and ans.get("disabled"):
                    # Kapsam dışı (ör. on-chain bölümü kapalı) — ne payda ne skora girer.
                    continue
                in_scope += 1
                stage_answers.append(ans["answer"] if ans else "nodata")
                stage_items.append(item)
            signal_items_count[title] = in_scope
            score, count, _ = self.calculate_stage_score(stage_answers, stage_items)
            signal_scores[title] = score
            signal_answered[title] = count

        signal_weighted, signal_norm, signal_eff = self._calculate_weighted(
            signal_scores, signal_answered, signal_items_count, self.signal_weights)

        # Risk / İşlem uygunluğu skoru hesapla
        risk_scores = {}
        risk_answered = {}
        risk_items_count = {}

        for stage in risk_stages:
            title = stage.title
            stage_answers = []
            stage_items = []
            in_scope = 0
            for item in stage.items:
                ans = next((a for a in answers if a["stage"] == title and a["question"] == item.label), None)
                if ans and ans.get("disabled"):
                    continue
                in_scope += 1
                stage_answers.append(ans["answer"] if ans else "nodata")
                stage_items.append(item)
            risk_items_count[title] = in_scope
            score, count, _ = self.calculate_stage_score(stage_answers, stage_items)
            risk_scores[title] = score
            risk_answered[title] = count

        risk_weighted, risk_norm, risk_eff = self._calculate_weighted(
            risk_scores, risk_answered, risk_items_count, self.risk_weights)

        # Risk kapsamı ve kullanılabilirlik
        # RISK COVERAGE V2 (R3, onaylı): Level 1'de yapısal olarak hiç
        # ölçülemeyen risk faktörleri (STRUCTURALLY_UNAVAILABLE_FACTOR_IDS)
        # risk_coverage/risk_breadth paydasından çıkarılır. Bu yalnız risk
        # stage'ine özel, dar kapsamlı bir filtredir -- `disabled` alanına
        # DOKUNMAZ; global in_scope_answers/coverage/confidence tamamen
        # etkilenmeden kalır (bkz. GLOBAL COVERAGE / CONFIDENCE DEPENDENCY
        # AUDIT, onaylanan Model C-A). `no` cevabı answered sayılır, yalnız
        # `nodata` breadth dışında kalır.
        risk_measurable_answers = [
            a for a in answers
            if a.get("stage") in self.risk_weights
            and not a.get("disabled")
            and _FACTOR_ID_BY_LABEL.get(a.get("question")) not in STRUCTURALLY_UNAVAILABLE_FACTOR_IDS
        ]
        risk_breadth_total = len(risk_measurable_answers)
        risk_breadth = sum(1 for a in risk_measurable_answers if a.get("answer") != "nodata")
        risk_coverage = (risk_breadth / risk_breadth_total * 100) if risk_breadth_total else 0
        risk_reliable = risk_breadth >= 2
        risk_available = risk_breadth > 0

        # Sinyal kapsamı
        signal_total_items = sum(signal_items_count.values())
        signal_valid_items = sum(signal_answered.values())
        signal_coverage = (signal_valid_items / signal_total_items * 100) if signal_total_items else 0

        # Genel istatistik (yalnız kapsam-içi cevaplar — disabled=True kapsam dışıdır)
        in_scope_answers = [a for a in answers if not a.get("disabled")]
        valid = [a for a in in_scope_answers if a.get("answer") != "nodata"]
        yes = sum(1 for a in valid if a["answer"] == "yes")
        wait = sum(1 for a in valid if a["answer"] == "wait")
        no = sum(1 for a in valid if a["answer"] == "no")
        total_valid = len(valid)
        total_all = len(in_scope_answers)
        coverage = (total_valid / total_all) * 100 if total_all else 0

        # Ayrı ham skorlar
        signal_answers_list = [a for a in answers if a.get("stage") in self.signal_weights]
        signal_valid = [a for a in signal_answers_list if a.get("answer") != "nodata"]
        s_yes = sum(1 for a in signal_valid if a["answer"] == "yes")
        s_wait = sum(1 for a in signal_valid if a["answer"] == "wait")
        signal_raw = ((s_yes * 1.0 + s_wait * 0.5) / len(signal_valid)) * 100 if signal_valid else 0

        risk_answers_list = [a for a in answers if a.get("stage") in self.risk_weights]
        risk_valid = [a for a in risk_answers_list if a.get("answer") != "nodata"]
        r_yes = sum(1 for a in risk_valid if a["answer"] == "yes")
        r_wait = sum(1 for a in risk_valid if a["answer"] == "wait")
        risk_raw = ((r_yes * 1.0 + r_wait * 0.5) / len(risk_valid)) * 100 if risk_valid else 0

        vetos = self.veto_engine.check(answers, symbol)
        confidence, confidence_desc = self.confidence_level(coverage)
        verdict_title, entry_status, verdict_desc = self.verdict(
            signal_weighted, risk_weighted, len(vetos) > 0, confidence,
            risk_coverage, risk_available, risk_reliable)

        # Ayrı etken analizi
        signal_pros, signal_cons = self.top_factors(signal_answers_list)
        risk_pros, risk_cons = self.top_factors(risk_answers_list)

        # Giriş zamanlaması değerlendirmesi
        (entry_timing_score, entry_timing_label, entry_timing_desc,
         entry_timing_answered, entry_timing_total) = self.entry_timing(answers)

        return {
            "signal_score": round(signal_weighted, 1),
            "risk_score": round(risk_weighted, 1) if risk_available else None,
            "signal_raw_score": round(signal_raw, 1),
            "risk_raw_score": round(risk_raw, 1) if risk_available else None,
            "stage_scores": {**signal_scores, **risk_scores},
            "signal_stage_scores": signal_scores,
            "risk_stage_scores": risk_scores,
            "stage_answered": {**signal_answered, **risk_answered},
            "stage_items_count": {**signal_items_count, **risk_items_count},
            "normalized_weights": {**signal_norm, **risk_norm},
            "effective_weights": {**signal_eff, **risk_eff},
            "signal_coverage": round(signal_coverage, 1),
            "risk_coverage": round(risk_coverage, 1),
            "risk_breadth": risk_breadth,
            "risk_breadth_total": risk_breadth_total,
            "risk_reliable": risk_reliable,
            "counts": {"yes": yes, "wait": wait, "no": no,
                       "total_valid": total_valid, "total_all": total_all,
                       "nodata": total_all - total_valid},
            "coverage": round(coverage, 1),
            "confidence": confidence,
            "confidence_desc": confidence_desc,
            "vetos": vetos,
            "verdict_title": verdict_title,
            "entry_status": entry_status,
            "verdict_desc": verdict_desc,
            "entry_timing_score": entry_timing_score,
            "entry_timing": entry_timing_label,
            "entry_timing_desc": entry_timing_desc,
            "entry_timing_answered": entry_timing_answered,
            "entry_timing_total": entry_timing_total,
            "signal_pros": signal_pros,
            "signal_cons": signal_cons,
            "risk_pros": risk_pros,
            "risk_cons": risk_cons,
            "pros": signal_pros,
            "cons": signal_cons,
            "answers": answers,
        }


# ═══════════════════════════════════════════════════════════════════
# 5.4 MODEL D — CONTROLLED PRODUCTION IMPLEMENTATION (additive policy katmanı)
# ═══════════════════════════════════════════════════════════════════
# KESİN İLKE: ScoreEngine/VetoEngine/ThresholdEngine/verdict()/entry_timing()
# hiçbir şekilde değiştirilmez, buradan hiç çağrılmaz/yazılmaz. Bu bölüm
# yalnız zaten üretilmiş report/technical_structure/BTC-1D verisini OKUR ve
# saf (yan etkisiz) fonksiyonlarla additive bir "restricted candidate" kararı
# üretir. research/primitive validation turunda onaylanan R4 (recent price
# action) ve B1 (BTC r7<-10%) EXACT formülleri birebir kullanılır -- yeni
# threshold/weight/momentum kuralı YOK. Fail-closed: herhangi bir primitive
# None/unknown/eksikse veya başka bir veto mevcutsa candidate DAİMA False.

def classify_recent_price_action(rpa: Optional[dict]) -> str:
    """MODEL D — RECENT PRICE ACTION CLASSIFIER (research R4 exact formülü).
    rpa: _compute_recent_price_action() çıktısı (change_2h/3h/4h_pct,
    last_3_bullish/bearish_count). Bu bir CONFIRMED TREND DEĞİLDİR, yalnız
    kısa vadeli fiyat yönü sınıflandırmasıdır.
    positive: change_2h/3h/4h_pct ÜÇÜ DE > 0  VE last_3_bullish_count >= last_3_bearish_count
    negative: change_2h/3h/4h_pct ÜÇÜ DE < 0  VE last_3_bearish_count >= last_3_bullish_count
    aksi: mixed. Herhangi bir change_*h_pct eksikse: unknown."""
    if not rpa:
        return "unknown"
    c2, c3, c4 = rpa.get("change_2h_pct"), rpa.get("change_3h_pct"), rpa.get("change_4h_pct")
    if c2 is None or c3 is None or c4 is None:
        return "unknown"
    b3 = rpa.get("last_3_bullish_count", 0) or 0
    r3 = rpa.get("last_3_bearish_count", 0) or 0
    if c2 > 0 and c3 > 0 and c4 > 0 and b3 >= r3:
        return "positive"
    if c2 < 0 and c3 < 0 and c4 < 0 and r3 >= b3:
        return "negative"
    return "mixed"


def evaluate_btc_strong_down(btc_daily_bearish: Optional[bool],
                              r7_pct: Optional[float]) -> Optional[bool]:
    """MODEL D — BTC STRONG-DOWN CLASSIFIER (research B1 exact formülü,
    -10.0% eşiği CAL/OOS ile doğrulanmış). Strong-down <=> btc_daily_bearish
    AND r7_pct < -10.0 (KESİN '<' -- sınırda r7_pct==-10.0 strong_down=False
    döner). Girdilerden biri None ise -> None (unknown). Model D bu None'ı
    ASLA True/False'a yuvarlamaz -- çağıran taraf None'ı fail-closed (hard
    veto korunur) olarak ele almalıdır."""
    if btc_daily_bearish is None or r7_pct is None:
        return None
    if not btc_daily_bearish:
        return False
    return bool(r7_pct < -10.0)


def evaluate_model_d_candidate(symbol: str, vetos: List[str],
                                confirmed_structure: Optional[str],
                                recent_class: str,
                                btc_strong_down: Optional[bool]) -> "tuple":
    """MODEL D — CANDIDATE CONTRACT. TRUE yalnız TÜM aşağıdakiler birden
    doğruysa: symbol != BTC, tam olarak TEK veto mevcut ve bu veto BTC
    günlük-trend vetosu, confirmed_structure == 'bearish', recent_class ==
    'positive', btc_strong_down == False (None DEĞİL). Herhangi biri
    sağlanmazsa FALSE (fail-closed) -- mevcut hard veto/'Elenir' AYNEN
    korunur, bu fonksiyon report['vetos']/verdict_title'a hiç dokunmaz.
    Return: (is_candidate: bool, reason: str) -- reason yalnız iç/teşhis
    amaçlıdır, kullanıcıya AYNEN gösterilmez."""
    if symbol.upper() == "BTC":
        return False, "symbol_is_btc"
    if not vetos:
        return False, "no_veto"
    if len(vetos) != 1:
        return False, "multiple_vetos"
    if vetos[0] != BTC_DAILY_VETO_REASON:
        return False, "veto_not_btc_daily_trend"
    if confirmed_structure != "bearish":
        return False, "confirmed_structure_not_bearish"
    if recent_class != "positive":
        return False, "recent_class_not_positive"
    if btc_strong_down is None:
        return False, "btc_strong_down_unknown"
    if btc_strong_down:
        return False, "btc_strong_down_true"
    return True, "model_d_candidate_confirmed"


# MODEL D — kullanıcı-facing kısa açıklama metni (sabit şablon, backtest
# yüzdesi/istatistik İÇERMEZ). AL/LONG/İŞLEM UYGUN/VETO KALKTI/TREND DÖNDÜ
# gibi ifadeler KASITLI OLARAK yok.
MODEL_D_RESTRICTED_LABEL = "Yüksek Riskli İzle"
MODEL_D_RESTRICTED_EXPLANATION = (
    "BTC günlük piyasa rejimi olumsuz olduğu için deterministik motor işlem "
    "onayı vermiyor. Ancak coin'in teyit edilmiş 1 saatlik yapısı bearish "
    "kalmasına rağmen son kısa vadeli fiyat davranışı toparlanma gösteriyor "
    "ve BTC güçlü düşüş rejiminde değil. Bu nedenle coin tamamen göz ardı "
    "edilmeyip yüksek riskli olarak izlenebilir."
)


def history_table_verdict_text(raw_verdict: Optional[str], model_d_reason: Optional[str]) -> str:
    """HISTORY TABLE MODEL D PRESENTATION PARITY (onaylı, controlled
    implementation): show_history_detail()'in KULLANDIĞI AYNI kanonik koşulu
    (`model_d_reason == "model_d_candidate_confirmed"`, satır ~10635) saf bir
    fonksiyona çıkarır -- DB'ye/report'a dokunmaz, evaluate_model_d_candidate()
    YENİDEN ÇAĞRILMAZ, yalnız zaten stored olan `model_d_reason` okunur.
    FALSE (başka stabil reason) ve LEGACY (None) -- ikisi de raw verdict'i
    DEĞİŞTİRMEDEN döner; yalnız TRUE (confirmed candidate) iki-katmanlı metne
    genişler, motor kararı (raw verdict) ASLA gizlenmez/override edilmez."""
    base = raw_verdict or "—"
    if model_d_reason == "model_d_candidate_confirmed":
        return f"{base} · {MODEL_D_RESTRICTED_LABEL}"
    return base


# ═══════════════════════════════════════════════════════════════════
# 5.5 AI ANALİST V1 — deterministik motordan bağımsız, izole açıklama katmanı
# ═══════════════════════════════════════════════════════════════════
# KESİN İLKE: Motor hesaplar ve karar verir. Bu katman yalnız doğrulanmış
# sonucu doğal Türkçeyle açıklar. ScoreEngine/VetoEngine/verdict()/
# entry_timing()/STAGES_CONFIG/VETO_RULES/RealFetcher/HistoryDB'ye hiçbir
# şekilde yazmaz, onlardan hiçbir şeyi okumak dışında bir şey yapmaz.

AI_ANALYST_DECISION_EVIDENCE_FIXED = (
    "decision:risk_unavailable",
    "decision:risk_coverage_insufficient",
    "decision:confidence_insufficient",
    "decision:verdict_rejected",
)


def determine_ai_analyst_mode(report: dict) -> str:
    """Üç katmanlı routing (rev2 — routing tasarımı v2 onaylı):

    HARD_RESTRICTED <=> veto var OR verdict=='Elenir' OR risk_score None
    OR confidence=='Yetersiz'. Motorun kendisi genel güven açısından zaten
    düşük durumda — AI yalnız reddi/kısıtı açıklayabilir, işlem-yönlü
    hiçbir alan üretemez.

    LIMITED <=> yukarıdaki HARD koşulların HİÇBİRİ yokken risk_reliable=False
    (RISK COVERAGE V2 / R3: bağımsız measurable risk boyutu sayısı < 2).
    Bu, Level 1'de yapısal olarak sık karşılaşılan durum (Risk & pozisyon
    yönetimi aşamasının 4 sorusundan 3'ü Level 1'de hiçbir zaman otomatik
    cevaplanamıyor, kalan tek boyut -- recent_bad_event -- tek başına
    çok-boyutlu risk doğrulaması sayılmaz) — sinyal/teknik/makro veri
    tamamen sağlam olabilir, yalnız risk tarafı doğrulanamıyor. AI zengin
    analitik yorum yapabilir ama genel işlem uygunluğunu onaylayamaz
    (risk_data_notice zorunlu).

    NORMAL <=> yukarıdakilerin hiçbiri yok (risk_reliable=True ve motor
    genel olarak güvenilir bir karar üretebilmiş).

    report["risk_available"] diye bir alan YOK (full_report()'un dönüş
    sözleşmesi koddan doğrulandı) — bu yüzden risk_score is None mevcut
    otoriter sözleşme olarak kullanılıyor, yeni bir deterministic alan
    eklenmedi. Burada yeniden karar verilmiyor, yalnız mevcut report
    alanları okunuyor."""
    if report.get("vetos"):
        return "HARD_RESTRICTED"
    if report.get("verdict_title") == "Elenir":
        return "HARD_RESTRICTED"
    if report.get("risk_score") is None:
        return "HARD_RESTRICTED"
    if report.get("confidence") == "Yetersiz":
        return "HARD_RESTRICTED"
    if not report.get("risk_reliable"):
        return "LIMITED"
    return "NORMAL"


def _zone_relation(price, zone) -> Optional[str]:
    """RECENT PRICE ACTION V1: verilen fiyatın (analysis_price) bir zone'a
    göre konumu -- tamamen deterministik, ABOVE/INSIDE/BELOW dışında değer
    üretmez. price veya zone None ise None döner (Claude bu durumda relation
    kullanamaz, validator zaten yalnız GERÇEKTEN dolu alanları evidence
    olarak kabul ediyor)."""
    if price is None or not zone:
        return None
    if price > zone["zone_high"]:
        return "ABOVE"
    if price < zone["zone_low"]:
        return "BELOW"
    return "INSIDE"


def _zone_role_state(historical_origin: str, zone: Optional[dict],
                      analysis_price: Optional[float]) -> Optional[str]:
    """MARKET MAP V1: zone'un TARİHSEL kimliği (historical_origin -- TSE'nin
    closed-candle anında support/resistance olarak seçtiği taraf) ASLA
    değişmez/yeniden hesaplanmaz; bu fonksiyon yalnız CANLI analysis_price'a
    göre GÜNCEL rolünü döner -- tamamen deterministik sınır karşılaştırması,
    yeni threshold/policy YOK. zone veya analysis_price yoksa None.

    support kökenli: price>zone_high -> active_support,
                      zone_low<=price<=zone_high -> support_test,
                      price<zone_low -> lost_support.
    resistance kökenli: price<zone_low -> active_resistance,
                         zone_low<=price<=zone_high -> resistance_test,
                         price>zone_high -> broken_resistance.

    ÖNEMLİ (A2): lost_support/broken_resistance döndürmek zone'u YENİ bir
    confirmed sınıfa (ör. otomatik resistance) DÖNÜŞTÜRMEZ -- yalnız bir
    durum etiketidir, çağıran taraf bunu asla "confirmed resistance" gibi
    yeni bir zone kimliği olarak ele almamalı."""
    if not zone or analysis_price is None:
        return None
    zl, zh = zone["zone_low"], zone["zone_high"]
    if historical_origin == "support":
        if analysis_price > zh:
            return "active_support"
        if zl <= analysis_price <= zh:
            return "support_test"
        return "lost_support"
    if historical_origin == "resistance":
        if analysis_price < zl:
            return "active_resistance"
        if zl <= analysis_price <= zh:
            return "resistance_test"
        return "broken_resistance"
    return None


def _build_market_map(technical_structure: Optional[dict],
                       analysis_price: Optional[float]) -> Optional[dict]:
    """MARKET MAP V1 (AŞAMA A): TechnicalStructureEngine'in swing/zone ÜRETİM
    algoritmasına (analyze() gövdesi) HİÇ dokunmadan, yalnız zaten hesaplanmış
    CONFIRMED zone'ları (support_zones ∪ resistance_zones -- ikisi birlikte,
    closed-candle anındaki current_price'a göre ikiye bölünmüş TÜM zone'ları
    temsil eder; BİLİNEN DAR İSTİSNA: closed-candle fiyatı tam bir zone
    İÇİNDEYSE o zone ne listede görünür, bu nadir durumda market_map o
    zone'u göremez) alıp CANLI analysis_price'a göre YENİDEN, tamamen
    deterministik biçimde sıralar/rollendirir. Yeni pivot/zone/fiyat seviyesi
    ÜRETİLMEZ -- yalnız zaten var olan zone_low/zone_high çiftleri okunur,
    significance ranking/touch_count'a göre "daha önemli" ilanı YOK (yalnız
    analysis_price'a mesafe sırası)."""
    if not technical_structure or technical_structure.get("status") != "ok" or analysis_price is None:
        return None

    entries = []
    seen = set()
    for origin, zlist in (("support", technical_structure.get("support_zones") or []),
                          ("resistance", technical_structure.get("resistance_zones") or [])):
        for z in zlist:
            if not z.get("confirmed"):
                continue
            key = (round(z["zone_low"], 8), round(z["zone_high"], 8))
            if key in seen:
                continue
            seen.add(key)
            entries.append({"zone": z, "historical_origin": origin})

    def _payload(e):
        z = e["zone"]
        return {
            "zone_low": z["zone_low"], "zone_high": z["zone_high"],
            "historical_origin": e["historical_origin"],
            "live_role_state": _zone_role_state(e["historical_origin"], z, analysis_price),
            "touch_count": z["touch_count"],
        }

    below = sorted([e for e in entries if e["zone"]["zone_high"] < analysis_price],
                    key=lambda e: -e["zone"]["zone_high"])
    above = sorted([e for e in entries if e["zone"]["zone_low"] > analysis_price],
                    key=lambda e: e["zone"]["zone_low"])
    inside = sorted([e for e in entries
                      if e["zone"]["zone_low"] <= analysis_price <= e["zone"]["zone_high"]],
                     key=lambda e: abs(((e["zone"]["zone_low"] + e["zone"]["zone_high"]) / 2) - analysis_price))

    # SEMANTIC CONSISTENCY DÜZELTMESİ: support_1/2 ve resistance_1/2 YALNIZ
    # historical_origin'i GERÇEKTEN o tarafla eşleşen (dolayısıyla live_role_state
    # DAİMA active_support/active_resistance olan) zone'lardan seçilir --
    # "fiyatın hangi tarafında" olduğu TEK BAŞINA yeterli değildir. Böylece
    # lost_support (tarihsel support, fiyat şimdi altına düşmüş -> "above"
    # listesinde görünür) ASLA resistance_1/2 slotuna, broken_resistance
    # (tarihsel resistance, fiyat şimdi üstüne çıkmış -> "below" listesinde
    # görünür) ASLA support_1/2 slotuna giremez -- yalnız kendi ayrı
    # lost_supports_nearby / broken_resistances_nearby listelerinde kalır.
    active_below = [e for e in below if e["historical_origin"] == "support"]
    active_above = [e for e in above if e["historical_origin"] == "resistance"]

    return {
        "analysis_price": analysis_price,
        "support_1": _payload(active_below[0]) if len(active_below) > 0 else None,
        "support_2": _payload(active_below[1]) if len(active_below) > 1 else None,
        "resistance_1": _payload(active_above[0]) if len(active_above) > 0 else None,
        "resistance_2": _payload(active_above[1]) if len(active_above) > 1 else None,
        "tested_zone": _payload(inside[0]) if inside else None,
        "lost_supports_nearby": [_payload(e) for e in above if e["historical_origin"] == "support"][:2],
        "broken_resistances_nearby": [_payload(e) for e in below if e["historical_origin"] == "resistance"][:2],
    }


def _structural_reference(confirmed_structure: Optional[str],
                           technical_structure: Optional[dict]) -> Optional[dict]:
    """MARKET CONTEXT SYNTHESIS V6: yalnız TSE'nin ZATEN hesapladığı confirmed
    swing_highs/swing_lows'tan (yeni algoritma/network çağrısı YOK) additive
    bir "yapısal referans" adayı üretir. confirmed_structure=='bullish' ise
    son confirmed HL, =='bearish' ise son confirmed LH. BU BİR INVALIDATION/
    STOP-LOSS SEVİYESİ DEĞİLDİR -- Structure V2 Modest Scenario Context
    Audit'te breach ile structure-change arasında güçlü ama GECİKMELİ
    (asla eşzamanlı değil, %0 same-checkpoint) bir ilişki bulundu; bu yüzden
    yalnız "izlenebilecek yapısal referans" olarak sunulmalı."""
    if not technical_structure or technical_structure.get("status") != "ok":
        return None
    if confirmed_structure == "bullish":
        pool = [s for s in (technical_structure.get("swing_lows") or []) if s.get("label") == "HL"]
        ref_type = "last_confirmed_hl"
    elif confirmed_structure == "bearish":
        pool = [s for s in (technical_structure.get("swing_highs") or []) if s.get("label") == "LH"]
        ref_type = "last_confirmed_lh"
    else:
        return None
    if not pool:
        return None
    s = max(pool, key=lambda x: x["bar_index"])
    return {"type": ref_type, "price": s["price"], "bar_index": s["bar_index"]}


def _build_ai_structure_context(technical_structure: Optional[dict], analysis_price: Optional[float] = None):
    """TECHNICAL STRUCTURE ENGINE → AI ANALYST INTEGRATION V1/V2: TSE'nin
    (zaten hesaplanmış, look-ahead-safe, PHASE 2C ile confirmed-immutable)
    çıktısından AI'ye gidecek KOMPAKT payload'ı üretir. Hiçbir yeniden
    hesaplama/network çağrısı yok — yalnız mevcut technical_structure
    dict'inin dar bir partisyonu. Ham OHLCV/tüm swing listesi/tüm zone
    listesi/debug trace/calibration parametreleri/internal counter'lar
    KASITLI OLARAK taşınmıyor (prompt boyutu + evidence yüzeyi kontrolü).

    RECENT PRICE ACTION V1 (additive, TSE confirmed algoritmasına dokunmaz):
    - analysis_price: CANLI Binance ticker fiyatı (report["analysis_price"],
      capture_analysis_snapshot() ile alınır) -- TSE'nin kendi "current_price"
      alanından (son KAPANMIŞ 1H mum kapanışı) AYRI ve mümkün olduğunca
      GÜNCEL bir referans. İki alan KASITLI OLARAK karıştırılmaz:
      "current_price" (mevcut, değişmedi) = closed-candle referansı,
      "closed_candle_price" = AYNI değerin açık isimli takma adı (additive,
      breaking rename yok), "analysis_price" = canlı ticker.
    - nearest_support_relation / nearest_resistance_relation: analysis_price'ın
      zone'a göre ABOVE/INSIDE/BELOW konumu -- tamamen deterministik.
    - analysis_price_distance_to_support_pct / _resistance_pct: closed-candle
      distance_pct'ten AYRI, canlı fiyata göre ek/additive mesafe.
    - recent_price_action: technical_structure'a RealFetcher._build_context
      içinde (TSE.analyze()'nin KENDİSİ hiç değişmeden) additive olarak
      eklenmiş, yalnız KAPANMIŞ mumlardan türetilen kompakt özet.

    Döner: (structure_context_or_None, valid_structure_evidence_set).
    valid_structure_evidence yalnız BU analizde GERÇEKTEN dolu olan
    alanlara karşılık gelen "structure:*"/"price_action:*" token'larını
    içerir — AI bir alanı (ör. nearest_support) None iken evidence olarak
    gösteremez, validator bunu mekanik olarak reddeder (bkz.
    validate_ai_analyst_response) -- validator kodu HİÇ değişmedi, bu
    fonksiyonun döndürdüğü tek set üzerinden aynı mekanizma çalışıyor."""
    if not technical_structure:
        return None, set()
    if technical_structure.get("status") != "ok":
        return {"status": technical_structure.get("status", "insufficient_data")}, set()

    def _zone_payload(zone, distance_pct):
        if not zone:
            return None
        return {
            "zone_low": zone["zone_low"],
            "zone_high": zone["zone_high"],
            "touch_count": zone["touch_count"],
            "last_touched_bars_ago": zone["last_touched_bars_ago"],
            "confidence": zone.get("confidence"),
            "distance_pct": distance_pct,
        }

    nearest_support_zone = technical_structure.get("nearest_support")
    nearest_resistance_zone = technical_structure.get("nearest_resistance")
    ns = _zone_payload(nearest_support_zone, technical_structure.get("distance_to_support_pct"))
    nr = _zone_payload(nearest_resistance_zone, technical_structure.get("distance_to_resistance_pct"))
    trend = technical_structure.get("trend_structure")
    closed_candle_price = technical_structure.get("current_price")

    support_relation = _zone_relation(analysis_price, nearest_support_zone)
    resistance_relation = _zone_relation(analysis_price, nearest_resistance_zone)
    dist_support_live = (
        (analysis_price - nearest_support_zone["zone_high"]) / analysis_price * 100
        if analysis_price and nearest_support_zone else None)
    dist_resistance_live = (
        (nearest_resistance_zone["zone_low"] - analysis_price) / analysis_price * 100
        if analysis_price and nearest_resistance_zone else None)

    structure_context = {
        "timeframe": "1h",
        "bar_count": technical_structure.get("bar_count"),
        "trend_structure": trend,
        "trend_reason": technical_structure.get("trend_reason"),
        "atr14": technical_structure.get("atr14"),
        # geriye dönük uyumluluk için DEĞİŞTİRİLMEDİ (closed-candle referansı):
        "current_price": closed_candle_price,
        # additive, açık isimli takma adlar:
        "closed_candle_price": closed_candle_price,
        "analysis_price": analysis_price,
        "nearest_support": ns,
        "nearest_resistance": nr,
        "nearest_support_relation": support_relation,
        "nearest_resistance_relation": resistance_relation,
        "analysis_price_distance_to_support_pct": (
            round(dist_support_live, 3) if dist_support_live is not None else None),
        "analysis_price_distance_to_resistance_pct": (
            round(dist_resistance_live, 3) if dist_resistance_live is not None else None),
        "recent_price_action": technical_structure.get("recent_price_action"),
        # MARKET MAP V1 (AŞAMA A): TSE'nin zone ÜRETİMİNE dokunmadan, yalnız
        # zaten hesaplanmış confirmed zone'ları CANLI analysis_price'a göre
        # yeniden rollendiren additive katman -- bkz. _build_market_map().
        "market_map": _build_market_map(technical_structure, analysis_price),
        # MARKET CONTEXT SYNTHESIS V6: yalnız confirmed swing fact'lerinden,
        # additive "yapısal referans" adayı -- bkz. _structural_reference().
        "structural_reference": _structural_reference(trend, technical_structure),
    }
    valid_structure_evidence = set()
    if trend and trend != "insufficient_data":
        valid_structure_evidence.add("structure:trend_structure")
    if ns:
        valid_structure_evidence.add("structure:nearest_support")
    if nr:
        valid_structure_evidence.add("structure:nearest_resistance")
    if structure_context["atr14"]:
        valid_structure_evidence.add("structure:atr14")
    if closed_candle_price is not None:
        valid_structure_evidence.add("structure:closed_candle_price")
    if analysis_price is not None:
        valid_structure_evidence.add("structure:analysis_price")
    if structure_context["structural_reference"]:
        valid_structure_evidence.add("structure:structural_reference")
    rpa = structure_context["recent_price_action"]
    if rpa:
        if rpa.get("change_2h_pct") is not None:
            valid_structure_evidence.add("price_action:change_2h")
        if rpa.get("change_3h_pct") is not None:
            valid_structure_evidence.add("price_action:change_3h")
        if rpa.get("change_4h_pct") is not None:
            valid_structure_evidence.add("price_action:change_4h")
        if rpa.get("last_3_bullish_count") is not None:
            valid_structure_evidence.add("price_action:last_3_candles")
        if rpa.get("last_4_bullish_count") is not None:
            valid_structure_evidence.add("price_action:last_4_candles")
    market_map = structure_context["market_map"]
    if market_map:
        for mm_key in ("support_1", "support_2", "resistance_1", "resistance_2", "tested_zone"):
            if market_map.get(mm_key):
                valid_structure_evidence.add(f"market_map:{mm_key}")
    return structure_context, valid_structure_evidence


def build_ai_analyst_context(report: dict, symbol: str, technical_structure: Optional[dict] = None) -> dict:
    """report (ScoreEngine.full_report() çıktısı, DEĞİŞTİRİLMEDEN) üzerinden
    Claude'a gönderilecek context'i üretir. Hiçbir yeni API çağrısı yapmaz,
    hiçbir veri yeniden hesaplanmaz — yalnız mevcut report'un partisyonu ve
    v1.1 formatlama sözleşmesinin uygulanmasıdır.

    technical_structure (opsiyonel, additive — TECHNICAL STRUCTURE ENGINE →
    AI ANALYST INTEGRATION V1): RealFetcher.get_technical_structure(symbol)
    ile ZATEN hesaplanmış (yeni network çağrısı YOK) TechnicalStructureEngine
    çıktısı. ScoreEngine/VetoEngine/ThresholdEngine/verdict()/entry_timing()
    hiçbirini ETKİLEMEZ — yalnız AI'nin narrative context'ine eklenir."""
    mode = determine_ai_analyst_mode(report)
    structure_context, valid_structure_evidence = _build_ai_structure_context(
        technical_structure, report.get("analysis_price"))

    decision_context = {
        "symbol": symbol.upper(),
        "signal_score": report.get("signal_score"),
        "signal_coverage": report.get("signal_coverage"),
        "confidence": report.get("confidence"),
        "confidence_desc": report.get("confidence_desc"),
        "risk_score": report.get("risk_score"),
        "risk_coverage": report.get("risk_coverage"),
        "risk_available": report.get("risk_score") is not None,
        # DECISION SURFACE PARITY AUDIT / BULGU 1 (onaylı, controlled
        # implementation): GUI/Clipboard/History üçü de risk_reliable=False
        # iken ham risk_score sayısını gizler ("Yetersiz veri"); AI context
        # bu bilgiyi taşımıyordu -- ham sayı risk_reliable provenance'ı
        # olmadan gidiyordu. Additive: mevcut risk_score/risk_available
        # anlamı DEĞİŞMEDİ, yalnız eksik olan güvenilirlik bilgisi eklendi.
        "risk_reliable": report.get("risk_reliable"),
        "verdict_title": report.get("verdict_title"),
        "verdict_desc": report.get("verdict_desc"),
        "verdict_entry_status": report.get("entry_status"),
        "entry_timing_label": report.get("entry_timing"),
        "entry_timing_score": report.get("entry_timing_score"),
        "entry_timing_answered": report.get("entry_timing_answered"),
        "entry_timing_total": report.get("entry_timing_total"),
        "vetos": list(report.get("vetos") or []),
        # MODEL D — additive fact'ler (ScoreEngine/VetoEngine hesaplamaz,
        # yalnız orkestrasyon katmanında Level1Worker tarafından zaten
        # hesaplanıp report'a eklenmiş). Yeni bir AI mode/schema alanı
        # DEĞİLDİR -- Claude bunları MEVCUT HARD_RESTRICTED şemasının
        # (ör. positive_but_insufficient_factors) narrative'ini
        # zenginleştirmek için okur, yeni bir çıktı alanı üretmez.
        "model_d_restricted_candidate": bool(report.get("restricted_candidate")),
        "model_d_recent_price_action_class": report.get("recent_price_action_class"),
        "model_d_btc_7d_return_pct": report.get("btc_7d_return_pct"),
        "model_d_btc_strong_down": report.get("btc_strong_down"),
    }

    usable_factors = []
    unavailable_factors = []
    valid_decision_evidence = set(AI_ANALYST_DECISION_EVIDENCE_FIXED)

    for a in (report.get("answers") or []):
        if a.get("disabled"):
            continue
        factor_id = _FACTOR_ID_BY_LABEL.get(a.get("question"))
        if factor_id is None or factor_id in STRUCTURALLY_UNAVAILABLE_FACTOR_IDS:
            continue

        answer = a.get("answer")
        meta = FACTOR_ID_TABLE[factor_id]
        entry_common = {"factor_id": factor_id, "label": a.get("question"), "stage": a.get("stage")}

        if answer == "nodata":
            unavailable_factors.append({**entry_common, "reason": a.get("reason") or ""})
            continue

        dc_statuses = decision_critical_to_statuses(factor_id, symbol)
        entry = {**entry_common, "status": answer,
                 "semantic_role": SEMANTIC_ROLE_BY_FACTOR_ID.get(factor_id),
                 "weight": a.get("weight"),
                 "base_priority": base_priority(factor_id),
                 "decision_critical_to_statuses": sorted(dc_statuses),
                 "decision_critical_effect": "veto" if dc_statuses else None}
        if meta.get("interpretation_guard"):
            entry["interpretation_guard"] = meta["interpretation_guard"]
        reason = a.get("reason") or ""
        if reason:
            entry["reason"] = reason

        if factor_id == "volume_spread_combined":
            components = a.get("components") or {}
            entry["components"] = {
                cm: {"display_value": format_component_display_value(cm, components.get(cm))}
                for cm in ("volume_24h", "spread_pct")
            }
        else:
            entry["display_value"] = format_display_value(factor_id, a.get("value"))

        usable_factors.append(entry)

    for v_reason in decision_context["vetos"]:
        veto_factor_id = _FACTOR_ID_BY_VETO_REASON.get(v_reason)
        if veto_factor_id:
            valid_decision_evidence.add(f"veto:{veto_factor_id}")

    # reassessment_triggers seçim havuzu — TEK doğruluk kaynağı. Bir
    # factor yalnız (a) bu analizde usable_factors'ta gerçekten varsa,
    # (b) FACTOR_TRANSITION_GUARDS'ta deterministik guard'ı varsa,
    # (c) STRUCTURALLY_UNAVAILABLE_FACTOR_IDS'te değilse (usable_factors
    # zaten bunları hiç içermiyor, burada yine de savunma amaçlı
    # tekrarlanıyor), (d) recent_bad_event değilse — Claude'a whitelist
    # olarak verilir VE validator bunu buradan (Claude'un cevabından
    # değil) okuyarak zorunlu uygular.
    reassessment_eligible_factors = sorted(
        {f["factor_id"] for f in usable_factors}
        & set(FACTOR_TRANSITION_GUARDS.keys())
        - STRUCTURALLY_UNAVAILABLE_FACTOR_IDS
        - {"recent_bad_event"}
    )
    _preferred_dc_factor = preferred_decision_critical_factor(usable_factors, symbol)
    # MODEL C (AŞAMA 2): reassessment_triggers artık Claude tarafından
    # SIFIRDAN kurulmuyor -- burada, mevcut validator kurallarından
    # deterministik türetilen, ZATEN geçerli bir candidate havuzu üretilir.
    # "reassessment_candidates": Claude'a AYNEN gönderilecek kompakt liste.
    # "_reassessment_candidates_by_id": backend/validator/GUI-render için
    # candidate_id -> tam candidate sözlüğü (Claude'a gönderilmez, "_"
    # prefix'i mevcut "_valid_decision_evidence" ile aynı backend-only
    # kuralına uyuyor).
    _reassessment_candidates = _generate_reassessment_candidates(
        usable_factors, reassessment_eligible_factors, _preferred_dc_factor)
    reassessment_candidates_by_id = {c["candidate_id"]: c for c in _reassessment_candidates}

    return {
        "mode": mode,
        "decision_context": decision_context,
        "usable_factors": usable_factors,
        "unavailable_factors": unavailable_factors,
        "_valid_decision_evidence": sorted(valid_decision_evidence),
        "reassessment_eligible_factors": reassessment_eligible_factors,
        "reassessment_candidates": _reassessment_candidates,
        "_reassessment_candidates_by_id": reassessment_candidates_by_id,
        # Birden fazla decision-critical aday varsa (universal filtrelerin
        # tamamı + varsa sembol-özel olanlar) reassessment_triggers'ta
        # yalnız BU factor "decision_critical" basis'iyle kullanılabilir —
        # tamamen deterministik seçildi (bkz. preferred_decision_critical_factor).
        "preferred_decision_critical_factor": _preferred_dc_factor,
        # TECHNICAL STRUCTURE ENGINE → AI ANALYST INTEGRATION V1 (additive):
        # structure_context None olabilir (technical_structure hiç yoksa/
        # feature kapalıysa) veya {"status": "insufficient_data"/"error"}
        # olabilir (250 bar altı veri vb.) — AI bu durumda yalnız kısaca
        # "confirmed technical structure verisi bu analiz için mevcut/
        # yeterli değil" diyebilir, hiçbir seviye/yön uyduramaz.
        "structure_context": structure_context,
        "_valid_structure_evidence": sorted(valid_structure_evidence),
    }


AI_ANALYST_SYSTEM_PROMPT = """Sen, bir kripto analiz motorunun ürettiği deterministik sonuçları doğal dilde AÇIKLAYAN bir katmansın. Karar verici değilsin.

KESİN KURALLAR:
1. Sana verilen decision_context alanlarını (skor, kapsam, güven, verdict, veto, entry_timing) asla yeniden hesaplama, değiştirme veya bunlara itiraz etme. Bunlar yalnızca tonunu ve anlatının sınırlarını belirler — market evidence olarak KULLANILAMAZLAR.
2. Yalnız usable_factors listesindeki factor_id'leri evidence olarak kullanabilirsin. unavailable_factors yalnız "veri belirsizliği" bağlamında, yalnız data_limitations alanında anılabilir.
3. Şu factor_id'ler Level 1'de YAPISAL OLARAK HİÇ ölçülmez, hiçbir zaman evidence olamaz: clear_support, risk_reward_ratio, token_unlock_risk, dxy_trend, etf_inflow, whale_accumulation, stablecoin_inflow.
   ÇOK ÖNEMLİ AYRIM (SIK YAPILAN HATA): Bu liste context'teki unavailable_factors kümesinin BİR PARÇASI DEĞİLDİR — bu factor_id'ler ne evidence dizilerinde ne de data_limitations alanında hiçbir zaman anılamaz (madde 9'daki "gerçek unavailable_factors" yalnız context'te ayrıca listelenen, bu maddedeki isimlerden TAMAMEN FARKLI bir kümedir). "Bu veri yapısal olarak hiç ölçülmüyor, o yüzden kullanıcıya bir sınırlama olarak bildireyim" mantığı YANLIŞTIR — bu factor_id'lerden hiç, hiçbir alanda bahsetme.
   YANLIŞ: data_limitations=[{"text": "ETF girişi verisi mevcut değil", "evidence": ["etf_inflow"]}] (etf_inflow burada da GEÇERSİZDİR -- REDDEDİLİR)
   DOĞRU: data_limitations'ta etf_inflow/clear_support/risk_reward_ratio/token_unlock_risk/dxy_trend/whale_accumulation/stablecoin_inflow'dan HİÇ bahsetme; yalnız context'in unavailable_factors listesinde GERÇEKTEN verilen factor_id'leri kullan.
4. Kendi başına hiçbir sayı hesaplama, türetme veya yuvarlama yapma. Yalnız sağlanan display_value'ları AYNEN kullanabilirsin. display_value'nun bir eşiğe ne kadar "yakın" göründüğünden asla kendi status yorumunu türetme — yalnız verilen status alanını (yes/wait/no) literal kabul et.
5. EVIDENCE SEMANTIC CEILING: Bir factor'dan yalnızca onun status + label + interpretation_guard + reason bilgisinin GERÇEKTEN desteklediği sonucu çıkarabilirsin. Bir metric'in NEDEN önemli olduğuna dair genel piyasa bilgisi/mekanizması, o metric'in MEVCUT gözleminin kanıtladığı sonuç gibi yazılamaz.
   Doğru: funding dengeli -> "funding sistemin dengeli kabul ettiği bantta"
   Yanlış: funding dengeli -> "kaldıraç birikimi yok"
   Doğru: OI aligned=yes -> "OI artışı fiyat hareketiyle uyumlu"
   Yanlış: OI aligned=yes -> "yeni para kesin piyasaya giriyor"
   Doğru: hacim yüksek -> "hacim katılımı güçlü"
   Yanlış: hacim yüksek -> "kurumsal alım var"
   Doğru: BTC günlük trend=yes -> "BTC günlük trendi destekleyici"
   Yanlış: BTC günlük trend=yes -> "altcoin rallisi başlayacak"
   Aynı tavan reassessment_triggers için de geçerlidir: yalnız "bu factor'ın zaten ölçtüğü durumun tersine dönmesi" tetikleyici olabilir, yeni bir causal eşik/kural icat edilemez.
6. Şunları asla üretme: destek/direnç seviyesi, swing high/low, FVG, supply/demand bölgesi, liquidity sweep, giriş fiyatı, stop-loss, take-profit, mum formasyonu yorumu, PSAR/DMI/MFI/Fisher/Vortex/ADX/Stochastic/EMA20/EMA200 (bu sistemde hesaplanmıyor), yönlü fiyat-yolu tahmini.
7. Yatırım tavsiyesi/garanti dili kullanma. "Ben düşünüyorum", "bence fiyat...", "beklentim..." gibi kendini bağımsız karar mercii gibi gösteren ifadeler kullanma — kanıt-merkezli dil kullan ("veriler birlikte ... gösteriyor", "mevcut yapı ...").
8. Her analitik iddia (overall dahil) en az bir evidence taşımalı. Evidence taşımayan bir iddia ÜRETME — boş bırakılabilecek bir alanı (supporting_factors, risks_conflicts, reassessment_triggers, data_limitations boş dizi olabilir; entry_assessment null olabilir) sırf doldurmak için uydurma içerik üretme.
9. data_limitations yalnız context'te GERÇEKTEN unavailable_factors içinde olan SPESİFİK factor_id'lere referans verebilir (her öğenin evidence dizisi bu factor_id'lerden en az birini içermek ZORUNDADIR — boş evidence dizisi GEÇERSİZDİR). unavailable_factors boşsa data_limitations de boş olmalı — veri eksikliği icat etme. ÇOK ÖNEMLİ AYRIM: risk_coverage/genel kapsam YÜZDESİ (ör. "risk verisi %25/%0 kapsıyor") kendi başına bir factor_id DEĞİLDİR ve data_limitations'a KONULAMAZ — bu kavram yalnız why_rejected_or_limited.decision_evidence (HARD_RESTRICTED) veya risk_data_notice (LIMITED) üzerinden, oradaki sabit decision: token'larıyla ifade edilir. data_limitations'ta yalnız GERÇEKTEN adı geçen, kendi factor_id'si olan tekil eksik veri noktalarından bahset (ör. mvrv_ratio, sopr_recovery) — "kapsam düşük/sıfır" gibi genel bir cümleyi factor evidence'sız BURAYA yazma; evidence bulamıyorsan o cümleyi hiç yazma, zaten aynı bilgi why_rejected_or_limited/risk_data_notice'ta ayrıca ve doğru şekilde veriliyor.
10. Yalnız sana verilen structured tool şemasına uygun bir yanıt döndür. Şema dışında hiçbir serbest metin ekleme.
11. REASSESSMENT TRIGGER SEÇİMİ (MODEL C — deterministic candidate + AI seçim, AŞAMA 2): reassessment_triggers'ta artık trigger objesi KURMUYORSUN. Context'te sana ayrıca verilen reassessment_candidates listesi, backend tarafında ZATEN geçerli (doğru factor_id, doğru status geçişi, doğru selection_basis, aynı candidate içinde çakışan semantic_role YOK) olarak üretilmiştir — sen yalnız bu adaylar arasından ANALİTİK OLARAK en önemli/en anlamlı olanları seçer, kısa bir meaning yazarsın. Görevin: hangi candidate mevcut karar açısından en kritik, hangisi bir çelişkiyi çözer, hangisi mevcut desteği korur — bunu SEN değerlendirirsin, ama candidate'in kendisini (factor/status/type) SEN İCAT ETMEZSİN. Kullanıcı mesajında tam format kuralları ve semantic_role çakışma kontrolü ayrıca açıklanıyor, oradaki talimatlara harfiyen uy.
12. TECHNICAL STRUCTURE CONTEXT (yalnız context'te structure_context alanı VARSA): Bu veri 1 saatlik (1h) zaman diliminde, mevcut 250 KAPANMIŞ mumdan hesaplanan, look-ahead-safe, CONFIRMED (teyit edilmiş) swing yapısına dayanır. confirmation doğası gereği 18-31 bar mertebesinde doğal bir gecikme taşır — bunu ASLA "şu an kesin anlık yön budur" gibi sunma. Yalnız "teyit edilmiş 1s yapı", "mevcut confirmed structure" gibi ifadeler kullan; trend_structure'ı mutlak/anlık gerçeklik gibi değil, GEÇMİŞE dönük teyit edilmiş bir durum olarak çerçevele.
    a) YENİ SEVİYE UYDURMA YASAĞI: Kendi support/resistance seviyeni, kendi swing high/low'unu, kendi BOS/CHoCH'unu, kendi mum/grafik formasyonunu ASLA üretme. Destek/direnç yalnız structure_context.nearest_support / structure_context.nearest_resistance zone'larından (zone_low/zone_high aralığı) gelebilir — context'te verilmeyen HİÇBİR fiyat seviyesi yazma.
    b) NONE SEMANTİĞİ: structure_context.nearest_support None ise "destek yok" DEME — doğru ifade: "mevcut 250 saatlik pencerede confirmed yakın destek bölgesi tespit edilmedi". Aynı kural nearest_resistance için de geçerli. structure_context.status alanı "insufficient_data"/"error" ise (structure_context içinde nearest_support/resistance/trend_structure hiç YOK demektir) bunu yalnız KISACA, BİR KEZ belirt — "teknik yapı verisi bu analiz için yeterli değil" gibi — hiçbir seviye/yön uydurma.
    c) EVIDENCE: structure_context'e dayanan bir iddia kullanırsan, o iddiayı taşıyan öğenin evidence dizisine context'te sana ayrıca verilen valid_structure_evidence listesinden İLGİLİ "structure:*" token'ını da ekle (ör. destek yakınlığından bahsediyorsan "structure:nearest_support") — yalnız bu listedeki token'lar geçerlidir, kendi token'ını icat etme. Bu token'lar normal factor_id'lerle AYNI evidence dizisine birlikte girebilir (bir iddia hem factor hem structure kanıtına dayanabilir). used_factors alanına GERÇEKTEN kullandığın her "structure:*" token'ını da (factor_id'lerle aynı şekilde) ekle.
13. STRUCTURE + ENTRY TIMING: entry_assessment'ta (varsa) yalnız mevcut ema50/price_change/rsi faktörlerini değil, structure_context.nearest_support/nearest_resistance'a mevcut fiyat yakınlığını da (distance_pct alanları üzerinden, kendi yüzde hesaplama YAPMADAN, verileni AYNEN kullanarak) değerlendirebilirsin — ama bu asla emir/al-sat/giriş fiyatı önerisine dönüşemez, yalnız zamanlamanın teknik bağlamını açıklar.
14. TEKRARSIZLIK: Eksik veri/kapsam sınırlaması bilgisini (ör. risk_coverage düşük, on-chain yok, MVRV/SOPR yok) YALNIZ BİR KEZ, kısa şekilde belirt — aynı limitation'ı farklı bölümlerde (overall, risks_conflicts, data_limitations vb.) defalarca tekrar ETME. Token'larının çoğunu yeni analitik içeriğe (structure, teknik çelişki, support/resistance yakınlığı, neyin teyit edilmesi/bozulması gerektiği) ayır.
15. TECHNICAL_WATCHPOINTS (TÜM MODLARDA ZORUNLU ALAN, HARD_RESTRICTED DAHİL): Bu alan motorun karar sınırını (veto/verdict/risk yetersizliği) HİÇBİR ŞEKİLDE geçersiz kılmaz, BUY/SELL/entry tavsiyesi DEĞİLDİR — yalnız "işlem uygunluğu ne olursa olsun, teknik açıdan hangi koşullar izlenmeli" sorusuna deterministik verilerle cevap verir. Motor reddetmiş/kısıtlamış olsa BİLE bu alanı (structure_context ve/veya usable_factors mevcutsa) BOŞ BIRAKMA — "risk verisi yetersiz" cümlesiyle yorumu bitirmek YASAK, ondan sonra mutlaka mevcut teknik yapıya geç.
    a) KOMPAKTLIK ZORUNLU: hedef 3 öğe — TAM OLARAK bu sırayla ve tercihen bu sayıda: (1) TEK bir "current_structure" öğesi (trend + varsa hem support hem resistance yakınlığını AYNI cümlede/aynı öğede özetle — trend için bir current_structure, support için ayrı bir current_structure, resistance için ayrı bir current_structure ÜRETME; üç ayrı zone bilgisini tek öğede birleştiremiyorsan bile en fazla 2 current_structure öğesiyle sınırlı kal), (2) en güçlü/anlamlı TEK "strengthens" öğesi, (3) en güçlü/anlamlı TEK "weakens" öğesi. Gerçekten farklı, bağımsız bir analitik nokta varsa (aynı kategoriyi tekrar etmeyen) 4. öğe eklenebilir — ama sayı doldurmak için EKLEME, context yetersizse 2 öğeyle de kalabilirsin. Kalite > sayı, sayı > 4 OLMASIN.
    b) zone_ref ALANINA ASLA FİYAT/SEVİYE SAYISI YAZMA — yalnız "nearest_support" veya "nearest_resistance" string'ini seç, gerçek fiyat aralığını sistem ayrıca deterministik olarak gösterecek. zone_ref kullandığın öğenin evidence dizisine MUTLAKA aynı "structure:nearest_support"/"structure:nearest_resistance" token'ını da ekle. structure_context'te o zone None ise (valid_structure_evidence listesinde yoksa) o zone_ref'i HİÇ kullanma — "destek/direnç yok" da DEME, yalnız "mevcut 250 saatlik pencerede confirmed yakın destek/direnç bölgesi tespit edilmedi" anlamını (meaning metninde, zone_ref OLMADAN) taşıyabilirsin.
    c) "strengthens"/"weakens" öğeleri zone-tabanlı olabileceği gibi (ör. "yakın direnç bölgesinin üzerine geçilmesi") mevcut factor_id-tabanlı da olabilir (ör. "MACD sinyalinin boğa kesişimine dönmesi") — ikinci durumda evidence'a ilgili factor_id'yi koy, zone_ref kullanma. BOS/CHoCH, "kalıcı kapanış" gibi context'te tanımlanmamış hiçbir yeni mekanik kriter icat etme — yalnız "zone'un aşılması/kaybedilmesi" kadar iddia et, daha kesin bir teknik dil kullanma.
    c2) CATEGORY × ZONE_REF SÖZLEŞMESİ (MEKANİK, validator tarafından zorunlu kılınır): zone_ref kullanan bir öğede yalnız şu iki eşleşme geçerlidir — "strengthens" + nearest_resistance (yakın direncin üzerine geçilmesi), "weakens" + nearest_support (yakın desteğin kaybedilmesi/altına inilmesi). "strengthens" + nearest_support ve "weakens" + nearest_resistance kombinasyonları GEÇERSİZDİR — bu combo'ları asla üretme, üretirsen yanıtın TAMAMI reddedilir. "current_structure" için her iki zone_ref de serbesttir (bu bir olay/tetikleyici iddiası değil, yalnız mevcut konumun tarifidir).
    c3) INTERNAL TOKEN YASAĞI: meaning metninde ASLA "nearest_support"/"nearest_resistance" (veya bunların büyük/küçük harf varyasyonları, alt çizgili hali) gibi ham İngilizce/machine-readable ifadeler kullanma — bunlar yalnız zone_ref alanının kendi (structured, kullanıcıya hiç gösterilmeyen) değeridir. Zone-tabanlı bir strengthens/weakens öğesinde zone event'inin KENDİSİNİ (hangi seviyenin aşıldığı/kaybedildiği) meaning'de TEKRAR TARİF ETME — bunu sistem zaten deterministik olarak (category+zone_ref'ten) ayrıca gösterecek. meaning'de YALNIZ kısaca "bu neden önemli" sorusuna cevap ver (ör. "Bu değişim mevcut bearish yapıya karşı bir denge sinyali oluşturabilir.") — fiyat seviyesi yazma, zone'u yeniden formüle etmeye çalışma.
    d) Her öğe en az bir evidence taşımak ZORUNDADIR (factor_id ve/veya "structure:*" token). meaning KISA olsun (tek cümle, ~15-25 kelime) — uzun paragraf yazma, watchpoint bir başlık+gerekçe değil, bir izleme notudur.
    e) TEKRAR YASAĞI (ÇOK ÖNEMLİ): overall'da veya risks_conflicts'te ZATEN anlattığın bir gözlemi (ör. "fiyat resistance'a X% yakın") technical_watchpoints'te AYNI CÜMLEYLE tekrar ETME. Roller kesin ayrı: overall = genel karar bağlamı (structure'dan yalnız TEK KISA referans, ör. "confirmed 1s yapı bearish"), technical_watchpoints = yalnız "şu an ne izlenmeli" — support/resistance'a olan YAKINLIK YÜZDESİNİ (distance_pct) yalnız BİR yerde (ya entry_assessment'ta ya technical_watchpoints'te, ikisinde birden değil) belirt.
16. ROL AYRIMI VE ANALİTİK SENTEZ (AI ANALYST V5 — TEKRAR YERİNE İLİŞKİ KUR): Her alanın kendine özgü, birbirini tekrar ETMEYEN bir görevi var. Görevin faktörleri ayrı ayrı yeniden listelemek değil, ARALARINDAKİ İLİŞKİYİ açıklamaktır.
    a) overall: kararın/durumun ÇOK KISA (tek cümle) üst-düzey çerçevesi. Asıl gerekçeyi burada TAM anlatma — bu görev HARD_RESTRICTED'de why_rejected_or_limited'a, NORMAL/LIMITED'de supporting_factors/risks_conflicts'e aittir.
    b) HARD_RESTRICTED — decisive_factors: yalnız HANGİ faktör(ler) veto/reddi TETİKLEDİ, bunu doğrula. NEDEN tetiklediğini (bu zaten why_rejected_or_limited'ın işi) tekrar açıklama.
    c) HARD_RESTRICTED — positive_but_insufficient_factors: Bu alanın GERÇEK görevi yalnız "pozitif ama yetersiz faktörleri listelemek" değildir — coin'in KENDİ teknik görünümünü, veto/global faktörden (ör. BTC trendi) BAĞIMSIZ olarak SENTEZLEMEKTİR. decisive_factors'ta zaten anlatılan veto faktörü HARİÇ, coin'in kendi usable_factors'ı arasında hem destekleyici hem teyit etmeyen/olumsuz olanları BİRLİKTE değerlendirip TEK bir sentez cümlesi/paragrafı üret (ör. "RSI ve EMA50 yakınlığı erken toparlanma bağlamı sunsa da, coin'in kendi confirmed structure'ı ve MACD/OBV momentumu bunu henüz teyit etmiyor"). KESİNLİKLE YASAK: "veto olmasaydı karar X olurdu" tarzı motor kararını yeniden hesaplama/tahmin etme (madde 1 hâlâ geçerli, decision_context'e asla itiraz edilmez) — yalnız "veto'dan/global faktörden bağımsız olarak coin'in kendi factor evidence'ı şu yönde" çerçevesinde kal, yeni bir karar üretme.
    d) ERKEN SİNYAL / TEYİT AYRIMI (TÜM MODLAR): Bir faktör (ör. RSI, ema50_distance) olumlu ama coin'in structure_context'i veya çoğunluk momentum faktörleri (MACD/OBV/Bollinger) olumsuzsa, bu tek faktörü "dönüş teyidi" gibi sunma — yalnız "erken/potansiyel işaret, henüz teyit değil" şeklinde çerçevele. Yalnız context bunu gerçekten destekliyorsa (ör. gerçekten karışık/çelişkili evidence varsa) bu ayrımı yap; yapay bir çelişki icat etme.
    e) Aynı SPESİFİK gerekçeyi (ör. "BTC günlük trendi ayı → veto tetikledi") birden fazla alanda TAM CÜMLE olarak tekrar etme — her alan yalnız KENDİ rolüne düşen YENİ bilgiyi ekler, bir öncekini yeniden anlatmaz.
17. RECENT PRICE ACTION vs CONFIRMED STRUCTURE (yalnız context'te ilgili alanlar VARSA — None ise hiç kullanma; VARSA ve ANLAMLI bir ilişki taşıyorsa madde 17e ZORUNLUDUR, isteğe bağlı değildir):
    a) İKİ AYRI, KARIŞTIRILMAMASI GEREKEN fiyat referansı olabilir: structure_context.closed_candle_price (son KAPANMIŞ 1 saatlik mumun kapanışı — nearest_support/nearest_resistance SEÇİMİNİN dayandığı referans) ve structure_context.analysis_price (CANLI, şu anki Binance ticker fiyatı — bu analizin yapıldığı ANDAKİ gerçek fiyat). Bunlar FARKLI anlara ait olabilir, birbirinin yerine geçmez, ikisini aynı cümlede karıştırma.
    b) ZONE RELATION (structure_context.nearest_support_relation / nearest_resistance_relation, değerleri yalnız "ABOVE"/"INSIDE"/"BELOW" olabilir): analysis_price'ın zone'a göre GERÇEK konumu, TAMAMEN deterministik olarak SANA VERİLMİŞTİR — kendi relation'ını hesaplama/tahmin etme, yalnız verileni oku. Bu ham "ABOVE"/"INSIDE"/"BELOW" string'ini KULLANICIYA GÖSTERME (meaning metnine yazma) — yalnız kendi cümleni doğru zamanda kurmak için (ör. "zaten geçmiş", "test ediyor", "henüz geçmedi") arka planda kullan. Watchpoint renderer zaten deterministik olarak doğru zamanı (geçmiş/şimdi/gelecek) seçiyor — kendi meaning metninde bu deterministik fact'e TERS DÜŞME (ör. renderer "üzerine geçmiş durumda" diyorsa sen "ileride aşarsa..." deme).
    c) RECENT_PRICE_ACTION (structure_context.recent_price_action, varsa): change_2h_pct/change_3h_pct/change_4h_pct (yalnız KAPANMIŞ mumlardan hesaplanmış % değişim) ve last_3/4_bullish_count/bearish_count (son 3/4 KAPANMIŞ mumun yön sayımı) alanlarını taşır. Bu, CONFIRMED yapının YERİNE GEÇMEZ — confirmed_1h_yapı (trend_structure) hâlâ yavaş/teyit edilmiş ana katmandır, recent_price_action ise onun İÇİNDEKİ kısa vadeli/henüz teyit edilmemiş davranıştır. YASAK: recent_price_action'dan "confirmed bullish", "trend döndü", "dönüş teyit edildi", "confirmed HH/HL/LH/LL" gibi confirmed structure'a ait bir iddia türetmek — confirmed_structure'ın kendisi DEĞİŞMEDEN kalır. Kendi HH/HL/LH/LL etiketini ASLA icat etme — bu yalnız TSE'nin confirmed swing_highs/swing_lows alanlarında (madde 12) var, recent_price_action'da hiç yok. Rakamları (change_2h_pct vb.) ham sayı dökümü olarak MEKANİK tekrar etme ("2h +X, 3h +Y, 4h +Z" tarzı) — önce ANLAMINI çıkar, yalnız gerçekten faydalıysa tek bir sayıya kısaca değin.
    d) BTC/piyasa rejimi vetosuyla ilişkisi: recent_price_action veya analysis_price'ı KULLANARAK "veto olmasaydı karar X olurdu" tarzı motor kararını yeniden hesaplama (madde 1 ve madde 16c hâlâ geçerli) — yalnız "veto kararı değişmiyor; coin'in kendi güncel fiyat davranışı ayrıca şu yönde" çerçevesinde kal.
    e) SENTEZ ZORUNLULUĞU: recent_price_action mevcutsa ve confirmed_structure/momentum/live-zone-state ile ANLAMLI bir ilişkisi varsa (aşağıdaki 5 durumdan biri açıkça geçerliyse), bunu coin'in kendi teknik görünümünü anlattığın bölümde (HARD_RESTRICTED: positive_but_insufficient_factors; NORMAL/LIMITED: overall/risks_conflicts/supporting_factors) EN AZ BİR CÜMLEYLE ele al — görmezden gelme:
       i) confirmed BEARISH + recent POZİTİF → "bearish ana yapı içinde kısa vadeli toparlanma / karşı hareket" (confirmed dönüş DEĞİL).
       ii) confirmed BULLISH + recent NEGATİF → "bullish ana yapı içinde kısa vadeli geri çekilme/zayıflama" (confirmed dönüş DEĞİL).
       iii) confirmed BEARISH + recent NEGATİF → recent hareket confirmed bearish yapıyı destekliyor, ayrıca vurgulamaya gerek yoksa kısa geç.
       iv) confirmed BULLISH + recent POZİTİF → recent hareket confirmed bullish yapıyla uyumlu, ayrıca vurgulamaya gerek yoksa kısa geç.
       v) recent veriler KARIŞIK (last_3/4 sayımları veya change_2h/3h/4h işaretleri tutarsız) → net bir kısa vadeli yön iddiası ÜRETME, "kısa vadede net bir teyit yok" gibi nötr bir ifadeyle geç.
       Bu sentezi MOMENTUM factor'leriyle (MACD/RSI/OBV/Bollinger, usable ise) VE (varsa) live zone relation ile birlikte oku: recent pozitif hareket momentum factor'leri tarafından da destekleniyorsa bunu belirt ("...MACD/OBV iyileşmesiyle uyumlu"); momentum factor'leri buna ters düşüyorsa bunu da belirt ("...ancak momentum bunu henüz teyit etmiyor"). Zone relation ABOVE/INSIDE ise ve recent_price_action pozitifse bunu birlikte değerlendirebilirsin (ör. "fiyat yakın direncin üzerine çıkmış durumda; bu recent güçlenmeyle uyumlu olsa da confirmed bearish yapıyı tek başına tersine çevirmiyor") — ama HER ZAMAN confirmed_structure'ın DEĞİŞMEDİĞİNİ netleştir. Recent veri anlamsız derecede küçük/belirsizse (ör. tek bir mumluk marjinal hareket) abartılı bir sonuç çıkarma — sessizce geçebilirsin, bu durumda madde 17e ZORUNLU değildir; hangi büyüklüğün "anlamlı" sayıldığına dair yeni bir sayısal eşik/kural İCAT ETME, kendi analitik değerlendirmenle karar ver.
18. MARKET MAP V1 (yalnız context'te structure_context.market_map VARSA): structure_context.nearest_support/nearest_resistance TSE'nin SON KAPANMIŞ MUM anındaki (closed_candle_price'a göre seçilmiş, TARİHSEL) referanslarıdır — bunları "şu anki/aktif" destek-direnç gibi sunma. "Şu an aktif" / "kaybedilmiş" / "aşılmış" gibi CANLI (analysis_price'a göre) bir iddia kurarken YALNIZ market_map alanlarını (support_1/support_2/resistance_1/resistance_2/tested_zone) kullan, her birinin zone_low/zone_high/historical_origin/live_role_state alanları vardır.
    a) live_role_state SÖZLÜĞÜ (ham İngilizce değeri KULLANICIYA GÖSTERME, yalnız arkaplanda oku): active_support/active_resistance = zone hâlâ tarihsel rolünde ve fiyat henüz aşmamış. support_test/resistance_test = fiyat şu an zone İÇİNDE. lost_support = TARİHSEL support idi ama fiyat şimdi zone_low'un ALTINA düşmüş — bunu KESİNLİKLE "yakın destek"/"aktif destek" diye SUNMA, yalnız "önceden destek olan ama şimdi kaybedilmiş bölge / olası direnç referansı" tarzı bir ifade kullan. broken_resistance = TARİHSEL resistance idi ama fiyat şimdi zone_high'ın ÜSTÜNE çıkmış — "aşılmış direnç / olası destek referansı" tarzı ifade kullan. lost_support/broken_resistance'ı ASLA yeni bir "confirmed support/resistance" gibi kesin bir dille sunma (bu yalnız bir olası referans, retest/onay gerektirir) — "artık kesin destek/direnç budur" deme.
    b) support_1/resistance_1 = analysis_price'a göre en yakın CANLI/AKTİF destek-direnç adayıdır — TANIM GEREĞİ yalnız historical_origin'i KENDİ tarafıyla eşleşen (yani live_role_state DAİMA active_support / active_resistance olan) zone'lardan seçilir. lost_support/broken_resistance ASLA support_1/resistance_1 slotuna GİRMEZ — bunlar yalnız ayrı lost_supports_nearby/broken_resistances_nearby dizilerinde bulunur. Normal "yakın destek/direnç" ifaden için ÖNCELİKLE support_1/resistance_1'i kullan, nearest_support/nearest_resistance'ı DEĞİL. resistance_1 None ise ("aktif confirmed direnç bulunamadı") ama lost_supports_nearby doluysa, bunu "aktif confirmed direnç bulunamadı; yukarıda [X–Y] kaybedilmiş destek referansı var" tarzı DÜRÜST bir ayrımla sun — lost support'u SESSİZCE resistance_1 yerine geçirme. Aynı mantık ters yönde broken_resistances_nearby için geçerli (support_1 None + broken_resistances_nearby doluysa "aktif confirmed destek bulunamadı; aşağıda [X–Y] aşılmış direnç referansı var"). tested_zone varsa fiyat şu an bir zone'un İÇİNDE demektir, bunu ayrıca belirtebilirsin.
    c) EVIDENCE: market_map alanına dayanan bir iddia kullanırsan evidence dizisine ilgili "market_map:support_1" / "market_map:support_2" / "market_map:resistance_1" / "market_map:resistance_2" / "market_map:tested_zone" token'ını (yalnız valid_structure_evidence listesindeyse) ekle — structure:nearest_support/resistance token'larından AYRI bir vocabulary'dir, ikisini karıştırma. used_factors'a da aynı şekilde ekle.
    d) TEKRARSIZLIK: market_map ile nearest_support/nearest_resistance'ın AYNI zone'u işaret ettiği (historical_origin'in live_role_state'i active_support/active_resistance olduğu, yani hiçbir rol değişikliği olmadığı) sıradan durumda bunu İKİ AYRI iddia gibi tekrar sunma — yalnız market_map'in verdiği (daha güncel) çerçeveyi kullan.
    e) ZONE_REF EVIDENCE TOKEN ZORUNLULUĞU (ÇOK ÖNEMLİ, SIK YAPILAN HATA): technical_watchpoints içinde zone_ref="nearest_support" veya zone_ref="nearest_resistance" kullandığın HER durumda, evidence dizisinde MUTLAKA ilgili "structure:nearest_support" veya "structure:nearest_resistance" token'ı da bulunmalıdır (bkz. madde 15c). "market_map:support_1"/"market_map:resistance_1" gibi YENİ token'lar bu zorunluluğun YERİNE GEÇMEZ — ikisi ayrı, birbirini İKAME ETMEYEN vocabulary'lerdir: structure:nearest_* = zone_ref'in validator tarafından zorunlu tutulan referans kimliği; market_map:* = o zone'un CANLI analysis_price karşısındaki güncel rol/konum bilgisi. Gerekirse ikisi AYNI evidence dizisinde BİRLİKTE kullanılabilir (structure:nearest_resistance zorunlu, market_map:resistance_1 isteğe bağlı ek bağlam), ama structure:nearest_* olmadan yalnız market_map:* ile zone_ref kullanmak yanıtın TAMAMININ reddedilmesine yol açar. Örnek:
       YANLIŞ: zone_ref="nearest_resistance", evidence=["market_map:resistance_1"] (structure:nearest_resistance EKSİK -- REDDEDİLİR)
       DOĞRU:  zone_ref="nearest_resistance", evidence=["structure:nearest_resistance", "market_map:resistance_1"]
       DOĞRU (market_map bağlamı gerekmiyorsa da geçerli): zone_ref="nearest_resistance", evidence=["structure:nearest_resistance"]
       Aynı kural zone_ref="nearest_support" + "structure:nearest_support" için simetrik olarak geçerlidir.
19. MARKET CONTEXT SYNTHESIS V6 (Structure V2 araştırma serisinin onaylanan SADECE 4 fact katmanı — R1-R4/P3/S1-S4/major-trend/swing-range/range_position/I2-as-invalidation gibi adaylar KESİN OLARAK REDDEDİLDİ, bunları hiç kullanma/icat etme): Görevin bu 4 katmanı BİRLİKTE yorumlayıp "coin şu anda teknik olarak ne yapıyor?" sorusuna kanıt-temelli, en faydalı kısa açıklamayı üretmek — yalnız fact'leri sıralamak değil.
    a) FACT HİYERARŞİSİ (4 katman, KARIŞTIRMA):
       - CURRENT CONFIRMED STRUCTURE (structure_context.trend_structure): yalnız BU ANKİ TSE okuması. "Mevcut teyit edilmiş 1 saatlik yapı" de — ASLA "major trend", "higher-order trend", "ana trend" deme (ikinci/major bir ölçek YOK, bu araştırmayla kapatıldı).
       - RECENT PRICE ACTION (structure_context.recent_price_action): yalnız son birkaç KAPANMIŞ mumun kısa vadeli davranışı. Trend DEĞİLDİR.
       - MARKET MAP (structure_context.market_map): canlı fiyatın aktif destek/direnç ve kaybedilmiş/aşılmış zone'lara göre GÜNCEL KONUMU. Trend DEĞİLDİR.
       - STRUCTURAL REFERENCE (structure_context.structural_reference, varsa): confirmed_structure=bullish ise son confirmed HL, =bearish ise son confirmed LH -- "type"/"price"/"bar_index" alanları taşır. Yalnız "izlenebilecek yapısal referans/zayıflama referansı" — KESİNLİKLE "invalidation seviyesi", "stop-loss", "bu seviye kırılırsa trend/kesin bozulur" gibi kesinlik iddiası taşıyan bir dille SUNMA (araştırma bunu doğrulamadı — breach ile structure-change arasında güçlü ama GECİKMELİ, asla eşzamanlı olmayan bir ilişki bulundu). Güvenli dil: "yapısal referans", "zayıflama açısından izlenebilir", "korunması mevcut yapıyla uyumludur". "structure:structural_reference" evidence token'ı YALNIZ context'te structural_reference gerçekten doluysa kullanılabilir; fiyatı KENDİN ÜRETME/YUVARLAMA, yalnız context'teki "price" değerini aynen kullan.
    b) YAPI+RECENT GÜVENLİ ŞABLONLAR (yalnız gerçekten geçerliyse, harfiyen bu çerçevede — "trend döndü"/"reversal confirmed" YASAK):
       confirmed bullish + recent positive → "Mevcut teyit edilmiş yapı ve son kısa vadeli hareket aynı yönde."
       confirmed bullish + recent negative → "Mevcut teyit edilmiş bullish yapı korunurken kısa vadede geri çekilme/baskı görülüyor."
       confirmed bearish + recent positive → "Mevcut teyit edilmiş bearish yapı sürerken kısa vadeli toparlanma/karşı hareket görülüyor."
       confirmed bearish + recent negative → "Mevcut teyit edilmiş bearish yapı ile son kısa vadeli hareket aynı yönde."
       range veya recent mixed/unknown → "Yönlü teyit şu an sınırlı/karışık" (net bir yön iddiası ÜRETME).
    c) MARKET MAP SENTEZİ: yapı+recent sentezinden SONRA, gerçekten destekleniyorsa fiyatın market_map'teki konumunu ekle (aktif direnç altında/aktif destek üzerinde/zone test ediliyor/kaybedilmiş eski destek/aşılmış eski direnç — madde 18a'daki güvenli dille). Örnek birleşimler: recent pozitif + üstte aktif direnç varsa → "...ancak fiyatın önünde aktif direnç bulunuyor"; recent negatif + altta aktif destek varsa → "...ancak aşağıda aktif destek bulunuyor". Rol tersine dönüşünü (lost_support/broken_resistance) ASLA "confirmed support/resistance" gibi kesin bir yeni sınıf olarak ilan etme (madde 18a zaten bunu yasaklıyor).
    d) BİLGİ HİYERARŞİSİ SIRASI (aynı bilgiyi 3 farklı yerde yeniden söyleme): (1) current confirmed structure, (2) recent price action ile uyum/çatışma, (3) live market map konumu, (4) yakındaki aktif engel/destek, (5) varsa lost/broken tarihsel referans, (6) structural reference (HL/LH), (7) technical_watchpoints (ayrı görev: bu bölüm "şu an ne oluyor" değil "bundan sonra ne izlenmeli" sorusuna cevap verir — aynı gözlemi iki bölümde tekrar etme, watchpoints yalnız İLERİYE dönük tetikleyicilere odaklanır).
    e) KESİNLİKLE ÜRETME: major/higher-order trend, "trend reversal confirmed", long/short setup, entry/TP/SL/R:R, FVG, order block, liquidity sweep, Quasimodo, yeni support/resistance seviyesi, yeni fiyat hedefi, swing-range/range_position (bu kavramlar bu turda kesin olarak reddedildi, mevcut değil)."""

AI_ANALYST_NORMAL_INSTRUCTION = """Bu analiz için VETO YOK ve motor işlem-yönlü değerlendirmeyi kapatmıyor. NORMAL şemayı kullan.

Görevin "kaç YES kaç NO var" diye saymak değil, hangi faktörlerin BİRLİKTE ne anlattığını yorumlamaktır. Yalnız gerçekten önemli faktörleri seç, her yes/no'yu mekanik listeleme. entry_assessment'ı temelde ema50_distance, price_change_24h, rsi_zone faktörlerinden (usable olanlardan) kur — context'te structure_context varsa nearest_support/nearest_resistance'a fiyat yakınlığını da (Sistem Promptu madde 13'e göre, yalnız verilen distance_pct'i aynen kullanarak) bu değerlendirmeye katabilirsin. reassessment_triggers için sistem promptundaki REASSESSMENT TRIGGER SEÇİM POLİTİKASI'na (selection_basis kuralları) harfiyen uy. TEK bir baskın anlatı oluştur — "eğer X olursa long, Y olursa short" tarzı çift yönlü kaçış senaryosu üretme. verdict_title/confidence'ın ton sınırlarına sadık kal. technical_watchpoints için Sistem Promptu madde 15'e uy. Sistem Promptu madde 16'daki rol ayrımı ve erken-sinyal/teyit ayrımı NORMAL modda da geçerli — supporting_factors/risks_conflicts arasında çelişkili faktörleri (ör. RSI olumlu ama structure/momentum olumsuz) tek tek yeniden listeleme, aralarındaki ilişkiyi/sentezi açıkla. Sistem Promptu madde 17 (özellikle 17e) de NORMAL modda geçerli — recent_price_action mevcutsa ve confirmed_structure/momentum ile anlamlı bir ilişkisi varsa bunu supporting_factors/risks_conflicts sentezine dahil et."""

AI_ANALYST_HARD_RESTRICTED_INSTRUCTION = """Bu analiz için motor işlemi reddetmiş veya genel güven açısından yetersiz durumda (veto/verdict="Elenir"/risk verisi hiç yok/genel güven yetersiz). HARD_RESTRICTED şemayı kullan.

why_rejected_or_limited.decision_evidence alanına yalnız sağlanan whitelist'ten değer koy. evidence alanına yalnız market factor_id — ikisini karıştırma. Risk verisi eksik/yetersizse bunu "piyasa negatif" diye YORUMLAMA — yalnız "işlem uygunluğu güvenilir biçimde değerlendirilemiyor" çerçevesinde kal, BİR KEZ belirt. main_scenario, entry_assessment, reassessment_triggers gibi işlem yönü çağrıştıran hiçbir alan bu şemada YOK — üretme.

technical_watchpoints ÖNEMLİ: motor bu işlemi reddetmiş/kısıtlamış olması technical_watchpoints'i boş bırakmak için bir sebep DEĞİLDİR — context'te structure_context (ok) ve/veya usable_factors mevcutsa, "işlem uygunluğu doğrulanamıyor, PEKİ teknik açıdan ne izlenmeli" sorusuna Sistem Promptu madde 15'e göre cevap ver (KOMPAKT — 3 öğe hedefi). Bu asla ret kararını geçersiz kılan bir öneri değildir, yalnız deterministik teknik bağlamdır.

KOMPAKTLIK VE ROL AYRIMI — decisive_factors/positive_but_insufficient_factors (bkz. Sistem Promptu madde 16b/16c, harfiyen uy): decisive_factors YALNIZ veto/reddi tetikleyen faktör(ler)i adlandırır, tekrar açıklamaz. positive_but_insufficient_factors ise coin'in KENDİ teknik görünümünün (veto faktöründen bağımsız) SENTEZİDİR — yalnız pozitif faktörleri listeleme, coin'in kendi destekleyici VE teyit etmeyen/olumsuz usable_factors'ını BİRLİKTE değerlendirip tek bir sentez üret. Benzer semantic_role'deki faktörleri (ör. macd_signal+obv_trend+bollinger_squeeze_or_break) AYRI AYRI cümlelerde anlatma — TEK cümlede birlikte özetle, evidence dizisinde hepsini TUT (evidence kaybı YASAK, yalnız METİN kısalsın). "ama veto koşulunu geçersiz kılmaz" gibi kapanış cümlesini HER öğede tekrar etme — yalnız bir kez, en sonda söylemek yeterli.

RECENT PRICE ACTION (Sistem Promptu madde 17, ÖZELLİKLE 17e ZORUNLU): structure_context.recent_price_action mevcutsa ve confirmed_structure/momentum ile anlamlı bir ilişkisi varsa, bunu YUKARIDAKİ positive_but_insufficient_factors sentezine dahil et (ayrı bir alan/cümle grubu değil, aynı sentezin parçası) — coin'in kendi teknik görünümü artık yalnız "hangi factor'ler destekliyor/desteklemiyor" değil, "confirmed yapı ile son saatlerin gerçek fiyat davranışı birbirine göre nasıl konumlanıyor" sorusunu da kapsar."""

AI_ANALYST_LIMITED_INSTRUCTION = """Bu analiz için motor REDDETMEDİ (veto yok, verdict "Elenir" değil, risk verisi kısmen mevcut, genel güven yeterli) — ama bağımsız risk boyutu sayısı karar güvenilirliği için yetersiz olduğu için motor genel işlem uygunluğunu doğrulayamıyor. LIMITED şemayı kullan.

Temel sözleşme: "Analitik yorum yapılabilir; genel işlem uygunluğu risk verisi kapsamı yetersiz olduğu için doğrulanamaz." Bu ayrım iki AYRI alan üzerinden yapısal olarak sağlanır — doğal dilde belirli bir cümleyi doğru yazmana bağlı DEĞİL:

- entry_assessment: YALNIZ teknik giriş zamanlamasını açıklar (ema50_distance/price_change_24h/rsi_zone faktörlerinden, usable olanlardan). İşlem uygunluğu/onayı/long/short/al/sat/entry önerisi kelimelerini veya bu anlama gelen hiçbir ifadeyi KULLANMA — bu konudaki tüm sınır zaten ayrı risk_data_notice alanında taşınıyor, burada tekrar etme veya karıştırma.
- risk_data_notice: LIMITED modun TEK ve zorunlu güvenlik sınırı alanıdır. decision_evidence alanına SADECE ["decision:risk_coverage_insufficient"] değerini koy, başka hiçbir değer geçersizdir.

overall/supporting_factors/risks_conflicts/reassessment_triggers alanlarında görevin, hangi faktörlerin BİRLİKTE ne anlattığını yorumlamak — mekanik yes/no listelemesi değil (bkz. Sistem Promptu madde 16, rol ayrımı ve erken-sinyal/teyit ayrımı, VE madde 17 — recent_price_action anlamlıysa 17e'ye göre supporting_factors/risks_conflicts'e dahil et). reassessment_triggers için sistem promptundaki REASSESSMENT TRIGGER SEÇİM POLİTİKASI'na (selection_basis kuralları) harfiyen uy; meaning'de risk_data_notice'ın sınırını aşan hiçbir ifade ("işlem uygunluğu artık geçerli" gibi) kullanma. "eğer X olursa long, Y olursa short" tarzı çift yönlü kaçış senaryosu üretme, yeni entry/TP/SL/destek/direnç üretme.

technical_watchpoints için Sistem Promptu madde 15'e uy — reassessment_triggers'tan AYRI ve BAĞIMSIZ bir alandır (reassessment_eligible_factors kısıtına tabi DEĞİLDİR, structure_context de kullanabilir)."""


def _ai_analyst_narrative_item_schema():
    return {"type": "object", "properties": {
        "text": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    }, "required": ["text", "evidence"]}


def _ai_analyst_reassessment_item_schema():
    """reassessment_triggers öğesi — current_state_text/target_state_text
    KASITLI OLARAK YOK: bu metinler Claude tarafından ÜRETİLMEZ, GUI
    render aşamasında FACTOR_TRANSITION_GUARDS'tan deterministik olarak
    türetilir. Claude yalnız hangi factor(lar)ın hangi status'ta/status
    geçişinde olduğunu (conditions) ve bunun analiz açısından anlamını
    (meaning) belirtir. condition alanları (status / from_status /
    to_status) type'a göre hangi kombinasyonun zorunlu olduğu validator
    tarafında uygulanır (hold->status, improve/deteriorate->from_status+
    to_status) — tool schema seviyesinde hepsi optional bırakılıyor."""
    condition_item = {"type": "object", "properties": {
        "factor_id": {"type": "string"},
        "status": {"type": "string"},
        "from_status": {"type": "string"},
        "to_status": {"type": "string"},
    }, "required": ["factor_id"]}
    return {"type": "object", "properties": {
        "type": {"type": "string", "enum": ["hold", "improve", "deteriorate"]},
        "selection_basis": {"type": "string",
                             "enum": ["decision_critical", "resolves_conflict", "protects_support"]},
        "conditions": {"type": "array", "items": condition_item},
        "meaning": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    }, "required": ["type", "selection_basis", "conditions", "meaning", "evidence"]}


def _ai_analyst_reassessment_selection_schema():
    """MODEL C (AŞAMA 2 — deterministic candidate generation + AI selection):
    _ai_analyst_reassessment_item_schema()'nın YERİNİ ALIR (o fonksiyon
    yalnız referans/tarihsel karşılaştırma için kodda bırakıldı, artık hiçbir
    tool schema'da kullanılmıyor). Claude artık type/selection_basis/
    conditions/evidence İCAT ETMEZ — bunların TÜMÜ context'te ayrıca
    verilen reassessment_candidates listesinden, backend tarafında ZATEN
    geçerli olarak üretilmiştir. Claude yalnız hangi candidate_id'nin
    analitik olarak en önemli/en anlamlı olduğuna karar verir ve kısa bir
    meaning yazar — factor kombinasyonu, semantic_role, status geçişi gibi
    hiçbir mekanik detayı KENDİSİ hesaplamaz."""
    return {"type": "object", "properties": {
        "candidate_id": {"type": "string"},
        "meaning": {"type": "string"},
    }, "required": ["candidate_id", "meaning"]}


def _ai_analyst_watchpoint_item_schema():
    """TRADER UTILITY / WATCHPOINTS V2: technical_watchpoints öğesi.
    zone_ref KASITLI OLARAK bir fiyat/seviye DEĞİL, yalnız context'teki
    nearest_support/nearest_resistance zone'una bir REFERANS anahtarıdır
    -- reassessment_triggers'daki current_state_text/target_state_text ile
    AYNI ilke: gerçek fiyat aralığı Claude tarafından asla yazılmaz, GUI
    render aşamasında context'ten deterministik doldurulur. Bu, doğal dil
    içinden regex ile fiyat yakalamaya dayanan kırılgan bir sisteme
    gerek bırakmadan "context dışı seviye" uydurmayı yapısal olarak
    imkansız kılar (validator zone_ref'i context'teki gerçek zone
    varlığıyla mekanik olarak eşleştirir, bkz. validate_ai_analyst_response)."""
    return {"type": "object", "properties": {
        "category": {"type": "string", "enum": ["current_structure", "strengthens", "weakens"]},
        "meaning": {"type": "string"},
        "zone_ref": {"type": "string", "enum": ["nearest_support", "nearest_resistance"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
    }, "required": ["category", "meaning", "evidence"]}


def _ai_analyst_normal_tool_schema():
    item = _ai_analyst_narrative_item_schema()
    reassessment_item = _ai_analyst_reassessment_selection_schema()
    watchpoint_item = _ai_analyst_watchpoint_item_schema()
    return {
        "name": "return_analysis",
        "description": "NORMAL modda AI Analist yapılandırılmış yanıtı.",
        "input_schema": {
            "type": "object",
            "properties": {
                "overall": item,
                "supporting_factors": {"type": "array", "items": item},
                "risks_conflicts": {"type": "array", "items": item},
                "entry_assessment": {"anyOf": [item, {"type": "null"}]},
                "reassessment_triggers": {"type": "array", "items": reassessment_item},
                "technical_watchpoints": {"type": "array", "items": watchpoint_item},
                "data_limitations": {"type": "array", "items": item},
                "used_factors": {"type": "array", "items": {"type": "string"}},
                "mentioned_unavailable_factors": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["overall", "supporting_factors", "risks_conflicts", "entry_assessment",
                         "reassessment_triggers", "technical_watchpoints", "data_limitations",
                         "used_factors", "mentioned_unavailable_factors"],
        },
    }


def _ai_analyst_hard_restricted_tool_schema():
    item = _ai_analyst_narrative_item_schema()
    watchpoint_item = _ai_analyst_watchpoint_item_schema()
    why_item = {"type": "object", "properties": {
        "text": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "decision_evidence": {"type": "array", "items": {"type": "string"}},
    }, "required": ["text", "evidence", "decision_evidence"]}
    return {
        "name": "return_analysis",
        "description": "HARD_RESTRICTED modda AI Analist yapılandırılmış yanıtı.",
        "input_schema": {
            "type": "object",
            "properties": {
                "overall": item,
                "why_rejected_or_limited": why_item,
                "decisive_factors": {"type": "array", "items": item},
                "positive_but_insufficient_factors": {"type": "array", "items": item},
                "technical_watchpoints": {"type": "array", "items": watchpoint_item},
                "data_limitations": {"type": "array", "items": item},
                "used_factors": {"type": "array", "items": {"type": "string"}},
                "mentioned_unavailable_factors": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["overall", "why_rejected_or_limited", "decisive_factors",
                         "positive_but_insufficient_factors", "technical_watchpoints", "data_limitations",
                         "used_factors", "mentioned_unavailable_factors"],
        },
    }


def _ai_analyst_limited_tool_schema():
    item = _ai_analyst_narrative_item_schema()
    reassessment_item = _ai_analyst_reassessment_selection_schema()
    watchpoint_item = _ai_analyst_watchpoint_item_schema()
    notice_item = {"type": "object", "properties": {
        "text": {"type": "string"},
        "decision_evidence": {"type": "array", "items": {"type": "string"}},
    }, "required": ["text", "decision_evidence"]}
    return {
        "name": "return_analysis",
        "description": "LIMITED modda AI Analist yapılandırılmış yanıtı.",
        "input_schema": {
            "type": "object",
            "properties": {
                "overall": item,
                "supporting_factors": {"type": "array", "items": item},
                "risks_conflicts": {"type": "array", "items": item},
                "entry_assessment": {"anyOf": [item, {"type": "null"}]},
                "reassessment_triggers": {"type": "array", "items": reassessment_item},
                "technical_watchpoints": {"type": "array", "items": watchpoint_item},
                "risk_data_notice": notice_item,
                "data_limitations": {"type": "array", "items": item},
                "used_factors": {"type": "array", "items": {"type": "string"}},
                "mentioned_unavailable_factors": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["overall", "supporting_factors", "risks_conflicts", "entry_assessment",
                         "reassessment_triggers", "technical_watchpoints", "risk_data_notice", "data_limitations",
                         "used_factors", "mentioned_unavailable_factors"],
        },
    }


_ai_analyst_anthropic_client = None
_ai_analyst_anthropic_client_lock = threading.Lock()


def _get_ai_analyst_anthropic_client():
    """AIAnalystClient.request_analysis() için PAYLAŞILAN, tembel (lazy)
    başlatılan Anthropic client. Birden fazla AIAnalystWorker (QThread) aynı
    anda çalışabildiği için (kullanıcı art arda birkaç coin analiz edebilir)
    bu client GERÇEK eşzamanlı kullanım altında da güvenli olmalı -- httpx.Client
    resmi olarak thread-safe'tir ve bu proje kapsamında gerçek eşzamanlı testle
    (4 thread, aynı client, gerçek API çağrısı) ayrıca doğrulandı. NewsRiskFetcher
    ile AYNI Anthropic host'a gitse de KASITLI olarak AYRI bir client (bkz.
    _get_news_anthropic_client) -- iki özelliği birbirine bağlamamak için."""
    global _ai_analyst_anthropic_client
    if _ai_analyst_anthropic_client is None:
        with _ai_analyst_anthropic_client_lock:
            if _ai_analyst_anthropic_client is None:
                import anthropic
                _ai_analyst_anthropic_client = anthropic.Anthropic(
                    api_key=ANTHROPIC_API_KEY, timeout=60.0, max_retries=0)
    return _ai_analyst_anthropic_client


class AIAnalystClient:
    """NewsRiskFetcher.classify_risk()'ten TAMAMEN ayrı, kendi model
    config'i (AI_ANALYST_MODEL) üzerinden çalışan Anthropic entegrasyonu.
    Forced tool-use ile yapılandırılmış JSON garantilenir (anthropic SDK
    >=0.120: response.content[0].input zaten parse edilmiş dict)."""

    @staticmethod
    def request_analysis(context: dict, mode: str) -> Optional[dict]:
        if not ANTHROPIC_API_KEY:
            return None
        _t_start = time.time()
        try:
            # Sınırlı (bounded) timeout + SDK'nın kendi otomatik retry'ı KAPALI
            # (max_retries=0): bu, worker'ın QThread.run()'ının en kötü durumda
            # bile bu süre + küçük bir işleme payı içinde kesin olarak
            # DÖNECEĞİNİN garantisidir — kapanış sırasında terminate() gibi sert
            # bir mekanizmaya gerek kalmadan, yalnızca normal tamamlanmayı
            # bekleyerek güvenli shutdown'ı mümkün kılar. max_retries=0 olmadan
            # SDK varsayılanı (2) sessizce iç retry yapabiliyor -- her deneme
            # kendi 60s timeout'una kadar sürebildiği için toplamda 180s'e kadar
            # çıkan, hiçbir hata görünmeyen bir gecikme yaşanabiliyordu (aynı kök
            # neden, NewsRiskFetcher.classify_risk()'te daha önce düzeltilmişti).
            # Client artık PAYLAŞILAN/persistent (bkz. _get_ai_analyst_anthropic_client)
            # -- birden fazla AIAnalystWorker aynı client'ı güvenle paylaşabilir
            # (gerçek eşzamanlı testle doğrulandı), bağlantı havuzu worker'lar
            # arasında korunur.
            client = _get_ai_analyst_anthropic_client()
            _t_import = time.time()
            tool_by_mode = {
                "NORMAL": _ai_analyst_normal_tool_schema,
                "LIMITED": _ai_analyst_limited_tool_schema,
                "HARD_RESTRICTED": _ai_analyst_hard_restricted_tool_schema,
            }
            instruction_by_mode = {
                "NORMAL": AI_ANALYST_NORMAL_INSTRUCTION,
                "LIMITED": AI_ANALYST_LIMITED_INSTRUCTION,
                "HARD_RESTRICTED": AI_ANALYST_HARD_RESTRICTED_INSTRUCTION,
            }
            tool = tool_by_mode[mode]()
            mode_instruction = instruction_by_mode[mode]
            payload = {
                "decision_context": context["decision_context"],
                "usable_factors": context["usable_factors"],
                "unavailable_factors": context["unavailable_factors"],
            }
            # MODEL C (AŞAMA 2): reassessment_eligible_factors/preferred_decision_
            # critical_factor artık Claude'a AYRI gönderilmiyor -- bu bilgiler
            # zaten reassessment_candidates listesinin İÇİNE, backend tarafında
            # deterministik olarak gömülü (yalnız NORMAL/LIMITED'de anlamlı,
            # HARD_RESTRICTED'te reassessment_triggers hiç yok).
            if mode in ("NORMAL", "LIMITED"):
                payload["reassessment_candidates"] = context["reassessment_candidates"]
            # TECHNICAL STRUCTURE ENGINE → AI ANALYST INTEGRATION V1: yalnız
            # gerçekten hesaplanmışsa (None değilse) payload'a eklenir --
            # prompt boyutunu gereksiz büyütmemek için hiç yoksa alan
            # atlanır (structure_context=None göndermek yerine).
            if context.get("structure_context") is not None:
                payload["structure_context"] = context["structure_context"]
                payload["valid_structure_evidence"] = context["_valid_structure_evidence"]
            extra = ("\n\nused_factors ALANI HAKKINDA ÇOK ÖNEMLİ KURAL: "
                      "used_factors, sana verilen usable_factors listesinin TAMAMI DEĞİLDİR. "
                      "used_factors yalnızca, narrative alanlarının (overall, "
                      "supporting_factors/decisive_factors, risks_conflicts, "
                      "positive_but_insufficient_factors, entry_assessment, "
                      "technical_watchpoints, why_rejected_or_limited.evidence) evidence "
                      "dizilerine GERÇEKTEN koyduğun factor_id'lerin VE 'structure:*' token'larının "
                      "TAM VE YALNIZCA birleşimidir — 'structure:*' token'ları used_factors'tan "
                      "İSTİSNA DEĞİLDİR, factor_id'lerle BİREBİR AYNI KURALA tabidir: bir "
                      "'structure:*' token'ını (ör. structure:trend_structure) yukarıdaki "
                      "alanlardan HERHANGİ BİRİNİN evidence dizisinde kullandıysan, onu da "
                      "used_factors'a EKLEMEK ZORUNDASIN (unutma — bu en sık yapılan hatadır). "
                      "Metninde atıfta bulunmadığın hiçbir factor_id/'structure:*' token'ını "
                      "used_factors'a ekleme — usable_factors'taki her faktörü kullanmak "
                      "ZORUNDA değilsin, yalnız GERÇEKTEN kullandıklarını (factor_id'ler VE "
                      "structure token'ları dahil) listele. "
                      "data_limitations'ın evidence'ı BU LİSTEYE DAHİL DEĞİLDİR — data_limitations'ta "
                      "kullandığın factor_id'ler used_factors'a DEĞİL, ayrıca ve yalnızca "
                      "mentioned_unavailable_factors'a eklenir; bir factor_id'yi yalnız "
                      "data_limitations'ta kullandıysan (başka hiçbir narrative alanda değil) "
                      "used_factors'a KOYMA.\n"
                      "evidence dizileri (decision_evidence HARİÇ) yalnızca usable_factors'taki "
                      "ham factor_id string'lerini VE (yalnız context'te varsa) aşağıda ayrıca "
                      "verilen valid_structure_evidence listesindeki 'structure:*' token'larını "
                      "içerebilir — 'decision:' veya 'veto:' önekli hiçbir değer normal evidence "
                      "dizisine giremez, bunlar YALNIZCA why_rejected_or_limited.decision_evidence / "
                      "risk_data_notice.decision_evidence içinde kullanılır.")
            if context.get("structure_context") is not None:
                extra += ("\n\nSTRUCTURE/PRICE-ACTION/MARKET-MAP EVIDENCE TOKEN'LARI: structure_context'e "
                          "(analysis_price, closed_candle_price, nearest_support/resistance, recent_price_action, "
                          "market_map, structural_reference dahil) dayanan bir iddia kullanırsan evidence dizisine "
                          "şu listeden İLGİLİ token'ı ekle, başka hiçbir 'structure:'/'price_action:'/'market_map:' değeri "
                          "GEÇERSİZDİR:\n"
                          + json.dumps(context["_valid_structure_evidence"], ensure_ascii=False) +
                          "\nBu listede olmayan bir alan (ör. nearest_support context'te None ise "
                          "'structure:nearest_support' bu listede YOKTUR) için o yönde hiçbir iddia/"
                          "seviye üretme. nearest_support_relation/nearest_resistance_relation KENDİLERİ "
                          "evidence token'ı DEĞİLDİR (yalnız ilgili 'structure:nearest_support'/"
                          "'structure:nearest_resistance' token'ı kullanılır) — relation yalnız SENİN kendi "
                          "cümleni doğru zamanda kurman için verilen yardımcı bir fact'tir.")
            if mode in ("NORMAL", "LIMITED"):
                extra += ("\n\nreassessment_triggers ALANI HAKKINDA ÇOK ÖNEMLİ KURAL (MODEL C — "
                          "candidate seçimi): reassessment_triggers'ta ARTIK TRIGGER OBJESİ KURMUYORSUN. "
                          "Yalnız yukarıda context'te ayrıca verilen reassessment_candidates listesinden "
                          "candidate_id seç. Her seçim öğesi TAM OLARAK şu iki alanı taşır, başka HİÇBİR "
                          "alan YAZMA: {\"candidate_id\": \"...\", \"meaning\": \"...\"} — type/"
                          "selection_basis/conditions/evidence/factor_id/semantic_role gibi hiçbir mekanik "
                          "detayı SEN hesaplamazsın, hepsi candidate'in kendisinde zaten hazır. "
                          "candidate_id AYNEN reassessment_candidates listesindeki bir candidate_id "
                          "olmalı — kendi candidate_id'ni ASLA uydurma. "
                          "Her candidate'in \"selection_basis\" alanına göre: \"resolves_conflict\" "
                          "candidate'i seçtiysen o candidate'in factor_id'si AYNI CEVABINDAKİ "
                          "risks_conflicts'in evidence dizisinde GERÇEKTEN geçmeli; \"protects_support\" "
                          "candidate'i seçtiysen candidate'in TÜM factor_id'leri AYNI CEVABINDAKİ "
                          "supporting_factors'in evidence dizisinde GERÇEKTEN geçmeli (bu factor'ları "
                          "önce ilgili narrative alanında GERÇEKTEN kullanmış olman gerekir). "
                          "SEMANTIC_ROLE ÇAKIŞMASI YASAK: seçtiğin candidate'lerin (decision_critical "
                          "hariç) \"semantic_roles\" listelerine bak — iki candidate arasında AYNI role "
                          "bir kez bile tekrar edemez, candidate'lerin roles alanlarını karşılaştırıp "
                          "kesişenlerden yalnız birini seç (bu bilgi candidate'te ZATEN hesaplanmış, "
                          "senin ayrıca semantic_role çıkarsaman GEREKMİYOR, yalnız verilen alanı oku). "
                          "TOPLAM 1-4 candidate seç, hedef 2-4 — sayı doldurmak için zayıf/marjinal bir "
                          "candidate SEÇME, gerçekten yalnız 1-2 güçlü aday varsa yalnız onları seç.")
            if mode == "HARD_RESTRICTED":
                extra += ("\n\nwhy_rejected_or_limited.decision_evidence alanında SADECE şu "
                          "değerlerden birini/birkaçını kullanabilirsin, başka hiçbir değer "
                          "GEÇERSİZDİR (kendi adlandırmanı icat etme, aşağıdaki listeden birebir kopyala):\n"
                          + json.dumps(context["_valid_decision_evidence"], ensure_ascii=False))
                # MODEL D: yalnız decision_context.model_d_restricted_candidate=true
                # iken eklenir -- yeni bir AI mode/schema alanı DEĞİL, MEVCUT
                # HARD_RESTRICTED şemasının (positive_but_insufficient_factors)
                # narrative'ine ince bir kural ekler.
                if context["decision_context"].get("model_d_restricted_candidate"):
                    extra += (
                        "\n\nMODEL D — DAR İSTİSNA DURUMU HAKKINDA ÖNEMLİ KURAL: "
                        "decision_context.model_d_restricted_candidate=true. Bu, offline "
                        "araştırmayla doğrulanmış dar bir istisna koşulunun (BTC günlük "
                        "trendi ayı + coin'in teyit edilmiş 1H yapısı hâlâ bearish + "
                        "coin'in KISA VADELİ fiyat davranışı toparlanma yönünde + BTC "
                        "GÜÇLÜ düşüş rejiminde DEĞİL) oluştuğu anlamına gelir. Bunu "
                        "positive_but_insufficient_factors alanında (structure_context/"
                        "recent_price_action'a dayanarak, mevcut evidence token kurallarıyla) "
                        "EN AZ BİR CÜMLEYLE ele al. Beş noktayı ayır: (1) BTC günlük ayı → "
                        "piyasa riski sürüyor, (2) coin'in teyit edilmiş 1H yapısı hâlâ zayıf, "
                        "(3) kısa vadeli fiyat davranışı toparlanma gösteriyor, (4) BTC güçlü "
                        "düşüş rejiminde değil → araştırmayla doğrulanmış dar istisna koşulu "
                        "mevcut, (5) sonuç işlem onayı DEĞİL, yalnız yüksek riskli izleme. "
                        "TERMİNOLOJİ AYRIMI (kullanıcı 'Elenir' ile 'Yüksek Riskli İzle'yi "
                        "karıştırmasın diye): metninde bu ikisini AÇIKÇA iki farklı katman "
                        "olarak ayır -- 'deterministik motor kararı: Elenir' (değişmeyen "
                        "gerçek) ile 'politika durumu: yüksek riskli izleme' (Model D'nin "
                        "additive, karar-değiştirmeyen izleme statüsü) aynı cümlede veya "
                        "art arda İKİ ayrı kavram olarak geçsin -- ama bunu metnin birden "
                        "fazla yerinde UZUN UZUN TEKRARLAMA, TEK KEZ net şekilde ayır yeter. "
                        "KESİNLİKLE ŞUNU DEME: 'veto kalktı', 'long sinyali', 'bullish "
                        "reversal confirmed', 'bullish dönüş teyit edildi', 'trend döndü', "
                        "'işlem uygun', 'alım fırsatı', 'alınabilir', 'motor kararı değişti' "
                        "-- bu motorun reddetme kararını (verdict='Elenir') DEĞİŞTİRMEZ, "
                        "yalnız coin'in tamamen göz ardı edilmemesi gerektiğini ifade eder.")
            elif mode == "LIMITED":
                extra += ("\n\nrisk_data_notice.decision_evidence alanında SADECE şu değeri "
                          "kullanabilirsin, başka hiçbir değer GEÇERSİZDİR:\n"
                          + json.dumps(["decision:risk_coverage_insufficient"], ensure_ascii=False))
            user_content = (mode_instruction + extra + "\n\nCONTEXT:\n" +
                             json.dumps(payload, ensure_ascii=False, indent=2))
            _t_prep = time.time()
            print(f"[AI ANALIST][TIMING] import+client kurulumu: {_t_import - _t_start:.2f}s, "
                  f"prompt hazirlama: {_t_prep - _t_import:.2f}s, "
                  f"user_content uzunlugu: {len(user_content)} karakter", flush=True)
            resp = client.messages.create(
                model=AI_ANALYST_MODEL,
                # TRADER UTILITY / WATCHPOINTS V2: technical_watchpoints alanı
                # (özellikle NORMAL/LIMITED gibi zaten en dolu şemalarda) çıktı
                # uzunluğunu artırdı -- gerçek testte LIMITED bir yanıt eski
                # 3000 sınırında kesilip reddedildi. 3600'e çıkarıldı (küçük,
                # additive headroom -- prompt'ta zaten TEKRARSIZLIK/kısalık
                # kuralları var, amaç limiti gevşetmek değil kesilmeyi önlemek).
                max_tokens=3600,
                system=AI_ANALYST_SYSTEM_PROMPT,
                tools=[tool],
                tool_choice={"type": "tool", "name": "return_analysis"},
                messages=[{"role": "user", "content": user_content}],
            )
            _t_call = time.time()
            usage = getattr(resp, "usage", None)
            print(f"[AI ANALIST][TIMING] client.messages.create() suresi: {_t_call - _t_prep:.2f}s "
                  f"(input_tokens={getattr(usage, 'input_tokens', '?')}, "
                  f"output_tokens={getattr(usage, 'output_tokens', '?')}) "
                  f"TOPLAM request_analysis: {_t_call - _t_start:.2f}s", flush=True)
            if resp.stop_reason == "max_tokens":
                print("[AI ANALIST] Yanıt max_tokens'ta kesildi, güvenilmez sayılıp reddedildi.", flush=True)
                return None
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    return block.input
            print(f"[AI ANALIST] tool_use block missing (stop_reason={resp.stop_reason!r})", flush=True)
            return None
        except Exception as e:
            print(f"[AI ANALIST][TIMING] hata ONCESI gecen sure: {time.time() - _t_start:.2f}s", flush=True)
            print(f"[AI ANALIST] API hatası: {e}", flush=True)
            return None

    @staticmethod
    def request_corrective_retry(context: dict, mode: str, original_raw: dict,
                                  top_reason: str, diag: dict) -> Optional[dict]:
        """CONTROLLED RELIABILITY FIX V1: dar bir whitelist'teki (bkz.
        AI_ANALYST_RETRY_WHITELIST_REASONS/_SUB_REASONS), mekanik olarak
        kesin teşhis edilmiş şema/sözleşme ihlalleri için TEK SEFERLİK (en
        fazla 1, çağıran taraf -- run_ai_analyst_with_corrective_retry --
        ikinci bir retry ASLA yapmaz) düzeltici çağrı. AI_ANALYST_SYSTEM_PROMPT
        DEĞİŞMEDEN aynen kullanılır (yeniden tasarlanmıyor) -- yalnız kullanıcı
        mesajı, önceki TAM cevabı ve validator'ın tespit ettiği EXACT mekanik
        sorunu içeren dar bir düzeltme talebine dönüşür. Yeni piyasa analizi
        İSTEMEZ, yalnız şema ihlalini düzeltmeyi ister (bkz.
        _build_corrective_retry_user_content)."""
        if not ANTHROPIC_API_KEY:
            return None
        _t_start = time.time()
        try:
            client = _get_ai_analyst_anthropic_client()
            tool_by_mode = {
                "NORMAL": _ai_analyst_normal_tool_schema,
                "LIMITED": _ai_analyst_limited_tool_schema,
                "HARD_RESTRICTED": _ai_analyst_hard_restricted_tool_schema,
            }
            tool = tool_by_mode[mode]()
            corrective_user_content = _build_corrective_retry_user_content(
                mode, context, original_raw, top_reason, diag)
            resp = client.messages.create(
                model=AI_ANALYST_MODEL,
                max_tokens=3600,
                system=AI_ANALYST_SYSTEM_PROMPT,
                tools=[tool],
                tool_choice={"type": "tool", "name": "return_analysis"},
                messages=[{"role": "user", "content": corrective_user_content}],
            )
            print(f"[AI ANALIST][RETRY][TIMING] corrective retry suresi: "
                  f"{time.time() - _t_start:.2f}s (top_reason={top_reason}, "
                  f"sub_reason={diag.get('sub_reason')})", flush=True)
            if resp.stop_reason == "max_tokens":
                print("[AI ANALIST][RETRY] Yanıt max_tokens'ta kesildi, güvenilmez sayılıp reddedildi.", flush=True)
                return None
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    return block.input
            print(f"[AI ANALIST][RETRY] tool_use block missing (stop_reason={resp.stop_reason!r})", flush=True)
            return None
        except Exception as e:
            print(f"[AI ANALIST][RETRY] API hatası: {e}", flush=True)
            return None


ENTRY_ASSESSMENT_ALLOWED_FACTOR_IDS = frozenset({"ema50_distance", "price_change_24h", "rsi_zone"})
LIMITED_RISK_DATA_NOTICE_DECISION_EVIDENCE = ["decision:risk_coverage_insufficient"]

# no<wait<yes: yalnız ScoreEngine.calculate_stage_score()'un (w_yes tam,
# w_wait yarım, w_no sıfır puan) kendi skorlama yönünü ifade eder — bir
# factor'ın içeriğinin tek-yönlü/bimodal olup olmadığından BAĞIMSIZ,
# motorun "bu geçiş puanı artırır mı azaltır mı" kararının aynısıdır.
# improve/deteriorate etiketleri bu YÖNDEN başka hiçbir şey iddia etmez.
_REASSESSMENT_STATUS_RANK = {"no": 0, "wait": 1, "yes": 2}


REASSESSMENT_SELECTION_BASES = frozenset({"decision_critical", "resolves_conflict", "protects_support"})
MAX_REASSESSMENT_TRIGGERS = 4
MAX_DECISION_CRITICAL_TRIGGERS = 1


def _generate_reassessment_candidates(usable_factors, eligible_ids, preferred_dc_factor):
    """MODEL C (AŞAMA 2 mimari kararı): Claude artık reassessment trigger
    OBJESİ kurmuyor -- burada, mevcut _check_reassessment_item/_check_
    reassessment_array kurallarından (aşağıda, DEĞİŞTİRİLMEDEN duruyor,
    defense-in-depth) BİREBİR türetilen, ZATEN geçerli bir candidate havuzu
    üretiliyor. Claude yalnız bu havuzdan candidate_id seçer (aşağıda
    validate_ai_analyst_response'ta candidate-set kontrolü). Yeni bir karar
    politikası İCAT EDİLMEDİ -- yalnız mevcut kuralların pre-validation
    (Claude'a ulaşmadan önce uygulanan) biçimi.

    KRİTİK GARANTİ: iki-faktörlü "protects_support/hold" candidate'ları
    YALNIZ farklı semantic_role'den çiftler için üretilir -- aynı role'den
    bir çift bu listede HİÇBİR ZAMAN oluşmaz (gerçek BTC/TEST_GOOD
    hatalarının tümü bu sınıftaydı, bkz. AŞAMA 2 audit raporu)."""
    factor_meta = {f["factor_id"]: f for f in usable_factors}
    candidates = []

    if preferred_dc_factor and preferred_dc_factor in factor_meta:
        meta = factor_meta[preferred_dc_factor]
        for to_status in sorted(meta.get("decision_critical_to_statuses") or []):
            candidates.append({
                "candidate_id": f"dc:{preferred_dc_factor}:{to_status}",
                "type": "deteriorate", "selection_basis": "decision_critical",
                "conditions": [{"factor_id": preferred_dc_factor, "from_status": meta.get("status"), "to_status": to_status}],
                "semantic_roles": [],  # decision_critical roles_used sayımına hiç girmez (validator'daki istisnayla birebir)
            })

    for fid in eligible_ids:
        meta = factor_meta.get(fid)
        if not meta:
            continue
        cur = meta.get("status")
        if cur not in ("no", "wait", "yes") or cur == "yes":
            continue
        role = meta.get("semantic_role")
        for to_status in ("wait", "yes"):
            if _REASSESSMENT_STATUS_RANK[to_status] <= _REASSESSMENT_STATUS_RANK[cur]:
                continue
            if fid in BOOL_TYPE_FACTOR_IDS and to_status == "wait":
                continue
            candidates.append({
                "candidate_id": f"rc:{fid}:{cur}->{to_status}",
                "type": "improve", "selection_basis": "resolves_conflict",
                "conditions": [{"factor_id": fid, "from_status": cur, "to_status": to_status}],
                "semantic_roles": [role] if role else [],
            })

    support_pool = sorted(fid for fid in eligible_ids
                           if factor_meta.get(fid, {}).get("status") == "yes")
    for fid in support_pool:
        role = factor_meta[fid].get("semantic_role")
        candidates.append({
            "candidate_id": f"ps1:{fid}",
            "type": "hold", "selection_basis": "protects_support",
            "conditions": [{"factor_id": fid, "status": "yes"}],
            "semantic_roles": [role] if role else [],
        })
    for a, b in itertools.combinations(support_pool, 2):
        role_a, role_b = factor_meta[a].get("semantic_role"), factor_meta[b].get("semantic_role")
        if role_a and role_b and role_a != role_b:
            candidates.append({
                "candidate_id": f"ps2:{a}+{b}",
                "type": "hold", "selection_basis": "protects_support",
                "conditions": [{"factor_id": a, "status": "yes"}, {"factor_id": b, "status": "yes"}],
                "semantic_roles": [role_a, role_b],
            })

    return candidates


def _check_reassessment_item(item, eligible_ids, factor_meta, risks_conflicts_evidence, supporting_factors_evidence):
    """v4 selection contract: her item hem yapısal (status/RANK/eligibility,
    v2'den beri var) hem de selection_basis'e göre narrative-linkage
    kurallarını (decision_critical/resolves_conflict/protects_support)
    sağlamalı. Tüm kontroller MEKANİK — aynı response'un evidence
    kümeleriyle üyelik (in) kontrolü, hiçbir NLP/semantic tahmin yok."""
    if not isinstance(item, dict):
        return False
    ttype = item.get("type")
    if ttype not in ("hold", "improve", "deteriorate"):
        return False
    basis = item.get("selection_basis")
    if basis not in REASSESSMENT_SELECTION_BASES:
        return False
    if basis == "decision_critical" and ttype != "deteriorate":
        return False
    if basis == "resolves_conflict" and ttype != "improve":
        return False
    if basis == "protects_support" and ttype not in ("hold", "deteriorate"):
        return False

    conditions = item.get("conditions")
    if not isinstance(conditions, list) or len(conditions) == 0:
        return False
    if ttype != "hold" and len(conditions) != 1:
        return False
    if basis == "decision_critical" and len(conditions) != 1:
        return False

    seen_factor_ids = []
    for c in conditions:
        if not isinstance(c, dict):
            return False
        fid = c.get("factor_id")
        if not isinstance(fid, str) or fid not in eligible_ids or fid in seen_factor_ids:
            return False
        seen_factor_ids.append(fid)
        meta = factor_meta.get(fid, {})
        real_status = meta.get("status")
        bool_type = fid in BOOL_TYPE_FACTOR_IDS

        if ttype == "hold":
            status = c.get("status")
            if status not in ("yes", "wait", "no") or status != real_status:
                return False
            if bool_type and status == "wait":
                return False
            if basis == "protects_support" and status != "yes":
                return False
        else:
            from_status = c.get("from_status")
            to_status = c.get("to_status")
            if from_status not in ("yes", "wait", "no") or to_status not in ("yes", "wait", "no"):
                return False
            if from_status != real_status or from_status == to_status:
                return False
            if bool_type and (from_status == "wait" or to_status == "wait"):
                return False
            rank_from, rank_to = _REASSESSMENT_STATUS_RANK[from_status], _REASSESSMENT_STATUS_RANK[to_status]
            if ttype == "improve" and not (rank_to > rank_from):
                return False
            if ttype == "deteriorate" and not (rank_to < rank_from):
                return False
            if basis == "protects_support" and (ttype != "deteriorate" or from_status != "yes"):
                return False
            if basis == "decision_critical":
                dc_statuses = meta.get("decision_critical_to_statuses") or set()
                dc_effect = meta.get("decision_critical_effect")
                if not dc_statuses or dc_effect != "veto":
                    return False
                if to_status not in dc_statuses:
                    return False

        if basis == "resolves_conflict" and fid not in risks_conflicts_evidence:
            return False
        if basis == "protects_support" and fid not in supporting_factors_evidence:
            return False

    meaning = item.get("meaning")
    if not isinstance(meaning, str) or not meaning:
        return False
    evidence = item.get("evidence")
    if not isinstance(evidence, list) or sorted(evidence) != sorted(seen_factor_ids):
        return False
    return True


def _check_reassessment_array(arr, eligible_ids, factor_meta, risks_conflicts_evidence,
                               supporting_factors_evidence, preferred_dc_factor):
    if not isinstance(arr, list) or len(arr) > MAX_REASSESSMENT_TRIGGERS:
        return False
    if not all(_check_reassessment_item(it, eligible_ids, factor_meta,
                                         risks_conflicts_evidence, supporting_factors_evidence) for it in arr):
        return False
    # Decision-critical display budget: sistemde birden fazla decision-critical
    # factor olabilir (VETO_RULES introspeksiyonu bunu doğru tespit ediyor —
    # gerçek API testinde keşfedildi), ama reassessment_triggers'ta EN FAZLA
    # MAX_DECISION_CRITICAL_TRIGGERS(=1) tanesi gösterilebilir VE bu, context
    # tarafından ÖNCEDEN deterministik seçilmiş preferred_dc_factor İLE AYNI
    # factor olmak zorunda — Claude'un kendi tercihine hiç bırakılmıyor.
    dc_items = [it for it in arr if it.get("selection_basis") == "decision_critical"]
    if len(dc_items) > MAX_DECISION_CRITICAL_TRIGGERS:
        return False
    if dc_items:
        dc_factor_id = dc_items[0]["conditions"][0]["factor_id"]
        if dc_factor_id != preferred_dc_factor:
            return False
    # Semantic_role dedup: decision_critical OLMAYAN trigger'lar arasında
    # (çoklu-condition hold içindeki TÜM factor'lar dahil) aynı role'den
    # birden fazla factor OLAMAZ. decision_critical bu sayıma hiç girmez —
    # ne bir role'ü rezerve eder ne başka bir trigger'ın aynı role'ü
    # kullanmasını engeller.
    roles_used = []
    for it in arr:
        if it.get("selection_basis") == "decision_critical":
            continue
        for c in it.get("conditions", []):
            role = factor_meta.get(c.get("factor_id"), {}).get("semantic_role")
            if role in roles_used:
                return False
            roles_used.append(role)
    return True


def _check_reassessment_selection_array(arr, candidates_by_id, eligible_ids, factor_meta,
                                         risks_conflicts_evidence, supporting_factors_evidence,
                                         preferred_dc_factor):
    """MODEL C (AŞAMA 2): Claude artık {candidate_id, meaning} seçer, tam
    trigger objesi kurmaz. Bu fonksiyon candidate_id'den TAM item'ı
    (candidate'in kendi conditions'ından, backend tarafında, deterministik
    olarak) rekonstrükte eder ve MEVCUT, HİÇ DEĞİŞTİRİLMEMİŞ
    _check_reassessment_array()'e verir -- intra/cross-trigger semantic_role
    dedup, decision_critical sayısı/kimliği, status/rank/evidence-linkage
    kontrollerinin TAMAMI defense-in-depth olarak aynen çalışmaya devam
    eder (kod tekrarı yok, eski fonksiyon tek doğruluk kaynağı kalıyor).
    Döner: (ok, reconstructed_items_or_None)."""
    if not isinstance(arr, list) or len(arr) > MAX_REASSESSMENT_TRIGGERS:
        return False, None
    seen_ids = []
    reconstructed = []
    for sel in arr:
        if not isinstance(sel, dict):
            return False, None
        cid = sel.get("candidate_id")
        meaning = sel.get("meaning")
        if not isinstance(cid, str) or cid not in candidates_by_id or cid in seen_ids:
            return False, None
        if not isinstance(meaning, str) or not meaning:
            return False, None
        seen_ids.append(cid)
        cand = candidates_by_id[cid]
        factor_ids = sorted({c["factor_id"] for c in cand["conditions"]})
        reconstructed.append({**cand, "meaning": meaning, "evidence": factor_ids})
    if not _check_reassessment_array(reconstructed, eligible_ids, factor_meta,
                                      risks_conflicts_evidence, supporting_factors_evidence, preferred_dc_factor):
        return False, None
    return True, reconstructed


_ai_validation_logger = None
_ai_validation_logger_lock = threading.Lock()


def _ai_validation_log_path() -> str:
    from pathlib import Path
    try:
        app_dir = Path(__file__).resolve().parent
    except NameError:
        app_dir = Path.cwd()
    logs_dir = app_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    return str(logs_dir / "ai_analyst_validation.log")


def _get_ai_validation_logger():
    """Yalnız validation FAIL olduğunda kullanılan, tembel başlatılan bir
    dosya logger'ı. RotatingFileHandler ile boyut sınırlı (2MB x 3 dosya) --
    ekstra bir logging framework/bağımlılık eklenmedi, stdlib'in kendi
    logging modülü kullanıldı. propagate=False -- ana konsol print() akışına
    hiç karışmaz, yalnız logs/ai_analyst_validation.log dosyasına yazar."""
    global _ai_validation_logger
    if _ai_validation_logger is None:
        with _ai_validation_logger_lock:
            if _ai_validation_logger is None:
                logger = logging.getLogger("ai_analyst_validation")
                logger.setLevel(logging.INFO)
                logger.propagate = False
                if not logger.handlers:
                    handler = RotatingFileHandler(
                        _ai_validation_log_path(), maxBytes=2_000_000, backupCount=2, encoding="utf-8")
                    handler.setFormatter(logging.Formatter("%(message)s"))
                    logger.addHandler(handler)
                _ai_validation_logger = logger
    return _ai_validation_logger


def _log_ai_validation_failure(symbol: str, request_id: str, mode: str, reason: str,
                                raw: Optional[dict], context: dict) -> None:
    """Yalnız AI Analist yanıtı validate_ai_analyst_response() tarafından
    REDDEDİLDİĞİNDE çağrılır -- başarılı yanıtlar HİÇ loglanmaz (log
    büyümesi sınırlı kalsın diye). GÜVENLİK: ANTHROPIC_API_KEY, TradingView
    kimlik bilgisi veya başka HİÇBİR secret/credential buraya yazılmaz --
    yalnız Claude'un kendi ürettiği yapılandırılmış çıktı (raw, hiçbir secret
    içermez) ve context'in factor_id özetleri loglanır. Prompt'un TAMAMI
    (system prompt, mode instruction) buraya YAZILMAZ. GUI'ye bu logun
    içeriğinden HİÇBİR ŞEY sızmaz -- yalnız teknik/dosya-seviyeli teşhis
    amaçlıdır, kullanıcı arayüzünde gösterilen nötr fail-closed mesajı
    (bkz. _on_ai_analyst_failed) bundan tamamen bağımsız ve DEĞİŞMEDİ."""
    try:
        logger = _get_ai_validation_logger()
        usable_ids = sorted(f["factor_id"] for f in (context or {}).get("usable_factors", []))
        unavailable_ids = sorted(f["factor_id"] for f in (context or {}).get("unavailable_factors", []))
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "request_id": request_id,
            "mode": mode,
            "validation_reason": reason,
            "usable_factor_ids": usable_ids,
            "unavailable_factor_ids": unavailable_ids,
            "structurally_unavailable_factor_ids": sorted(STRUCTURALLY_UNAVAILABLE_FACTOR_IDS),
            "raw_data_limitations": (raw or {}).get("data_limitations"),
            "raw_mentioned_unavailable_factors": (raw or {}).get("mentioned_unavailable_factors"),
            "raw_tool_use_input": raw,
        }
        logger.info(json.dumps(entry, ensure_ascii=False, default=str))
    except Exception as e:
        # Loglama ASLA ana akışı bozmamalı -- yalnız konsola bir not düşülür,
        # fail-closed davranış (self.failed.emit(...)) çağıran tarafta zaten
        # bu fonksiyondan bağımsız olarak devam eder.
        print(f"[AI ANALIST] validation debug log yazılamadı: {e}", flush=True)


def validate_ai_analyst_response(mode: str, raw: Optional[dict], context: dict):
    """CONTEXT+PROMPT CONTRACT v1.1 §10 sıralı doğrulama (üç modlu — rev2).
    Tamamen deterministik — LLM çağrısı yok. Herhangi bir adım başarısız
    olursa (False, None, sebep) döner; AI Analist katmanı fail-closed
    kapanır, ana deterministik rapor hiç etkilenmez."""
    if raw is None or not isinstance(raw, dict):
        return False, None, "no_response"

    usable_ids = {f["factor_id"] for f in context["usable_factors"]}
    unavailable_ids = {f["factor_id"] for f in context["unavailable_factors"]}
    valid_decision_evidence = set(context["_valid_decision_evidence"])
    reassessment_eligible_ids = set(context["reassessment_eligible_factors"])
    # TECHNICAL STRUCTURE ENGINE → AI ANALYST INTEGRATION V1: normal evidence
    # dizilerinde (reassessment_triggers.conditions HARİÇ -- o ayrı bir
    # kontrolden, _check_reassessment_array'den geçer ve reassessment_eligible_ids
    # dışına ASLA genişletilmez) factor_id'lere EK olarak "structure:*"
    # token'ları da kabul edilir -- ama SADECE context["_valid_structure_evidence"]
    # içinde GERÇEKTEN listelenenler (yani bu analizde o alan gerçekten dolu
    # olanlar). Bu, AI'nin context'te olmayan bir support/resistance/trend
    # iddiasını mekanik olarak reddetmenin küçük, additive yoludur.
    valid_structure_evidence = set(context.get("_valid_structure_evidence") or [])
    usable_ids_with_structure = usable_ids | valid_structure_evidence
    factor_meta = {
        f["factor_id"]: {
            "status": f["status"],
            "semantic_role": f.get("semantic_role"),
            "decision_critical_to_statuses": set(f.get("decision_critical_to_statuses") or []),
            "decision_critical_effect": f.get("decision_critical_effect"),
        }
        for f in context["usable_factors"]
    }

    if mode == "NORMAL":
        required = ["overall", "supporting_factors", "risks_conflicts", "entry_assessment",
                    "reassessment_triggers", "technical_watchpoints", "data_limitations", "used_factors",
                    "mentioned_unavailable_factors"]
    elif mode == "LIMITED":
        required = ["overall", "supporting_factors", "risks_conflicts", "entry_assessment",
                    "reassessment_triggers", "technical_watchpoints", "risk_data_notice", "data_limitations",
                    "used_factors", "mentioned_unavailable_factors"]
    else:  # HARD_RESTRICTED
        required = ["overall", "why_rejected_or_limited", "decisive_factors",
                    "positive_but_insufficient_factors", "technical_watchpoints", "data_limitations",
                    "used_factors", "mentioned_unavailable_factors"]
    for key in required:
        if key not in raw:
            return False, None, f"missing_field:{key}"

    def _check_item(item, allow_decision=False, allowed_ids=None):
        if not isinstance(item, dict) or "text" not in item or "evidence" not in item:
            return False
        ev = item["evidence"]
        if not isinstance(ev, list) or len(ev) == 0:
            return False
        ids = allowed_ids if allowed_ids is not None else usable_ids_with_structure
        if not all(isinstance(e, str) and e in ids for e in ev):
            return False
        if allow_decision:
            dec = item.get("decision_evidence")
            if not isinstance(dec, list) or len(dec) == 0:
                return False
            if not all(isinstance(d, str) and d in valid_decision_evidence for d in dec):
                return False
        return True

    def _check_array(arr):
        return isinstance(arr, list) and all(_check_item(it) for it in arr)

    def _check_limitation_array(arr):
        if not isinstance(arr, list):
            return False
        for it in arr:
            if not isinstance(it, dict) or "text" not in it or "evidence" not in it:
                return False
            ev = it["evidence"]
            if not isinstance(ev, list) or len(ev) == 0:
                return False
            if not all(isinstance(e, str) and e in unavailable_ids for e in ev):
                return False
        return True

    _VALID_WATCHPOINT_CATEGORIES = {"current_structure", "strengthens", "weakens"}
    # PRESENTATION V2 / CATEGORY × ZONE_REF CONTRACT: mevcut sistem promptunun
    # (madde 15c) tek açık worked example'ı yalnız strengthens+nearest_resistance
    # ("bölgenin üzerine geçilmesi") idi -- weakens+nearest_support simetriği
    # ("bölgenin altına inilmesi/kaybedilmesi") aynı ilkenin doğrudan yönlü
    # karşılığı olduğundan İKİSİ DE izinli. strengthens+nearest_support ve
    # weakens+nearest_resistance ise mevcut sözleşmeden türetilemeyen, keyfi
    # yorum gerektiren kombinasyonlar olduğu için MEKANİK olarak reddedilir
    # (yeni bir teknik analiz kuralı değil, yalnız zaten var olan iki nötr olay
    # ailesiyle -- üzerine geçilmesi / altına inilmesi -- sınırlama). current_structure
    # bir event/tetikleyici iddiası taşımadığından (yalnız mevcut konumu tarif
    # eder) her iki zone_ref ile de serbesttir.
    _INVALID_WATCHPOINT_CATEGORY_ZONE_PAIRS = {
        ("strengthens", "nearest_support"),
        ("weakens", "nearest_resistance"),
    }

    def _check_watchpoints(arr):
        """TRADER UTILITY / WATCHPOINTS V2: zone_ref MEKANİK olarak
        context'teki gerçek zone varlığıyla eşleştirilir -- structure_context
        None ise veya ilgili zone None ise (valid_structure_evidence'ta
        karşılık gelen token yoksa) AI o zone_ref'i kullanamaz, kullanırsa
        yanıtın TAMAMI reddedilir (context dışı seviye uydurmayı yapısal
        olarak imkansız kılan mekanizma)."""
        if not isinstance(arr, list):
            return False
        for it in arr:
            if not isinstance(it, dict):
                return False
            category = it.get("category")
            if category not in _VALID_WATCHPOINT_CATEGORIES:
                return False
            if not it.get("meaning") or not isinstance(it["meaning"], str):
                return False
            ev = it.get("evidence")
            if not isinstance(ev, list) or len(ev) == 0:
                return False
            if not all(isinstance(e, str) and e in usable_ids_with_structure for e in ev):
                return False
            zone_ref = it.get("zone_ref")
            if zone_ref is not None:
                if zone_ref not in ("nearest_support", "nearest_resistance"):
                    return False
                token = f"structure:{zone_ref}"
                if token not in valid_structure_evidence or token not in ev:
                    return False
                if (category, zone_ref) in _INVALID_WATCHPOINT_CATEGORY_ZONE_PAIRS:
                    return False
        return True

    def _check_risk_data_notice(item):
        # entry_assessment'tan tamamen ayrı yapısal güvenlik alanı — evidence
        # taşımaz, yalnızca sabit tek decision_evidence değeriyle doğrulanır.
        # Bu doğal-dil içerik kontrolünün YERİNİ alır (best-effort metin
        # taraması burada YOK, yalnız yapısal zorunluluk).
        if not isinstance(item, dict) or "text" not in item:
            return False
        if not item.get("text"):
            return False
        dec = item.get("decision_evidence")
        return dec == LIMITED_RISK_DATA_NOTICE_DECISION_EVIDENCE

    if not _check_item(raw["overall"]):
        return False, None, "invalid_overall"

    if not _check_watchpoints(raw["technical_watchpoints"]):
        return False, None, "invalid_technical_watchpoints"
    # ACCEPTANCE KRİTERİ (TRADER UTILITY / WATCHPOINTS V2): structure_context
    # gerçekten kullanılabilir bir alan içeriyorsa (valid_structure_evidence
    # boş değilse), technical_watchpoints BOŞ OLAMAZ -- "risk verisi yetersiz"
    # deyip yorumu bitirmek (HARD_RESTRICTED dahil) yapısal olarak reddedilir.
    if valid_structure_evidence and not raw["technical_watchpoints"]:
        return False, None, "empty_technical_watchpoints_despite_available_structure"

    if mode in ("NORMAL", "LIMITED"):
        if not _check_array(raw["supporting_factors"]):
            return False, None, "invalid_supporting_factors"
        if not _check_array(raw["risks_conflicts"]):
            return False, None, "invalid_risks_conflicts"
        # narrative<->trigger mekanik bağı: SEÇİMDEN ÖNCE değil, AYNI response
        # içindeki risks_conflicts/supporting_factors zaten yukarıda doğrulandığı
        # için buradan çıkarılan evidence kümeleri güvenilir bir referans.
        risks_conflicts_evidence = {e for it in raw["risks_conflicts"] for e in it.get("evidence", [])}
        supporting_factors_evidence = {e for it in raw["supporting_factors"] for e in it.get("evidence", [])}

    # MODEL C (AŞAMA 2): reassessment_triggers artık Claude'un {candidate_id,
    # meaning} seçimlerinden, backend'in ZATEN geçerli candidate havuzuna
    # (context["_reassessment_candidates_by_id"]) göre REKONSTRÜKTE edilir --
    # raw["reassessment_triggers"] aşağıda tam item formatına (type/
    # selection_basis/conditions/meaning/evidence) DÖNÜŞTÜRÜLÜR ki hem
    # collected_used toplaması hem de GUI render'ı (_render_reassessment_
    # trigger) HİÇ DEĞİŞMEDEN, eskisiyle AYNI formatı görsün.
    candidates_by_id = context.get("_reassessment_candidates_by_id", {})
    if mode == "NORMAL":
        ea = raw["entry_assessment"]
        if ea is not None and not _check_item(ea):
            return False, None, "invalid_entry_assessment"
        ok_ra, reconstructed_ra = _check_reassessment_selection_array(
            raw["reassessment_triggers"], candidates_by_id, reassessment_eligible_ids, factor_meta,
            risks_conflicts_evidence, supporting_factors_evidence, context["preferred_decision_critical_factor"])
        if not ok_ra:
            return False, None, "invalid_reassessment_triggers"
        raw = dict(raw)
        raw["reassessment_triggers"] = reconstructed_ra
    elif mode == "LIMITED":
        ea = raw["entry_assessment"]
        if ea is not None and not _check_item(ea, allowed_ids=ENTRY_ASSESSMENT_ALLOWED_FACTOR_IDS | valid_structure_evidence):
            return False, None, "invalid_entry_assessment"
        ok_ra, reconstructed_ra = _check_reassessment_selection_array(
            raw["reassessment_triggers"], candidates_by_id, reassessment_eligible_ids, factor_meta,
            risks_conflicts_evidence, supporting_factors_evidence, context["preferred_decision_critical_factor"])
        if not ok_ra:
            return False, None, "invalid_reassessment_triggers"
        raw = dict(raw)
        raw["reassessment_triggers"] = reconstructed_ra
        if not _check_risk_data_notice(raw["risk_data_notice"]):
            return False, None, "invalid_risk_data_notice"
    else:  # HARD_RESTRICTED
        if not _check_item(raw["why_rejected_or_limited"], allow_decision=True):
            return False, None, "invalid_why_rejected"
        if not _check_array(raw["decisive_factors"]):
            return False, None, "invalid_decisive_factors"
        if not _check_array(raw["positive_but_insufficient_factors"]):
            return False, None, "invalid_positive_but_insufficient"

    if not _check_limitation_array(raw["data_limitations"]):
        return False, None, "invalid_data_limitations"
    if not context["unavailable_factors"] and raw["data_limitations"]:
        return False, None, "invented_data_limitation"

    collected_used = set(raw["overall"].get("evidence", []))
    for arr_key in ("supporting_factors", "risks_conflicts", "reassessment_triggers",
                    "decisive_factors", "positive_but_insufficient_factors", "technical_watchpoints"):
        for f in raw.get(arr_key, []):
            collected_used.update(f.get("evidence", []))
    if mode in ("NORMAL", "LIMITED") and raw.get("entry_assessment"):
        collected_used.update(raw["entry_assessment"].get("evidence", []))
    if mode == "HARD_RESTRICTED":
        collected_used.update(raw["why_rejected_or_limited"].get("evidence", []))
    # risk_data_notice.decision_evidence used_factors'a HİÇ girmez (market
    # evidence değil, decision-namespace) — kasıtlı olarak toplanmıyor.

    if set(raw["used_factors"]) != collected_used:
        return False, None, "used_factors_mismatch"

    collected_unavailable = set()
    for f in raw["data_limitations"]:
        collected_unavailable.update(f.get("evidence", []))
    if set(raw["mentioned_unavailable_factors"]) != collected_unavailable:
        return False, None, "mentioned_unavailable_mismatch"

    return True, raw, "ok"


# ═══════════════════════════════════════════════════════════════════
# 5.9 CONTROLLED RELIABILITY FIX V1 — bounded corrective retry (additive)
# ═══════════════════════════════════════════════════════════════════
# validate_ai_analyst_response() YUKARIDA DEĞİŞMEDEN duruyor -- kabul/red
# kararını HER ZAMAN yalnız o fonksiyon verir. Aşağıdaki teşhis (diagnose)
# fonksiyonları validator'ın mantığını (defense-in-depth olarak) AYNEN
# tekrar eder ama karar vermezler -- yalnız bir düzeltme çağrısına hangi
# EXACT mekanik metni vereceğimizi belirlemek için ek, salt-okunur bilgi
# üretirler. İki ayrı doğruluk kaynağı arasında sürüklenme riskini en aza
# indirmek için mantık BİREBİR aynı sırayla ve aynı koşullarla yürütülür.

# CONTROLLED RELIABILITY FIX V1.1: iki residual failure class'ı whitelist'e
# eklendi -- ikisi de RESIDUAL RELIABILITY / CONTRACT SURFACE AUDIT'te
# "salt mekanik, AI'nin analitik seçimini değiştirmeyen" olarak kanıtlandı.
# unknown_candidate_id BİLİNÇLİ OLARAK eklenmedi (dynamic candidate enum
# ayrı, şema-seviyeli bir hardening adayı olarak kayıtta kalıyor).
AI_ANALYST_RETRY_WHITELIST_REASONS = frozenset({
    "invalid_data_limitations", "invalid_reassessment_triggers",
    "invalid_overall", "used_factors_mismatch",
})
AI_ANALYST_RETRY_WHITELIST_SUB_REASONS = frozenset({
    "empty_evidence", "invalid_factor_reference",
    "duplicate_semantic_role", "missing_supporting_factor", "missing_conflict_factor",
    "decision_token_leak", "canonical_mismatch",
})


def _diagnose_data_limitations_failure(raw: dict, context: dict) -> dict:
    """invalid_data_limitations FAIL'i, madde 9/_check_limitation_array ile
    AYNI kurallarla tekrar tarar ve HANGİ alt-mekanizmanın (boş evidence mi,
    geçersiz factor_id referansı mı) tetiklediğini ayırır. Yalnız teşhis --
    kabul/red kararını etkilemez."""
    unavailable_ids = {f["factor_id"] for f in context.get("unavailable_factors", [])}
    dl = raw.get("data_limitations")
    if not isinstance(dl, list):
        return {"sub_reason": "other", "detail": None}
    empty_items, bad_ref_items = [], []
    for it in dl:
        if not isinstance(it, dict):
            return {"sub_reason": "other", "detail": None}
        ev = it.get("evidence")
        if not isinstance(ev, list) or len(ev) == 0:
            empty_items.append(it.get("text"))
            continue
        if not all(isinstance(e, str) and e in unavailable_ids for e in ev):
            bad_ref_items.append({"text": it.get("text"), "evidence": ev})
    if empty_items and not bad_ref_items:
        return {"sub_reason": "empty_evidence", "detail": {"texts": empty_items}}
    if bad_ref_items:
        return {"sub_reason": "invalid_factor_reference",
                "detail": {"items": bad_ref_items, "valid_unavailable_ids": sorted(unavailable_ids)}}
    return {"sub_reason": "other", "detail": None}


def _diagnose_reassessment_triggers_failure(raw: dict, context: dict) -> dict:
    """invalid_reassessment_triggers FAIL'i, _check_reassessment_selection_array/
    _check_reassessment_array/_check_reassessment_item ile AYNI sırayla,
    AYNI kurallarla tekrar tarar ve İLK karşılaşılan mekanik ihlali (hangi
    candidate, hangi factor, hangi kural) döner. Yalnız teşhis -- kabul/red
    kararını etkilemez, validator'daki karşılığından bağımsız bir karar
    üretmez."""
    candidates_by_id = context.get("_reassessment_candidates_by_id", {}) or {}
    factor_meta = {
        f["factor_id"]: {"semantic_role": f.get("semantic_role")}
        for f in context.get("usable_factors", [])
    }
    preferred_dc_factor = context.get("preferred_decision_critical_factor")
    risks_conflicts_evidence = {
        e for it in (raw.get("risks_conflicts") or []) if isinstance(it, dict)
        for e in (it.get("evidence") or [])
    }
    supporting_factors_evidence = {
        e for it in (raw.get("supporting_factors") or []) if isinstance(it, dict)
        for e in (it.get("evidence") or [])
    }

    arr = raw.get("reassessment_triggers")
    if not isinstance(arr, list):
        return {"sub_reason": "other", "detail": None}
    if len(arr) > MAX_REASSESSMENT_TRIGGERS:
        return {"sub_reason": "too_many_triggers", "detail": {"count": len(arr)}}

    reconstructed, seen_ids = [], []
    for sel in arr:
        if not isinstance(sel, dict):
            return {"sub_reason": "malformed_selection", "detail": None}
        cid = sel.get("candidate_id")
        if not isinstance(cid, str) or cid not in candidates_by_id:
            return {"sub_reason": "unknown_candidate_id",
                    "detail": {"candidate_id": cid, "valid_candidate_ids": sorted(candidates_by_id.keys())}}
        if cid in seen_ids:
            return {"sub_reason": "duplicate_candidate_id", "detail": {"candidate_id": cid}}
        seen_ids.append(cid)
        cand = candidates_by_id[cid]
        reconstructed.append({**cand, "candidate_id": cid})

    for item in reconstructed:
        basis = item.get("selection_basis")
        for c in item.get("conditions", []):
            fid = c["factor_id"]
            if basis == "resolves_conflict" and fid not in risks_conflicts_evidence:
                return {"sub_reason": "missing_conflict_factor",
                        "detail": {"candidate_id": item["candidate_id"], "factor_id": fid}}
            if basis == "protects_support" and fid not in supporting_factors_evidence:
                return {"sub_reason": "missing_supporting_factor",
                        "detail": {"candidate_id": item["candidate_id"], "factor_id": fid}}

    dc_items = [it for it in reconstructed if it.get("selection_basis") == "decision_critical"]
    if len(dc_items) > MAX_DECISION_CRITICAL_TRIGGERS:
        return {"sub_reason": "too_many_decision_critical", "detail": {"count": len(dc_items)}}
    if dc_items:
        dc_factor_id = dc_items[0]["conditions"][0]["factor_id"]
        if dc_factor_id != preferred_dc_factor:
            return {"sub_reason": "wrong_decision_critical_factor",
                    "detail": {"selected": dc_factor_id, "expected": preferred_dc_factor}}

    roles_used = []
    for it in reconstructed:
        if it.get("selection_basis") == "decision_critical":
            continue
        for c in it.get("conditions", []):
            role = factor_meta.get(c["factor_id"], {}).get("semantic_role")
            if role in roles_used:
                return {"sub_reason": "duplicate_semantic_role",
                        "detail": {"role": role, "candidate_id": it["candidate_id"], "factor_id": c["factor_id"]}}
            roles_used.append(role)

    return {"sub_reason": "other", "detail": None}


def _diagnose_invalid_overall_failure(raw: dict, context: dict) -> dict:
    """invalid_overall FAIL'i icinde YALNIZ 'decision:'/'veto:' namespace
    sizintisini (overall.evidence -- ki bu alan hicbir decision-namespace
    token'i kabul etmez, why_rejected_or_limited'in aksine kendi ayri bir
    decision_evidence alani da yok) ayirir. Baska HERHANGI bir invalid_overall
    nedeni (eksik text, bos/gecersiz evidence, gecersiz factor_id -- sizinti
    DISINDA) kasitli olarak 'other' donup whitelist DISI birakilir -- retry
    yalniz mekanik olarak kesin cozulebilir tek bir alt-desende denenir."""
    overall = raw.get("overall")
    if not isinstance(overall, dict):
        return {"sub_reason": "other", "detail": None}
    ev = overall.get("evidence")
    if not isinstance(ev, list) or len(ev) == 0:
        return {"sub_reason": "other", "detail": None}
    leaked = [e for e in ev if isinstance(e, str) and (e.startswith("decision:") or e.startswith("veto:"))]
    if not leaked:
        return {"sub_reason": "other", "detail": None}
    remaining = [e for e in ev if e not in leaked]
    if not remaining:
        # Duzeltme sonrasi evidence BOS kalirdi -- bu da GECERSIZ (item
        # evidence bos olamaz) ve mekanik olarak cozulemez (yeni evidence
        # icat etmeyi gerektirir) -- whitelist DISI birak.
        return {"sub_reason": "other", "detail": None}
    usable_ids = {f["factor_id"] for f in context.get("usable_factors", [])}
    valid_structure_evidence = set(context.get("_valid_structure_evidence") or [])
    usable_ids_with_structure = usable_ids | valid_structure_evidence
    if not all(isinstance(e, str) and e in usable_ids_with_structure for e in remaining):
        # Sizinti DISINDA baska bir gecersiz token da varsa (ayri bir hata
        # sinifi) -- mekanik olarak "yalniz sizintiyi cikar" yeterli olmaz,
        # whitelist DISI birak.
        return {"sub_reason": "other", "detail": None}
    return {"sub_reason": "decision_token_leak",
            "detail": {"leaked_tokens": leaked, "remaining_evidence": remaining}}


def _diagnose_used_factors_mismatch_failure(raw: dict, context: dict) -> dict:
    """used_factors_mismatch icin, validate_ai_analyst_response'un KENDI
    collected_used hesaplamasini (satir ~6609-6620) BIREBIR ayni sirayla
    tekrar eder ve dogru (canonical) kumeyi dondurur. RESIDUAL RELIABILITY
    AUDIT'te kanitlandigi gibi used_factors %100 mekanik turetilebilir bir
    bookkeeping alani -- bu yuzden burada TEK bir sub_reason var
    ('canonical_mismatch'), cunku duzeltme HER zaman ayni sekilde (hesaplanan
    kanonik kumeyi birebir yazdirarak) mumkun."""
    mode = context.get("mode")
    candidates_by_id = context.get("_reassessment_candidates_by_id", {}) or {}
    reconstructed_trigger_evidence = []
    for sel in (raw.get("reassessment_triggers") or []):
        if isinstance(sel, dict):
            cand = candidates_by_id.get(sel.get("candidate_id"))
            if cand:
                reconstructed_trigger_evidence.append(
                    sorted({c["factor_id"] for c in cand["conditions"]}))
    collected_used = set()
    overall = raw.get("overall")
    if isinstance(overall, dict):
        collected_used.update(overall.get("evidence", []) or [])
    for arr_val in (raw.get("supporting_factors"), raw.get("risks_conflicts"),
                     raw.get("decisive_factors"), raw.get("positive_but_insufficient_factors"),
                     raw.get("technical_watchpoints")):
        for f in (arr_val or []):
            if isinstance(f, dict):
                collected_used.update(f.get("evidence", []) or [])
    for factor_ids in reconstructed_trigger_evidence:
        collected_used.update(factor_ids)
    if mode in ("NORMAL", "LIMITED") and isinstance(raw.get("entry_assessment"), dict):
        collected_used.update(raw["entry_assessment"].get("evidence", []) or [])
    if mode == "HARD_RESTRICTED" and isinstance(raw.get("why_rejected_or_limited"), dict):
        collected_used.update(raw["why_rejected_or_limited"].get("evidence", []) or [])
    return {"sub_reason": "canonical_mismatch",
            "detail": {"canonical_used_factors": sorted(collected_used),
                       "raw_used_factors": raw.get("used_factors")}}


def _build_corrective_retry_user_content(mode: str, context: dict, original_raw: dict,
                                          top_reason: str, diag: dict) -> str:
    """Düzeltici çağrının TEK kullanıcı mesajı -- tüm system prompt'u yeniden
    ANLATMAZ (system=AI_ANALYST_SYSTEM_PROMPT aynen kullanılır), yalnız
    önceki cevabı + EXACT mekanik sorunu + dar bir düzeltme talebini taşır."""
    sub = diag.get("sub_reason")
    detail = diag.get("detail") or {}
    if top_reason == "invalid_data_limitations":
        if sub == "empty_evidence":
            instr = (
                "data_limitations alanındaki şu item(lar) GEÇERSİZ, çünkü evidence dizisi BOŞ: "
                + json.dumps(detail.get("texts"), ensure_ascii=False)
                + ". Eğer bu bilgi (ör. 'risk kapsamı düşük') zaten why_rejected_or_limited veya "
                  "risk_data_notice içinde veriliyorsa, data_limitations'ta TEKRAR ETME -- "
                  "data_limitations dizisini BOŞ ([]) bırak. Yalnız context'te GERÇEKTEN "
                  "unavailable_factors içinde spesifik bir factor_id varsa, o factor_id'yi kendi "
                  "evidence'ı olarak yaz.")
        elif sub == "invalid_factor_reference":
            instr = (
                "data_limitations alanında GEÇERSİZ factor_id referansı var: "
                + json.dumps(detail.get("items"), ensure_ascii=False)
                + ". data_limitations yalnız şu GERÇEK unavailable factor_id'lerini referans "
                  "verebilir: " + json.dumps(detail.get("valid_unavailable_ids"), ensure_ascii=False)
                + ". Bu listede olmayan hiçbir factor_id'den data_limitations içinde bahsetme.")
        else:
            instr = "data_limitations alanı şema kuralını ihlal ediyor (madde 9), buna göre düzelt."
    elif top_reason == "invalid_reassessment_triggers":
        if sub == "duplicate_semantic_role":
            instr = (
                f"reassessment_triggers'ta seçtiğin {detail.get('candidate_id')} candidate'i "
                f"(factor_id={detail.get('factor_id')}) '{detail.get('role')}' semantic_role'ünü, "
                "ZATEN seçtiğin başka bir candidate ile PAYLAŞIYOR -- bu YASAK (madde 11). Bu iki "
                "candidate'ten yalnız birini tut, diğerini çıkar (istersen farklı role'den başka "
                "bir candidate ile değiştir).")
        elif sub == "missing_supporting_factor":
            instr = (
                f"reassessment_triggers'ta seçtiğin {detail.get('candidate_id')} candidate'i "
                f"(protects_support) '{detail.get('factor_id')}' factor'ünü gerektiriyor, ama bu "
                "factor_id AYNI CEVABINDAKİ supporting_factors'ın evidence dizisinde YOK. Ya bu "
                "factor_id'yi supporting_factors'a (gerçekten ondan bahseden bir cümleyle) ekle, "
                "ya da bu candidate'i seçme.")
        elif sub == "missing_conflict_factor":
            instr = (
                f"reassessment_triggers'ta seçtiğin {detail.get('candidate_id')} candidate'i "
                f"(resolves_conflict) '{detail.get('factor_id')}' factor'ünü gerektiriyor, ama bu "
                "factor_id AYNI CEVABINDAKİ risks_conflicts'in evidence dizisinde YOK. Ya bu "
                "factor_id'yi risks_conflicts'e (gerçekten ondan bahseden bir cümleyle) ekle, "
                "ya da bu candidate'i seçme.")
        else:
            instr = "reassessment_triggers alanı candidate-seçim sözleşmesini ihlal ediyor (madde 11), buna göre düzelt."
    elif top_reason == "invalid_overall" and sub == "decision_token_leak":
        instr = (
            "overall.evidence dizisinde GEÇERSİZ 'decision:'/'veto:' namespace token'ı var: "
            + json.dumps(detail.get("leaked_tokens"), ensure_ascii=False)
            + ". overall.text'in ANLAMINI/TONUNU DEĞİŞTİRME -- yalnız overall.evidence dizisinden bu "
              "geçersiz token'ları ÇIKAR, yalnız şu geçerli factor_id/'structure:*' token'larını "
              "bırak: " + json.dumps(detail.get("remaining_evidence"), ensure_ascii=False)
            + ". Bu token'ları başka hiçbir alana taşıma (why_rejected_or_limited.decision_evidence "
              "zaten kendi doğru yerinde olmalı, onu ayrıca değiştirme), yeni evidence/factor icat etme.")
    elif top_reason == "used_factors_mismatch":
        instr = (
            "used_factors alanı, cevabının diğer bölümlerinde GERÇEKTEN kullandığın evidence "
            "token'larının birleşimiyle EŞLEŞMİYOR. Hiçbir analitik alanı (overall, "
            "supporting_factors, risks_conflicts, decisive_factors, "
            "positive_but_insufficient_factors, technical_watchpoints, entry_assessment, "
            "why_rejected_or_limited, reassessment_triggers) DEĞİŞTİRME, yeni factor icat etme -- "
            "yalnız used_factors alanını AŞAĞIDAKİ TAM listeyle birebir değiştir: "
            + json.dumps(detail.get("canonical_used_factors"), ensure_ascii=False))
    else:
        instr = "Önceki cevabın yapısal doğrulamadan geçmedi, hatayı düzelt."

    return (
        "DÜZELTME TURU (CORRECTIVE RETRY): Önceki cevabın yapısal doğrulamadan geçmedi. "
        "Aşağıda önceki TAM cevabın veriliyor. YALNIZ belirtilen spesifik sorunu düzelt -- "
        "yeni piyasa analizi yapma, yeni fiyat/factor/evidence icat etme, verdict/stance/overall "
        "tonunu değiştirme (düzeltme bunu zorunlu kılmıyorsa), diğer alanları gereksiz yere "
        "yeniden yazma. Yalnız hatalı alan(lar)ı düzelt, DÜZELTİLMİŞ TAM CEVABI (aynı şema, tüm "
        "zorunlu alanlarla) tekrar döndür.\n\n"
        "TESPİT EDİLEN SORUN:\n" + instr + "\n\n"
        "ÖNCEKİ CEVABIN (TAM JSON):\n" + json.dumps(original_raw, ensure_ascii=False, indent=2)
    )


_AI_ANALYST_RETRY_DIAGNOSTIC_DISPATCH = {
    "invalid_data_limitations": _diagnose_data_limitations_failure,
    "invalid_reassessment_triggers": _diagnose_reassessment_triggers_failure,
    "invalid_overall": _diagnose_invalid_overall_failure,
    "used_factors_mismatch": _diagnose_used_factors_mismatch_failure,
}


_ai_reliability_telemetry_lock = threading.Lock()


def _ai_reliability_telemetry_log_path() -> str:
    from pathlib import Path
    try:
        app_dir = Path(__file__).resolve().parent
    except NameError:
        app_dir = Path.cwd()
    logs_dir = app_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    return str(logs_dir / "ai_analyst_reliability.jsonl")


def _build_ai_reliability_telemetry_event(symbol, mode, request_id, result: dict, duration_s: float) -> dict:
    """AI ANALYST V6 -- PRODUCTION RELIABILITY TELEMETRY: bir
    run_ai_analyst_with_corrective_retry() çağrısının TÜM lifecycle'ını
    (primary + varsa tek retry) TEK bir event'e özetler -- attempt başına
    değil, RUN başına bir kayıt (aksi halde retry sıklığı arttıkça log
    şişer ve primary/final oranları karışır). primary_ok/final_ok
    semantikleri: primary_ok = retry'den ÖNCEKİ ilk cevabın validator
    sonucu; final_ok = TÜM orchestration bittikten sonra kullanıcıya
    kabul edilebilir sonuç kalıp kalmadığı. retry_triggered=False iken
    retry_top_reason/retry_sub_reason/retry_ok/retry_reason HER ZAMAN
    None (tutarlı, karışıklığa yer bırakmayan semantik)."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "mode": mode,
        "request_id": request_id,
        "primary_ok": result.get("primary_ok"),
        "primary_reason": result.get("primary_reason"),
        "retry_triggered": result.get("retry_triggered"),
        "retry_top_reason": result.get("retry_top_reason"),
        "retry_sub_reason": result.get("retry_sub_reason"),
        "retry_ok": result.get("retry_ok"),
        "final_ok": result.get("final_ok"),
        "final_reason": result.get("final_reason"),
        "attempt_count": 2 if result.get("retry_triggered") else 1,
        "duration_s": round(duration_s, 2),
    }


def _log_ai_reliability_telemetry(event: dict) -> None:
    """OBSERVE, DO NOT INFLUENCE: yalnız append-only JSONL yan-etki --
    validator/retry/GUI/karar mekanizmasının HİÇBİRİNİ etkilemez, hiçbir
    değer döndürmez, hiçbir exception'ı çağırana sızdırmaz (FAIL-OPEN --
    yazma başarısız olursa AI Analyst akışı hiç etkilenmeden devam eder).
    Mevcut _log_ai_validation_failure (yalnız FAIL, ayrı dosya, ayrı format)
    İLE DEĞİŞTİRİLMEDİ -- bu PARALEL, additive bir kayıt. PASS+FAIL HER
    run için tek satır yazar (mevcut logger yalnız FAIL'de yazıyordu).
    Full prompt/AI response/market context/haber metni/ham evidence
    YAZILMAZ -- yalnız reliability metadata'sı. threading.Lock ile
    korunuyor çünkü birden fazla AIAnalystWorker/Level1Worker aynı anda
    farklı coin'ler için çalışabiliyor (mevcut proje threading deseni) --
    yeni bir bağımlılık eklenmedi, stdlib json+threading+pathlib yeterli."""
    try:
        line = json.dumps(event, ensure_ascii=False, default=str)
        with _ai_reliability_telemetry_lock:
            with open(_ai_reliability_telemetry_log_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        print(f"[AI ANALIST][TELEMETRY] yazilamadi (ana akis etkilenmedi): {e}", flush=True)


def run_ai_analyst_with_corrective_retry(context: dict, mode: str, request_id: str = None) -> dict:
    """CONTROLLED RELIABILITY FIX V1: production (AIAnalystWorker) VE test
    script'lerinin ORTAK kullandığı TEK orkestrasyon noktası -- iş
    mantığının iki yerde kopyalanmasını (önceki turda reliability test
    script'inin AIAnalystWorker'ı bypass etmesiyle yaşanan gözlemlenebilirlik
    boşluğunun AYNISI) önler. validate_ai_analyst_response HİÇ değişmedi --
    hem ilk hem düzeltme cevabı AYNI, dokunulmamış fonksiyonla doğrulanır.
    Yalnız AI_ANALYST_RETRY_WHITELIST_REASONS + ..._SUB_REASONS içindeki dar,
    mekanik olarak teşhis edilmiş hatalarda TEK bir ek çağrı yapılır --
    18e/watchpoint dahil DİĞER TÜM reason'larda retry hiç denenmez, mevcut
    fail-closed davranış AYNEN korunur. İkinci bir retry KESİNLİKLE yapılmaz.
    PRODUCTION RELIABILITY TELEMETRY (additive): dönmeden önce -- karar
    mantığı TAMAMEN bittikten sonra, kararı hiçbir şekilde etkilemeden --
    tek bir _log_ai_reliability_telemetry() event'i yazılır (tüm early-return
    yollar TEK bir kuyruk noktasında birleştirildi, davranış birebir aynı).
    request_id opsiyonel (yalnız telemetry'de görünür, karara etkisi yok);
    verilmezse None kalır, geriye dönük uyumluluk bozulmaz.
    Döner: {final_raw, final_ok, final_reason, primary_ok, primary_reason,
    retry_triggered, retry_top_reason, retry_sub_reason, retry_ok, retry_reason}."""
    _t_start = time.time()
    try:
        symbol = (context.get("decision_context") or {}).get("symbol")
    except Exception:
        symbol = None

    raw = AIAnalystClient.request_analysis(context, mode)
    if raw is None:
        result = {"final_raw": None, "final_ok": False, "final_reason": "api_unavailable",
                  "primary_ok": False, "primary_reason": "api_unavailable",
                  "retry_triggered": False, "retry_top_reason": None, "retry_sub_reason": None,
                  "retry_ok": None, "retry_reason": None}
        _log_ai_reliability_telemetry(_build_ai_reliability_telemetry_event(
            symbol, mode, request_id, result, time.time() - _t_start))
        return result

    ok, parsed, reason = validate_ai_analyst_response(mode, raw, context)
    result = {"final_raw": parsed if ok else raw, "final_ok": ok, "final_reason": reason,
              "primary_ok": ok, "primary_reason": reason,
              "retry_triggered": False, "retry_top_reason": None, "retry_sub_reason": None,
              "retry_ok": None, "retry_reason": None}

    if not ok and reason in AI_ANALYST_RETRY_WHITELIST_REASONS:
        diag_fn = _AI_ANALYST_RETRY_DIAGNOSTIC_DISPATCH.get(reason)
        diag = diag_fn(raw, context) if diag_fn is not None else {"sub_reason": None, "detail": None}
        if diag.get("sub_reason") in AI_ANALYST_RETRY_WHITELIST_SUB_REASONS:
            result["retry_triggered"] = True
            result["retry_top_reason"] = reason
            result["retry_sub_reason"] = diag.get("sub_reason")
            retry_raw = AIAnalystClient.request_corrective_retry(context, mode, raw, reason, diag)
            if retry_raw is None:
                result["retry_ok"] = False
                result["retry_reason"] = "api_unavailable"
            else:
                retry_ok, retry_parsed, retry_reason = validate_ai_analyst_response(mode, retry_raw, context)
                result["retry_ok"] = retry_ok
                result["retry_reason"] = retry_reason
                if retry_ok:
                    result["final_raw"] = retry_parsed
                    result["final_ok"] = True
                    result["final_reason"] = "ok"
                # retry basarisiz ise final_raw/final_ok/final_reason ORIJINAL
                # (primary) degerlerinde kalir -- ikinci retry KESINLIKLE yapilmaz.

    _log_ai_reliability_telemetry(_build_ai_reliability_telemetry_event(
        symbol, mode, request_id, result, time.time() - _t_start))
    return result


# ═══════════════════════════════════════════════════════════════════
# 6. GUI (PySide6)
# ═══════════════════════════════════════════════════════════════════

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QSortFilterProxyModel, QRectF
from PySide6.QtGui import (
    QFont, QColor, QPainter, QPen, QLinearGradient, QRadialGradient, QBrush, QConicalGradient,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QLineEdit, QRadioButton, QButtonGroup,
    QCheckBox, QScrollArea, QFrame, QTabWidget, QStackedWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QDialog, QTextEdit, QMessageBox, QFileDialog,
    QProgressBar, QSizePolicy, QAbstractItemView, QCompleter, QListWidget,
    QSpacerItem, QGraphicsDropShadowEffect, QDockWidget,
)

# ── Premium fintech / cyberpunk melez palet — mor × turkuaz gradyan vurgu ──
BG = "#050510"
SIDEBAR_BG = "#08081a"
CARD = "#100f22"
CARD_ALT = "#1a1830"
CARD_HOVER = "#221f3d"
TEXT_PRIMARY = "#f3f1ff"
TEXT_SECONDARY = "#a9a5c9"
TEXT_TERTIARY = "#6d6892"
BORDER = "#26224a"
BORDER_LIGHT = "#38315e"
POS = "#2de6a8"           # neon turkuaz-yeşil
POS_BG = "#062420"
WARN = "#ffb020"          # neon amber
WARN_BG = "#241705"
DANGER = "#ff3d78"        # neon pembe-kırmızı
DANGER_BG = "#280a1e"
NEUTRAL = "#8781a8"
NEUTRAL_BG = "#181530"
BLUE = "#8b5cf6"           # ana vurgu — elektrik mor (eski "amatör mavi" yerine)
BLUE_BG = "#211c42"
ACCENT2 = "#2dd4bf"        # ikincil vurgu — turkuaz (gradyan eşleşmesi)
VETO_BG = "#280a1e"
GLOW1 = "#7c3aed"          # arkaplan ambiyans parıltısı — mor
GLOW2 = "#0d9488"          # arkaplan ambiyans parıltısı — turkuaz

DARK_QSS = f"""
QMainWindow {{
    background: qradialgradient(cx:0.14, cy:0.0, radius:0.9, fx:0.14, fy:0.0,
        stop:0 #211a3d, stop:0.35 #120f28, stop:1 {BG});
}}
QWidget {{
    color: {TEXT_PRIMARY};
    font-family: 'Segoe UI';
    font-size: 10.5pt;
}}
QLabel {{ background: transparent; }}
QFrame#Card {{
    background-color: {CARD};
    border: 1px solid {BORDER};
    border-radius: 18px;
}}
QFrame#Sidebar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {SIDEBAR_BG}, stop:1 #0c0a1e);
    border-right: 1px solid {BORDER};
}}
QPushButton#NavItem {{
    background-color: transparent;
    color: {TEXT_SECONDARY};
    border: none;
    border-radius: 18px;
    padding: 11px 16px;
    text-align: left;
    font-weight: 600;
    font-size: 10.5pt;
}}
QPushButton#NavItem:hover {{ background-color: {CARD_ALT}; color: {TEXT_PRIMARY}; }}
QPushButton#NavItem:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {BLUE_BG}, stop:1 #0f2e2a);
    color: {TEXT_PRIMARY};
    border: 1px solid {BLUE};
}}
QFrame#Header {{
    background-color: {CARD};
    border-bottom: 2px solid {BLUE};
}}
QFrame#VetoBanner {{
    background-color: {VETO_BG};
    border: 1.5px solid {DANGER};
    border-radius: 12px;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    top: -1px;
    background-color: {BG};
}}
QTabBar::tab {{
    background-color: transparent;
    color: {TEXT_SECONDARY};
    padding: 10px 20px;
    margin-right: 4px;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    font-weight: 500;
}}
QTabBar::tab:selected {{
    background-color: {CARD};
    color: {TEXT_PRIMARY};
    font-weight: 700;
    border-bottom: 2px solid {BLUE};
}}
QTabBar::tab:hover:!selected {{ background-color: {CARD_ALT}; color: {TEXT_PRIMARY}; }}
QPushButton {{
    background-color: {CARD_ALT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 18px;
    padding: 9px 18px;
    font-weight: 600;
}}
QPushButton:hover {{ background-color: {CARD_HOVER}; border-color: {BLUE}; color: {TEXT_PRIMARY}; }}
QPushButton:pressed {{ background-color: #0d0b1c; }}
QPushButton:disabled {{ color: {TEXT_TERTIARY}; border-color: {BORDER}; background-color: {CARD}; }}
QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {BLUE}, stop:1 {ACCENT2});
    color: #060512;
    font-weight: 700;
    border: none;
    border-radius: 19px;
    padding: 10px 24px;
}}
QPushButton#Primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #a479ff, stop:1 #4de8d1);
}}
QPushButton#Primary:disabled {{ background: #262b38; color: {TEXT_TERTIARY}; }}
QLineEdit, QComboBox {{
    background-color: {CARD_ALT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 10px;
    padding: 6px 12px;
}}
QLineEdit:focus, QComboBox:focus {{ border: 1.5px solid {BLUE}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background-color: {CARD_ALT};
    color: {TEXT_PRIMARY};
    selection-background-color: {BLUE};
    selection-color: #06111f;
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QRadioButton, QCheckBox {{ spacing: 7px; padding: 2px; }}
QRadioButton::indicator, QCheckBox::indicator {{ width: 16px; height: 16px; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: {BG}; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER_LIGHT}; border-radius: 6px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_TERTIARY}; }}
QTableWidget {{
    background-color: {CARD};
    alternate-background-color: {CARD_ALT};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QHeaderView::section {{
    background-color: {CARD_ALT};
    color: {TEXT_PRIMARY};
    padding: 8px 6px;
    border: none;
    border-bottom: 2px solid {BLUE};
    font-weight: 700;
}}
QTableWidget::item {{ padding: 4px; }}
QTableWidget::item:selected {{ background-color: {BLUE_BG}; }}
QProgressBar {{
    background-color: {BORDER};
    border: none;
    border-radius: 5px;
    text-align: center;
    color: transparent;
    height: 10px;
}}
QProgressBar::chunk {{ border-radius: 5px; background-color: {BLUE}; }}
QTextEdit {{
    background-color: {CARD_ALT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 10px;
    font-family: Consolas;
    padding: 8px;
}}
QDialog {{ background-color: {BG}; }}
QListWidget {{
    background-color: {CARD_ALT};
    color: {TEXT_SECONDARY};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 4px;
}}
QListWidget::item {{ padding: 5px 8px; border-radius: 6px; }}
QListWidget::item:selected {{ background-color: {BLUE_BG}; color: {TEXT_PRIMARY}; }}
"""


def rgba(hex_color: str, alpha: float) -> str:
    """'#RRGGBB' + 0-1 alfa -> 'rgba(r,g,b,a)'. Qt QSS'te 8 haneli hex #AARRGGBB
    (alfa ÖNDE) olarak yorumlanır — hex'e alfa EKLEMEK yanlış renk üretir, bu yüzden
    her yerde bunun yerine bu fonksiyon kullanılmalı."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def status_color(status: str) -> str:
    return {"yes": POS, "wait": WARN, "no": DANGER, "nodata": NEUTRAL}.get(status, NEUTRAL)


def status_bg(status: str) -> str:
    return {"yes": POS_BG, "wait": WARN_BG, "no": DANGER_BG, "nodata": NEUTRAL_BG}.get(status, NEUTRAL_BG)


def status_icon(status: str) -> str:
    return {"yes": "✓", "wait": "~", "no": "✕", "nodata": "?"}.get(status, "?")


RISK_STAGE_TITLE = "Risk & pozisyon yönetimi"


def fallback_risk_coverage(answers: list) -> float:
    """Eski (v2 öncesi) kayıtlarda risk_coverage sütunu NULL olduğunda,
    yalnızca answers JSON'ındaki Risk & pozisyon yönetimi aşaması cevaplarından
    kapsamı geriye hesaplar. Soru metni eşleştirmesi YAPMAZ — yalnız 'stage'
    alanına ve zaten mevcut olan 'answer'/'disabled' alanlarına bakar."""
    risk_answers = [a for a in answers
                     if a.get("stage") == RISK_STAGE_TITLE and not a.get("disabled")]
    if not risk_answers:
        return 0.0
    answered = sum(1 for a in risk_answers if a.get("answer") != "nodata")
    return answered / len(risk_answers) * 100


def compute_risk_reliable(answers: list) -> bool:
    """RISK COVERAGE V2 (R3): History kayıtları (eski VEYA yeni fark etmez)
    için `risk_reliable`'ı stored `answers` JSON'ından türetir. DB şeması
    değişmedi -- risk_breadth/risk_reliable persist edilmiyor, bu yüzden
    History table/detail her açılışta bu fonksiyonla YENİDEN hesaplar
    (fallback_risk_coverage ile aynı üsluptaki bir read-time türetme).
    Eski kayıtların stored risk_coverage/verdict/risk_score değerlerini
    DEĞİŞTİRMEZ, yalnız 'Yetersiz veri' gate'inin verdict()/AI-mode ile
    AYNI eşiği (risk_reliable) kullanmasını sağlar -- aksi halde History
    yeni kayıtlarda risk_coverage=%100 (breadth=1) iken yanlışlıkla tam
    risk_score gösterirdi (verdict()/GUI/clipboard ile tutarsız, unsafe)."""
    risk_answers = [a for a in answers
                     if a.get("stage") == RISK_STAGE_TITLE and not a.get("disabled")]
    measurable = [a for a in risk_answers
                  if _FACTOR_ID_BY_LABEL.get(a.get("question")) not in STRUCTURALLY_UNAVAILABLE_FACTOR_IDS]
    breadth = sum(1 for a in measurable if a.get("answer") != "nodata")
    return breadth >= 2


def score_color(score: float) -> str:
    if score is None:
        return NEUTRAL
    return POS if score >= 65 else WARN if score >= 50 else DANGER


def score_bg(score: float) -> str:
    if score is None:
        return NEUTRAL_BG
    return POS_BG if score >= 65 else WARN_BG if score >= 50 else DANGER_BG


STAGE_ICONS = {
    "Temel filtreleme": "🧪",
    "Teknik analiz onayı": "📈",
    "Zincir üstü veriler": "⛓️",
    "Makro & türev piyasası": "🌐",
    "Risk & pozisyon yönetimi": "🛡️",
}
STAGE_ACCENTS = {
    "Temel filtreleme": ACCENT2,
    "Teknik analiz onayı": BLUE,
    "Zincir üstü veriler": POS,
    "Makro & türev piyasası": WARN,
    "Risk & pozisyon yönetimi": DANGER,
}


def _apply_shadow(widget, blur=28, alpha=140, y_offset=6, color=None):
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, y_offset)
    effect.setColor(QColor(0, 0, 0, alpha) if color is None else color)
    widget.setGraphicsEffect(effect)


class Card(QFrame):
    def __init__(self, parent=None, accent: str = None, glow: bool = False):
        super().__init__(parent)
        self.setObjectName("Card")
        # Preferred/Maximum: kart hiçbir zaman içeriğinden daha uzun boy kaplamasın
        # (sabit yükseklikli çocuklar dışındaki tek esnek widget'a "sahte boşluk" sızmasın).
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        if accent:
            self.setStyleSheet(
                f"QFrame#Card {{ background-color: {CARD}; border: 1px solid {rgba(accent, 0.33)}; "
                f"border-radius: 16px; border-top: 2px solid {accent}; }}"
            )
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(20, 18, 20, 18)
        self.layout_.setSpacing(8)
        if accent and glow:
            c = QColor(accent)
            c.setAlpha(110)
            _apply_shadow(self, blur=42, y_offset=0, color=c)
        else:
            _apply_shadow(self)

    def add(self, widget):
        self.layout_.addWidget(widget)
        return widget

    def set_accent(self, accent: str, glow: bool = True):
        self.setStyleSheet(
            f"QFrame#Card {{ background-color: {CARD}; border: 1px solid {rgba(accent, 0.33)}; "
            f"border-radius: 16px; border-top: 2px solid {accent}; }}"
        )
        if glow:
            c = QColor(accent)
            c.setAlpha(110)
            _apply_shadow(self, blur=42, y_offset=0, color=c)

    def clear(self):
        while self.layout_.count():
            item = self.layout_.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()


def h_label(text, size=10, bold=False, color=TEXT_PRIMARY, wrap=False):
    lbl = QLabel(text)
    f = QFont("Segoe UI", size)
    f.setBold(bold)
    lbl.setFont(f)
    lbl.setStyleSheet(f"color: {color};")
    if wrap:
        lbl.setWordWrap(True)
    return lbl


def make_badge(text: str, color: str, bg: str = None, size: int = 9) -> QLabel:
    """Dolgulu, renkli kenarlıklı rozet (pill) — durum/karar göstergeleri için."""
    bg = bg or rgba(color, 0.13)
    lbl = QLabel(text)
    f = QFont("Segoe UI", size)
    f.setBold(True)
    lbl.setFont(f)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet(
        f"color: {color}; background-color: {bg}; border: 1px solid {color}; "
        f"border-radius: 10px; padding: 3px 11px;"
    )
    return lbl


def icon_chip(emoji: str, color: str, size: int = 32) -> QLabel:
    """Köşeleri yuvarlatılmış, renkli dolgulu ikon rozeti (kart başlıkları için)."""
    lbl = QLabel(emoji)
    lbl.setFixedSize(size, size)
    lbl.setAlignment(Qt.AlignCenter)
    f = QFont("Segoe UI", int(size * 0.42))
    lbl.setFont(f)
    lbl.setStyleSheet(
        f"background-color: {rgba(color, 0.15)}; border: 1px solid {rgba(color, 0.33)}; "
        f"border-radius: {size // 3}px;"
    )
    return lbl


class ProgressBarStyled(QFrame):
    """Skor çubuğu — yüzdeye göre renkli dolgu."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(10)
        self._pct = 0
        self._color = BORDER

    def set_value(self, pct: float, color: str):
        self._pct = max(0, min(100, pct))
        self._color = color
        self.setStyleSheet(
            f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 {color}, stop:{max(self._pct/100, 0.001):.3f} {color}, "
            f"stop:{min(self._pct/100 + 0.001, 1):.3f} {BORDER}, stop:1 {BORDER});"
            f"border-radius: 5px;"
        )


class RingGauge(QWidget):
    """Neon dairesel skor göstergesi — düz yüzde metni yerine parlayan halka."""
    def __init__(self, diameter=132, thickness=11, parent=None, suffix="%"):
        super().__init__(parent)
        self._value = None
        self._color = NEUTRAL
        self._diameter = diameter
        self._thickness = thickness
        self._suffix = suffix
        self.setFixedSize(diameter, diameter)

    def set_value(self, value, color):
        self._value = value
        self._color = color
        if value is not None:
            c = QColor(color)
            c.setAlpha(190)
            _apply_shadow(self, blur=30, y_offset=0, color=c)
        else:
            self.setGraphicsEffect(None)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t = self._thickness
        rect = QRectF(t / 2, t / 2, self._diameter - t, self._diameter - t)

        track_pen = QPen(QColor(BORDER_LIGHT))
        track_pen.setWidth(t)
        track_pen.setCapStyle(Qt.RoundCap)
        p.setPen(track_pen)
        p.drawArc(rect, 0, 360 * 16)

        if self._value is not None:
            base = QColor(self._color)
            grad = QConicalGradient(rect.center(), 90)
            light = QColor(base).lighter(150)
            grad.setColorAt(0.0, light)
            grad.setColorAt(0.85, base)
            grad.setColorAt(1.0, base.darker(130))
            pen = QPen(QBrush(grad), t)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            span = int(-(max(0, min(100, self._value)) / 100) * 360 * 16)
            p.drawArc(rect, 90 * 16, span)

        p.setPen(QColor(TEXT_PRIMARY))
        text = f"{self._value:.1f}{self._suffix}" if self._value is not None else "—"
        # Ondalıklı gösterim önceki ("67%") göre daha uzun ("66.8%") — halkaya
        # sığması için uzun metinlerde fontu küçült (yalnız görsel sığdırma, veri değişmiyor).
        size_factor = 0.17 if len(text) <= 3 else (0.145 if len(text) <= 5 else 0.125)
        f = QFont("Segoe UI", max(9, int(self._diameter * size_factor)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, text)
        p.end()


def capture_analysis_snapshot(symbol: str):
    """History/tracking referans çifti: rapor tamamlandıktan HEMEN sonra,
    aynı işlem noktasında alınan (zaman, fiyat). Level 1/2/3'ün hepsi bunu
    kullanır — save butonuna basılma anıyla hiçbir ilişkisi yoktur, ve
    RealFetcher'ın kendi analiz metrikleri için kullandığı ayrı ticker
    verisiyle (ör. 'price_change_24h_pct_ticker') karıştırılmaz."""
    price = BinanceClient.get_price(symbol)
    analysis_time = datetime.now(timezone.utc).isoformat()
    return analysis_time, price


def run_level1_core(symbol: str, mock_fetcher=None, included_onchain: set = frozenset(),
                     on_progress=lambda msg: None, should_cancel=lambda: False) -> dict:
    """MOBILE WEB MVP / PHASE 1 (onaylı, controlled implementation) — CANONICAL
    LEVEL 1 ORCHESTRATION. Bu fonksiyon, önceden `Level1Worker.run()` içine
    gömülü olan tüm saf motor mantığının (STAGES_CONFIG döngüsü, RealFetcher
    çağrıları, ScoreEngine.full_report(), Model D additive blok,
    capture_analysis_snapshot()) BİREBİR AYNI, satır satır taşınmış halidir --
    hiçbir dal/koşul/sıra DEĞİŞMEDİ, yalnız Qt'ye özgü 4 nokta (progress.emit,
    isInterruptionRequested, finished_ok.emit, cancelled.emit) parametreleştirildi:
      - on_progress(msg): ilerleme bildirimi (Qt tarafında Signal.emit, web
        tarafında no-op).
      - should_cancel(): kooperatif iptal kontrolü (Qt tarafında
        QThread.isInterruptionRequested, web tarafında her zaman False).
      - Dönüş değeri: eskiden finished_ok.emit'e giden dict'in AYNISI
        (request_id HARİÇ -- onu artık çağıran taraf ekliyor).
      - Level1Cancelled: eskiden olduğu gibi fırlatılır, ÇAĞIRAN taraf yakalar
        (bu fonksiyon kendi try/except'ini KURMAZ -- Level1Worker.run() ve
        web wrapper'ı kendi hata/iptal semantiklerini kendileri uygular).

    Böylece masaüstü (Level1Worker) ve web wrapper'ı AYNI canonical execution
    path'i paylaşır -- iki ayrı Level 1 algoritması YOKTUR."""
    _t_level1 = time.time()
    symbol = symbol.upper()
    is_test = symbol.startswith("TEST_") or symbol in ("BTC_MOCK",)
    if is_test:
        fetcher = mock_fetcher
        source_status = {"MockFetcher": "test profili"}
    else:
        fetcher = RealFetcher(on_progress=on_progress, should_cancel=should_cancel)
        source_status = None

    answers = []
    for stage in STAGES_CONFIG:
        if should_cancel():
            raise Level1Cancelled()
        on_progress(f"{stage.title} değerlendiriliyor...")
        for item in stage.items:
            # Opsiyonel on-chain sorusu, o METRİK tik edilmediyse → KAPSAM DIŞI.
            # Fetcher'a hiç ulaşılmaz, hiçbir araştırma/API çağrısı yapılmaz
            # (gerçek ve mock için aynı — TEST_ profilleri de tik durumuna uyar).
            # Tik edilmişse (kapsamda) fetcher YİNE DE normal şekilde çağrılır —
            # gerçek bir otomatik kaynak yoksa dp.available=False -> nodata olur,
            # burada asla "tik edildi diye yes" varsayılmaz.
            if item.optional:
                onchain_metric = _metric_for_label(item.label)
                if onchain_metric not in included_onchain:
                    answers.append({
                        "stage": stage.title, "question": item.label, "answer": "nodata",
                        "weight": item.weight, "_item": item, "value": None, "source": "",
                        "reason": "Analize dahil edilmedi (tik edilmedi)",
                        "disabled": True,
                    })
                    continue

            if item.thresholds.get("type") == "volume_spread":
                dp_vol = fetcher.fetch(symbol, "volume_24h")
                dp_spread = fetcher.fetch(symbol, "spread_pct")
                th = item.thresholds
                both_available = dp_vol.available and dp_spread.available
                if not both_available:
                    ans = "nodata"
                else:
                    vol, spread = dp_vol.value, dp_spread.value
                    if vol >= th.get("yes_vol", 999) and spread <= th.get("yes_spread", 0):
                        ans = "yes"
                    elif vol >= th.get("wait_vol", 0) and spread <= th.get("wait_spread", 999):
                        ans = "wait"
                    else:
                        ans = "no"
                if both_available:
                    value = f"hacim={dp_vol.value}M$, spread={dp_spread.value}%"
                    reason = ""
                else:
                    value = None
                    vol_reason = dp_vol.reason if not dp_vol.available else ""
                    spread_reason = dp_spread.reason if not dp_spread.available else ""
                    if vol_reason and spread_reason:
                        reason = f"Hacim: {vol_reason} | Spread: {spread_reason}"
                    else:
                        reason = vol_reason or spread_reason or "Veri alınamadı"
                srcs = [s for s in (dp_vol.source, dp_spread.source) if s]
                # aynı kaynak metni iki kez tekrar etmesin (ör. ikisi de yalnızca "Binance" dönerse)
                seen = []
                for s in srcs:
                    if s not in seen:
                        seen.append(s)
                source = " | ".join(seen)
                answers.append({
                    "stage": stage.title, "question": item.label, "answer": ans,
                    "weight": item.weight, "_item": item,
                    "value": value,
                    "source": source,
                    "reason": reason,
                    "disabled": False,
                    # AI Analist için: birleşik string'i geriye parse
                    # etmek yerine ham sayısal bileşenler ayrıca
                    # taşınıyor (analiz anındaki AYNI DataPoint'lerden,
                    # yeniden fetch edilmeden).
                    "components": {
                        "volume_24h": dp_vol.value if dp_vol.available else None,
                        "spread_pct": dp_spread.value if dp_spread.available else None,
                    },
                })
                continue

            metric = _metric_for_label(item.label)
            dp = fetcher.fetch(symbol, metric)
            ans = dp.status if dp.available else "nodata"
            answers.append({
                "stage": stage.title, "question": item.label, "answer": ans,
                "weight": item.weight, "_item": item,
                "value": dp.value, "source": dp.source, "reason": dp.reason,
                "disabled": False,
            })

    if source_status is None:
        source_status = fetcher.get_status_report()
        news_detail = fetcher.get_news_detail(symbol)
        # TECHNICAL STRUCTURE ENGINE → AI ANALYST INTEGRATION V1: bu
        # ZATEN _build_context() içinde hesaplanmış (yeni network
        # çağrısı YOK), yalnız cache'ten okunuyor -- ScoreEngine/
        # report'u hiç etkilemez, yalnız AI Analyst context'ine
        # additive olarak taşınır (bkz. start_ai_analyst).
        technical_structure = fetcher.get_technical_structure(symbol)
    else:
        news_detail = {"items": [], "result": {}}
        technical_structure = None

    if should_cancel():
        raise Level1Cancelled()

    on_progress("Rapor hazırlanıyor...")
    _t_report = time.time()
    score_engine = ScoreEngine(SIGNAL_STAGE_WEIGHTS, RISK_STAGE_WEIGHTS,
                                VetoEngine(VETO_RULES))
    report = score_engine.full_report(answers, STAGES_CONFIG, symbol)

    # MODEL D — additive orchestration katmanı: report["vetos"]/
    # verdict_title/verdict_desc/entry_status/skorlar HİÇ DOKUNULMAZ,
    # yalnız zaten hesaplanmış technical_structure/BTC-1D verisinden
    # YENİ, SAF alanlar türetilip report'a EKLENİR. Herhangi bir
    # primitive eksikse (fail-closed) restricted_candidate=False
    # kalır, mevcut hard veto davranışı hiç değişmez.
    recent_class = "unknown"
    confirmed_structure = None
    if technical_structure and technical_structure.get("status") == "ok":
        confirmed_structure = technical_structure.get("trend_structure")
        recent_class = classify_recent_price_action(
            technical_structure.get("recent_price_action"))
    btc_daily_bearish, btc_r7_pct = (None, None)
    if isinstance(fetcher, RealFetcher):
        btc_daily_bearish, btc_r7_pct = fetcher.get_btc_regime_r7(symbol)
    btc_strong_down = evaluate_btc_strong_down(btc_daily_bearish, btc_r7_pct)
    is_model_d_candidate, model_d_reason = evaluate_model_d_candidate(
        symbol, report.get("vetos", []), confirmed_structure,
        recent_class, btc_strong_down)
    report["recent_price_action_class"] = recent_class
    report["btc_7d_return_pct"] = btc_r7_pct
    report["btc_strong_down"] = btc_strong_down
    report["restricted_candidate"] = is_model_d_candidate
    report["restricted_reason"] = model_d_reason

    if is_test:
        # Test profilleri History'ye hiç kaydedilmiyor; sözleşim
        # tutarlılığı için yine de alanlar dolduruluyor.
        report["analysis_time"] = datetime.now(timezone.utc).isoformat()
        report["analysis_price"] = None
    else:
        report["analysis_time"], report["analysis_price"] = capture_analysis_snapshot(symbol)
    print(f"[TIMING] ScoreEngine.full_report + snapshot: {time.time() - _t_report:.1f}s", flush=True)
    print(f"[TIMING] >>>>> TOPLAM LEVEL 1 ({symbol}): {time.time() - _t_level1:.1f}s <<<<<", flush=True)
    return {
        "report": report, "symbol": symbol, "source_status": source_status,
        "news_detail": news_detail, "is_test": is_test,
        "technical_structure": technical_structure,
    }


class Level1Worker(QThread):
    """request_id: AI Analyst'teki stale-result korumasının Level 1
    eşleniği. MainWindow yalnız `self._active_level1_request_id` ile
    eşleşen sinyalleri GUI'ye uygular (progress/finished_ok/failed/
    cancelled hepsi request_id taşır) -- eski/iptal edilmiş bir worker'ın
    gecikmiş sinyali yeni analizi ASLA ezemez.

    Cooperative cancellation: QThread.terminate() KULLANILMAZ. Bunun yerine
    Qt'nin kendi requestInterruption()/isInterruptionRequested() mekanizması
    kullanılır -- MainWindow iptal isteğinde yalnız requestInterruption()
    çağırır (worker'ı öldürmez), worker kendi güvenli kontrol noktalarında
    (aşama başları + RealFetcher._build_context içindeki _check_cancel())
    bunu görüp Level1Cancelled fırlatarak run()'dan NORMAL şekilde çıkar.
    Aktif bloklanmış bir ağ çağrısı (Binance/CoinGecko/CMC/RSS/Anthropic/
    TradingView) asla zorla kesilmez -- yalnız çağrı kendi doğal
    (bounded) süresiyle döndükten SONRA bir sonraki kontrol noktasında
    iptal fark edilir.

    MOBILE WEB MVP / PHASE 1: run()'ın gövdesi artık `run_level1_core()`'a
    (modül seviyesi, Qt'siz, web wrapper'ıyla PAYLAŞILAN canonical fonksiyon)
    taşındı -- bu sınıf yalnız Qt Signal'lerini bu fonksiyona bağlayan ince
    bir adaptördür, motor mantığının kendisi BURADA değil."""
    progress = Signal(str, str)          # (request_id, msg)
    finished_ok = Signal(dict)           # dict içinde "request_id" var
    failed = Signal(str, str)            # (request_id, msg)
    cancelled = Signal(str)              # (request_id)

    def __init__(self, symbol: str, mock_fetcher, included_onchain: set, request_id: str, parent=None):
        super().__init__(parent)
        self.symbol = symbol.upper()
        self.mock_fetcher = mock_fetcher
        self.included_onchain = included_onchain  # tik edilen metrik anahtarları (kapsama dahil)
        self.request_id = request_id

    def run(self):
        try:
            result = run_level1_core(
                self.symbol, self.mock_fetcher, self.included_onchain,
                on_progress=lambda msg: self.progress.emit(self.request_id, msg),
                should_cancel=self.isInterruptionRequested)
            self.finished_ok.emit({"request_id": self.request_id, **result})
        except Level1Cancelled:
            print(f"[LEVEL1] {self.request_id} iptal edildi, run() normal şekilde çıkıyor", flush=True)
            self.cancelled.emit(self.request_id)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.failed.emit(self.request_id, str(e))


class AIAnalystWorker(QThread):
    """Level1Worker'dan TAMAMEN bağımsız, ayrı thread. Deterministik rapor
    zaten kullanıcıya gösterildikten SONRA başlar; başarısız olursa yalnız
    kendi sinyalini (failed) yayınlar, ana analizi hiç etkilemez.
    request_id, stale-response korumasının GUI tarafındaki anahtarıdır."""
    ready = Signal(dict)
    failed = Signal(str, str)  # (request_id, reason)

    def __init__(self, request_id: str, report: dict, symbol: str, technical_structure: dict = None, parent=None):
        super().__init__(parent)
        self.request_id = request_id
        self.report = report
        self.symbol = symbol
        self.technical_structure = technical_structure

    def run(self):
        _t_worker_start = time.time()
        try:
            context = build_ai_analyst_context(self.report, self.symbol, self.technical_structure)
            mode = context["mode"]
            _t_context = time.time()
            print(f"[AI ANALIST][TIMING] build_ai_analyst_context: {_t_context - _t_worker_start:.2f}s "
                  f"(mode={mode})", flush=True)
            # CONTROLLED RELIABILITY FIX V1: ilk çağrı + (yalnız dar bir
            # whitelist'teki mekanik hatalarda) en fazla 1 düzeltici retry --
            # bkz. run_ai_analyst_with_corrective_retry. validate_ai_analyst_
            # response HİÇ değişmedi, karar HER ZAMAN yalnız o fonksiyondan
            # gelir; burada yalnız orkestrasyon var.
            retry_result = run_ai_analyst_with_corrective_retry(context, mode, request_id=self.request_id)
            print(f"[AI ANALIST][TIMING] >>>>> TOPLAM AIAnalystWorker (context+request_analysis"
                  f"{'+corrective_retry' if retry_result['retry_triggered'] else ''}): "
                  f"{time.time() - _t_worker_start:.2f}s <<<<<", flush=True)
            if retry_result["primary_reason"] == "api_unavailable":
                self.failed.emit(self.request_id, "api_unavailable")
                return
            if retry_result["retry_triggered"]:
                print(f"[AI ANALIST][RETRY] whitelist eslesti: primary_reason="
                      f"{retry_result['retry_top_reason']} sub_reason={retry_result['retry_sub_reason']} "
                      f"-> retry_ok={retry_result['retry_ok']}", flush=True)
            if not retry_result["final_ok"]:
                # Kullanıcıya GÖSTERİLMEZ (GUI hep aynı sade nötr metni gösterir) —
                # yalnız debug/console için gerçek reddedilme sebebi. Fail-closed
                # davranış DEĞİŞMEDİ, yalnız gözlemlenebilirlik eklendi.
                # Konsol/IDLE'a ek olarak, kullanıcının uygulamayı konsolsuz
                # (çift tık ile .pyw) çalıştırdığı durumlar için de teşhis
                # edilebilir olsun diye AYRICA logs/ai_analyst_validation.log
                # dosyasına yazılır (yalnız FAIL durumunda -- başarılı yanıtlar
                # hiç loglanmaz, bkz. _log_ai_validation_failure).
                reason = retry_result["final_reason"]
                print(f"[AI ANALIST] validation failed (primary_reason={retry_result['primary_reason']}, "
                      f"retry_triggered={retry_result['retry_triggered']}): {reason}", flush=True)
                _log_ai_validation_failure(self.symbol, self.request_id, mode, reason,
                                            retry_result["final_raw"], context)
                self.failed.emit(self.request_id, reason)
                return
            parsed = retry_result["final_raw"]
            # factor_labels: reassessment_triggers render'ının GUI'de deterministik
            # başlık üretebilmesi için factor_id->label eşlemesi. Claude'un cevabı
            # yalnız factor_id taşıyor; insan-okunur label context'ten (Claude'un
            # hiç görmediği bir kanaldan değil, kendisine de gösterilen aynı
            # usable_factors listesinden) buraya taşınıyor.
            factor_labels = {f["factor_id"]: f["label"] for f in context["usable_factors"]}
            # structure_context: PRESENTATION V1 -- zone_ref'li watchpoint'lerin
            # GUI/clipboard'da deterministik zone_low/zone_high render edebilmesi
            # için context'te ZATEN var olan (yeniden hesaplanmayan) partisyon
            # aynen taşınıyor. Karar mantığına dokunmuyor, yalnız görüntüleme.
            self.ready.emit({"request_id": self.request_id, "mode": mode, "data": parsed,
                              "factor_labels": factor_labels, "symbol": self.symbol,
                              "structure_context": context.get("structure_context")})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.failed.emit(self.request_id, f"exception:{e}")


# Eşleşme bulunamayan bir label için kullanılan fail-closed sentinel.
# Herhangi bir gerçek metrik adıyla KESİNLİKLE çakışmaz -- RealFetcher.fetch()
# bunu kendi `if metric == ...` zincirinin hiçbirinde tanımayıp zaten var olan
# son satırına (`return nodata("", "Desteklenmeyen metrik")`) düşer. Böylece
# tek bir eşleşmeyen soru yalnız KENDİ cevabını nodata yapar, ne yanlış bir
# metrik'e (eskiden: sessizce "fear_greed") bağlanır ne de analizi çökertir.
_UNMAPPED_METRIC_SENTINEL = "__unmapped_metric_label__"


def _metric_for_label(label: str) -> str:
    maps = {
        "24s hacim / 7g ortalama": "volume_24h",
        "Coin en az 2 büyük borsada listeli": "exchange_count",
        "Likidite derinliği yeterli": "slippage_pct",
        "Günlük volatilite > %3 veya Bollinger squeeze": "volatility_pct",
        "Fiyat 50 EMA üzerinde": "price",
        "Fiyat EMA50'den makul uzaklıkta": "ema50_distance_pct",
        "Son 24 saat fiyat değişimi": "price_change_24h_pct",
        "RSI (14)": "rsi",
        "MACD histogram": "macd_bullish",
        "OBV yükseliş": "obv_rising",
        "Bollinger Bands daralması": "bb_squeeze_or_break",
        "MVRV oranı": "mvrv",
        "Borsalardan net çıkış": "exchange_outflow",
        "Balina cüzdan": "whale_accumulation",
        "SOPR 1'in altına": "sopr_recovery",
        "Aktif adres": "active_addresses_rising",
        "Stablecoin inflow": "stablecoin_inflow",
        "DXY (dolar endeksi)": "dxy_loosening",
        "BTC günlük trendi": "btc_above_ema50",
        "Funding rate dengeli": "funding_pct",
        "Open Interest": "oi_rising_aligned",
        "ETF'lere sürekli giriş": "etf_inflow",
        "Fear & Greed Index": "fear_greed",
        "TOTAL3": "total3_rising_or_btc_dom_falling",
        "Yakın destek seviyesi": "clear_support",
        "Yakın direnç hedefi": "rr_ratio",
        "token unlock": "unlock_pct",
        "hack / exploit": "recent_bad_event",
        "Volatilite risk seviyesi": "volatility_risk",
    }
    for key, val in maps.items():
        if key in label:
            return val
    return _UNMAPPED_METRIC_SENTINEL


class SymbolLoaderWorker(QThread):
    loaded = Signal(list)

    def run(self):
        try:
            symbols = BinanceClient.get_exchange_info_usdt_symbols()
        except Exception:
            symbols = []
        self.loaded.emit(symbols)


def detail_row(icon: str, color: str, title: str, subtitle: str = "",
               badge_text: str = None, badge_color: str = None) -> QFrame:
    """Popup'larda kullanılan kompakt, kart-benzeri liste satırı."""
    row = QFrame()
    row.setStyleSheet(f"QFrame {{ background-color: {CARD_ALT}; border-radius: 12px; }}")
    lay = QHBoxLayout(row)
    lay.setContentsMargins(12, 10, 12, 10)
    lay.setSpacing(10)
    lay.addWidget(icon_chip(icon, color, size=28))
    text_box = QVBoxLayout()
    text_box.setSpacing(2)
    text_box.addWidget(h_label(title, size=9.5, bold=True, wrap=True))
    if subtitle:
        text_box.addWidget(h_label(subtitle, size=8.5, color=TEXT_TERTIARY, wrap=True))
    lay.addLayout(text_box, 1)
    if badge_text:
        lay.addWidget(make_badge(badge_text, badge_color or NEUTRAL, size=8))
    return row


class DetailDialog(QDialog):
    """Sonuç sekmesindeki popup'lar için kart/satır tabanlı, temaya uygun pencere."""
    def __init__(self, title: str, parent=None, icon: str = "🔎", accent: str = None,
                 width: int = 720, height: int = 620):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(width, height)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 14)
        outer.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(icon_chip(icon, accent or BLUE, size=32))
        head.addWidget(h_label(title, size=14, bold=True))
        head.addStretch()
        outer.addLayout(head)

        inner = QWidget()
        self.body = QVBoxLayout(inner)
        self.body.setSpacing(10)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        outer.addWidget(area, 1)

        close_btn = QPushButton("Kapat")
        close_btn.setObjectName("Primary")
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

    def add(self, widget):
        self.body.addWidget(widget)
        return widget

    def add_empty(self, text: str):
        self.body.addWidget(h_label(text, size=10, color=TEXT_TERTIARY))

    def add_group(self, title: str, icon: str, accent: str, rows: list):
        card = Card(accent=accent)
        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        head_row.addWidget(icon_chip(icon, accent, size=24))
        head_row.addWidget(h_label(title, size=11, bold=True))
        head_row.addStretch()
        card.layout_.addLayout(head_row)
        for r in rows:
            card.add(r)
        self.add(card)
        return card


class InfoDialog(QDialog):
    def __init__(self, title: str, body_html_or_text: str, parent=None, is_html=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(680, 560)
        layout = QVBoxLayout(self)
        text = QTextEdit()
        text.setReadOnly(True)
        if is_html:
            text.setHtml(body_html_or_text)
        else:
            text.setPlainText(body_html_or_text)
        layout.addWidget(text)
        close_btn = QPushButton("Kapat")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kripto Sinyal Sistemi v6 — Karar Destek Motoru")
        self.resize(1180, 860)
        self.setMinimumSize(1000, 700)

        self.veto_engine = VetoEngine(VETO_RULES)
        self.score_engine = ScoreEngine(SIGNAL_STAGE_WEIGHTS, RISK_STAGE_WEIGHTS, self.veto_engine)
        self.threshold_engine = ThresholdEngine()
        self.mock_fetcher = MockFetcher(seed=42)
        self.history_db = HistoryDB()

        self.level2_entries = {}
        self.level2_groups = {}
        self.level2_bool_groups = {}
        self.level3_groups = {}

        self._last_report = None
        self._last_symbol = ""
        self._last_level = ""
        self._last_news_detail = {"items": [], "result": {}}
        self._last_source_status = {}

        # Level 1 — AI Analyst'teki _ai_workers/_active_ai_request_id deseninin
        # birebir eşleniği. TÜM çalışan Level1Worker'lar (yalnız sonuncusu
        # değil) request_id -> worker sözlüğünde GÜÇLÜ referansla tutulur; bir
        # worker'ın bitmesi başka bir worker'ı etkilemez. Yalnız
        # _active_level1_request_id ile eşleşen sinyal (progress/finished_ok/
        # failed/cancelled) GUI state'ini değiştirebilir -- iptal edilmiş veya
        # "mantıken artık aktif değil" sayılan bir worker'ın gecikmiş sinyali
        # sessizce yok sayılır.
        self._level1_workers = {}
        self._active_level1_request_id = None
        self._level1_request_seq = 0

        # AI Analist V1 — deterministik akıştan izole. request_id, stale
        # response korumasının anahtarı: yalnız _active_ai_request_id ile
        # eşleşen sonuç GUI'ye uygulanır. TÜM çalışan worker'lar (yalnız
        # sonuncusu değil) request_id -> worker sözlüğünde GÜÇLÜ referansla
        # tutulur; bir worker'ın bitmesi başka bir worker'ı etkilemez, yeni
        # analiz başlaması eskisini öldürmez (yalnız sonucu stale sayılır).
        self._ai_workers = {}
        self._active_ai_request_id = None
        # PRESENTATION V1 (Raporu Kopyala + AI Analyst): copy_report()'un GUI'de
        # gösterilenle AYNI semantik içeriği panoya yazabilmesi için AI'nin
        # şu anki durumu + (varsa) son doğrulanmış AI sonucu burada tutulur.
        # _last_ai_result yalnız request_id'si _active_ai_request_id ile
        # eşleşiyorsa "güncel/stale değil" sayılır -- mevcut stale-response
        # korumasıyla AYNI mekanizma, yeni paralel bir state sistemi YOK.
        self._ai_state = "none"  # none | disabled | loading | ready | failed
        self._last_ai_result = None
        # Uygulama kapanış süreci -- hem AI Analyst hem Level1Worker yeni
        # istek başlatmayı bu bayrakla reddeder (bkz. start_ai_analyst,
        # run_level1); closeEvent hem _ai_workers hem _level1_workers boşalana
        # kadar kapanmayı erteler.
        self._ai_shutting_down = False

        self.build_ui()

        QTimer.singleShot(400, self.load_binance_symbols)
        QTimer.singleShot(1000, self.refresh_all_tracking)

    def closeEvent(self, event):
        # Cooperative shutdown — QThread.terminate() KULLANILMAZ (çalışan
        # Python/Anthropic/ağ thread'ini keyfi bir noktada zorla kesmek
        # kaynak/lock/state bozulması riski taşır). Bunun yerine: hâlâ çalışan
        # AI Analist VEYA Level1Worker varsa kapanışı ERTELE (event.ignore()),
        # yeni istek başlatılmasını durdur (_ai_shutting_down bayrağı hem
        # start_ai_analyst hem run_level1'de kontrol ediliyor), Level1Worker'lara
        # cooperative requestInterruption() gönder (terminate() DEĞİL — worker
        # kendi güvenli kontrol noktasında normal çıkar), ve worker'ların
        # NORMAL tamamlanmasını bekle. Son worker bittiğinde pencere kendini
        # güvenle kapatır.
        active_workers = list(self._ai_workers.values()) + list(self._level1_workers.values())
        if not active_workers:
            super().closeEvent(event)
            return

        if not self._ai_shutting_down:
            self._ai_shutting_down = True
            self.res_ai_content_label.setText(
                "Uygulama kapanıyor — devam eden AI Analist isteği tamamlanana kadar bekleniyor...")
            if self._level1_workers:
                self.l1_status_label.setText(
                    "Uygulama kapanıyor — devam eden analiz güvenli şekilde durduruluyor...")
            for worker in list(self._ai_workers.values()):
                worker.finished.connect(self._retry_close_after_ai_shutdown)
            for worker in list(self._level1_workers.values()):
                worker.requestInterruption()  # cooperative -- terminate() değil
                worker.finished.connect(self._retry_close_after_ai_shutdown)

        event.ignore()

    def _retry_close_after_ai_shutdown(self):
        if not self._ai_workers and not self._level1_workers:
            self.close()  # worker kalmadı -> closeEvent tekrar tetiklenir, bu kez gerçekten kapanır

    # ─────────────────────────────────────────────────────────────
    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Sol kenar çubuğu: marka + gezinme ─────────────────────
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(232)
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(18, 22, 18, 18)
        sb.setSpacing(4)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        brand_row.addWidget(h_label("📈", size=20))
        brand_box = QVBoxLayout()
        brand_box.setSpacing(0)
        brand_box.addWidget(h_label("KRİPTO SİNYAL", size=12.5, bold=True))
        brand_box.addWidget(h_label("Karar Destek Motoru", size=8, color=TEXT_TERTIARY))
        brand_row.addLayout(brand_box)
        brand_row.addStretch()
        sb.addLayout(brand_row)
        sb.addSpacing(6)

        badge_row = QHBoxLayout()
        badge_row.addWidget(make_badge("v6 PRO", ACCENT2, size=8))
        badge_row.addStretch()
        sb.addLayout(badge_row)
        sb.addSpacing(18)

        self.tabs = QStackedWidget()
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons = []

        def add_nav(text, icon):
            btn = QPushButton(f"  {icon}   {text}")
            btn.setObjectName("NavItem")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            self._nav_group.addButton(btn)
            sb.addWidget(btn)
            self._nav_buttons.append(btn)
            return btn

        nav_l1 = add_nav("Level 1 — Otomatik", "⚡")
        nav_l2 = add_nav("Level 2 — Yarı Otomatik", "🧮")
        nav_l3 = add_nav("Level 3 — Manuel", "✍️")
        nav_res = add_nav("Sonuç", "📊")
        nav_hist = add_nav("Geçmiş", "🕘")

        sb.addStretch()
        sb.addWidget(h_label("© BRKZGRC", size=8, color=TEXT_TERTIARY))
        outer.addWidget(sidebar)

        # ── Sağ taraf: içerik alanı ─────────────────────────────
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self.tabs, 1)
        outer.addWidget(content, 1)

        self.tab1 = QWidget()
        self.tabs.addWidget(self.tab1)
        self.build_level1_tab()

        self.tab2 = QWidget()
        self.tabs.addWidget(self.tab2)
        self.build_level2_tab()

        self.tab3 = QWidget()
        self.tabs.addWidget(self.tab3)
        self.build_level3_tab()

        self.tab_result = QWidget()
        self.tabs.addWidget(self.tab_result)
        self.build_result_tab()

        self.tab_history = QWidget()
        self.tabs.addWidget(self.tab_history)
        self.build_history_tab()

        page_map = [nav_l1, nav_l2, nav_l3, nav_res, nav_hist]
        for i, btn in enumerate(page_map):
            btn.clicked.connect(lambda checked, idx=i: self.tabs.setCurrentIndex(idx))
        self.tabs.currentChanged.connect(
            lambda idx: page_map[idx].setChecked(True) if 0 <= idx < len(page_map) else None)
        nav_l1.setChecked(True)

    def _scroll_wrap(self, inner: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        return area

    def _toggle_ai_dock(self):
        """Yalnız panel görünürlüğünü aç/kapatır — AI worker'ı hiç tetiklemez/
        durdurmaz, yalnızca res_ai_content_label'ın (zaten mevcut/güncel olan)
        içeriğini gösterip gizler. Lifecycle'a hiçbir etkisi yok."""
        self.res_ai_dock.setVisible(self.res_ai_toggle_btn.isChecked())

    # ═══════════════════════════════════════════════════════════
    # LEVEL 1
    # ═══════════════════════════════════════════════════════════
    def build_level1_tab(self):
        outer = QVBoxLayout(self.tab1)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(14)

        outer.addWidget(h_label("⚡  Otomatik Analiz", size=16, bold=True))
        outer.addWidget(h_label(
            "Binance, CoinGecko, CoinMarketCap ve haber kaynaklarından gerçek veri çekilir. "
            "Bulunamayan veriler dürüstçe 'Veri Yok' olarak işaretlenir.",
            size=9.5, color=TEXT_SECONDARY, wrap=True))

        sel_card = Card(accent=BLUE, glow=True)
        sel_row = QHBoxLayout()
        sel_row.addWidget(h_label("Coin:"))
        self.l1_symbol = QComboBox()
        self.l1_symbol.setEditable(True)
        self.l1_symbol.setMinimumWidth(180)
        self.l1_symbol.addItems(["BTC", "ETH", "SOL"])
        self.l1_symbol.insertSeparator(3)
        self.l1_symbol.addItems(["TEST_GOOD", "TEST_MEDIUM", "TEST_BAD", "TEST_VETO", "TEST_MISSING"])
        self.l1_completer = QCompleter([self.l1_symbol.itemText(i) for i in range(self.l1_symbol.count())])
        self.l1_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.l1_symbol.setCompleter(self.l1_completer)
        sel_row.addWidget(self.l1_symbol)

        sel_row.addStretch()
        self.l1_run_btn = QPushButton("▶  ANALİZ ET")
        self.l1_run_btn.setObjectName("Primary")
        self.l1_run_btn.clicked.connect(self._on_l1_button_clicked)
        sel_row.addWidget(self.l1_run_btn)
        sel_card.layout_.addLayout(sel_row)

        # On-chain: tek tek tik. Tik edilmeyen metrik hiç araştırılmaz/sorulmaz,
        # analize hiçbir etkisi olmaz (kapsam dışı). Tik edilen metrik KAPSAMA
        # dahil edilir ve gerçek bir otomatik kaynak (CoinMetrics Community API)
        # üzerinden araştırılır — sonuç bulunursa yes/no, güvenilir kaynak yoksa
        # (whale_accumulation, stablecoin_inflow; bazı coin'lerde diğer ikisi de)
        # dürüstçe nodata. Tik hiçbir zaman "Evet" anlamına gelmez.
        onchain_layout = QHBoxLayout()
        onchain_layout.setSpacing(14)
        self.l1_onchain_checks: Dict[str, QCheckBox] = {}
        for key, label in [
            ("exchange_outflow", "Borsalardan net çıkış"),
            ("whale_accumulation", "Balina birikimi"),
            ("active_addresses_rising", "Aktif adres artışı"),
            ("stablecoin_inflow", "Stablecoin inflow"),
        ]:
            cb = QCheckBox(label)
            self.l1_onchain_checks[key] = cb
            onchain_layout.addWidget(cb)
        onchain_layout.addStretch()
        sel_card.layout_.addLayout(onchain_layout)
        outer.addWidget(sel_card)

        progress_card = Card()
        progress_card.add(h_label("▸ ANALİZ İLERLEMESİ", size=10, bold=True, color=TEXT_TERTIARY))
        self.l1_progress_bar = QProgressBar()
        self.l1_progress_bar.setRange(0, 0)
        self.l1_progress_bar.setVisible(False)
        progress_card.add(self.l1_progress_bar)
        self.l1_progress_list = QListWidget()
        self.l1_progress_list.setFixedHeight(140)
        progress_card.add(self.l1_progress_list)
        outer.addWidget(progress_card)

        self.l1_status_label = h_label("Coin seçip 'Analiz Et' butonuna basın.", size=9.5, color=TEXT_TERTIARY)
        outer.addWidget(self.l1_status_label)
        outer.addStretch()

    def load_binance_symbols(self):
        self._symbol_loader = SymbolLoaderWorker()
        self._symbol_loader.loaded.connect(self._on_symbols_loaded)
        self._symbol_loader.start()

    def _on_symbols_loaded(self, symbols):
        if not symbols:
            self.l1_status_label.setText(
                "Binance coin listesi alınamadı — sabit liste kullanılıyor (BTC, ETH, SOL).")
            return
        current = self.l1_symbol.currentText()
        self.l1_symbol.blockSignals(True)
        self.l1_symbol.clear()
        self.l1_symbol.addItems(symbols)
        self.l1_symbol.insertSeparator(len(symbols))
        self.l1_symbol.addItems(["TEST_GOOD", "TEST_MEDIUM", "TEST_BAD", "TEST_VETO", "TEST_MISSING"])
        self.l1_symbol.setEditText(current or "BTC")
        self.l1_symbol.blockSignals(False)
        self.l1_completer = QCompleter(symbols)
        self.l1_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.l1_symbol.setCompleter(self.l1_completer)
        self.l1_status_label.setText(f"{len(symbols)} USDT paritesi yüklendi.")

    def _on_l1_button_clicked(self):
        # Tek buton iki işlevi taşır: RUNNING değilken ANALİZ ET, RUNNING iken
        # ANALİZİ DURDUR. Durum, _active_level1_request_id'nin dolu/boş
        # olmasından okunur (ayrı bir bool state'e gerek yok).
        if self._active_level1_request_id is not None:
            self.cancel_level1()
        else:
            self.run_level1()

    def run_level1(self):
        if self._ai_shutting_down:
            return  # uygulama kapanma sürecinde — yeni analiz başlatılmaz
        symbol = self.l1_symbol.currentText().strip().upper()
        if not symbol:
            QMessageBox.warning(self, "Uyarı", "Bir coin sembolü girin.")
            return

        # Tik edilen metrik yalnızca KAPSAMA dahil edilir — otomatik gerçek bir
        # kaynak aranır; "tik edildi" hiçbir zaman "Evet" anlamına gelmez.
        # Tik edilmeyen metrik dict'e hiç girmez -> Level1Worker onu tamamen
        # kapsam dışı (disabled) sayar, hiçbir fetch/araştırma çağrısı yapılmaz.
        included_onchain = {key for key, cb in self.l1_onchain_checks.items() if cb.isChecked()}

        self._level1_request_seq += 1
        request_id = f"{symbol}::{self._level1_request_seq}"
        self._active_level1_request_id = request_id

        self.l1_run_btn.setText("⏹  ANALİZİ DURDUR")
        self.l1_progress_bar.setVisible(True)
        self.l1_progress_list.clear()
        self.l1_status_label.setText(f"{symbol} analiz ediliyor...")
        # Yeni analiz başlıyor — önceki AI içeriği (varsa) artık geçersiz;
        # aktif request_id sıfırlanır ki gecikmiş eski bir AI yanıtı bu
        # yeni analize sessizce uygulanmasın (stale-response koruması).
        # Panel açık/kapalı tercihi (res_ai_dock) KULLANICI KARARI — burada
        # değiştirilmiyor, yalnız İÇERİK temizleniyor ki panel açıksa bile
        # eski coin'in AI sonucu bir an için görünmesin.
        self._active_ai_request_id = None
        self._ai_state = "none"
        self._last_ai_result = None
        self.res_ai_content_label.setText("")

        worker = Level1Worker(symbol, self.mock_fetcher, included_onchain, request_id, parent=self)
        worker.progress.connect(self._on_level1_progress)
        worker.finished_ok.connect(self._on_level1_finished)
        worker.failed.connect(self._on_level1_failed)
        worker.cancelled.connect(self._on_level1_cancelled)
        # Worker GERÇEKTEN bittiğinde (aktif/stale FARK ETMEKSİZİN) registry'den
        # çıkar ve deleteLater() çağır -- AI Analyst'teki _cleanup_ai_worker ile
        # birebir aynı desen, hiçbir worker referansı sızmasın.
        worker.finished.connect(lambda rid=request_id: self._cleanup_level1_worker(rid))
        self._level1_workers[request_id] = worker
        worker.start()

    def cancel_level1(self):
        """Cooperative iptal: worker'a yalnız requestInterruption() sinyali
        gönderilir (terminate() YOK) -- worker kendi güvenli kontrol
        noktalarında bunu görüp normal şekilde çıkacak. GUI durumu ise
        ANINDA (worker'ın fiziksel olarak bitmesini BEKLEMEDEN) sıfırlanır:
        _active_level1_request_id None'a çekilir, bu andan itibaren eski
        worker'dan gelecek HER sinyal (progress/finished_ok/failed/cancelled)
        request_id eşleşmediği için otomatik stale sayılıp yok sayılacaktır
        (bkz. _on_level1_* handler'ları). Bu güvenli, çünkü RealFetcher'ın
        kullandığı tüm paylaşılan kaynaklar (persistent Anthropic client'lar,
        CoinGecko/News session'ları, TradingView _client_lock) eşzamanlı
        kullanım için ayrıca doğrulandı/korumaya alındı (bkz. FAZ B audit)."""
        worker = self._level1_workers.get(self._active_level1_request_id)
        if worker is not None:
            worker.requestInterruption()
            print(f"[LEVEL1] iptal istendi: {self._active_level1_request_id} "
                  f"(worker arka planda doğal olarak bitene kadar sessizce çalışmaya devam edebilir)",
                  flush=True)
        self._active_level1_request_id = None
        self.l1_run_btn.setText("▶  ANALİZ ET")
        self.l1_progress_bar.setVisible(False)
        self.l1_status_label.setText("İptal edildi.")

    def _cleanup_level1_worker(self, request_id: str):
        worker = self._level1_workers.pop(request_id, None)
        if worker is not None:
            worker.deleteLater()

    def _on_level1_progress(self, request_id: str, msg: str):
        if request_id != self._active_level1_request_id:
            return  # stale — artık aktif olmayan (iptal edilmiş/eskimiş) bir worker'ın mesajı
        self.l1_progress_list.addItem(msg)
        self.l1_progress_list.scrollToBottom()

    def _on_level1_finished(self, result: dict):
        if result.get("request_id") != self._active_level1_request_id:
            return  # stale — sessizce yok say: rapor render edilmez, History'ye yazılmaz, AI Analyst başlamaz
        self._active_level1_request_id = None
        self.l1_run_btn.setText("▶  ANALİZ ET")
        self.l1_progress_bar.setVisible(False)
        report = result["report"]
        symbol = result["symbol"]
        self._last_source_status = result["source_status"]
        self._last_news_detail = result["news_detail"]
        self._last_level = "Level 1 — Otomatik"
        self._is_test_profile = result["is_test"]
        self.l1_status_label.setText(f"{symbol} analizi tamamlandı.")
        self.display_report_in_result(report, symbol)
        self.tabs.setCurrentWidget(self.tab_result)
        # Deterministik rapor ZATEN gösterildi — AI Analist yalnız BUNDAN
        # SONRA, ayrı bir thread'de, kullanıcıyı hiç beklemeden başlar.
        if not result["is_test"]:
            self.start_ai_analyst(report, symbol, result.get("technical_structure"))

    def _on_level1_failed(self, request_id: str, msg: str):
        if request_id != self._active_level1_request_id:
            return  # stale
        self._active_level1_request_id = None
        self.l1_run_btn.setText("▶  ANALİZ ET")
        self.l1_progress_bar.setVisible(False)
        self.l1_status_label.setText("Analiz başarısız.")
        QMessageBox.critical(self, "Hata", f"Analiz sırasında beklenmeyen bir hata oluştu:\n{msg}")

    def _on_level1_cancelled(self, request_id: str):
        # GUI durumu zaten cancel_level1() içinde ANINDA sıfırlanmıştı --
        # bu yalnız worker'ın GERÇEKTEN kendi isteğiyle çıktığının teyidi
        # (debug/log amaçlı). request_id o an aktif olsa bile (kullanıcı
        # iptal ETMEDEN worker'ın kendi kendine cancelled üretmesi teorik
        # olarak imkansız -- yalnız requestInterruption() sonrası oluşur)
        # burada GUI'ye ekstra bir şey yazmıyoruz, çift mesaj göstermemek için.
        print(f"[LEVEL1] {request_id} worker'ı gerçekten iptal olarak sonlandı.", flush=True)

    # ═══════════════════════════════════════════════════════════
    # AI ANALİST V1 — deterministik akıştan izole
    # ═══════════════════════════════════════════════════════════
    def start_ai_analyst(self, report: dict, symbol: str, technical_structure: dict = None):
        if self._ai_shutting_down:
            return  # uygulama kapanma sürecinde — yeni AI isteği başlatılmaz
        if not AI_ANALYST_ENABLED or not ANTHROPIC_API_KEY:
            self.res_ai_dock.setVisible(False)
            self._active_ai_request_id = None
            self._ai_state = "disabled"
            self._last_ai_result = None
            return

        # Önceki isteği YALNIZ "artık aktif değil" say (stale-response
        # koruması _on_ai_analyst_ready/_failed'te request_id kontrolüyle
        # zaten yapılıyor) — eski worker'ı ÖLDÜRMEYE çalışmıyoruz, çalışmaya
        # devam edebilir; sonucu stale olduğu için sessizce yok sayılacak.
        request_id = f"{symbol.upper()}::{report.get('analysis_time')}"
        self._active_ai_request_id = request_id
        self._ai_state = "loading"
        self._last_ai_result = None

        self.res_ai_content_label.setText("AI yorumu oluşturuluyor...")

        worker = AIAnalystWorker(request_id, report, symbol, technical_structure, parent=self)
        worker.ready.connect(self._on_ai_analyst_ready)
        worker.failed.connect(self._on_ai_analyst_failed)
        # Bir worker'ın bitmesi yalnız KENDİSİNİ collection'dan çıkarır ve
        # deleteLater() çağırır — başka hiçbir worker'ı etkilemez.
        worker.finished.connect(lambda rid=request_id: self._cleanup_ai_worker(rid))
        self._ai_workers[request_id] = worker
        worker.start()

    def _cleanup_ai_worker(self, request_id: str):
        worker = self._ai_workers.pop(request_id, None)
        if worker is not None:
            worker.deleteLater()

    def _on_ai_analyst_ready(self, result: dict):
        if result.get("request_id") != self._active_ai_request_id:
            return  # stale — ekranda artık başka bir analiz var, sessizce yok say
        self._ai_state = "ready"
        self._last_ai_result = result
        self._render_ai_analyst_card(result["mode"], result["data"], result.get("factor_labels", {}),
                                      result.get("structure_context"))

    def _on_ai_analyst_failed(self, request_id: str, reason: str):
        if request_id != self._active_ai_request_id:
            return  # stale
        self._ai_state = "failed"
        self._last_ai_result = None
        # Ana deterministik analiz hiç etkilenmedi — yalnız AI kartı nötr
        # bir "oluşturulamadı" durumuna geçiyor. Retry yok (V1).
        self.res_ai_content_label.setText(
            "AI yorumu oluşturulamadı. Deterministik analiz sonucu yukarıda değişmeden geçerlidir.")

    def _render_reassessment_trigger(self, it: dict, factor_labels: dict) -> list:
        """current_state_text/target_state_text Claude'dan GELMEZ — FACTOR_TRANSITION_GUARDS
        + Claude'un seçtiği conditions (factor_id/status) üzerinden tamamen
        deterministik üretilir. Claude yalnız type/conditions/meaning/evidence
        döndürür; yes/wait/no kodları burada da kullanıcıya HİÇ gösterilmez —
        yalnız guard metinleri (doğal Türkçe) render edilir.

        selection_basis=="decision_critical" özel olarak ele alınır: "Sonuç:"
        satırı — bu değişimin motorun veto koşulunu tetikleyip analiz
        durumunu HARD_RESTRICTED'e düşüreceği bilgisi — SABİT bir şablondan
        gelir, Claude'un meaning'inden DEĞİL. Claude yalnız kendi analitik
        yorumunu (Anlamı satırı) üretir."""
        ttype = it["type"]
        conditions = it["conditions"]
        labels = [factor_labels.get(c["factor_id"], c["factor_id"]) for c in conditions]

        # NOT: Bu satırlar artık doğrudan (zaten HTML-güvenli) HTML parçaları olarak
        # döndürülür -- her dinamik/serbest metin (guard etiketleri, factor label'ları,
        # Claude'un ürettiği "meaning") html.escape() ile kaçırılıyor; yalnız burada
        # bizim yazdığımız SABİT etiketler (Şu an:, Anlamı: vb.) <b> ile kalın.
        esc = html.escape
        joined_labels = esc(" / ".join(labels))
        if it.get("selection_basis") == "decision_critical":
            c = conditions[0]
            guard = FACTOR_TRANSITION_GUARDS.get(c["factor_id"], {})
            out = [f"  ⛔ <b>KARAR-KRİTİK — {joined_labels}</b>"]
            out.append(f"     <b>Şu an:</b> {esc(str(guard.get(c['from_status'], c['from_status'])))}")
            out.append(f"     <b>Kritik değişim:</b> {esc(str(guard.get(c['to_status'], c['to_status'])))}")
            out.append("     <b>Sonuç:</b> Bu değişim motorun veto koşulunu tetikler ve "
                        "değerlendirme HARD_RESTRICTED duruma geçer.")
            out.append(f"     <b>Anlamı:</b> {esc(it['meaning'])}")
            return out

        header_by_type = {"hold": "KORUNMASI GEREKEN", "improve": "GÜÇLENME İÇİN İZLE",
                           "deteriorate": "ZAYIFLAMA RİSKİ"}
        icon_by_type = {"hold": "🛡️", "improve": "🔼", "deteriorate": "🔽"}

        out = [f"  {icon_by_type[ttype]} <b>{header_by_type[ttype]} — {joined_labels}</b>"]
        if len(conditions) == 1:
            c = conditions[0]
            guard = FACTOR_TRANSITION_GUARDS.get(c["factor_id"], {})
            if ttype == "hold":
                out.append(f"     <b>Şu an:</b> {esc(str(guard.get(c['status'], c['status'])))}")
                out.append("     <b>Korunması gereken:</b> bu durumun sürmesi")
            else:
                out.append(f"     <b>Şu an:</b> {esc(str(guard.get(c['from_status'], c['from_status'])))}")
                out.append(f"     <b>İzlenecek değişim:</b> {esc(str(guard.get(c['to_status'], c['to_status'])))}")
        else:
            out.append("     <b>Şu an:</b>")
            for c in conditions:
                guard = FACTOR_TRANSITION_GUARDS.get(c["factor_id"], {})
                status_key = c["status"] if ttype == "hold" else c["from_status"]
                out.append(f"       - {esc(str(guard.get(status_key, status_key)))}")
            out.append("     <b>Korunması gereken:</b> bu durumların birlikte sürmesi"
                        if ttype == "hold" else "     <b>İzlenecek değişim:</b>")
        out.append(f"     <b>Anlamı:</b> {esc(it['meaning'])}")
        return out

    _AI_WATCHPOINT_CATEGORY_LABELS = {
        "current_structure": "Şu an", "strengthens": "Güçlenme için", "weakens": "Zayıflama için"}

    # PRESENTATION V2 / DETERMINISTIC ZONE-EVENT + AI INTERPRETATION AYRIMI:
    # yalnız validator'ın izin verdiği İKİ geçerli category×zone_ref kombinasyonu
    # (bkz. _check_watchpoints._INVALID_WATCHPOINT_CATEGORY_ZONE_PAIRS) için sabit,
    # nötr Türkçe event ifadesi -- Claude'un kendi doğal dilinde bu event'i (ve
    # internal "nearest_*" token'ını) yeniden üretmesine hiç gerek kalmıyor.
    # current_structure bir event/tetikleyici iddiası taşımadığı için burada YOK
    # -- o hâlâ kendi serbest meaning metnine zone aralığını parantez içinde alır.
    _AI_WATCHPOINT_ZONE_EVENT_PHRASES = {
        ("strengthens", "nearest_resistance"): "Yakın direnç bölgesinin üzerine geçilmesi",
        ("weakens", "nearest_support"): "Yakın destek bölgesinin kaybedilmesi",
    }
    # RECENT PRICE ACTION V1 / WATCHPOINT TENSE FIX: canlı analysis_price'ın
    # zone'a göre GERÇEK konumu (ABOVE/INSIDE/BELOW, _zone_relation() ile
    # deterministik hesaplanır) biliniyorsa, event cümlesi "gelecekte olacak"
    # yerine doğru zamanda (gerçekleşmiş/test ediliyor/henüz olmadı) kurulur.
    # Claude bu tense'i HİÇ seçmez -- yalnız renderer, relation'a bakarak.
    # relation None ise (analysis_price yok) eski/varsayılan (gelecek zaman)
    # davranışa sessizce düşülür -- geriye dönük uyumlu, regresyon yok.
    _AI_WATCHPOINT_ZONE_EVENT_PHRASES_BY_RELATION = {
        ("strengthens", "nearest_resistance"): {
            "BELOW": "Yakın direnç bölgesinin üzerine geçilmesi",
            "INSIDE": "Fiyatın yakın direnç bölgesini test etmesi",
            "ABOVE": "Fiyatın yakın direnç bölgesinin üzerine geçmiş durumda olması",
        },
        ("weakens", "nearest_support"): {
            "ABOVE": "Yakın destek bölgesinin kaybedilmesi",
            "INSIDE": "Fiyatın yakın destek bölgesini test etmesi",
            "BELOW": "Fiyatın yakın destek bölgesinin altına geçmiş durumda olması",
        },
    }

    @staticmethod
    def _resolve_watchpoint_zone_obj(zone_ref, structure_context) -> Optional[dict]:
        """MARKET MAP V1 SEMANTIC CONSISTENCY FIX: Claude'un döndürdüğü zone_ref
        ('nearest_support'/'nearest_resistance' string'i, ŞEMA/VALIDATOR HİÇ
        DEĞİŞMEDİ) artık CANLI market_map.support_1/resistance_1'e resolve
        edilir -- bunlar TANIM GEREĞİ her zaman historical_origin'i kendi
        tarafıyla eşleşen (yani DAİMA active_support/active_resistance olan)
        zone'lardır, lost_support/broken_resistance ASLA buraya gelemez
        (bkz. _build_market_map). market_map hiç hesaplanamadıysa (analysis_price
        yok) -- YALNIZ bu durumda -- eski technical_structure tabanlı
        nearest_support/nearest_resistance'a (geriye dönük uyumlu) düşülür.
        market_map hesaplanmış AMA ilgili slot (support_1/resistance_1) None
        ise (ör. o yönde hiç aktif zone yoksa) BİLEREK eski/muhtemelen-stale
        zone'a DÜŞÜLMEZ -- None döner (fail-safe, uydurma/stale bilgi yok)."""
        if not zone_ref or not structure_context:
            return None
        market_map = structure_context.get("market_map")
        if market_map is not None:
            wanted_origin = "support" if zone_ref == "nearest_support" else "resistance"
            mm_key = "support_1" if zone_ref == "nearest_support" else "resistance_1"
            zone_entry = market_map.get(mm_key)
            if zone_entry is None:
                # Fiyat tam zone İÇİNDEYSE (support_test/resistance_test) bu
                # zone support_1/resistance_1'e HİÇ girmez (yalnız kesinlikle
                # dışarıda kalan aktif zone'lar girer) -- ama bu hâlâ GEÇERLİ,
                # güncel bir referanstır (stale DEĞİL), bu yüzden origin
                # eşleşiyorsa tested_zone'a düşülür. lost_support/
                # broken_resistance İSE burada ASLA kullanılmaz (onlar ayrı
                # bir semantik sınıf -- yalnız *_nearby listelerinde kalır).
                tested = market_map.get("tested_zone")
                if tested and tested.get("historical_origin") == wanted_origin:
                    zone_entry = tested
            return zone_entry
        return structure_context.get(zone_ref)

    @classmethod
    def _resolve_watchpoint_zone_range(cls, zone_ref, structure_context) -> Optional[str]:
        """PRESENTATION V1/V2 / ZONE_REF DETERMINISTIC RENDER: Claude zone_ref
        (yalnız 'nearest_support'/'nearest_resistance' anahtarı) döndürür,
        gerçek zone_low/zone_high (artık market_map üzerinden, bkz.
        _resolve_watchpoint_zone_obj) burada okunur -- Claude'dan hiçbir yeni
        fiyat/seviye üretilmez. Zone None ise hiçbir uydurma değer basılmaz."""
        zone = cls._resolve_watchpoint_zone_obj(zone_ref, structure_context)
        if not zone:
            return None
        return f"{zone['zone_low']:.4f}–{zone['zone_high']:.4f}"

    @classmethod
    def _resolve_watchpoint_zone_text(cls, zone_ref, structure_context) -> Optional[str]:
        """current_structure için: 'destek/direnç bölgesi L–H' -- yalnız konum
        tarifi, event iddiası taşımaz. Zone artık market_map'ten (DAİMA aktif
        rolde) geldiği için "eski/kaybedilmiş" gibi bir etikete gerek YOK --
        böyle bir zone burada hiç görünmez (bkz. _resolve_watchpoint_zone_obj).
        "Kaybedilmiş destek" gibi tarihsel bir bulgu artık yalnız Claude'un
        kendi serbest metninde, market_map:* evidence token'larıyla (Sistem
        Promptu madde 18) ifade edilir -- bu deterministik renderer'ın işi
        DEĞİLDİR."""
        rng = cls._resolve_watchpoint_zone_range(zone_ref, structure_context)
        if not rng:
            return None
        label = "destek" if zone_ref == "nearest_support" else "direnç"
        return f"{label} bölgesi {rng}"

    def _watchpoint_render_parts(self, it: dict, structure_context):
        """category+zone_ref+structure_context'ten deterministik olarak
        (fact_line, ai_meaning_line_or_None) üretir -- fact_line her zaman
        Claude'un meaning'inden BAĞIMSIZ, sabit şablon/veriden gelir; Claude'un
        kendi metni yalnız (varsa) ayrı bir 'Anlamı' satırında gösterilir.
        Döner: (kategori_etiketi, fact_metni, ai_meaning_veya_None)."""
        category = it.get("category")
        zone_ref = it.get("zone_ref")
        cat_label = self._AI_WATCHPOINT_CATEGORY_LABELS.get(category, category)
        # MARKET MAP V1 SEMANTIC CONSISTENCY FIX: relation artık ESKİ
        # "{zone_ref}_relation" alanından (o, TSE'nin closed-candle tabanlı
        # nearest_support/resistance'ına göre hesaplanmıştı ve artık
        # gösterilen zone'la EŞLEŞMEYEBİLİRDİ) DEĞİL, gerçekten resolve
        # edilen (market_map-tabanlı) zone'a karşı TAZE hesaplanır --
        # support_1/resistance_1 TANIM GEREĞİ her zaman "henüz olmamış"
        # tarafta olduğu için bu pratikte hep weakens+support->ABOVE,
        # strengthens+resistance->BELOW verir (relation None ise, ör.
        # market_map hiç hesaplanamadıysa, eski sabit tablo tarafı işler --
        # regresyon yok, geriye dönük uyumlu).
        resolved_zone = self._resolve_watchpoint_zone_obj(zone_ref, structure_context) if zone_ref else None
        analysis_price = (structure_context or {}).get("analysis_price")
        relation = _zone_relation(analysis_price, resolved_zone) if resolved_zone else None
        phrases_by_relation = self._AI_WATCHPOINT_ZONE_EVENT_PHRASES_BY_RELATION.get((category, zone_ref))
        if phrases_by_relation and relation and relation in phrases_by_relation:
            event_phrase = phrases_by_relation[relation]
        else:
            event_phrase = self._AI_WATCHPOINT_ZONE_EVENT_PHRASES.get((category, zone_ref))
        zone_range = self._resolve_watchpoint_zone_range(zone_ref, structure_context)
        if event_phrase and zone_range:
            # zone-tabanlı strengthens/weakens: fact backend'den, Claude'un
            # metni yalnız ayrı bir yorum satırı olarak eklenir.
            return cat_label, f"{event_phrase} ({zone_range})", it["meaning"]
        if zone_ref and zone_range:
            # current_structure + zone_ref: event iddiası yok, Claude'un kendi
            # konum tarifi + parantez içinde zone aralığı (mevcut V1 davranışı).
            return cat_label, f"{it['meaning']} ({self._resolve_watchpoint_zone_text(zone_ref, structure_context)})", None
        # factor-based (zone_ref yok): Claude'un meaning'i olduğu gibi.
        return cat_label, it["meaning"], None

    def _render_watchpoints_html(self, watchpoints: list, structure_context) -> list:
        esc = html.escape
        out = ["<b>Teknik izleme noktaları:</b>"]
        for it in watchpoints:
            cat_label, fact_text, ai_meaning = self._watchpoint_render_parts(it, structure_context)
            out.append(f"  • <b>{esc(cat_label)}:</b> {esc(fact_text)}")
            if ai_meaning:
                out.append(f"     <b>Anlamı:</b> {esc(ai_meaning)}")
        return out

    def _render_watchpoints_plain(self, watchpoints: list, structure_context) -> list:
        out = ["Teknik izleme noktaları:"]
        for it in watchpoints:
            cat_label, fact_text, ai_meaning = self._watchpoint_render_parts(it, structure_context)
            out.append(f"  • {cat_label}: {fact_text}")
            if ai_meaning:
                out.append(f"     Anlamı: {ai_meaning}")
        return out

    def _render_reassessment_trigger_plain(self, it: dict, factor_labels: dict) -> list:
        """_render_reassessment_trigger ile BİREBİR aynı bilgiyi, HTML
        etiketleri/&nbsp; olmadan üretir -- aynı canonical (candidate_id'den
        rekonstrükte edilmiş) veriden, aynı FACTOR_TRANSITION_GUARDS metinleriyle."""
        ttype = it["type"]
        conditions = it["conditions"]
        labels = [factor_labels.get(c["factor_id"], c["factor_id"]) for c in conditions]
        joined_labels = " / ".join(labels)
        if it.get("selection_basis") == "decision_critical":
            c = conditions[0]
            guard = FACTOR_TRANSITION_GUARDS.get(c["factor_id"], {})
            out = [f"  ⛔ KARAR-KRİTİK — {joined_labels}"]
            out.append(f"     Şu an: {guard.get(c['from_status'], c['from_status'])}")
            out.append(f"     Kritik değişim: {guard.get(c['to_status'], c['to_status'])}")
            out.append("     Sonuç: Bu değişim motorun veto koşulunu tetikler ve "
                        "değerlendirme HARD_RESTRICTED duruma geçer.")
            out.append(f"     Anlamı: {it['meaning']}")
            return out

        header_by_type = {"hold": "KORUNMASI GEREKEN", "improve": "GÜÇLENME İÇİN İZLE",
                           "deteriorate": "ZAYIFLAMA RİSKİ"}
        icon_by_type = {"hold": "🛡️", "improve": "🔼", "deteriorate": "🔽"}

        out = [f"  {icon_by_type[ttype]} {header_by_type[ttype]} — {joined_labels}"]
        if len(conditions) == 1:
            c = conditions[0]
            guard = FACTOR_TRANSITION_GUARDS.get(c["factor_id"], {})
            if ttype == "hold":
                out.append(f"     Şu an: {guard.get(c['status'], c['status'])}")
                out.append("     Korunması gereken: bu durumun sürmesi")
            else:
                out.append(f"     Şu an: {guard.get(c['from_status'], c['from_status'])}")
                out.append(f"     İzlenecek değişim: {guard.get(c['to_status'], c['to_status'])}")
        else:
            out.append("     Şu an:")
            for c in conditions:
                guard = FACTOR_TRANSITION_GUARDS.get(c["factor_id"], {})
                status_key = c["status"] if ttype == "hold" else c["from_status"]
                out.append(f"       - {guard.get(status_key, status_key)}")
            out.append("     Korunması gereken: bu durumların birlikte sürmesi"
                        if ttype == "hold" else "     İzlenecek değişim:")
        out.append(f"     Anlamı: {it['meaning']}")
        return out

    @staticmethod
    def _ai_html_indent(line: str) -> str:
        """Düz metindeki soldan boşluk girintisini HTML'de de korumak için --
        HTML birden çok boşluğu tek boşluğa sıkıştırır, bu yüzden satır başındaki
        boşluklar &nbsp;'a çevriliyor. Yalnız GİRİNTİ (satır başı) dönüştürülüyor,
        satırın geri kalanına (zaten escape edilmiş/<b> etiketli) dokunulmuyor."""
        stripped = line.lstrip(" ")
        n = len(line) - len(stripped)
        return ("&nbsp;" * n) + stripped

    def _render_ai_analyst_card(self, mode: str, data: dict, factor_labels: dict = None,
                                 structure_context: dict = None):
        # Bölüm başlıkları (ör. "Destekleyen faktörler:") <b> ile kalınlaştırılıyor;
        # AI'dan gelen HER serbest metin (overall/text alanları, meaning, vb.)
        # html.escape() ile kaçırılıyor -- yalnız burada bizim yazdığımız sabit
        # Türkçe etiketler kalın/ham HTML olarak eklenir. res_ai_content_label
        # RichText modunda (bkz. build_result_tab).
        esc = html.escape
        factor_labels = factor_labels or {}
        lines = []
        lines.append(esc(data["overall"]["text"]))

        if mode == "LIMITED":
            # risk_data_notice, LIMITED'ın TEK ve zorunlu güvenlik sınırı —
            # görsel olarak ayrı ve öne çıkan bir konumda (overall'dan hemen
            # sonra, analitik ayrıntılardan ÖNCE) gösterilir.
            lines.append("")
            lines.append(f"⚠️ {esc(data['risk_data_notice']['text'])}")

        if mode in ("NORMAL", "LIMITED"):
            if data.get("supporting_factors"):
                lines.append("")
                lines.append("<b>Destekleyen faktörler:</b>")
                for it in data["supporting_factors"]:
                    lines.append(f"  • {esc(it['text'])}")
            if data.get("risks_conflicts"):
                lines.append("")
                lines.append("<b>Riskler / çelişkiler:</b>")
                for it in data["risks_conflicts"]:
                    lines.append(f"  • {esc(it['text'])}")
            if data.get("entry_assessment"):
                lines.append("")
                lines.append(f"<b>Teknik giriş zamanlaması:</b> {esc(data['entry_assessment']['text'])}")
            if data.get("reassessment_triggers"):
                lines.append("")
                lines.append("<b>Yeniden değerlendirme koşulları:</b>")
                for it in data["reassessment_triggers"]:
                    lines.extend(self._render_reassessment_trigger(it, factor_labels))
        else:  # HARD_RESTRICTED
            lines.append("")
            lines.append(f"<b>Neden kısıtlı/reddedildi:</b> {esc(data['why_rejected_or_limited']['text'])}")
            if data.get("decisive_factors"):
                lines.append("")
                lines.append("<b>Belirleyici faktörler:</b>")
                for it in data["decisive_factors"]:
                    lines.append(f"  • {esc(it['text'])}")
            if data.get("positive_but_insufficient_factors"):
                lines.append("")
                lines.append("<b>Coin'in kendi teknik görünümü (veto'dan bağımsız):</b>")
                for it in data["positive_but_insufficient_factors"]:
                    lines.append(f"  • {esc(it['text'])}")
        # technical_watchpoints: TÜM modlarda ortak, data_limitations'tan ÖNCE
        # (bkz. bilgi hiyerarşisi) -- root cause: bu blok önceden hiç yoktu,
        # schema/validator/worker zaten doğruluyordu ama render hiç okumuyordu.
        if data.get("technical_watchpoints"):
            lines.append("")
            lines.extend(self._render_watchpoints_html(data["technical_watchpoints"], structure_context))
        if data.get("data_limitations"):
            lines.append("")
            lines.append("<b>Veri sınırlamaları:</b>")
            for it in data["data_limitations"]:
                lines.append(f"  • {esc(it['text'])}")
        html_lines = [self._ai_html_indent(ln) for ln in lines]
        self.res_ai_content_label.setText("<br>".join(html_lines))

    def build_ai_analyst_plain_text(self, mode: str, data: dict, factor_labels: dict = None,
                                     structure_context: dict = None) -> str:
        """PRESENTATION V1 / CANONICAL AI FORMATTER: _render_ai_analyst_card ile
        BİREBİR AYNI kaynak (raw validated mode/data/factor_labels/structure_context)
        ve AYNI bölüm SIRASI üzerinden, yalnız HTML yerine düz metin üretir --
        Raporu Kopyala bu fonksiyonu çağırır, QLabel'ın render edilmiş metnini
        HİÇ scrape etmez. GUI ile clipboard'ın zamanla sapmaması için satır
        içerikleri/sıralaması kasıtlı olarak _render_ai_analyst_card ile
        paralel tutulur (yalnız <b>/&nbsp; yok, esc() yok)."""
        factor_labels = factor_labels or {}
        lines = [data["overall"]["text"]]

        if mode == "LIMITED":
            lines.append("")
            lines.append(f"⚠️ {data['risk_data_notice']['text']}")

        if mode in ("NORMAL", "LIMITED"):
            if data.get("supporting_factors"):
                lines.append("")
                lines.append("Destekleyen faktörler:")
                for it in data["supporting_factors"]:
                    lines.append(f"  • {it['text']}")
            if data.get("risks_conflicts"):
                lines.append("")
                lines.append("Riskler / çelişkiler:")
                for it in data["risks_conflicts"]:
                    lines.append(f"  • {it['text']}")
            if data.get("entry_assessment"):
                lines.append("")
                lines.append(f"Teknik giriş zamanlaması: {data['entry_assessment']['text']}")
            if data.get("reassessment_triggers"):
                lines.append("")
                lines.append("Yeniden değerlendirme koşulları:")
                for it in data["reassessment_triggers"]:
                    lines.extend(self._render_reassessment_trigger_plain(it, factor_labels))
        else:  # HARD_RESTRICTED
            lines.append("")
            lines.append(f"Neden kısıtlı/reddedildi: {data['why_rejected_or_limited']['text']}")
            if data.get("decisive_factors"):
                lines.append("")
                lines.append("Belirleyici faktörler:")
                for it in data["decisive_factors"]:
                    lines.append(f"  • {it['text']}")
            if data.get("positive_but_insufficient_factors"):
                lines.append("")
                lines.append("Coin'in kendi teknik görünümü (veto'dan bağımsız):")
                for it in data["positive_but_insufficient_factors"]:
                    lines.append(f"  • {it['text']}")
        if data.get("technical_watchpoints"):
            lines.append("")
            lines.extend(self._render_watchpoints_plain(data["technical_watchpoints"], structure_context))
        if data.get("data_limitations"):
            lines.append("")
            lines.append("Veri sınırlamaları:")
            for it in data["data_limitations"]:
                lines.append(f"  • {it['text']}")
        return "\n".join(lines)

    # ═══════════════════════════════════════════════════════════
    # LEVEL 2
    # ═══════════════════════════════════════════════════════════
    def build_level2_tab(self):
        outer = QVBoxLayout(self.tab2)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(10)

        outer.addWidget(h_label("🧮  Yarı Otomatik (Sayısal Giriş)", size=16, bold=True))
        outer.addWidget(h_label(
            "Sayısal değerleri girin. Sistem eşik kurallarına göre otomatik Evet/Nötr/Hayır üretir.",
            size=9.5, color=TEXT_SECONDARY, wrap=True))

        sym_row = QHBoxLayout()
        sym_row.addWidget(h_label("Coin sembolü:"))
        self.l2_symbol = QLineEdit("BTC")
        self.l2_symbol.setFixedWidth(120)
        sym_row.addWidget(self.l2_symbol)
        sym_row.addWidget(h_label("(Veto kuralları için gerekli)", size=8.5, color=TEXT_TERTIARY))
        sym_row.addStretch()
        outer.addLayout(sym_row)

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setSpacing(12)

        self.l2_onchain_cb = None
        self.l2_onchain_panel = None

        for si, stage in enumerate(STAGES_CONFIG):
            card = Card(accent=STAGE_ACCENTS.get(stage.title, BORDER))
            stage_title_row = QHBoxLayout()
            stage_title_row.setSpacing(8)
            stage_title_row.addWidget(icon_chip(STAGE_ICONS.get(stage.title, "•"),
                                                 STAGE_ACCENTS.get(stage.title, BLUE), size=28))
            stage_title_row.addWidget(h_label(f"{si+1}. {stage.title}", size=12, bold=True))
            stage_title_row.addStretch()
            card.layout_.addLayout(stage_title_row)
            card.add(h_label(stage.subtitle, size=9, color=TEXT_TERTIARY, wrap=True))

            onchain_panel = None
            if stage.title == "Zincir üstü veriler":
                self.l2_onchain_cb = QCheckBox(
                    "Detaylı on-chain verileri dahil et (Whale, Active, Outflow, Stablecoin)")
                card.add(self.l2_onchain_cb)
                onchain_panel = QWidget()
                onchain_panel.setLayout(QVBoxLayout())
                onchain_panel.layout().setContentsMargins(0, 0, 0, 0)
                self.l2_onchain_panel = onchain_panel
                self.l2_onchain_cb.toggled.connect(lambda ch, p=onchain_panel: p.setVisible(ch))
                onchain_panel.setVisible(False)

            for ii, item in enumerate(stage.items):
                th = item.thresholds or {}
                ttype = th.get("type", "bool")
                key = f"{si}-{ii}"

                q_widget = QWidget()
                q_layout = QVBoxLayout(q_widget)
                q_layout.setContentsMargins(0, 4, 0, 6)
                q_layout.addWidget(h_label(item.label, size=10, bold=True, wrap=True))
                q_layout.addWidget(h_label(f"Ağırlık: {item.weight}/10  |  {item.desc}",
                                            size=8.5, color=TEXT_SECONDARY, wrap=True))

                if ttype == "bool":
                    row = QHBoxLayout()
                    group = QButtonGroup(q_widget)
                    for idx, (text, val, color) in enumerate([
                        ("✓ Evet", "yes", POS), ("~ Nötr", "wait", WARN),
                        ("✕ Hayır", "no", DANGER), ("? Veri Yok", "nodata", NEUTRAL)]):
                        rb = QRadioButton(text)
                        rb.setStyleSheet(f"color: {color};")
                        if val == "nodata":
                            rb.setChecked(True)
                        group.addButton(rb, idx)
                        row.addWidget(rb)
                    row.addStretch()
                    q_layout.addLayout(row)
                    self.level2_groups[key] = (group, ["yes", "wait", "no", "nodata"])

                elif ttype == "bool_or_min":
                    row = QHBoxLayout()
                    row.addWidget(h_label("Volatilite (%):", size=9))
                    entry = QLineEdit()
                    entry.setFixedWidth(80)
                    row.addWidget(entry)
                    self.level2_entries[f"{key}-0"] = entry
                    row.addWidget(h_label("Squeeze:", size=9))
                    group = QButtonGroup(q_widget)
                    for idx, (text, val, color) in enumerate([("Evet", "1", POS), ("Hayır", "0", DANGER)]):
                        rb = QRadioButton(text)
                        rb.setStyleSheet(f"color: {color};")
                        group.addButton(rb, idx)
                        row.addWidget(rb)
                    row.addStretch()
                    q_layout.addLayout(row)
                    self.level2_bool_groups[key] = (group, ["1", "0"])
                    hint = (f"Volatilite ≥{th.get('yes_vol','?')}% = EVET, ≥{th.get('wait_vol','?')}% = NÖTR, "
                            f"Squeeze aktifse NÖTR")
                    q_layout.addWidget(h_label(f"Eşik: {hint}", size=8, color=BLUE, wrap=True))

                elif ttype == "volume_spread":
                    row = QHBoxLayout()
                    for j, lbl in enumerate(th.get("input_labels", ["Değer 1", "Değer 2"])):
                        row.addWidget(h_label(f"{lbl}:", size=9))
                        entry = QLineEdit()
                        entry.setFixedWidth(90)
                        row.addWidget(entry)
                        self.level2_entries[f"{key}-{j}"] = entry
                    row.addStretch()
                    q_layout.addLayout(row)
                    hint = f"Hacim≥{th.get('yes_vol','?')}M$ VE Spread≤{th.get('yes_spread','?')}% = EVET"
                    q_layout.addWidget(h_label(f"Eşik: {hint}", size=8, color=BLUE, wrap=True))

                else:
                    row = QHBoxLayout()
                    for j, lbl in enumerate(th.get("input_labels", ["Değer"])):
                        row.addWidget(h_label(f"{lbl}:", size=9))
                        entry = QLineEdit()
                        entry.setFixedWidth(100)
                        row.addWidget(entry)
                        self.level2_entries[f"{key}-{j}"] = entry
                    row.addStretch()
                    q_layout.addLayout(row)
                    hint = self._threshold_hint(th)
                    if hint:
                        q_layout.addWidget(h_label(f"Eşik: {hint}", size=8, color=BLUE, wrap=True))

                unit_hint = th.get("unit_hint", "")
                if unit_hint:
                    q_layout.addWidget(h_label(f"ℹ  {unit_hint}", size=8, color=TEXT_TERTIARY, wrap=True))

                if item.optional and onchain_panel is not None:
                    onchain_panel.layout().addWidget(q_widget)
                else:
                    card.add(q_widget)

            if onchain_panel is not None:
                card.layout_.addWidget(onchain_panel)

            inner_layout.addWidget(card)

        btn_row = QHBoxLayout()
        calc_btn = QPushButton("Hesapla")
        calc_btn.setObjectName("Primary")
        calc_btn.clicked.connect(self.run_level2)
        clear_btn = QPushButton("Temizle")
        clear_btn.clicked.connect(self.clear_level2)
        btn_row.addWidget(calc_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        inner_layout.addLayout(btn_row)

        self.l2_result_text = QTextEdit()
        self.l2_result_text.setReadOnly(True)
        self.l2_result_text.setFixedHeight(160)
        self.l2_result_text.setPlainText("Değerleri girip 'Hesapla' butonuna basın...")
        inner_layout.addWidget(self.l2_result_text)

        outer.addWidget(self._scroll_wrap(inner), 1)

    def _threshold_hint(self, th: dict) -> str:
        ttype = th.get("type", "")
        if ttype == "ratio":
            return f"≥{th.get('yes','?')}x = EVET, ≥{th.get('wait','?')}x = NÖTR"
        elif ttype == "min":
            return f"≥{th.get('yes','?')} = EVET, ≥{th.get('wait','?')} = NÖTR"
        elif ttype == "max":
            return f"≤{th.get('yes','?')} = EVET, ≤{th.get('wait','?')} = NÖTR"
        elif ttype == "range":
            return f"{th.get('yes_min','?')}-{th.get('yes_max','?')} = EVET"
        elif ttype == "funding":
            return f"{th.get('yes_min','?')} ile {th.get('yes_max','?')} arası = EVET"
        elif ttype == "count":
            return f"≥{th.get('yes','?')} = EVET, ≥{th.get('wait','?')} = NÖTR"
        return ""

    def run_level2(self):
        answers = []
        for si, stage in enumerate(STAGES_CONFIG):
            for ii, item in enumerate(stage.items):
                key = f"{si}-{ii}"
                if item.optional and self.l2_onchain_cb is not None and not self.l2_onchain_cb.isChecked():
                    answers.append({"stage": stage.title, "question": item.label, "answer": "nodata",
                                     "weight": item.weight, "_item": item,
                                     "reason": "Analize dahil edilmedi (on-chain detay kapalı)",
                                     "disabled": True})
                    continue

                th = item.thresholds or {}
                ttype = th.get("type", "bool")
                # LEVEL 2 RAW VALUE PROVENANCE (onaylı, controlled implementation):
                # her dal, mevcut Level 1 answer sözleşmesiyle (value/components)
                # birebir uyumlu ham kanıtı `provenance` içine toplar -- yeni bir
                # şema İCAT EDİLMEDİ, yalnız zaten var olan consumer contract'ı
                # (format_display_value/format_component_display_value) dolduruluyor.
                # `bool` tipi kategorik olduğu için provenance boş kalır (madde 4).
                provenance = {}

                if ttype == "bool":
                    group, values = self.level2_groups.get(key, (None, None))
                    ans = "nodata"
                    if group is not None:
                        cid = group.checkedId()
                        if cid >= 0:
                            ans = values[cid]

                elif ttype == "bool_or_min":
                    entry = self.level2_entries.get(f"{key}-0")
                    vol_val = None
                    if entry and entry.text().strip():
                        try:
                            vol_val = float(entry.text().strip().replace(",", "."))
                        except ValueError:
                            QMessageBox.warning(self, "Geçersiz Değer", f"{item.label}: Volatilite sayısal olmalı.")
                            return
                    group, values = self.level2_bool_groups.get(key, (None, None))
                    squeeze_val = None
                    if group is not None:
                        cid = group.checkedId()
                        if cid >= 0:
                            squeeze_val = float(values[cid])
                    if vol_val is None and squeeze_val is None:
                        ans = "nodata"
                    elif vol_val is None:
                        ans = "wait" if squeeze_val >= 1 else "no"
                    elif squeeze_val is None:
                        # PARTIAL-INPUT SEMANTICS FIX (onaylı, controlled): squeeze
                        # cevaplanmadı diye otomatik "Hayır" (0) VARSAYILMAZ.
                        # ThresholdEngine.evaluate()'in GERÇEK bool_or_min formülü
                        # (vol>=yes_vol -> yes; (vol>=wait_vol OR squeeze>=1) -> wait;
                        # aksi -> no) şu özelliği taşır: vol tek başına yes_vol veya
                        # wait_vol eşiğini zaten geçiyorsa, squeeze'in gerçek değeri
                        # SONUCU HİÇ DEĞİŞTİRMEZ (OR koşulu vol tarafından zaten
                        # sağlanmış). Yalnız vol HER İKİ eşiğin de altındaysa
                        # (wait_vol'a bile ulaşmıyorsa) squeeze'in bilinmemesi
                        # gerçekten belirsizlik yaratır (squeeze=True olsaydı "wait",
                        # False olsaydı "no" olurdu) -- yalnız bu dar durumda nodata.
                        yes_vol = item.thresholds.get("yes_vol", 999)
                        wait_vol = item.thresholds.get("wait_vol", 0)
                        if vol_val >= yes_vol:
                            ans = "yes"  # squeeze bilinmese de vol tek başına kanıtlıyor
                        elif vol_val >= wait_vol:
                            ans = "wait"  # OR zaten vol tarafından sağlanıyor, squeeze etkisiz
                        else:
                            ans = "nodata"  # gerçek belirsizlik: squeeze bilinmeden karar verilemez
                    else:
                        ans = self.threshold_engine.evaluate(item, [vol_val, squeeze_val])
                    # PROVENANCE: Level 1'in bu factor_id (volatility_or_squeeze) için
                    # kullandığı KANONİK şekil tek scalar `value` (bkz.
                    # RealFetcher.fetch() "volatility_pct" dalı, dp.value=vol) --
                    # component_metrics=None (FACTOR_ID_TABLE), yani mevcut AI
                    # context tüketicisi squeeze için ayrı bir composite alan
                    # BEKLEMİYOR. Bu yüzden yalnız vol_val (mevcut sözleşmeyle
                    # birebir aynı) taşınıyor; squeeze için yeni bir şema İCAT
                    # EDİLMEDİ (bkz. Final Report'taki kapsam notu).
                    provenance["value"] = vol_val

                elif ttype == "volume_spread":
                    inputs = []
                    has_data = True
                    for j in range(2):
                        entry = self.level2_entries.get(f"{key}-{j}")
                        val = entry.text().strip() if entry else ""
                        if val == "":
                            has_data = False
                            break
                        try:
                            inputs.append(float(val.replace(",", ".")))
                        except ValueError:
                            QMessageBox.warning(self, "Geçersiz Değer", f"{item.label}: Sayısal değer girin.")
                            return
                    ans = "nodata" if not has_data else self.threshold_engine.evaluate(item, inputs)
                    if has_data:
                        # PROVENANCE: Level 1'in volume_spread_combined için kullandığı
                        # KANONİK "components" şekliyle birebir aynı (bkz. run_level1()
                        # answers.append, "components": {"volume_24h":..., "spread_pct":...}).
                        provenance["components"] = {"volume_24h": inputs[0], "spread_pct": inputs[1]}

                else:
                    labels = th.get("input_labels", ["Değer"])
                    inputs = []
                    has_data = True
                    for j in range(len(labels)):
                        entry = self.level2_entries.get(f"{key}-{j}")
                        val = entry.text().strip() if entry else ""
                        if val == "":
                            has_data = False
                            break
                        try:
                            inputs.append(float(val.replace(",", ".")))
                        except ValueError:
                            QMessageBox.warning(self, "Geçersiz Değer", f"{item.label}: Sayısal değer girin.")
                            return
                    ans = "nodata" if not has_data else self.threshold_engine.evaluate(item, inputs)
                    if has_data:
                        # PROVENANCE: range/min/max/ratio/funding/count -- hepsi
                        # AI context tarafında SCALAR factor_id'ler (component_metrics
                        # None, FACTOR_ID_TABLE'da doğrulandı) -- Level 1'deki tek-
                        # değer dp.value sözleşmesiyle birebir aynı, ilk (ve ratio
                        # dışında tek) girdi taşınıyor.
                        provenance["value"] = inputs[0]

                answers.append({"stage": stage.title, "question": item.label, "answer": ans,
                                 "weight": item.weight, "_item": item, **provenance})

        symbol = self.l2_symbol.text().strip().upper() or "UNKNOWN"
        report = self.score_engine.full_report(answers, STAGES_CONFIG, symbol)
        # Rapor tamamlandığı anda History referans çifti sabitlenir — save
        # anında tekrar fiyat çekilmez. Fiyat alınamazsa (ör. geçersiz sembol)
        # analysis_price=None kalır, sessiz fallback yapılmaz.
        report["analysis_time"], report["analysis_price"] = capture_analysis_snapshot(symbol)
        self._last_level = "Level 2 — Yarı Otomatik"
        self._last_source_status = {}
        self._last_news_detail = {"items": [], "result": {}}
        self.display_report_in_result(report, symbol)
        self.l2_result_text.setPlainText("\n".join(self._format_report(report)))
        self.tabs.setCurrentWidget(self.tab_result)

    def clear_level2(self):
        for w in self.level2_entries.values():
            w.clear()
        for group, values in self.level2_groups.values():
            idx = values.index("nodata") if "nodata" in values else -1
            if idx >= 0:
                btn = group.button(idx)
                if btn:
                    btn.setChecked(True)
        for group, values in self.level2_bool_groups.values():
            group.setExclusive(False)
            for btn in group.buttons():
                btn.setChecked(False)
            group.setExclusive(True)

    # ═══════════════════════════════════════════════════════════
    # LEVEL 3
    # ═══════════════════════════════════════════════════════════
    def build_level3_tab(self):
        outer = QVBoxLayout(self.tab3)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(10)

        outer.addWidget(h_label("✍️  Manuel Değerlendirme", size=16, bold=True))
        outer.addWidget(h_label("Her soruyu inceleyip Evet / Nötr / Hayır / Veri Yok seçin.",
                                 size=9.5, color=TEXT_SECONDARY))

        sym_row = QHBoxLayout()
        sym_row.addWidget(h_label("Coin sembolü:"))
        self.l3_symbol = QLineEdit("BTC")
        self.l3_symbol.setFixedWidth(120)
        sym_row.addWidget(self.l3_symbol)
        sym_row.addWidget(h_label("(Veto kuralları için gerekli)", size=8.5, color=TEXT_TERTIARY))
        sym_row.addStretch()
        outer.addLayout(sym_row)

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setSpacing(12)

        self.l3_onchain_cb = None
        self.l3_onchain_panel = None

        for si, stage in enumerate(STAGES_CONFIG):
            card = Card(accent=STAGE_ACCENTS.get(stage.title, BORDER))
            stage_title_row = QHBoxLayout()
            stage_title_row.setSpacing(8)
            stage_title_row.addWidget(icon_chip(STAGE_ICONS.get(stage.title, "•"),
                                                 STAGE_ACCENTS.get(stage.title, BLUE), size=28))
            stage_title_row.addWidget(h_label(f"{si+1}. {stage.title}", size=12, bold=True))
            stage_title_row.addStretch()
            card.layout_.addLayout(stage_title_row)
            card.add(h_label(stage.subtitle, size=9, color=TEXT_TERTIARY, wrap=True))

            onchain_panel = None
            if stage.title == "Zincir üstü veriler":
                self.l3_onchain_cb = QCheckBox(
                    "Detaylı on-chain verileri dahil et (Whale, Active, Outflow, Stablecoin)")
                card.add(self.l3_onchain_cb)
                onchain_panel = QWidget()
                onchain_panel.setLayout(QVBoxLayout())
                onchain_panel.layout().setContentsMargins(0, 0, 0, 0)
                self.l3_onchain_panel = onchain_panel
                self.l3_onchain_cb.toggled.connect(lambda ch, p=onchain_panel: p.setVisible(ch))
                onchain_panel.setVisible(False)

            for ii, item in enumerate(stage.items):
                key = f"{si}-{ii}"
                q_widget = QWidget()
                q_layout = QVBoxLayout(q_widget)
                q_layout.setContentsMargins(0, 4, 0, 6)
                q_layout.addWidget(h_label(item.label, size=10, bold=True, wrap=True))
                q_layout.addWidget(h_label(f"Ağırlık: {item.weight}/10  |  {item.desc}",
                                            size=8.5, color=TEXT_SECONDARY, wrap=True))
                row = QHBoxLayout()
                group = QButtonGroup(q_widget)
                for idx, (text, val, color) in enumerate([
                        ("✓ Evet", "yes", POS), ("~ Nötr", "wait", WARN),
                        ("✕ Hayır", "no", DANGER), ("? Veri Yok", "nodata", NEUTRAL)]):
                    rb = QRadioButton(text)
                    rb.setStyleSheet(f"color: {color};")
                    if val == "nodata":
                        rb.setChecked(True)
                    group.addButton(rb, idx)
                    row.addWidget(rb)
                row.addStretch()
                q_layout.addLayout(row)
                self.level3_groups[key] = (group, ["yes", "wait", "no", "nodata"])

                if item.optional and onchain_panel is not None:
                    onchain_panel.layout().addWidget(q_widget)
                else:
                    card.add(q_widget)

            if onchain_panel is not None:
                card.layout_.addWidget(onchain_panel)

            inner_layout.addWidget(card)

        btn_row = QHBoxLayout()
        calc_btn = QPushButton("Hesapla")
        calc_btn.setObjectName("Primary")
        calc_btn.clicked.connect(self.run_level3)
        reset_btn = QPushButton("Tümünü Veri Yok Yap")
        reset_btn.clicked.connect(self.set_all_nodata)
        btn_row.addWidget(calc_btn)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()
        inner_layout.addLayout(btn_row)

        outer.addWidget(self._scroll_wrap(inner), 1)

    def run_level3(self):
        answers = []
        for si, stage in enumerate(STAGES_CONFIG):
            for ii, item in enumerate(stage.items):
                key = f"{si}-{ii}"
                disabled = bool(item.optional and self.l3_onchain_cb is not None
                                and not self.l3_onchain_cb.isChecked())
                if disabled:
                    ans = "nodata"
                else:
                    group, values = self.level3_groups.get(key, (None, None))
                    ans = "nodata"
                    if group is not None:
                        cid = group.checkedId()
                        if cid >= 0:
                            ans = values[cid]
                entry = {"stage": stage.title, "question": item.label, "answer": ans,
                         "weight": item.weight, "_item": item, "disabled": disabled}
                if disabled:
                    entry["reason"] = "Analize dahil edilmedi (on-chain detay kapalı)"
                answers.append(entry)

        symbol = self.l3_symbol.text().strip().upper() or "UNKNOWN"
        self._last_level = "Level 3 — Manuel"
        self._last_source_status = {}
        self._last_news_detail = {"items": [], "result": {}}
        report = self.score_engine.full_report(answers, STAGES_CONFIG, symbol)
        # Rapor tamamlandığı anda History referans çifti sabitlenir (bkz.
        # Level 2'deki aynı gerekçe) — save anında tekrar fiyat çekilmez.
        report["analysis_time"], report["analysis_price"] = capture_analysis_snapshot(symbol)
        self.display_report_in_result(report, symbol)
        self.tabs.setCurrentWidget(self.tab_result)

    def set_all_nodata(self):
        for group, values in self.level3_groups.values():
            idx = values.index("nodata")
            btn = group.button(idx)
            if btn:
                btn.setChecked(True)

    # ═══════════════════════════════════════════════════════════
    # SONUÇ
    # ═══════════════════════════════════════════════════════════
    def build_result_tab(self):
        outer = QVBoxLayout(self.tab_result)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(0)

        inner = QWidget()
        self.res_layout = QVBoxLayout(inner)
        self.res_layout.setSpacing(12)

        top_bar = QHBoxLayout()
        self.res_symbol_label = h_label("", size=11, bold=True, color=TEXT_TERTIARY)
        top_bar.addWidget(self.res_symbol_label)
        top_bar.addStretch()
        # AI Analist artık ana scroll alanında DEĞİL — sağ QDockWidget panelinde.
        # Bu buton yalnız panelin görünürlüğünü aç/kapatır; AI worker lifecycle'ı
        # (_active_ai_request_id/_ai_workers/cooperative shutdown) ile hiçbir
        # etkileşimi yok, o mekanizma tamamen değişmeden duruyor.
        self.res_ai_toggle_btn = QPushButton("✦  AI Analist")
        self.res_ai_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.res_ai_toggle_btn.setCheckable(True)
        self.res_ai_toggle_btn.setStyleSheet(
            f"QPushButton {{ background: {rgba(ACCENT2, 0.14)}; border: 1px solid {rgba(ACCENT2, 0.4)}; "
            f"border-radius: 8px; color: {ACCENT2}; font-weight: bold; padding: 5px 12px; }} "
            f"QPushButton:checked {{ background: {rgba(ACCENT2, 0.28)}; }} "
            f"QPushButton:hover {{ background: {rgba(ACCENT2, 0.22)}; }}"
        )
        self.res_ai_toggle_btn.clicked.connect(self._toggle_ai_dock)
        # AI_ANALYST_ENABLED/ANTHROPIC_API_KEY çalışma boyunca sabit — mevcut
        # start_ai_analyst()'teki aynı koşulun statik hali, per-call tekrar
        # kontrol edilmesine gerek yok.
        self.res_ai_toggle_btn.setVisible(bool(AI_ANALYST_ENABLED and ANTHROPIC_API_KEY))
        top_bar.addWidget(self.res_ai_toggle_btn)
        self.res_layout.addLayout(top_bar)

        # Skor kartları — neon halka göstergeler
        self.res_score_row = QHBoxLayout()
        self.res_score_row.setSpacing(14)
        score_icons = {"Sinyal Gücü": "⚡", "İşlem Uygunluğu": "🛡️", "Giriş Uygunluğu": "🎯"}
        score_chip_color = {"Sinyal Gücü": BLUE, "İşlem Uygunluğu": ACCENT2, "Giriş Uygunluğu": WARN}
        score_suffix = {"Sinyal Gücü": "%", "İşlem Uygunluğu": "%", "Giriş Uygunluğu": ""}
        self.res_score_cards = {}
        for key in ["Sinyal Gücü", "İşlem Uygunluğu", "Giriş Uygunluğu"]:
            card = Card(accent=NEUTRAL, glow=False)
            title_row = QHBoxLayout()
            title_row.setAlignment(Qt.AlignCenter)
            title_row.setSpacing(8)
            title_row.addWidget(icon_chip(score_icons.get(key, ""), score_chip_color.get(key, BLUE), size=26))
            title_row.addWidget(h_label(key.upper(), size=9.5, bold=True, color=TEXT_TERTIARY))
            card.layout_.addLayout(title_row)
            ring = RingGauge(diameter=126, thickness=10, suffix=score_suffix.get(key, "%"))
            ring_row = QHBoxLayout()
            ring_row.setAlignment(Qt.AlignCenter)
            ring_row.addWidget(ring)
            card.layout_.addLayout(ring_row)
            badge_row = QHBoxLayout()
            badge_row.setAlignment(Qt.AlignCenter)
            badge_lbl = make_badge("—", NEUTRAL)
            badge_row.addWidget(badge_lbl)
            card.layout_.addLayout(badge_row)
            caption_lbl = h_label("", size=8, color=TEXT_TERTIARY)
            caption_lbl.setAlignment(Qt.AlignCenter)
            caption_lbl.setWordWrap(True)
            card.add(caption_lbl)
            self.res_score_cards[key] = (card, ring, badge_lbl, caption_lbl)
            self.res_score_row.addWidget(card)
        self.res_layout.addLayout(self.res_score_row)

        # "Bu coin neden güçlü/zayıf?" — en önemli etkenler, en üstte, tek bakışta
        self.res_hero_card = Card(accent=BLUE, glow=True)
        self.res_layout.addWidget(self.res_hero_card)

        # Karar kutusu
        self.res_verdict_card = Card(accent=BLUE, glow=True)
        verdict_top = QHBoxLayout()
        self.res_verdict_title = h_label("Sonuç", size=17, bold=True)
        verdict_top.addWidget(self.res_verdict_title)
        verdict_top.addStretch()
        self.res_verdict_badge = make_badge("—", NEUTRAL, size=10)
        verdict_top.addWidget(self.res_verdict_badge)
        self.res_verdict_card.layout_.addLayout(verdict_top)
        self.res_verdict_status = h_label("Giriş durumu: —", size=10.5, color=TEXT_SECONDARY)
        self.res_verdict_desc = h_label("—", size=9.5, color=TEXT_SECONDARY, wrap=True)
        self.res_verdict_card.add(self.res_verdict_status)
        self.res_verdict_card.add(self.res_verdict_desc)

        # Veri Kapsamı — tek kart, tıklanınca eksik veri dökümü açılır. Header'daki
        # "Güven" rozeti artık addStretch() ile uzak köşeye itilmiyor — başlıkla
        # aynı kümede, solda duruyor; gövdedeki bar/pct genişliği kullanmaya devam ediyor.
        self.res_coverage_card = Card(accent=NEUTRAL)
        self.res_coverage_card.setCursor(Qt.PointingHandCursor)
        self.res_coverage_card.mousePressEvent = lambda e: self.show_missing_data()
        cov_head = QHBoxLayout()
        cov_head.setSpacing(6)
        cov_head.addWidget(icon_chip("📡", BLUE, size=24))
        cov_head.addWidget(h_label("VERİ KAPSAMI", size=10.5, bold=True, color=TEXT_TERTIARY))
        cov_head.addWidget(h_label("·  Güven:", size=9, color=TEXT_TERTIARY))
        self.res_confidence_badge = make_badge("—", NEUTRAL, size=8.5)
        cov_head.addWidget(self.res_confidence_badge)
        cov_head.addStretch()
        self.res_coverage_card.layout_.addLayout(cov_head)

        cov_body = QHBoxLayout()
        cov_body.setSpacing(16)
        self.res_coverage_bar = ProgressBarStyled()
        self.res_coverage_bar.setFixedHeight(12)
        bar_col = QVBoxLayout()
        bar_col.addWidget(self.res_coverage_bar)
        counts_row = QHBoxLayout()
        self.res_cov_ok_lbl = h_label("✓ 0 otomatik", size=9, color=POS)
        self.res_cov_manual_lbl = h_label("👤 0 manuel", size=9, color=BLUE)
        self.res_cov_missing_lbl = h_label("⚠ 0 veri yok", size=9, color=WARN)
        counts_row.addWidget(self.res_cov_ok_lbl)
        counts_row.addWidget(self.res_cov_manual_lbl)
        counts_row.addWidget(self.res_cov_missing_lbl)
        counts_row.addStretch()
        bar_col.addLayout(counts_row)
        cov_body.addLayout(bar_col, 1)
        self.res_coverage_pct_lbl = h_label("—", size=22, bold=True)
        cov_body.addWidget(self.res_coverage_pct_lbl)
        self.res_coverage_card.layout_.addLayout(cov_body)
        self.res_cov_disabled_lbl = h_label("", size=8, color=TEXT_TERTIARY)
        self.res_coverage_card.add(self.res_cov_disabled_lbl)
        cov_link = QPushButton("👆  Eksik verileri gör")
        cov_link.setCursor(Qt.PointingHandCursor)
        cov_link.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {TEXT_TERTIARY}; "
            f"font-size: 8pt; text-align: left; padding: 0; }} QPushButton:hover {{ color: {BLUE}; }}"
        )
        cov_link.clicked.connect(self.show_missing_data)
        self.res_coverage_card.add(cov_link)

        # Özet — QGridLayout artık sütun 0/1'i büyütmüyor (setColumnStretch(0/1, 0)),
        # boşluk yalnız sağdaki spacer sütununda (2) toplanıyor -> "Pozitif: 12" bitişik
        # bir blok gibi solda kalıyor, kilometrelerce sağa kaymıyor.
        self.res_summary_card = Card()
        self.res_summary_card.add(h_label("📋  Ham Özet", size=12, bold=True))
        self.res_summary_grid = QGridLayout()
        self.res_summary_grid.setHorizontalSpacing(10)
        self.res_summary_grid.setColumnStretch(0, 0)
        self.res_summary_grid.setColumnStretch(1, 0)
        self.res_summary_grid.setColumnStretch(2, 1)
        self.res_summary_labels = {}
        for i, lbl_name in enumerate(["Pozitif", "Nötr", "Negatif", "Veri Yok", "Toplam"]):
            self.res_summary_grid.addWidget(h_label(lbl_name + ":", size=9.5), i, 0)
            v = h_label("—", size=9.5, bold=True)
            self.res_summary_grid.addWidget(v, i, 1)
            self.res_summary_labels[lbl_name] = v
        self.res_summary_card.layout_.addLayout(self.res_summary_grid)

        # ROW 4: KARAR/DURUM | VERİ KAPSAMI | HAM ÖZET — üç kompakt kart aynı satırda.
        # Stretch oranı piksel değil, Qt stretch oranı (4:5:3).
        # NOT: İlk denemede Qt.AlignTop + Card'in varsayılan dikey Maximum politikası
        # kullanılmıştı; bu, kartların ARKA PLAN/ÇERÇEVESİNİ birbirine eşitlemiyordu,
        # yalnızca boşluğu kartın İÇİNDEN kartın ALTINA (satırın çerçevesiz arka planına)
        # taşıyordu -- sonuç: alt kenarları hizasız, "amatörce" görünen bir satır.
        # Doğru çözüm: bu 3 karta özel olarak dikey QSizePolicy.Expanding veriyoruz
        # (yalnız bu 3 instance, Card sınıfının genel varsayılanı değişmiyor) ve her
        # kartın KENDİ iç layout_'una bir addStretch() ekliyoruz -- böylece kart
        # çerçevesi satırın tam yüksekliğine kadar büyüyor, fazladan boşluk da kartın
        # İÇİNDE ama İÇERİĞİN ALTINDA (kompakt, kasıtlı bir alt-dolgu gibi) toplanıyor.
        for _row4_card in (self.res_verdict_card, self.res_coverage_card, self.res_summary_card):
            _row4_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.res_verdict_card.layout_.addStretch()
        self.res_coverage_card.layout_.addStretch()
        self.res_summary_card.layout_.addStretch()
        row4 = QHBoxLayout()
        row4.setSpacing(14)
        row4.addWidget(self.res_verdict_card, 4)
        row4.addWidget(self.res_coverage_card, 5)
        row4.addWidget(self.res_summary_card, 3)
        self.res_layout.addLayout(row4)

        # Veto banner — karar kartının hemen altında, göründüğünde tam genişlik kalıyor
        # (nadir/kritik bir uyarı olduğu için daraltılmadı).
        self.res_veto_frame = QFrame()
        self.res_veto_frame.setObjectName("VetoBanner")
        veto_layout = QVBoxLayout(self.res_veto_frame)
        self.res_veto_label = h_label("", size=10, color=DANGER, wrap=True)
        veto_layout.addWidget(self.res_veto_label)
        self.res_layout.addWidget(self.res_veto_frame)
        self.res_veto_frame.setVisible(False)

        # Aşama dökümü
        self.res_stages_card = Card()
        self.res_stages_card.add(h_label("Aşama Dökümü", size=12, bold=True))

        # Etkenler — res_signal_factors_card, DESTEKLEYENLER/BASKILAYANLAR (res_hero_card)
        # ile TAM duplicate olduğu için artık ana Sonuç akışına mount EDİLMİYOR (aşağıda
        # hiçbir row/addWidget çağrısına eklenmiyor). Widget hâlâ oluşturuluyor ve
        # display_report_in_result() içinde _fill_factors_card() ile doldurulmaya devam
        # ediyor (veri/davranış silinmedi) — yalnız hiçbir layout'a bağlı olmadığı için
        # ekranda görünmüyor. Aynı bilgi "🧮 Skor Etkenleri" butonundaki detay penceresinde
        # (_format_report -> OLUMLU/OLUMSUZ ETKENLER) eksiksiz korunuyor.
        self.res_signal_factors_card = Card()
        self.res_risk_factors_card = Card()

        # ROW 5: AŞAMA DÖKÜMÜ | İŞLEM UYGUNLUĞU ETKENLERİ — stretch ~8:4 (oran 2:1).
        # NOT (duplicate kontrolü): res_risk_factors_card yalnız risk_pros/risk_cons
        # gösteriyor; bu maddeler res_hero_card'daki BASKILAYANLAR/DESTEKLEYENLER
        # birleşik listesinde (signal+risk birlikte) zaten TAMAMEN mevcut — yani bu kart
        # da signal_factors_card ile aynı şekilde bir duplicate. Kaldırma yetkisi bu
        # turda yalnız signal_factors_card için verildiği için risk_factors_card burada
        # bilinçli olarak tutuldu; nihai kararı kullanıcıya bırakıyorum.
        # Bu iki kart display_report_in_result() içinde her analizde .clear() edilip
        # yeniden dolduruluyor -- clear() layout_'daki her şeyi (addStretch() dahil)
        # siliyor, bu yüzden dikey Expanding politikasını burada (kalıcı, instance
        # bazlı) veriyoruz ama trailing addStretch()'i her rebuild'in SONUNA
        # (display_report_in_result içinde) eklemek gerekiyor -- aşağıya bakınız.
        self.res_stages_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.res_risk_factors_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        row5 = QHBoxLayout()
        row5.setSpacing(14)
        row5.addWidget(self.res_stages_card, 2)
        row5.addWidget(self.res_risk_factors_card, 1)
        self.res_layout.addLayout(row5)

        # Footer — tek satır: solda detay/aksiyon butonları, sağda Kopyala/Kaydet.
        footer_row = QHBoxLayout()
        footer_row.setSpacing(8)
        for text, handler in [
            ("📈  Teknik Detay", self.show_technical_detail),
            ("🌐  Veri Kaynakları", self.show_data_sources),
            ("📰  Haber Detayı", self.show_news_detail),
            ("❓  Eksik Veriler", self.show_missing_data),
            ("🧮  Skor Etkenleri", self.show_score_factors),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(handler)
            footer_row.addWidget(btn)
        footer_row.addStretch()
        self.res_save_label = h_label("Analiz sonucu bekleniyor...", size=9, color=TEXT_TERTIARY)
        footer_row.addWidget(self.res_save_label)
        self.res_copy_btn = QPushButton("📋  Raporu Kopyala")
        self.res_copy_btn.clicked.connect(self.copy_report)
        self.res_save_btn = QPushButton("💾  Geçmişe Kaydet")
        self.res_save_btn.setObjectName("Primary")
        self.res_save_btn.clicked.connect(self.save_to_history)
        self.res_save_btn.setEnabled(False)
        footer_row.addWidget(self.res_copy_btn)
        footer_row.addWidget(self.res_save_btn)
        self.res_layout.addLayout(footer_row)

        self.res_layout.addStretch()

        outer.addWidget(self._scroll_wrap(inner))

        # ── FAZ 1: AI Analist artık ana scroll alanında DEĞİL, sağ QDockWidget
        # panelinde. İçerik (başlık/uyarı/res_ai_content_label) BİREBİR AYNI —
        # yalnız konteyner değişti (Card -> QDockWidget içindeki Card). AI
        # worker lifecycle'ı (_active_ai_request_id/_ai_workers/cooperative
        # shutdown/AIAnalystClient/validator/v4 selection) HİÇ etkilenmiyor —
        # onlar yalnız self.res_ai_content_label.setText(...) çağırıyor, bu
        # widget'ın nerede/nasıl gösterildiğini hiç bilmiyor.
        self.res_ai_card = Card(accent=ACCENT2, glow=False)
        ai_title_row = QHBoxLayout()
        ai_title_row.addWidget(h_label("🤖  AI Analist Yorumu (deneysel)", size=12, bold=True, color=ACCENT2))
        ai_title_row.addStretch()
        self.res_ai_card.layout_.addLayout(ai_title_row)
        self.res_ai_card.layout_.addWidget(h_label(
            "Bu bölüm, yukarıdaki deterministik motor sonucunu doğal dille açıklar — "
            "kendi başına yeni bir karar üretmez, motorun verdict/veto/skor sonucunu değiştirmez.",
            size=8.5, color=TEXT_TERTIARY, wrap=True))
        self.res_ai_content_label = h_label("", size=9.5, color=TEXT_SECONDARY, wrap=True)
        # RichText: _render_ai_analyst_card() bölüm başlıklarını <b> ile kalınlaştırıyor
        # (ör. "Destekleyen faktörler:") -- metnin geri kalanı (AI'dan gelen serbest
        # metin dahil) her zaman html.escape() ile kaçırılıyor, yalnız burada bizim
        # yazdığımız sabit başlık etiketleri kalın gösteriliyor.
        self.res_ai_content_label.setTextFormat(Qt.RichText)
        self.res_ai_card.layout_.addWidget(self.res_ai_content_label)

        self.res_ai_dock = QDockWidget("AI Analist", self)
        self.res_ai_dock.setObjectName("AIAnalystDock")
        self.res_ai_dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.res_ai_dock.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)
        self.res_ai_dock.setWidget(self._scroll_wrap(self.res_ai_card))
        self.res_ai_dock.setMinimumWidth(420)
        self.addDockWidget(Qt.RightDockWidgetArea, self.res_ai_dock)
        self.res_ai_dock.setVisible(False)
        # Kullanıcı paneli kendi X'inden kapatırsa toggle butonu da senkron kalsın.
        self.res_ai_dock.visibilityChanged.connect(self.res_ai_toggle_btn.setChecked)

    @staticmethod
    def _set_badge(lbl: QLabel, text: str, color: str, size: int = 9):
        lbl.setText(text)
        f = QFont("Segoe UI", size)
        f.setBold(True)
        lbl.setFont(f)
        lbl.setStyleSheet(
            f"color: {color}; background-color: {rgba(color, 0.13)}; border: 1px solid {color}; "
            f"border-radius: 9px; padding: 3px 10px;"
        )

    def display_report_in_result(self, report: dict, symbol: str = ""):
        self._last_report = report
        self._last_symbol = symbol
        self.res_symbol_label.setText(f"Coin: {symbol}" if symbol else "")

        is_mock = (self._last_level == "Level 1 — Mock" or symbol.upper().startswith("TEST_")
                   or getattr(self, "_is_test_profile", False))
        if is_mock:
            self.res_save_btn.setEnabled(False)
            self.res_save_label.setText("Test profilleri geçmişe kaydedilmez")
            self.res_save_label.setStyleSheet(f"color: {WARN};")
        else:
            self.res_save_btn.setEnabled(True)
            self.res_save_label.setText("Geçmişe kaydetmek için butona basın")
            self.res_save_label.setStyleSheet(f"color: {TEXT_SECONDARY};")

        signal_score = report["signal_score"]
        risk_score = report.get("risk_score")
        risk_coverage = report.get("risk_coverage", 0)
        et_score = report.get("entry_timing_score")
        et_answered = report.get("entry_timing_answered")
        et_total = report.get("entry_timing_total")
        confidence = report["confidence"]

        # Sinyal Gücü — yalnız sayısal skor + ScoreEngine'in ürettiği "confidence" alanı.
        # Burada ayrı bir Güçlü/Orta/Zayıf GUI sınıflandırması YOK — nihai yorumu aşağıdaki
        # verdict kartı verir, bu kart ikinci bir karar sistemi oluşturmuyor.
        card, ring, badge_lbl, caption_lbl = self.res_score_cards["Sinyal Gücü"]
        ring.set_value(signal_score, score_color(signal_score))
        confidence_color = {"Yüksek": POS, "Orta": WARN, "Düşük": WARN, "Yetersiz": DANGER}.get(confidence, NEUTRAL)
        self._set_badge(badge_lbl, f"{confidence} güven", confidence_color)
        caption_lbl.setText("")

        # İşlem Uygunluğu — yalnız gerçek risk_score + risk_coverage + risk_breadth
        # gösterilir. Burada da (Sinyal Gücü kartındaki gibi) ayrı bir Uygun/Sınırda/
        # Zayıf GUI kararı ÜRETİLMİYOR; nihai işlem kararını yalnız aşağıdaki
        # verdict()/entry_status verir. RISK COVERAGE V2 (R3): gate artık ham
        # risk_coverage yüzdesi değil, verdict()'in de kullandığı risk_reliable
        # (bağımsız measurable risk boyutu >= 2) ile aynı eşik.
        card, ring, badge_lbl, caption_lbl = self.res_score_cards["İşlem Uygunluğu"]
        risk_breadth = report.get("risk_breadth", 0)
        risk_reliable = report.get("risk_reliable", False)
        guvenilirlik_txt = "Yeterli" if risk_reliable else "Sınırlı"
        caption_lbl.setText(
            f"Risk kapsamı: %{risk_coverage:.1f} · Bağımsız risk boyutu: {risk_breadth} · "
            f"Güvenilirlik: {guvenilirlik_txt}")
        if risk_score is None or not risk_reliable:
            ring.set_value(None, NEUTRAL)
            badge_lbl.setVisible(True)
            self._set_badge(badge_lbl, "Yetersiz veri", NEUTRAL if risk_score is None else WARN)
        else:
            ring.set_value(risk_score, score_color(risk_score))
            badge_lbl.setVisible(False)

        # Giriş Uygunluğu — ENTRY TIMING V2 / R2 FIX: R:R artık resmi 3-faktör
        # contract'ta hiç yok (eskiden 4. faktördü, Level 1'de hep nodata idi).
        # 3/3 (tam veri) durumunda yeşil rozet + kalıcı "R:R skora dahil değil"
        # notu gösterilir; 3'ten az cevaplıysa (RSI/EMA/chg eksikse) hâlâ amber
        # "Eksik veri" rozeti gösterilir.
        card, ring, badge_lbl, caption_lbl = self.res_score_cards["Giriş Uygunluğu"]
        et_label = report.get("entry_timing", "—")
        full_data = et_answered is not None and et_total is not None and et_answered >= et_total
        if et_score is None:
            ring.set_value(None, NEUTRAL)
            self._set_badge(badge_lbl, et_label, NEUTRAL)
            caption_lbl.setText(f"{et_answered or 0}/{et_total or 3} veri" if et_total else "")
        elif full_data:
            ring.set_value(et_score, score_color(et_score))
            self._set_badge(badge_lbl, et_label, score_color(et_score))
            # ENTRY TIMING V2 / R2 FIX: R:R resmi contract'tan çıktığı için
            # 3/3 artık "tam veri" sayılıyor -- eski amber "Eksik veri · 3/4"
            # uyarısı burada kaybolur, o yüzden R:R'nin skora hiç dahil
            # olmadığını kalıcı olarak burada da belirtiyoruz.
            caption_lbl.setText(f"{et_answered}/{et_total} veri · R:R skora dahil değil")
        else:
            ring.set_value(et_score, WARN)
            self._set_badge(badge_lbl, f"Eksik veri · {et_answered}/{et_total}", WARN)
            caption_lbl.setText("Tam giriş teyidi için yetersiz veri")

        # MODEL D — PRESENTATION vs PERSISTED VERDICT AYRIMI: report["verdict_title"]
        # ("Elenir") ASLA değiştirilmez/ezilmez -- yalnız GUI'de GÖSTERİLEN metin,
        # restricted_candidate=True iken additive olarak "Yüksek Riskli İzle"ye
        # çevrilir. History/save_analysis hâlâ report["verdict_title"]'ı (internal,
        # "Elenir") kullanır -- bu satır yalnız bu QLabel'ın text'ini etkiler.
        is_restricted = bool(report.get("restricted_candidate"))
        if is_restricted:
            # MODEL D — FINAL PRESENTATION FIX: motor gerçeği ile Model D
            # politika durumu ARTIK AYNI ETİKETİ PAYLAŞMIYOR -- iki ayrı
            # etiketli satır (aynı QLabel içinde, yeni widget YOK).
            displayed_verdict = f"MOTOR KARARI: {report['verdict_title']}\nPOLİTİKA DURUMU: {MODEL_D_RESTRICTED_LABEL}"
        else:
            displayed_verdict = report["verdict_title"]
        self.res_verdict_title.setText(displayed_verdict)
        self.res_verdict_status.setText(f"Giriş durumu: {report['entry_status']}")
        verdict_desc_text = report["verdict_desc"]
        if is_restricted:
            verdict_desc_text = verdict_desc_text + "\n\n" + MODEL_D_RESTRICTED_EXPLANATION
        self.res_verdict_desc.setText(verdict_desc_text)
        verdict_color = WARN if is_restricted else (DANGER if report["vetos"] else score_color(signal_score))
        self._set_badge(self.res_verdict_badge, report["entry_status"], verdict_color, size=10)
        self.res_verdict_card.set_accent(verdict_color)

        if report["vetos"]:
            veto_prefix = "⚠  PİYASA VETO RİSKİ (VAR) — " if is_restricted else "⚠  VETO: "
            self.res_veto_label.setText(veto_prefix + " | ".join(report["vetos"]))
            self.res_veto_frame.setVisible(True)
        else:
            self.res_veto_frame.setVisible(False)

        # FAZ 2: DESTEKLEYENLER | BASKILAYANLAR iki sütun — burada signal_score'dan
        # türetilen ayrı bir Güçlü/Orta/Zayıf sınıflandırması YOK, yalnız motorun
        # zaten ürettiği signal_pros/signal_cons/risk_pros/risk_cons (top_factors()'ın
        # kendi ağırlık sırasıyla) birleştirilip gösteriliyor — yeni skor/sıralama/
        # önem algoritması YOK, yalnız kategori sırasıyla (sinyal önce, risk sonra)
        # concat. wait/nodata bu listelere top_factors() tarafından zaten hiç
        # konmuyor (bkz. satır ~3679) — burada ek bir filtre yok.
        pros_combined = report.get("signal_pros", []) + report.get("risk_pros", [])
        cons_combined = report.get("signal_cons", []) + report.get("risk_cons", [])
        self._fill_two_column_pros_cons(self.res_hero_card, pros_combined, cons_combined)
        self.res_hero_card.set_accent(score_color(signal_score))

        # Veri Kapsamı kartı — otomatik/manuel/veri-yok, her cevabın gerçek `source`'undan
        # türetiliyor (hardcode sayı yok). Level 2/3 cevaplarında "source" alanı hiç
        # bulunmaz (None) — bunlar da manuel sayılır, otomatik değil.
        coverage = report["coverage"]
        c = report["counts"]
        auto_count = 0
        manual_count = 0
        for a in report.get("answers", []):
            if a.get("answer") == "nodata":
                continue
            src = a.get("source")
            if src and src != "Manuel giriş (kullanıcı)":
                auto_count += 1
            else:
                manual_count += 1
        self.res_coverage_bar.set_value(coverage, score_color(coverage))
        self.res_coverage_pct_lbl.setText(f"{coverage:.1f}%")
        self.res_coverage_pct_lbl.setStyleSheet(f"color: {score_color(coverage)};")
        self.res_cov_ok_lbl.setText(f"✓ {auto_count} otomatik")
        self.res_cov_manual_lbl.setText(f"👤 {manual_count} manuel")
        self.res_cov_missing_lbl.setText(f"⚠ {c['nodata']} veri yok")
        disabled_count = sum(1 for a in report.get("answers", []) if a.get("disabled"))
        self.res_cov_disabled_lbl.setText(
            f"⛔ {disabled_count} opsiyonel on-chain metriği analize dahil edilmedi (devre dışı)"
            if disabled_count else "")
        self._set_badge(self.res_confidence_badge, report["confidence"], score_color(coverage), size=8.5)
        self.res_coverage_card.set_accent(score_color(coverage), glow=False)

        # Aşama dökümü
        self.res_stages_card.clear()
        self.res_stages_card.add(h_label("📊  Aşama Dökümü", size=12, bold=True))
        all_titles = ([s.title for s in STAGES_CONFIG if s.title in self.score_engine.signal_weights] +
                      [s.title for s in STAGES_CONFIG if s.title in self.score_engine.risk_weights])
        for title in all_titles:
            is_risk = title in self.score_engine.risk_weights
            sc = report["stage_scores"].get(title, 0)
            answered = report["stage_answered"].get(title, 0)
            total_q = report["stage_items_count"].get(title, 1)
            row_widget = QWidget()
            row_layout = QVBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 3, 0, 3)
            top_row = QHBoxLayout()
            top_row.setSpacing(8)
            top_row.addWidget(icon_chip(STAGE_ICONS.get(title, "•"), STAGE_ACCENTS.get(title, BLUE), size=22))
            label_text = title + (" [İşlem Uygunluğu]" if is_risk else "")
            top_row.addWidget(h_label(label_text, size=9.5))
            detail_btn = QPushButton("Detay ▸")
            detail_btn.setCursor(Qt.PointingHandCursor)
            detail_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; color: {TEXT_TERTIARY}; "
                f"font-size: 8pt; padding: 0px 4px; }} QPushButton:hover {{ color: {BLUE}; }}"
            )
            detail_btn.clicked.connect(lambda checked=False, t=title: self.show_stage_detail(t))
            top_row.addWidget(detail_btn)
            # Eskiden buradaki addStretch() etiketi sola, rozeti karta göre çok uzak
            # bir sağ köşeye itiyordu. Artık tüm üst-satır öğeleri (icon, başlık,
            # detay butonu, soru sayacı, yüzde rozeti) tek bir kompakt küme halinde
            # solda duruyor; genişliği asıl kullanan öğe zaten alttaki progress bar.
            top_row.addWidget(h_label(f"{answered}/{total_q} soru", size=8.5, color=TEXT_TERTIARY))
            top_row.addWidget(make_badge(f"{sc:.1f}%", score_color(sc), size=8.5))
            top_row.addStretch()
            row_layout.addLayout(top_row)
            bar = ProgressBarStyled()
            bar.set_value(sc, score_color(sc))
            row_layout.addWidget(bar)
            self.res_stages_card.add(row_widget)
        # ROW 5'te res_risk_factors_card ile eşleşiyor (dikey Expanding) -- clear()
        # az önce eski stretch'i sildiği için burada yeniden ekleniyor, böylece
        # kartın çerçevesi satırın tam yüksekliğine büyürken fazla boşluk İÇERİĞİN
        # ALTINDA (kompakt bir alt-dolgu gibi) toplanıyor, kart dışına taşmıyor.
        self.res_stages_card.layout_.addStretch()

        # Etkenler
        self._fill_factors_card(self.res_signal_factors_card, "Sinyal Gücü Etkenleri",
                                 report.get("signal_pros", []), report.get("signal_cons", []))
        self._fill_factors_card(self.res_risk_factors_card, "İşlem Uygunluğu Etkenleri",
                                 report.get("risk_pros", []), report.get("risk_cons", []))

        c = report["counts"]
        self.res_summary_labels["Pozitif"].setText(str(c["yes"]))
        self.res_summary_labels["Nötr"].setText(str(c["wait"]))
        self.res_summary_labels["Negatif"].setText(str(c["no"]))
        self.res_summary_labels["Veri Yok"].setText(str(c["nodata"]))
        self.res_summary_labels["Toplam"].setText(f"{c['total_valid']}/{c['total_all']}")

    @staticmethod
    def _top_factors_combined(report: dict, n: int = 6):
        """Sinyal + risk pros/cons'u ağırlığa göre birleştirip en etkili n maddeyi döndürür."""
        items = []
        for text, w in report.get("signal_pros", []) + report.get("risk_pros", []):
            items.append(("yes", text, w))
        for text, w in report.get("signal_cons", []) + report.get("risk_cons", []):
            items.append(("no", text, w))
        items.sort(key=lambda x: x[2], reverse=True)
        return items[:n]

    def show_stage_detail(self, stage_title: str):
        report = self._last_report
        if not report:
            return
        dlg = DetailDialog(stage_title, self, icon=STAGE_ICONS.get(stage_title, "📊"),
                            accent=STAGE_ACCENTS.get(stage_title, BLUE), width=640, height=560)
        found = False
        for a in report.get("answers", []):
            if a["stage"] != stage_title:
                continue
            found = True
            sub = ""
            if a.get("value") not in (None, ""):
                sub = f"Değer: {a['value']}"
                if a.get("source"):
                    sub += f"  ·  Kaynak: {a['source']}"
            elif a.get("answer") == "nodata" and a.get("reason"):
                sub = a["reason"]
            dlg.add(detail_row(status_icon(a["answer"]), status_color(a["answer"]), a["question"], sub))
        if not found:
            dlg.add_empty("Bu aşama için veri yok.")
        dlg.exec()

    def _fill_factors_card(self, card: Card, title: str, pros, cons):
        card.clear()
        card.add(h_label(f"🔎  {title}", size=12, bold=True))
        if cons:
            card.add(h_label("▼  Düşürenler", size=9.5, bold=True, color=DANGER))
            for text, w in cons:
                card.add(h_label(f"•  {text}", size=9.5, color=TEXT_SECONDARY, wrap=True))
        if pros:
            card.add(h_label("▲  Yükseltenler", size=9.5, bold=True, color=POS))
            for text, w in pros:
                card.add(h_label(f"•  {text}", size=9.5, color=TEXT_SECONDARY, wrap=True))
        if not pros and not cons:
            card.add(h_label("Yeterli veri yok.", size=9.5, color=TEXT_TERTIARY))
        # res_risk_factors_card ROW 5'te dikey Expanding olarak eşleşiyor (bkz.
        # build_result_tab) -- fazla yükseklik burada, içeriğin ALTINDA toplanıyor.
        card.layout_.addStretch()

    def _fill_two_column_pros_cons(self, card: Card, pros: list, cons: list):
        """DESTEKLEYENLER | BASKILAYANLAR iki-sutun gorunum. Yeni siralama/skor
        mantigi YOK -- yalniz verilen pros/cons listelerini (zaten top_factors()'in
        kendi agirlik sirasiyla uretilmis) OLDUGU GIBI, kategori sirasiyla
        (signal once, risk sonra) birlestirip render eder."""
        card.clear()
        card.add(h_label("🎯  Sinyal Gücü Etkenleri", size=13, bold=True))
        cols_row = QHBoxLayout()
        cols_row.setSpacing(18)

        pros_col = QVBoxLayout()
        pros_col.addWidget(h_label("✓  DESTEKLEYENLER", size=10.5, bold=True, color=POS))
        if pros:
            for text, w in pros:
                pros_col.addWidget(h_label(f"•  {text}", size=9.5, color=TEXT_SECONDARY, wrap=True))
        else:
            pros_col.addWidget(h_label("Destekleyen belirgin faktör yok.", size=9, color=TEXT_TERTIARY, wrap=True))
        pros_col.addStretch()

        cons_col = QVBoxLayout()
        cons_col.addWidget(h_label("↓  BASKILAYANLAR", size=10.5, bold=True, color=DANGER))
        if cons:
            for text, w in cons:
                cons_col.addWidget(h_label(f"•  {text}", size=9.5, color=TEXT_SECONDARY, wrap=True))
        else:
            cons_col.addWidget(h_label("Baskılayan belirgin faktör yok.", size=9, color=TEXT_TERTIARY, wrap=True))
        cons_col.addStretch()

        cols_row.addLayout(pros_col, 1)
        cols_row.addLayout(cons_col, 1)
        # OVERLAP FIX (root cause: Card.clear() yalnız top-level WIDGET item'larını
        # siler -- cols_row çıplak bir QHBoxLayout olarak eklenirse clear()'ın
        # item.widget() çağrısı None döner, eski QLabel'lar hiç silinmeden card'ın
        # child'ı olarak kalır ve bir sonraki analizde yeni içerikle üst üste biner.
        # res_stages_card'ın zaten kullandığı güvenli desenle aynı: layout'u bir
        # QWidget'a sarıp card'a WIDGET olarak eklemek, clear()'ın onu (ve tüm
        # torunlarını) doğru şekilde deleteLater() etmesini sağlar.
        row_widget = QWidget()
        row_widget.setLayout(cols_row)
        card.add(row_widget)

    def _format_report(self, report: dict) -> List[str]:
        lines = []
        lines.append(f"{'='*60}")
        lines.append("  SİNYAL RAPORU")
        lines.append(f"{'='*60}")
        lines.append(f"  Coin:             {self._last_symbol}")
        lines.append(f"  Zaman:            {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        # RECENT PRICE ACTION V1: report["analysis_price"] (capture_analysis_snapshot
        # ile alınan CANLI ticker fiyatı) -- History/tracking'in mevcut kullanımı
        # (calculate_tracking/save_analysis) DEĞİŞMEDİ, yalnız aynı zaten var olan
        # değer artık deterministik rapora da additive olarak yazılıyor.
        analysis_price = report.get("analysis_price")
        if analysis_price is not None:
            lines.append(f"  Analiz Anı Fiyatı: {analysis_price:.4f} USDT")
        lines.append(f"  Sinyal Gücü:      {report['signal_score']:.1f}%")

        # İşlem Uygunluğu — ana sonuç ekranıyla aynı semantik: risk_reliable=False iken
        # ham risk_score'u (yanıltıcı kesinlik) göstermiyoruz, yalnızca kapsamı gösteriyoruz.
        # (RISK COVERAGE V2 / R3: gate artık risk_coverage% değil risk_reliable.)
        risk_val = report.get('risk_score')
        risk_cov = report.get('risk_coverage', 0)
        risk_reliable = report.get('risk_reliable', False)
        if risk_val is None or not risk_reliable:
            lines.append(f"  İşlem Uygunluğu:  Yetersiz veri (risk kapsamı %{risk_cov:.1f})")
        else:
            lines.append(f"  İşlem Uygunluğu:  {risk_val:.1f}%")

        # Giriş Uygunluğu — 3 faktörün hepsi yoksa ham "Uygun" etiketini basmıyoruz
        # (ENTRY TIMING V2 / R2 FIX: resmi contract artık 3 faktör, R:R dahil değil).
        et_score = report.get('entry_timing_score')
        et_answered = report.get('entry_timing_answered')
        et_total = report.get('entry_timing_total')
        et_label = report.get('entry_timing', '—')
        if et_score is None:
            lines.append("  Giriş Uygunluğu:  Hesaplanamadı")
        elif et_answered is not None and et_total and et_answered < et_total:
            lines.append(f"  Giriş Uygunluğu:  Eksik veri · {et_answered}/{et_total} (skor: {et_score:.1f})")
        else:
            lines.append(f"  Giriş Uygunluğu:  {et_label} (skor: {et_score:.1f}) · R:R skora dahil değil")

        lines.append(f"  Sinyal Kapsamı:   {report.get('signal_coverage', report['coverage']):.1f}%")
        lines.append(f"  Risk Kapsamı:     {report.get('risk_coverage', 0):.1f}% · "
                     f"Bağımsız risk boyutu: {report.get('risk_breadth', 0)} · "
                     f"Güvenilirlik: {'Yeterli' if report.get('risk_reliable') else 'Sınırlı'}")
        lines.append(f"  Genel Kapsam:     {report['coverage']:.1f}%")
        lines.append(f"  Güven Düzeyi:     {report['confidence']}")
        lines.append(f"  Veto:             {'VAR' if report['vetos'] else 'Yok'}")
        lines.append(f"{'='*60}")
        # MODEL D — FINAL PRESENTATION FIX: restricted_candidate=False iken
        # bu satır AYNEN eskisi gibi "KARAR: ..." kalır (madde 5 -- minimum
        # değişiklik). True iken "MOTOR KARARI: ..." etiketine geçer, böylece
        # kullanıcı bunun deterministik motor GERÇEĞİ olduğunu, aşağıdaki
        # POLİTİKA DURUMU satırından AYRI bir katman olduğunu görür.
        is_restricted_txt = bool(report.get("restricted_candidate"))
        karar_label = "MOTOR KARARI" if is_restricted_txt else "KARAR"
        lines.append(f"\n  {karar_label}: {report['verdict_title']}")
        lines.append(f"  {report['verdict_desc']}")
        if report["vetos"]:
            lines.append("\n  VETO SEBEPLERİ:")
            for v in report["vetos"]:
                lines.append(f"    → {v}")
        # MODEL D — additive bölüm: yalnız restricted_candidate=True iken
        # eklenir, deterministik KARAR/MOTOR KARARI/VETO SEBEPLERİ bloklarını
        # DEĞİŞTİRMEZ. Backtest yüzdesi/istatistik İÇERMEZ.
        if is_restricted_txt:
            lines.append(f"\n  POLİTİKA DURUMU: {MODEL_D_RESTRICTED_LABEL}")
            lines.append(f"    {MODEL_D_RESTRICTED_EXPLANATION}")
        if report.get("signal_cons"):
            lines.append("\n  OLUMSUZ ETKENLER:")
            for text, w in report["signal_cons"] + report.get("risk_cons", []):
                lines.append(f"    - {text}")
        if report.get("signal_pros"):
            lines.append("\n  OLUMLU ETKENLER:")
            for text, w in report["signal_pros"] + report.get("risk_pros", []):
                lines.append(f"    - {text}")
        # disabled=True (ör. on-chain bölümü kapalı) maddeler kapsam dışıdır — show_missing_data()
        # popup'ıyla aynı kural: gerçek eksik veri listesine karışmaz, ayrı bilgi olarak gösterilir.
        nodata_items = [a for a in report.get("answers", [])
                         if a.get("answer") == "nodata" and not a.get("disabled")]
        disabled_items = [a for a in report.get("answers", []) if a.get("disabled")]
        if nodata_items:
            lines.append("\n  VERİ YOK MADDELERİ:")
            for a in nodata_items:
                lines.append(f"    - [{a['stage']}] {a['question']}")
        if disabled_items:
            lines.append(f"\n  ANALİZE DAHİL EDİLMEYEN OPSİYONEL METRİKLER: {len(disabled_items)}")
        c = report["counts"]
        lines.append("\n  HAM ÖZET:")
        lines.append(f"    Pozitif: {c['yes']}  Nötr: {c['wait']}  Negatif: {c['no']}  "
                      f"Veri Yok: {c['nodata']}  Toplam: {c['total_valid']}/{c['total_all']}")
        lines.append(f"{'='*60}")
        return lines

    def _build_ai_section_for_copy(self) -> Optional[str]:
        """PRESENTATION V1 / RAPORU KOPYALA: mevcut _ai_state/_last_ai_result
        (start_ai_analyst/_on_ai_analyst_ready/_on_ai_analyst_failed/run_level1
        tarafından ZATEN tutulan, yeni bir paralel state sistemi İCAT EDİLMEDEN
        kullanılan) mekanizmasına göre AI bölümünü üretir:
          - ready  -> build_ai_analyst_plain_text() ile GUI'yle AYNI canonical veri
          - failed -> sabit nötr mesaj (debug/reason/raw response YOK)
          - loading/disabled/none -> AI bölümü hiç eklenmez (None)
        stale-response koruması: _last_ai_result yalnız request_id/symbol hâlâ
        aktif analizle eşleşiyorsa kullanılır (mevcut request_id mekanizmasıyla
        aynı, ekstra bir doğrulama katmanı değil)."""
        if self._ai_state == "ready" and self._last_ai_result:
            r = self._last_ai_result
            if r.get("request_id") != self._active_ai_request_id or r.get("symbol") != self._last_symbol:
                return None  # stale (normal akışta buraya hiç düşülmez)
            body = self.build_ai_analyst_plain_text(
                r["mode"], r["data"], r.get("factor_labels", {}), r.get("structure_context"))
        elif self._ai_state == "failed":
            body = "AI yorumu oluşturulamadı. Deterministik analiz sonucu geçerlidir."
        else:
            return None
        return (f"\n\n{'='*60}\nAI ANALİST YORUMU\n{'='*60}\n{body}\n\n"
                "Not: AI Analyst yorumu deterministik motor sonucunu değiştirmez.")

    def copy_report(self):
        if not self._last_report:
            QMessageBox.information(self, "Bilgi", "Kopyalanacak bir analiz sonucu yok.")
            return
        text = "\n".join(self._format_report(self._last_report))
        ai_section = self._build_ai_section_for_copy()
        if ai_section:
            text += ai_section
        QApplication.clipboard().setText(text)
        self.res_save_label.setText("Rapor panoya kopyalandı.")
        self.res_save_label.setStyleSheet(f"color: {POS};")

    def show_technical_detail(self):
        if not self._last_report:
            return
        dlg = DetailDialog("Teknik Detay", self, icon=STAGE_ICONS.get("Teknik analiz onayı", "📈"),
                            accent=STAGE_ACCENTS.get("Teknik analiz onayı", BLUE))
        found = False
        for a in self._last_report.get("answers", []):
            if a["stage"] != "Teknik analiz onayı":
                continue
            found = True
            sub = f"Değer: {a.get('value', '—')}  ·  Kaynak: {a.get('source') or '—'}"
            dlg.add(detail_row(status_icon(a["answer"]), status_color(a["answer"]), a["question"], sub))
        if not found:
            dlg.add_empty("Veri yok.")
        dlg.exec()

    def show_data_sources(self):
        dlg = DetailDialog("Veri Kaynakları", self, icon="🌐", accent=BLUE)
        if not self._last_source_status:
            dlg.add_empty("Bu analiz için kaynak bilgisi mevcut değil (Level 2/3 manuel giriş).")
        else:
            for src, status in self._last_source_status.items():
                ok = status.startswith("ok")
                color = POS if ok else (WARN if ("yok" in status or "key" in status) else DANGER)
                dlg.add(detail_row("✓" if ok else "!", color, src, status))
        dlg.exec()

    def show_news_detail(self):
        detail = self._last_news_detail or {}
        result = detail.get("result", {})
        items = detail.get("items", [])
        status = result.get("status", "nodata")
        status_label = {"yes": "EVET — olumsuz olay yok", "wait": "NÖTR — belirsiz",
                         "no": "HAYIR — olumsuz olay var", "nodata": "VERİ YOK"}.get(status, "—")
        dlg = DetailDialog("Haber Detayı", self, icon="📰", accent=status_color(status))
        dlg.add(detail_row(status_icon(status), status_color(status), status_label,
                            result.get("reason", "—")))
        if items:
            news_card = Card(accent=BORDER)
            news_card.add(h_label(f"Taranan Haberler ({len(items)})", size=11, bold=True))
            for it in items:
                news_card.add(detail_row("📰", NEUTRAL, it["title"],
                                          f"{it['source']}" + (f"  ·  {it['link']}" if it.get("link") else "")))
            dlg.add(news_card)
        else:
            dlg.add_empty("İlgili haber bulunamadı veya haber taraması yapılmadı.")
        dlg.exec()

    def show_missing_data(self):
        report = self._last_report
        if not report:
            return
        all_answers = report.get("answers", [])
        disabled_count = sum(1 for a in all_answers if a.get("disabled"))
        # disabled=True (ör. on-chain bölümü kapalı) maddeler kapsam dışıdır — gerçek
        # "eksik veri" değildir, bu listede GÖSTERİLMEZ.
        nodata = [a for a in all_answers if a.get("answer") == "nodata" and not a.get("disabled")]
        dlg = DetailDialog("Eksik Veriler", self, icon="❔", accent=WARN)

        if disabled_count:
            dlg.add(h_label(f"ℹ️  {disabled_count} opsiyonel on-chain metriği analize dahil edilmedi "
                             f"(devre dışı) — bu bölümün eksik veri sayısına dahil değildir.",
                             size=9, color=TEXT_TERTIARY, wrap=True))

        if not nodata:
            dlg.add_empty("Kapsam-içi tüm veriler mevcut — eksik yok.")
            dlg.exec()
            return

        total = len([a for a in all_answers if not a.get("disabled")])
        dlg.add(h_label(f"{len(nodata)} / {total} soru otomatik doldurulamadı.",
                         size=10, bold=True, color=TEXT_SECONDARY))

        by_stage = {}
        for a in nodata:
            by_stage.setdefault(a["stage"], []).append(a)

        for stage, items in by_stage.items():
            card = Card(accent=STAGE_ACCENTS.get(stage, WARN))
            head = QHBoxLayout()
            head.setSpacing(8)
            head.addWidget(icon_chip(STAGE_ICONS.get(stage, "•"), STAGE_ACCENTS.get(stage, WARN), size=24))
            head.addWidget(h_label(stage, size=11, bold=True))
            head.addStretch()
            card.layout_.addLayout(head)

            by_reason = {}
            for it in items:
                reason = (it.get("reason") or "Veri sağlanamadı").strip()
                by_reason.setdefault(reason, []).append(it["question"])
            for reason, labels in by_reason.items():
                bullets = "\n".join(f"•  {lbl}" for lbl in labels)
                card.add(h_label(bullets, size=9.5, color=TEXT_SECONDARY, wrap=True))
                card.add(h_label(reason, size=8.5, color=TEXT_TERTIARY, wrap=True))
            dlg.add(card)

        dlg.add(h_label(
            "Bu metrikler ücretsiz/güvenilir bir API ile otomatik alınamadığı için hesaplamaya "
            "dahil edilmedi. İstersen Level 2 veya Level 3'te manuel girebilirsin.",
            size=8.5, color=TEXT_TERTIARY, wrap=True))
        dlg.exec()

    def show_score_factors(self):
        if not self._last_report:
            return
        text = "\n".join(self._format_report(self._last_report))
        InfoDialog("Skor Etkenleri / Tam Rapor", text, self).exec()

    def save_to_history(self):
        if not self._last_report or not self._last_symbol:
            QMessageBox.warning(self, "Uyarı", "Kaydedilecek analiz bulunamadı.")
            return
        symbol = self._last_symbol.strip().upper()
        if symbol.startswith("TEST_") or getattr(self, "_is_test_profile", False):
            QMessageBox.warning(self, "Uyarı", "Test profilleri geçmişe kaydedilmez.")
            return

        level = self._last_level or "Bilinmiyor"
        report = self._last_report
        # save_to_history() hiçbir Level için yeni piyasa snapshot'ı oluşturmaz —
        # yalnızca report'ta analiz tamamlandığı anda zaten sabitlenmiş
        # analysis_time/analysis_price çiftini DB'ye yazar.
        analysis_time = report.get("analysis_time")
        analysis_price = report.get("analysis_price")
        if analysis_price is not None:
            self.res_save_label.setText(f"Analiz anı fiyatı: {analysis_price:.4f} USDT")
        else:
            self.res_save_label.setText("Analiz anı fiyatı alınamamıştı, kayıt fiyatsız devam ediyor")
            self.res_save_label.setStyleSheet(f"color: {WARN};")

        data_source = "manual" if level.startswith("Level 2") or level.startswith("Level 3") \
            else "binance+coingecko+cmc+news"
        result = self.history_db.save_analysis(symbol, level, report, analysis_price, data_source,
                                                 analysis_time=analysis_time)
        if result == -1:
            self.res_save_label.setText("Bu coin için bu dakikada zaten kayıt var")
            self.res_save_label.setStyleSheet(f"color: {DANGER};")
        else:
            self.res_save_label.setText(f"Kaydedildi (ID: {result})")
            self.res_save_label.setStyleSheet(f"color: {POS};")
            self.refresh_history_table()

    # ═══════════════════════════════════════════════════════════
    # GEÇMİŞ
    # ═══════════════════════════════════════════════════════════
    def build_history_tab(self):
        outer = QVBoxLayout(self.tab_history)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        outer.addWidget(h_label("🕘  Geçmiş Analizler", size=16, bold=True))
        outer.addWidget(h_label(
            "Kaydedilen analizleri görüntüleyin, detayları açın, CSV olarak dışa aktarın.",
            size=9.5, color=TEXT_SECONDARY))

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("🔄  Yenile")
        refresh_btn.clicked.connect(self.refresh_history_table)
        track_btn = QPushButton("📊  Fiyatları Güncelle")
        track_btn.clicked.connect(self.on_update_tracking)
        csv_btn = QPushButton("📁  CSV Dışa Aktar")
        csv_btn.clicked.connect(self.export_csv)
        del_btn = QPushButton("🗑️  Seçiliyi Sil")
        del_btn.clicked.connect(self.delete_history)
        for b in (refresh_btn, track_btn, csv_btn, del_btn):
            btn_row.addWidget(b)
        btn_row.addStretch()
        outer.addLayout(btn_row)

        cols = ["ID", "Tarih", "Coin", "Level", "Sinyal", "Risk", "Karar", "Veto",
                "Kapsam", "24s MFE", "24s MAE", "24s Kapanış", "Durum"]
        self.history_table = QTableWidget(0, len(cols))
        self.history_table.setHorizontalHeaderLabels(cols)
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.history_table.doubleClicked.connect(self.show_history_detail)
        outer.addWidget(self.history_table, 1)

        self.refresh_history_table()

    def refresh_history_table(self):
        rows = self.history_db.get_all_analyses()
        self.history_table.setRowCount(0)
        for row in rows:
            r = self.history_table.rowCount()
            self.history_table.insertRow(r)
            try:
                answers = json.loads(row["answers"]) if row["answers"] else []
            except (json.JSONDecodeError, TypeError):
                answers = []
            risk_reliable = compute_risk_reliable(answers)
            if row['risk_score'] is None or not risk_reliable:
                risk_val = "Yetersiz veri"
            else:
                risk_val = f"{row['risk_score']:.1f}%"
            veto_val = "VAR" if row['vetos'] and row['vetos'] != "[]" else "Yok"
            tr = self.history_db.get_tracking(row["id"], horizon_hours=24)
            if tr:
                # Eksik Veri (continuity-invalid) satırlarda mfe/mae/close_return
                # None olabilir -- format-string'e girmeden önce None-safe çevir.
                fmt_pct = lambda v: f"{v:+.1f}%" if v is not None else "—"
                mfe24, mae24 = fmt_pct(tr['mfe_pct']), fmt_pct(tr['mae_pct'])
                kap24, dur24 = fmt_pct(tr['close_return_pct']), tr.get("status", "—")
            else:
                mfe24 = mae24 = kap24 = dur24 = "—"
            values = [
                str(row["id"]), row["analysis_time"][:16].replace("T", " "), row["symbol"],
                row["level"], f"{row['signal_score']:.1f}%", risk_val,
                history_table_verdict_text(row["verdict"], row.get("model_d_reason")),
                veto_val, f"{row['coverage']:.0f}%", mfe24, mae24, kap24, dur24,
            ]
            for c, val in enumerate(values):
                item = QTableWidgetItem(val)
                if c == 7 and veto_val == "VAR":
                    item.setForeground(QColor(DANGER))
                self.history_table.setItem(r, c, item)

    def show_history_detail(self, index=None):
        row_idx = self.history_table.currentRow()
        if row_idx < 0:
            return
        analysis_id = int(self.history_table.item(row_idx, 0).text())
        row = self.history_db.get_analysis_by_id(analysis_id)
        if not row:
            return

        try:
            answers = json.loads(row['answers']) if row['answers'] else []
        except (json.JSONDecodeError, TypeError):
            answers = []

        risk_cov = row.get("risk_coverage")
        if risk_cov is None:
            risk_cov = fallback_risk_coverage(answers)
        # RISK COVERAGE V2 (R3): gate risk_coverage% değil risk_reliable
        # (stored answers'tan read-time türetilir, DB şeması değişmedi).
        risk_reliable = compute_risk_reliable(answers)

        lines = [f"Coin: {row['symbol']}", f"Tarih: {row['analysis_time']}",
                 f"Level: {row['level']}", f"Kaynak: {row['data_source']}", ""]
        lines.append(f"Sinyal Gücü: {row['signal_score']:.1f}%")
        if row['risk_score'] is None or not risk_reliable:
            lines.append(f"İşlem Uygunluğu: Yetersiz veri (risk kapsamı %{risk_cov:.1f})")
        else:
            lines.append(f"İşlem Uygunluğu: {row['risk_score']:.1f}%")
        lines.append(f"Sinyal Ham Skoru: {row['signal_raw_score']:.1f}%")
        lines.append(f"Kapsam: {row['coverage']:.0f}%")
        lines.append(f"Güven: {row['confidence']}")
        # MODEL D HISTORY PRESENTATION (MODEL B, onaylı): yalnız stored
        # model_d_reason == "model_d_candidate_confirmed" iken live GUI'nin
        # iki-katmanlı gösterimiyle (satır ~9744) semantik parite kurulur.
        # Diğer 8 stable reason (Model D değerlendirdi, aday değil) VE
        # NULL (UNKNOWN_LEGACY, Model D o tarihte yoktu) AYNI, mevcut
        # pre-Model-D davranışını kullanır -- sahte False üretilmez,
        # today's evaluate_model_d_candidate() YENİDEN ÇAĞRILMAZ.
        if row.get("model_d_reason") == "model_d_candidate_confirmed":
            lines.append(f"Motor Kararı: {row['verdict']}")
            lines.append(f"Politika Durumu: {MODEL_D_RESTRICTED_LABEL}")
        else:
            lines.append(f"Karar: {row['verdict']}")
        lines.append(f"Durum: {row['entry_status']}")

        et_score = row.get("entry_timing_score")
        if et_score is None:
            lines.append("Giriş uygunluğu ayrıntısı: eski kayıtta mevcut değil")
        else:
            et_label = row.get("entry_timing", "—")
            et_answered = row.get("entry_timing_answered")
            et_total = row.get("entry_timing_total")
            if et_answered is not None and et_total is not None and et_answered < et_total:
                lines.append(
                    f"Giriş Uygunluğu: Eksik veri · {et_answered}/{et_total} (skor: {et_score:.1f})"
                )
            else:
                # ENTRY TIMING V2 / R2 FIX: eski kayıtlarda et_total=4 (o zamanki
                # gerçek contract), yeni kayıtlarda et_total=3 -- ikisi de bu
                # koşulda "tam veri" anlamına doğru şekilde geliyor, migration
                # YOK, yalnız R:R notu yeni contract'ı yansıtsın diye eklendi.
                lines.append(f"Giriş Uygunluğu: {et_label} (skor: {et_score:.1f}) · R:R skora dahil değil")
        lines.append("")

        try:
            vetos = json.loads(row['vetos']) if row['vetos'] else []
        except (json.JSONDecodeError, TypeError):
            vetos = []
        if vetos:
            lines.append("VETO:")
            for v in vetos:
                lines.append(f"  → {v}")
            lines.append("")
        try:
            stage_scores = json.loads(row['stage_scores']) if row['stage_scores'] else {}
        except (json.JSONDecodeError, TypeError):
            stage_scores = {}
        lines.append("Aşama Skorları:")
        for title, score in stage_scores.items():
            lines.append(f"  {title:<35} {score:>6.1f}%")
        lines.append("")

        disabled_answers = [a for a in answers if a.get("disabled")]
        normal_answers = [a for a in answers if not a.get("disabled")]

        lines.append("Cevaplar:")
        current_stage = ""
        for ans in normal_answers:
            if ans.get("stage") != current_stage:
                current_stage = ans.get("stage", "")
                lines.append(f"\n[{current_stage}]")
            lines.append(f"  {status_icon(ans.get('answer'))} {ans.get('question', '')}")

        if disabled_answers:
            lines.append("\nDevre dışı / analize dahil edilmedi:")
            for ans in disabled_answers:
                lines.append(f"  ⊘ [{ans.get('stage', '')}] {ans.get('question', '')}")

        if not answers:
            pass
        elif not any(a.get("disabled") for a in answers) and not any("disabled" in a for a in answers):
            lines.append("\n(Not: bu eski kayıtta 'devre dışı' bilgisi tutulmuyordu — "
                          "yukarıdaki 'Veri Yok' maddeleri gerçekten eksik ya da o dönem "
                          "kapsam dışı bırakılmış olabilir, kayıttan ayırt edilemiyor.)")

        InfoDialog(f"Analiz Detayı — {row['symbol']} #{analysis_id}", "\n".join(lines), self).exec()

    def update_tracking_for_analysis(self, analysis_id, symbol, analysis_time_iso, analysis_price):
        results = BinanceClient.calculate_tracking(symbol, analysis_time_iso, analysis_price,
                                                     horizons=[1, 6, 12, 24, 48])
        if results:
            self.history_db.save_tracking(analysis_id, results)
            self.refresh_history_table()

    def refresh_all_tracking(self):
        rows = self.history_db.get_all_analyses()
        updated = 0
        for row in rows:
            price, atime = row.get("analysis_price"), row.get("analysis_time")
            if not price or not atime:
                continue
            results = BinanceClient.calculate_tracking(row["symbol"], atime, price)
            if results:
                self.history_db.save_tracking(row["id"], results)
                updated += 1
        if updated > 0:
            self.refresh_history_table()
        return updated

    def on_update_tracking(self):
        updated = self.refresh_all_tracking()
        if updated > 0:
            QMessageBox.information(self, "Güncelleme", f"{updated} kaydın fiyat takibi güncellendi.")
        else:
            QMessageBox.information(self, "Güncelleme", "Güncellenecek eksik kayıt bulunamadı.")

    def delete_history(self):
        row_idx = self.history_table.currentRow()
        if row_idx < 0:
            QMessageBox.warning(self, "Uyarı", "Silmek için bir kayıt seçin.")
            return
        analysis_id = int(self.history_table.item(row_idx, 0).text())
        symbol = self.history_table.item(row_idx, 2).text()
        reply = QMessageBox.question(self, "Onay",
                                      f"#{analysis_id} {symbol} kaydını silmek istediğinize emin misiniz?")
        if reply == QMessageBox.Yes:
            self.history_db.delete_analysis(analysis_id)
            self.refresh_history_table()

    def export_csv(self):
        filepath, _ = QFileDialog.getSaveFileName(self, "CSV olarak kaydet", "", "CSV files (*.csv)")
        if not filepath:
            return
        if self.history_db.export_csv(filepath):
            QMessageBox.information(self, "Başarılı", f"{filepath} olarak kaydedildi.")
        else:
            QMessageBox.warning(self, "Uyarı", "Dışa aktarılacak kayıt bulunamadı.")


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
